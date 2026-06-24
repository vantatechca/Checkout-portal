"""
Highriskify (a.k.a. 2530gateway) direct API integration.

Docs: Highriskify-API-Documentation.pdf bundled with this repo.

Two-step model per docs (§4 Integration Overview):
  1. POST /control/wallet.php → get encrypted `address_in` + temp polygon wallet
  2. Redirect customer to https://checkout.2530gateway.com/process-payment.php
     with `address=<address_in>&amount=<...>&provider=<...>&...`

Payment confirmation arrives ONLY via the callback URL we provide in step 1
(server-to-server GET request). See routes/webhooks.py for the handler.

No API key is needed for the merchant endpoints — the payout wallet IS the
identity. The IPT tracking endpoint uses a shared `X-IPT-Key` header (the
key is documented and identical across all Highriskify merchants).

Why direct vs the WP plugin:
  * No WordPress middleman
  * We pick ONE provider (Transak) — no Coinbase/Kryptonim roulette
  * Cleaner callback handling (server-to-server, includes txids)
  * Per-store config flows through our existing CSV
"""
import logging
import urllib.parse
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Per docs §3 Base URLs
API_BASE      = "https://api.2530gateway.com"
CHECKOUT_BASE = "https://checkout.2530gateway.com"
IPT_URL       = "https://2530gateway.com/wp-json/ipt/v1/track"

# The IPT tracking key is shared across all Highriskify merchants — they
# scope per-merchant by the payload's wallet/order IDs, not by a unique key.
# It's literally printed in the public docs (§11 / §11.3).
DEFAULT_IPT_KEY = "highriskify-1c07b45fd6542e20c2f82b3ee9e5d4cc"


class HighriskifyError(Exception):
    pass


class HighriskifyClient:
    """
    Thin client over Highriskify's two-step hosted checkout API.
    """

    def __init__(self):
        # Merchant USDC Polygon payout wallet — this is the EOA you set up in
        # Trust Wallet / MetaMask. Customers' card payments convert to USDC
        # which Highriskify forwards here automatically.
        self.merchant_wallet = (getattr(settings, "HIGHRISKIFY_WALLET", "") or "").strip()
        # Provider to pin per docs §6. "transak" is the cleanest card-form
        # UX with guest checkout under $150. Override per env if needed.
        self.provider = (getattr(settings, "HIGHRISKIFY_PROVIDER", "") or "transak").strip().lower()
        # Optional override of IPT key — defaults to the public one.
        self.ipt_key = (getattr(settings, "HIGHRISKIFY_IPT_KEY", "") or DEFAULT_IPT_KEY).strip()

    def configured(self) -> bool:
        return bool(self.merchant_wallet and self.merchant_wallet.startswith("0x"))

    # ── Step 1: Create wallet ─────────────────────────────────────────────────
    async def create_wallet(
        self, *,
        order_id:     str,
        callback_url: str,
    ) -> dict:
        """
        Per docs §5 — generate a unique encrypted receiving wallet for this
        order. The `callback_url` must be unique per transaction; if we reuse
        one Highriskify returns the same temp wallet (= would credit two
        orders to one record). We always include the order_id in the callback.

        Returns the JSON response with keys:
          - address_in         (encrypted wallet for step 2)
          - polygon_address_in (the actual temp wallet on Polygon)
          - callback_url       (echoed)
          - ipn_token          (for support / reconciliation)
        """
        if not self.configured():
            raise HighriskifyError(
                "HIGHRISKIFY_WALLET not configured — set your USDC Polygon "
                "payout wallet address in .env"
            )

        # `address` is the merchant payout wallet (our EOA). `callback`
        # MUST be URL-encoded per docs.
        params = {
            "address":  self.merchant_wallet,
            "callback": callback_url,
        }
        url = f"{API_BASE}/control/wallet.php"

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)

        if resp.status_code != 200:
            raise HighriskifyError(
                f"wallet.php failed ({resp.status_code}): {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except Exception:
            raise HighriskifyError(
                f"wallet.php returned non-JSON: {resp.text[:300]}"
            )

        # Sanity check — both keys we need must be present.
        if not data.get("address_in"):
            raise HighriskifyError(
                f"wallet.php missing address_in: {data}"
            )

        logger.info(
            f"[highriskify] wallet created for {order_id}: "
            f"temp={data.get('polygon_address_in')} "
            f"ipn_token={(data.get('ipn_token') or '')[:12]}..."
        )
        return data

    # ── Step 2: Build the hosted-checkout redirect URL ────────────────────────
    def build_checkout_url(
        self, *,
        address_in: str,
        amount:     float,
        currency:   str,
        email:      str,
        provider:   Optional[str] = None,
    ) -> str:
        """
        Per docs §6 — build the URL to redirect the customer to. The
        `address` parameter MUST be the encrypted `address_in` from step 1
        (passing our payout wallet directly returns 400 Bad Request).

        IMPORTANT: the `address_in` value from wallet.php comes back
        ALREADY URL-encoded (the response JSON contains `%2B`/`%2F`/`%3D`
        for `+`/`/`/`=`). Do NOT re-encode it via urlencode — that turns
        `%` into `%25` and corrupts the encrypted blob, causing a 404 on
        the process-payment endpoint. Pass it through verbatim.
        """
        # urlencode the safe-to-encode fields, then append `address` as-is.
        other_params = {
            "amount":   f"{float(amount):.2f}",
            "provider": (provider or self.provider),
            "email":    email or "",
            "currency": (currency or "USD").upper(),
        }
        qs_others = urllib.parse.urlencode(other_params, safe="")
        # `address_in` is already a properly-encoded query-string value.
        return f"{CHECKOUT_BASE}/process-payment.php?address={address_in}&{qs_others}"

    # ── IPT tracking (optional but recommended per docs §11) ─────────────────
    async def ipt_track(self, payload: dict) -> None:
        """
        Send a tracking event to the IPT master endpoint. Non-blocking
        philosophy: failures are logged but never raised — checkout must
        never be broken by a tracking outage (docs §11.2).

        Caller is responsible for setting the right `event_type` and
        `platform` fields. For our use case `platform` should be `custom-api`.
        """
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    IPT_URL,
                    headers={
                        "Content-Type": "application/json",
                        "X-IPT-Key":    self.ipt_key,
                    },
                    json=payload,
                )
            # Expected per docs: {"ok": true, "queued": true}. Anything else
            # is logged but ignored.
            if resp.status_code >= 400:
                logger.warning(
                    f"[highriskify ipt] tracking call returned "
                    f"{resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            logger.warning(f"[highriskify ipt] tracking call failed: {e}")
