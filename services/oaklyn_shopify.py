"""
services/oaklyn_shopify.py

Creates a real Shopify cart on Lasso's connected merchant store (Oaklyn) so we
can hand Lasso a valid shopify_cart_token. Lasso refuses to honor arbitrary
cart payloads from our server — it validates every cart against its merchant's
Shopify using a token only Shopify can issue.

Flow:
  1. fetch_variants()    → list all variants in Oaklyn's catalog with prices (cached)
  2. pick_basket(total)  → greedy-pick variants summing to total cents
  3. create_cart(items)  → POST to Shopify's /cart/add.js + GET /cart.js
                          → returns (cart_token, line_items)

The line_items shape matches what Lasso's sync-cart expects in currentCart.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from config import settings

logger = logging.getLogger(__name__)


class OaklynShopifyError(Exception):
    pass


@dataclass
class Variant:
    variant_id:   int
    product_id:   int
    title:        str          # product title + variant title
    price_cents:  int
    available:    bool


# ─── In-memory variant cache ──────────────────────────────────────────────────
# Refetched every CACHE_TTL seconds. Oaklyn's catalog is small + changes rarely
# so caching avoids hammering Shopify on every checkout.
CACHE_TTL = 600  # 10 min
_cache: dict = {"ts": 0.0, "variants": []}

# Products whose titles match these substrings are excluded from candidates.
# Shopify apps often inject these into the cart with weird behavior (multi-tier
# pricing, auto-multiply, app-level discounts) which throws off our math.
EXCLUDED_TITLE_KEYWORDS = (
    "shipping protection",
    "shipping insurance",
    "package protection",
    "route protection",
    "insurance",
    "warranty",
    "donation",
    "gift card",
    "gift wrap",
    "subscription",
    "tip",
)


class OaklynShopifyClient:
    def __init__(self):
        self.domain = settings.LASSO_SHOPIFY_DOMAIN.strip()
        self.token  = settings.LASSO_SHOPIFY_ADMIN_TOKEN.strip()

        if not self.domain:
            raise OaklynShopifyError("LASSO_SHOPIFY_DOMAIN not configured")
        if not self.token:
            raise OaklynShopifyError("LASSO_SHOPIFY_ADMIN_TOKEN not configured")

        self.storefront_base = f"https://{self.domain}"
        self.admin_base      = f"https://{self.domain}/admin/api/2024-07"

    # ─── Catalog ──────────────────────────────────────────────────────────────

    async def fetch_variants(self, force: bool = False) -> list[Variant]:
        """
        Returns all available variants in Oaklyn's catalog. Cached in memory.
        """
        now = time.time()
        if not force and (now - _cache["ts"]) < CACHE_TTL and _cache["variants"]:
            return _cache["variants"]

        variants: list[Variant] = []
        url = (
            f"{self.admin_base}/products.json"
            f"?limit=250&status=active&published_status=published"
            f"&fields=id,title,status,published_at,published_scope,variants"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Shopify paginates via Link header. For small catalogs one page is enough;
            # if Oaklyn ever exceeds 250 products we'd need to follow `next` links.
            try:
                resp = await client.get(
                    url,
                    headers={"X-Shopify-Access-Token": self.token},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise OaklynShopifyError(
                    f"Shopify product fetch failed: {e.response.status_code} {e.response.text[:200]}"
                ) from e
            except httpx.RequestError as e:
                raise OaklynShopifyError(f"Shopify unreachable: {e}") from e

        for product in resp.json().get("products", []):
            # Must be published to the storefront — /cart/add.js rejects anything else
            if product.get("status") != "active":
                continue
            if not product.get("published_at"):
                continue
            scope = product.get("published_scope")
            if scope and scope not in ("web", "global"):
                continue

            product_id    = int(product["id"])
            product_title = product.get("title", "")

            # Filter out app-injected / metadata products (shipping protection,
            # insurance, gift cards, etc.) — these cause weird cart behavior
            title_lower = product_title.lower()
            if any(kw in title_lower for kw in EXCLUDED_TITLE_KEYWORDS):
                continue

            for v in product.get("variants", []):
                price = v.get("price")
                if price is None:
                    continue
                try:
                    price_cents = round(float(price) * 100)
                except (ValueError, TypeError):
                    continue
                if price_cents <= 0:
                    continue

                # Variant is purchasable if either:
                #   - inventory is untracked (inventory_management is null), or
                #   - oversell allowed (inventory_policy == "continue"), or
                #   - has inventory on hand
                inv_mgmt   = v.get("inventory_management")
                inv_policy = v.get("inventory_policy", "deny")
                inv_qty    = v.get("inventory_quantity") or 0
                purchasable = (
                    inv_mgmt is None
                    or inv_policy == "continue"
                    or inv_qty > 0
                )
                if not purchasable:
                    continue

                variant_title = v.get("title") or ""
                full_title = product_title if variant_title in ("Default Title", "") else f"{product_title} - {variant_title}"
                variants.append(Variant(
                    variant_id  = int(v["id"]),
                    product_id  = product_id,
                    title       = full_title,
                    price_cents = price_cents,
                    available   = True,
                ))

        _cache.update(ts=now, variants=variants)
        logger.info(f"[OaklynShopify] Cached {len(variants)} variants from {self.domain}")
        return variants

    # ─── Basket builder ───────────────────────────────────────────────────────

    def pick_basket(
        self,
        variants: list[Variant],
        target_cents: int,
        max_qty_per_variant: int = 50,
    ) -> list[tuple[Variant, int]]:
        """
        Finds the (variant × qty) combination that gets the cart total
        closest to target_cents.

        Strategy: tries every variant with every plausible quantity (1..50),
        keeps the option with smallest absolute error from target. Prefers
        slight undershoot over overshoot when errors are equal.

        Returns a list with 1 (variant, qty) entry — single line items hit
        target more precisely than mixing variants.
        """
        if target_cents <= 0:
            raise OaklynShopifyError(f"Invalid target_cents={target_cents}")

        usable = [v for v in variants if v.available and v.price_cents > 0]
        if not usable:
            raise OaklynShopifyError("No usable variants in Oaklyn catalog")

        # Try single-variant solutions: for each variant, find the qty that
        # lands closest to target. Score by (abs_error, prefer_undershoot, fewer_units).
        best = None  # tuple of (abs_error, is_overshoot, qty, variant)

        for v in usable:
            # Skip variants more than 2x the target — would always wildly overshoot
            if v.price_cents > target_cents * 2:
                continue
            qty_floor = max(1, target_cents // v.price_cents)
            for qty in {qty_floor, qty_floor + 1}:
                if qty <= 0 or qty > max_qty_per_variant:
                    continue
                total = qty * v.price_cents
                error = total - target_cents
                abs_err = abs(error)
                is_over = 1 if error > 0 else 0
                key = (abs_err, is_over, qty)
                if best is None or key < best[0]:
                    best = (key, v, qty)

        if best is None:
            raise OaklynShopifyError(
                f"No variant fits target={target_cents} within constraints"
            )

        _, v, qty = best
        total = v.price_cents * qty
        diff  = total - target_cents
        logger.info(
            f"[OaklynShopify] Best single-variant match: {v.title[:40]} x{qty} "
            f"= {total} cents (target={target_cents}, diff={diff:+d})"
        )

        return [(v, qty)]

    # ─── Permalink builder (browser-side fallback) ────────────────────────────

    def build_cart_permalink(
        self,
        items: list[tuple[Variant, int]],
        order_id: str | None = None,
    ) -> str:
        """
        Returns a Shopify cart permalink URL. Used as a fallback when the
        Cloudflare worker isn't configured — sends customer's browser to
        build the cart instead of doing it server-side.
        """
        if not items:
            raise OaklynShopifyError("Empty items list for permalink")
        path = ",".join(f"{v.variant_id}:{qty}" for v, qty in items)
        url = f"{self.storefront_base}/cart/{path}"
        if order_id:
            from urllib.parse import quote
            url += f"?attributes[order_id]={quote(order_id)}"
        return url

    # ─── Worker-backed cart creation ──────────────────────────────────────────

    async def build_cart_via_worker(
        self,
        target_cents: int,
        baskets: list[tuple[Variant, int]],
        undershoot_tolerance_cents: int = 2500,   # allow ~$25 under
        overshoot_tolerance_cents:  int = 2500,   # allow ~$25 over
    ) -> tuple[str, list[dict]]:
        """
        Posts a TARGET + ordered list of (variant, qty) baskets to the
        Cloudflare Worker. Worker tries each basket in order; first one that
        adds successfully wins (single line item, exact qty as computed).

        Raises if final cart total deviates from target beyond either tolerance.
        Triggers fallback (permalink) in caller so customer still gets through.
        """
        worker_url    = settings.OAKLYN_CART_WORKER_URL.rstrip("/")
        worker_secret = settings.OAKLYN_CART_WORKER_SECRET

        if not worker_url or not worker_secret:
            raise OaklynShopifyError(
                "OAKLYN_CART_WORKER_URL / OAKLYN_CART_WORKER_SECRET not configured"
            )

        payload = {
            "shop":         self.domain,
            "target_cents": target_cents,
            "candidates":   [
                {"id": v.variant_id, "qty": qty, "price_cents": v.price_cents}
                for v, qty in baskets
            ],
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                resp = await client.post(
                    f"{worker_url}/build-cart",
                    json=payload,
                    headers={
                        "Content-Type":    "application/json",
                        "X-Worker-Secret": worker_secret,
                    },
                )
            except httpx.RequestError as e:
                raise OaklynShopifyError(f"Worker unreachable: {e}") from e

        if resp.status_code != 200:
            raise OaklynShopifyError(
                f"Worker returned {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        token       = data.get("token")
        line_items  = data.get("items") or []
        cart_total  = int(data.get("total_price") or 0)
        added       = data.get("added", 0)
        skipped     = data.get("skipped", 0)
        skipped_det = data.get("skipped_details", [])

        if not token or not line_items:
            raise OaklynShopifyError(
                f"Worker built no usable cart: added={added} skipped={skipped} "
                f"skipped_details={skipped_det}"
            )

        if cart_total < target_cents - undershoot_tolerance_cents:
            raise OaklynShopifyError(
                f"Cart total too low: expected ~{target_cents} cents, "
                f"got {cart_total} cents (under by {target_cents - cart_total}). "
                f"added={added} skipped={skipped}"
            )

        if cart_total > target_cents + overshoot_tolerance_cents:
            raise OaklynShopifyError(
                f"Cart total too high: expected ~{target_cents} cents, "
                f"got {cart_total} cents (over by {cart_total - target_cents}). "
                f"Oaklyn's apps inflated the cart. added={added}"
            )

        logger.info(
            f"[OaklynShopify] Worker built cart token={token[:12]}... "
            f"added={added} skipped={skipped} "
            f"cart_total={cart_total} target={target_cents}"
        )
        return token, line_items

    def pick_top_baskets(
        self,
        variants: list[Variant],
        target_cents: int,
        top_n: int = 15,
        max_qty: int = 50,
    ) -> list[tuple[Variant, int]]:
        """
        Returns the top_n (variant, qty) combinations ranked by how close
        their total comes to target_cents. Worker tries them in this order;
        first one that isn't sold-out wins.
        """
        if not variants:
            return []
        ranked: list[tuple[int, int, int, Variant, int]] = []
        for v in variants:
            if v.price_cents <= 0 or v.price_cents > target_cents * 2:
                continue
            qty_floor = max(1, target_cents // v.price_cents)
            for qty in {qty_floor, qty_floor + 1}:
                if qty <= 0 or qty > max_qty:
                    continue
                total = qty * v.price_cents
                error = total - target_cents
                ranked.append((abs(error), 1 if error > 0 else 0, qty, v, qty))
        ranked.sort(key=lambda x: (x[0], x[1], x[2]))
        seen: set[int] = set()
        result: list[tuple[Variant, int]] = []
        for _, _, _, v, qty in ranked:
            if v.variant_id in seen:
                continue
            seen.add(v.variant_id)
            result.append((v, qty))
            if len(result) >= top_n:
                break
        return result

    async def build_cart_for_total(
        self,
        target_cents: int,
    ) -> tuple[str, list[dict]]:
        """
        Fetch catalog → rank single-variant baskets by accuracy → send to
        worker. Worker tries them in order until one succeeds.
        """
        variants = await self.fetch_variants()
        baskets  = self.pick_top_baskets(variants, target_cents)
        if not baskets:
            raise OaklynShopifyError("No suitable variants found in Oaklyn catalog")
        return await self.build_cart_via_worker(target_cents, baskets)

    async def build_permalink_for_total(
        self,
        target_cents: int,
        order_id: str | None = None,
    ) -> tuple[str, list[tuple[Variant, int]]]:
        """Fallback path used when worker isn't configured."""
        variants = await self.fetch_variants()
        basket   = self.pick_basket(variants, target_cents)
        url      = self.build_cart_permalink(basket, order_id=order_id)
        return url, basket
