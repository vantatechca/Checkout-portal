"""
Authorize.net direct card processor integration.

Flow:
  1. Customer enters card details in the checkout's card form.
  2. Accept.js (loaded in the browser) tokenizes the card client-side and
     returns an opaqueData nonce — single-use, 15-minute TTL.
  3. Frontend POSTs the nonce + order info to /api/checkout/authnet.
  4. This client calls createTransactionRequest on api.authorize.net to
     actually charge the card (or apitest.authorize.net in sandbox).
  5. Funds settle via TSYS to the merchant bank account; from there the
     merchant moves money to Grey wallet manually.

Implementation notes (deep-research verified, see memory/onramp-providers
won't apply but the integration patterns came from official Auth.net docs):
  * Field order matters in createTransactionRequest — merchantAuthentication
    requires `name` BEFORE `transactionKey`. Same for nested billTo, etc.
  * Success requires BOTH `messages.resultCode == "Ok"` AND
    `transactionResponse.responseCode == "1"`. Either alone is NOT enough.
  * Nonces are single-use and expire after 15 minutes — capture and charge
    fast. If a nonce 404s, the most likely cause is reuse or expiry.
  * Webhook signatures: the AUTHNET_SIGNATURE_KEY is a 128-char hex string in
    the dashboard, but Auth.net signs the webhook body with HMAC-SHA512 using
    the BINARY form of that key. Hex→bytes conversion is the most common bug.
  * Refunds reference the original `refTransId` + last 4 of card; valid for
    180 days post-settlement. Unsettled transactions must be voided instead.
  * Duplicate detection: default 120 sec; raises Error 11. Adjust per-request
    via transactionSettings.duplicateWindow (0..28800 sec).

Test Mode:
  Toggle "Transaction Processing Mode" in the Auth.net dashboard. When
  disabled, the same API + credentials return simulated responses — no real
  charges. We DON'T flip URLs to apitest.* for test mode; we use production
  URL with dashboard-controlled simulation. AUTHNET_SANDBOX=true only flips
  to apitest.* if you sign up for a separate sandbox account.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Per deep-research verified Auth.net docs:
PRODUCTION_API_URL = "https://api.authorize.net/xml/v1/request.api"
SANDBOX_API_URL    = "https://apitest.authorize.net/xml/v1/request.api"

# Accept.js script URLs — referenced by the frontend template.
ACCEPTJS_PROD_URL = "https://js.authorize.net/v1/Accept.js"
ACCEPTJS_TEST_URL = "https://jstest.authorize.net/v1/Accept.js"

# Constant used as `dataDescriptor` for all Accept.js card nonces.
DATA_DESCRIPTOR_CARD = "COMMON.ACCEPT.INAPP.PAYMENT"


class AuthnetError(Exception):
    """Raised when an Auth.net API call fails at the transport or parse level."""
    pass


class AuthnetClient:
    """
    Thin client over the Authorize.net JSON API.

    Uses raw httpx (not the official authorizenet SDK) — fewer dependencies,
    consistent with our other integrations (highriskify, onramp_wp), easier
    to debug.
    """

    def __init__(self):
        self.login_id        = (getattr(settings, "AUTHNET_LOGIN_ID", "") or "").strip()
        self.transaction_key = (getattr(settings, "AUTHNET_TRANSACTION_KEY", "") or "").strip()
        self.signature_key   = (getattr(settings, "AUTHNET_SIGNATURE_KEY", "") or "").strip()
        self.use_sandbox     = bool(getattr(settings, "AUTHNET_SANDBOX", False))
        self.api_url         = SANDBOX_API_URL if self.use_sandbox else PRODUCTION_API_URL

    def configured(self) -> bool:
        return bool(self.login_id and self.transaction_key)

    # ── Step 1 / 3: charge a card via Accept.js opaque data ──────────────────
    async def charge_card(
        self,
        *,
        opaque_data_value: str,
        amount: float,
        order_id: str,
        opaque_data_descriptor: str = DATA_DESCRIPTOR_CARD,
        invoice_number: Optional[str] = None,
        description: Optional[str] = None,
        billing: Optional[dict] = None,
        customer_email: Optional[str] = None,
        customer_ip: Optional[str] = None,
        duplicate_window_seconds: int = 60,
    ) -> dict:
        """
        Charge a card using an Accept.js opaque data nonce.

        Returns a normalized dict:
          {
            "success":        bool,    # responseCode == "1" AND resultCode == "Ok"
            "response_code":  str,     # "1" approved / "2" declined / "3" error / "4" held
            "transaction_id": str,     # Auth.net trans ID — store on the order
            "auth_code":      str,     # bank auth code (approved only)
            "avs_result":     str,     # AVS match code
            "cvv_result":     str,     # CVV match code
            "account_number": str,     # masked card number (last 4)
            "account_type":   str,     # Visa, MasterCard, etc.
            "message":        str,     # human-readable result
            "raw":            dict,    # full Auth.net response, for diagnostics
          }
        """
        if not self.configured():
            raise AuthnetError(
                "Auth.net not configured — set AUTHNET_LOGIN_ID + "
                "AUTHNET_TRANSACTION_KEY in .env"
            )

        # ⚠️ Field order matters in Auth.net's JSON API — it translates to
        # XML internally and the XSD schema enforces strict element order.
        # Per the AnetApiSchema XSD, transactionRequest children must appear in:
        #   transactionType → amount → payment → order → customer → billTo →
        #   customerIP → ... → transactionSettings (and any later siblings)
        # Python 3.7+ preserves dict insertion order, so we insert in that order.
        #
        # Also: merchantAuthentication requires `name` BEFORE `transactionKey`.
        tx_request: dict = {
            "transactionType": "authCaptureTransaction",   # charge + capture in one call
            "amount":          f"{float(amount):.2f}",
            "payment": {
                "opaqueData": {
                    "dataDescriptor": opaque_data_descriptor,
                    "dataValue":      opaque_data_value,
                }
            },
            "order": {
                "invoiceNumber": (invoice_number or order_id)[:20],
                "description":   (description or f"Order {order_id}")[:255],
            },
        }

        # Customer info — must come BEFORE billTo per XSD. Used for receipts
        # and AFDS fraud scoring.
        if customer_email:
            tx_request["customer"] = {"email": customer_email[:255]}

        # Billing — Auth.net uses this for AVS verification. Higher AVS match
        # → lower fraud score → better acceptance + lower interchange fees.
        # Must come AFTER customer and BEFORE customerIP / transactionSettings.
        if billing:
            tx_request["billTo"] = {k: v for k, v in billing.items() if v}

        # Customer's IP — used by AFDS fraud scoring and chargeback dispute
        # representment. Comes after billTo, before transactionSettings.
        if customer_ip:
            tx_request["customerIP"] = customer_ip

        # transactionSettings comes LAST (after billTo/customer/customerIP).
        # Putting it earlier breaks XSD validation with:
        #   "invalid child element 'billTo' in namespace ...AnetApiSchema.xsd"
        tx_request["transactionSettings"] = {
            "setting": [
                # Duplicate-window: default 120s, max 28800s. 60s = reasonable
                # middle ground (we already have client-side debouncing).
                {"settingName": "duplicateWindow",
                 "settingValue": str(int(duplicate_window_seconds))},
            ]
        }

        payload = {
            "createTransactionRequest": {
                "merchantAuthentication": {
                    "name":           self.login_id,
                    "transactionKey": self.transaction_key,
                },
                "refId":              order_id[:20],   # max 20 chars
                "transactionRequest": tx_request,
            }
        }

        logger.info(f"[authnet] charging ${amount:.2f} for order {order_id}")

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    self.api_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except httpx.RequestError as e:
                raise AuthnetError(f"Network error calling Auth.net: {e}")

        # Auth.net's JSON response is prefixed with a UTF-8 BOM (﻿) —
        # well-known gotcha. Strip it before parsing or json.loads fails.
        text = resp.text.lstrip("﻿")
        try:
            data = json.loads(text)
        except Exception:
            raise AuthnetError(f"Failed to parse Auth.net JSON: {text[:300]}")

        return self._normalize_charge_response(data)

    def _normalize_charge_response(self, data: dict) -> dict:
        result_code = (data.get("messages", {}) or {}).get("resultCode", "")
        tx_resp     = data.get("transactionResponse", {}) or {}
        response_code = tx_resp.get("responseCode", "")

        # The ONLY combination that means "actually charged" — per docs.
        success = (result_code == "Ok" and response_code == "1")

        return {
            "success":        success,
            "response_code":  response_code,
            "transaction_id": tx_resp.get("transId", "") or "",
            "auth_code":      tx_resp.get("authCode", "") or "",
            "avs_result":     tx_resp.get("avsResultCode", "") or "",
            "cvv_result":     tx_resp.get("cvvResultCode", "") or "",
            "account_number": tx_resp.get("accountNumber", "") or "",   # last 4
            "account_type":   tx_resp.get("accountType", "") or "",
            "message":        self._extract_message(data, tx_resp),
            "raw":            data,
        }

    @staticmethod
    def _extract_message(data: dict, tx_resp: dict) -> str:
        """
        Pick the most relevant human-readable message from the response. Bank
        messages (in transactionResponse.messages) take priority over top-level
        ones — they tell us things like "Declined: Insufficient funds".
        """
        # Priority 1: transactionResponse.messages (bank decision)
        msgs = tx_resp.get("messages") or []
        if msgs and isinstance(msgs, list) and msgs[0].get("description"):
            return msgs[0]["description"]
        # Priority 2: transactionResponse.errors (declines, AVS mismatches)
        errors = tx_resp.get("errors") or []
        if errors and isinstance(errors, list) and errors[0].get("errorText"):
            return errors[0]["errorText"]
        # Priority 3: top-level messages (API-level errors like bad nonce)
        top_msgs = (data.get("messages", {}) or {}).get("message") or []
        if top_msgs and isinstance(top_msgs, list) and top_msgs[0].get("text"):
            return top_msgs[0]["text"]
        return "Unknown response"

    # ── Refund / void ─────────────────────────────────────────────────────────
    async def refund_transaction(
        self,
        *,
        original_trans_id: str,
        amount: float,
        last4: str,
    ) -> dict:
        """
        Refund a SETTLED transaction. Partial refunds supported (amount < original).
        Valid for 180 days post-settlement; older transactions need ECC approval.
        """
        payload = {
            "createTransactionRequest": {
                "merchantAuthentication": {
                    "name":           self.login_id,
                    "transactionKey": self.transaction_key,
                },
                "transactionRequest": {
                    "transactionType": "refundTransaction",
                    "amount":          f"{float(amount):.2f}",
                    "payment": {
                        # For refunds, only last 4 + masked exp is required;
                        # Auth.net uses refTransId to find the original card.
                        "creditCard": {
                            "cardNumber":     str(last4),
                            "expirationDate": "XXXX",
                        }
                    },
                    "refTransId": str(original_trans_id),
                }
            }
        }
        return await self._post_simple(payload)

    async def void_transaction(self, original_trans_id: str) -> dict:
        """
        Void an UNSETTLED transaction. Once the batch closes (usually daily
        cutoff), voids become impossible and you must issue a refund instead.
        """
        payload = {
            "createTransactionRequest": {
                "merchantAuthentication": {
                    "name":           self.login_id,
                    "transactionKey": self.transaction_key,
                },
                "transactionRequest": {
                    "transactionType": "voidTransaction",
                    "refTransId":      str(original_trans_id),
                }
            }
        }
        return await self._post_simple(payload)

    async def _post_simple(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self.api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        text = resp.text.lstrip("﻿")
        try:
            return json.loads(text)
        except Exception:
            raise AuthnetError(f"Failed to parse: {text[:300]}")

    # ── Webhook signature verification ───────────────────────────────────────
    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """
        Verify the X-ANET-Signature header against the webhook body.

        Auth.net signs with HMAC-SHA512 using the BINARY form of the Signature
        Key. The dashboard shows it as 128 hex chars — we convert to bytes
        before computing. Most-common-bug-of-all-time alert: forgetting the
        hex→bytes conversion produces a signature that NEVER matches.

        Header format: "sha512=<128-hex-chars>"
        """
        if not self.signature_key:
            logger.warning("[authnet] webhook signature verification skipped — no AUTHNET_SIGNATURE_KEY set")
            return False
        if not signature_header:
            return False

        # Strip the "sha512=" prefix the header always includes.
        received = signature_header.replace("sha512=", "").strip().lower()
        if not received:
            return False

        # Hex → bytes. If the user pasted the key with spaces or wrong format,
        # this throws — log and fail closed.
        try:
            key_bytes = bytes.fromhex(self.signature_key)
        except ValueError:
            logger.error("[authnet] AUTHNET_SIGNATURE_KEY is not a valid hex string")
            return False

        expected = hmac.new(key_bytes, raw_body, hashlib.sha512).hexdigest().lower()
        return hmac.compare_digest(received, expected)
