"""
POST /api/checkout/card      → Helcim credit card
POST /api/checkout/interac   → Interac e-Transfer (create pending order)
POST /api/checkout/crypto    → BTCPay Server invoice
GET  /api/checkout/status/{order_id}
"""
import logging
import re
from decimal import Decimal
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import get_db
from models.order import Order, OrderItem, InteracPayment, CryptoInvoice, ZellePayment, NowPaymentsInvoice, PaymentMethod, PaymentStatus
from models.brand import Brand
from services.order_id import generate_order_id
from services.helcim import HelcimClient, HelcimError
from services.btcpay import BTCPayClient, BTCPayError
from config import settings
from services.shopify_draft import create_draft_order, ShopifyError
from services.nowpayments import NowPaymentsClient, NowPaymentsError
from services.pymtz import PymtzClient, PymtzError
from services.lasso import LassoClient, LassoError
from services.whop import WhopClient, WhopError
from utils.cloaking import cloak_items, cloak_items_lasso, build_lasso_cart

router  = APIRouter(prefix="/api/checkout", tags=["checkout"])
logger  = logging.getLogger(__name__)

# pymtz settles in USD, so CAD orders must be converted before we send the
# amount. This MUST stay in sync with USD_CONVERSION_RATE in checkout.html
# (the "≈ $X USD" badge), or the charge won't match what the customer was shown.
USD_CONVERSION_RATE = 1.38  # 1 USD = 1.38 CAD


def _v2_referer_suffix(request: Request) -> str:
    """
    Build a "?v=2&country=XX" suffix from the request's Referer header so
    server-side redirect URLs (pymtz return_url, NowPayments success_url) can
    forward the v2 reskin flag + country palette onto the confirmation page.

    Returns "" if neither flag is present.
    """
    ref = request.headers.get("referer") or ""
    parts = []
    if "v=2" in ref:
        parts.append("v=2")
    m = re.search(r"[?&]country=([A-Za-z]{2})", ref)
    if m:
        parts.append(f"country={m.group(1).upper()}")
    return ("?" + "&".join(parts)) if parts else ""


# ─── Shared input schemas ───────────────────────────────────────────────────

class CartItem(BaseModel):
    product_id: str | None = Field(None, max_length=50)
    title:      str        = Field(..., min_length=1, max_length=500)
    variant:    str | None = Field(None, max_length=200)
    qty:        int        = Field(1, ge=1, le=100)
    price:      float      = Field(..., ge=0, le=10000)
    # Product image URL (Shopify CDN). Optional. Used to render the actual
    # product thumbnail in the v2 confirmation pages.
    image:      str | None = Field(None, max_length=500)


class CheckoutBase(BaseModel):
    # Optional pre-reserved order ID (from /api/checkout/reserve on page load)
    order_id: str | None = None

    # Contact
    email:      str
    first_name: str | None = None
    last_name:  str
    phone:      str | None = None

    # Shipping
    address1:    str | None = None
    address2:    str | None = None
    city:        str | None = None
    province:    str | None = None
    postal_code: str | None = None
    country:     str        = "CA"

    # Billing
    bill_same:     str = "1"
    bill_address1: str | None = None
    bill_address2: str | None = None
    bill_city:     str | None = None
    bill_province: str | None = None
    bill_postal:   str | None = None
    bill_country:  str | None = None

    # Cart (JSON-encoded from frontend)
    items: list[CartItem] = Field(default_factory=list)

    # Totals (we validate server-side)
    subtotal: float
    currency: str = "CAD"
    source_domain: str | None = None
    store_name: str | None = None   # friendly store name from the ?storename= URL param (for display)
    store_country: str = "CA"   # "CA" or "US" — which store the order came from (not shipping)

    # Discount info — applies to all payment methods
    discount_code: str | None = None
    discount_amount: float = 0.0
    payment_method_discount: float = 0.0

    # Optional password for the "soft account" prefill feature. When set,
    # we upsert a row in customer_accounts (email + pbkdf2 hash + saved
    # profile) so the customer can sign in on a return visit and have all
    # their fields prefilled. The plaintext password never reaches the
    # orders table.
    account_password: str | None = Field(None, min_length=5, max_length=64)


class CardCheckoutRequest(CheckoutBase):
    helcim_pay_token: str | None = None
    # Onramp provider override for the "Card (On-Ramp)" path. Customer picks
    # one in the provider-picker modal on checkout. Validated against an
    # allowlist in the onramp_wp endpoint — unknown values fall back to the
    # configured default (HIGHRISKIFY_PROVIDER).
    provider: str | None = None


class AuthnetCheckoutRequest(CheckoutBase):
    """Payload for POST /api/checkout/authnet — card paid via Authorize.net."""
    # Opaque data from Accept.js. dataDescriptor is always
    # "COMMON.ACCEPT.INAPP.PAYMENT" for cards but we accept it from the
    # frontend to stay forward-compatible.
    opaque_data_value:      str
    opaque_data_descriptor: str | None = None


class StripeDirectCheckoutRequest(CheckoutBase):
    """Payload for POST /api/checkout/stripe_direct — card paid via Stripe."""
    # PaymentMethod ID from Stripe Elements (e.g. "pm_1xxxxx...").
    # Stripe.js tokenizes the card in the browser; we only see this ID.
    payment_method_id: str


class InteracCheckoutRequest(CheckoutBase):
    pass


class CryptoCheckoutRequest(CheckoutBase):
    pass


class ReserveRequest(BaseModel):
    """Payload for POST /api/checkout/reserve — creates a bare pending order."""
    items:         list[CartItem] = Field(default_factory=list)
    subtotal:      float
    currency:      str = "CAD"
    source_domain: str | None = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _get_brand(request: Request) -> Brand | None:
    return getattr(request.state, "brand", None)


def _compute_total(subtotal: float, discount_pct: float) -> tuple[float, float]:
    discount_amount = round(subtotal * discount_pct / 100, 2)
    total = round(subtotal - discount_amount, 2)
    return discount_amount, total


def _safe_pct(amount: float, subtotal: float) -> float:
    """Compute discount % from amount + subtotal. Returns 0 if subtotal is 0."""
    sub = float(subtotal or 0)
    amt = float(amount or 0)
    if sub <= 0 or amt <= 0:
        return 0.0
    return round((amt / sub) * 100, 2)

MAX_ITEMS_PER_ORDER = 50
MAX_TOTAL_ORDER     = 100000.0


def _validate_cart(items: list, claimed_subtotal: float, promo_discount: float = 0.0) -> None:
    """
    Verify the claimed cart subtotal matches what the line items add up to,
    so a tampered checkout payload can't ship cheap.

    Two payload shapes are accepted (different Shopify themes do it differently):
      A) Items at original unit price, subtotal lowered by the promo savings.
         Customer-typed discount on our checkout page produces this shape.
         Match: sum(items) - promo_discount == subtotal
      B) Items already pre-discounted by the source theme, subtotal == sum(items).
         URL-applied discount (?discount=CODE from a theme that pre-discounts
         line item prices) produces this shape.
         Match: sum(items) == subtotal
    """
    if not items:
        raise HTTPException(400, "Cart is empty")
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise HTTPException(400, "Too many items in cart")

    computed = 0.0
    for it in items:
        computed += float(it.price) * int(it.qty)
    computed = round(computed, 2)

    if computed > MAX_TOTAL_ORDER:
        raise HTTPException(400, "Order total exceeds maximum")

    claimed = round(float(claimed_subtotal), 2)

    # Shape A: items at full price, subtotal already reduced by discount_amount
    expected_A = round(computed - max(0.0, float(promo_discount or 0.0)), 2)
    # Shape B: items already pre-discounted, subtotal equals items sum
    expected_B = computed

    if abs(expected_A - claimed) > 0.01 and abs(expected_B - claimed) > 0.01:
        raise HTTPException(400, "Cart total mismatch")


async def _create_base_order(
    db: AsyncSession,
    data: CheckoutBase,
    payment_method: PaymentMethod,
    brand: Brand | None,
    discount_pct: float,
    request: Request,
) -> Order:
    """
    Create OR update an Order + OrderItems. Returns the Order.

    If data.order_id is provided AND matches an existing pending order,
    we UPDATE that row instead of inserting a new one. This prevents
    duplicate orders when a customer switches payment methods, double-clicks,
    or refreshes the checkout page.
    """
    # Prefer the friendly storename passed from checkout (?storename=); fall
    # back to the brand, then a generic label.
    store_name = (data.store_name or "").strip() or (brand.store_name if brand else "Checkout")
    discount_amount, total = _compute_total(data.subtotal, discount_pct)

    # Enforce store-pinned currency. If this v2 store has a country fixed in
    # data/checkout_v2_stores.txt (`domain:US` / `domain:CA`), we ALWAYS use
    # that store's currency — protects against a theme that didn't pass
    # `&country=US` and would otherwise default to CAD. Non-listed stores
    # keep the payload-supplied currency.
    try:
        from main import _v2_store_country
        _src = data.source_domain or request.query_params.get("source") or request.headers.get("host", "")
        _pinned = _v2_store_country(_src)
        if _pinned == "US":
            data.currency = "USD"
        elif _pinned == "CA":
            data.currency = "CAD"
    except Exception:
        pass  # never block order creation on the currency-enforcement check

    # Try to reuse an existing reserved order
    order: Order | None = None
    if data.order_id:
        result = await db.execute(select(Order).where(Order.id == data.order_id))
        order  = result.scalar_one_or_none()
        # Guard: only reuse if it's still pending — don't touch paid/failed/cancelled
        if order and order.payment_status != PaymentStatus.pending:
            order = None
        # If switching payment method on a pending order, wipe stale payment-method
        # rows so we don't leave orphaned InteracPayment/ZellePayment/CryptoInvoice
        # records pointing at this order under a method the customer abandoned.
        if order and order.payment_method != payment_method:
            await db.execute(
                InteracPayment.__table__.delete().where(InteracPayment.order_id == order.id)
            )
            await db.execute(
                ZellePayment.__table__.delete().where(ZellePayment.order_id == order.id)
            )
            await db.execute(
                CryptoInvoice.__table__.delete().where(CryptoInvoice.order_id == order.id)
            )

    if order:
        # UPDATE path — reuse the reserved order
        order.brand_id        = brand.id if brand else order.brand_id or 1
        order.store_name      = store_name
        order.email           = data.email
        order.first_name      = data.first_name
        order.last_name       = data.last_name
        order.address1        = data.address1
        order.address2        = data.address2
        order.city            = data.city
        order.province        = data.province
        order.postal_code     = data.postal_code
        order.country         = data.country
        order.bill_same       = data.bill_same
        order.bill_address1   = data.bill_address1
        order.bill_address2   = data.bill_address2
        order.bill_city       = data.bill_city
        order.bill_province   = data.bill_province
        order.bill_postal     = data.bill_postal
        order.bill_country    = data.bill_country
        # Compute original (pre-promo) subtotal — needed for accurate email display
        promo_amt   = float(data.discount_amount or 0)
        post_promo  = float(data.subtotal or 0)
        original_sub = round(post_promo + promo_amt, 2) if promo_amt > 0 else post_promo
        promo_pct_calc = round((promo_amt / original_sub) * 100, 2) if original_sub > 0 and promo_amt > 0 else 0.0

        order.subtotal              = Decimal(str(post_promo))
        order.original_subtotal     = Decimal(str(original_sub))
        order.discount_code         = data.discount_code
        order.promo_discount_amount = Decimal(str(promo_amt))
        order.promo_discount_pct    = Decimal(str(promo_pct_calc))
        order.discount_pct          = Decimal(str(discount_pct))
        order.discount_amount       = Decimal(str(discount_amount))
        order.total                 = Decimal(str(total))
        order.currency        = data.currency
        order.payment_method  = payment_method
        order.ip_address      = request.client.host if request.client else None
        order.user_agent      = request.headers.get("user-agent", "")
        if data.source_domain:
            order.source_domain = data.source_domain

        # Replace line items — delete existing, re-add from payload
        await db.execute(
            OrderItem.__table__.delete().where(OrderItem.order_id == order.id)
        )
        for item in data.items:
            db.add(OrderItem(
                order_id       = order.id,
                product_id     = item.product_id,
                title          = item.title,
                variant        = item.variant,
                qty            = item.qty,
                price          = Decimal(str(item.price)),
                original_price = Decimal(str(getattr(item, "original_price", None) or item.price)),
                total          = Decimal(str(round(item.price * item.qty, 2))),
                image_url      = getattr(item, "image", None),
            ))

        await db.flush()
        await _maybe_upsert_customer_account(db, data)
        return order

    # INSERT path — no reserved order, create fresh
    order_id = generate_order_id()
    while True:
        existing = await db.execute(select(Order).where(Order.id == order_id))
        if not existing.scalar_one_or_none():
            break
        order_id = generate_order_id()

    # Compute promo math for INSERT path
    promo_amt_i    = float(data.discount_amount or 0)
    post_promo_i   = float(data.subtotal or 0)
    original_sub_i = round(post_promo_i + promo_amt_i, 2) if promo_amt_i > 0 else post_promo_i
    promo_pct_i    = round((promo_amt_i / original_sub_i) * 100, 2) if original_sub_i > 0 and promo_amt_i > 0 else 0.0

    order = Order(
        id              = order_id,
        brand_id        = brand.id if brand else 1,
        store_name      = store_name,
        email           = data.email,
        first_name      = data.first_name,
        last_name       = data.last_name,
        address1        = data.address1,
        address2        = data.address2,
        city            = data.city,
        province        = data.province,
        postal_code     = data.postal_code,
        country         = data.country,
        bill_same       = data.bill_same,
        bill_address1   = data.bill_address1,
        bill_address2   = data.bill_address2,
        bill_city       = data.bill_city,
        bill_province   = data.bill_province,
        bill_postal     = data.bill_postal,
        bill_country    = data.bill_country,
        subtotal              = Decimal(str(post_promo_i)),
        original_subtotal     = Decimal(str(original_sub_i)),
        discount_code         = data.discount_code,
        promo_discount_amount = Decimal(str(promo_amt_i)),
        promo_discount_pct    = Decimal(str(promo_pct_i)),
        discount_pct          = Decimal(str(discount_pct)),
        discount_amount       = Decimal(str(discount_amount)),
        total                 = Decimal(str(total)),
        currency        = data.currency,
        payment_method  = payment_method,
        payment_status  = PaymentStatus.pending,
        ip_address      = request.client.host if request.client else None,
        user_agent      = request.headers.get("user-agent", ""),
        source_domain   = data.source_domain or request.query_params.get("source") or request.headers.get("host", ""),
    )
    db.add(order)

    for item in data.items:
        db.add(OrderItem(
            order_id       = order_id,
            product_id     = item.product_id,
            title          = item.title,
            variant        = item.variant,
            qty            = item.qty,
            price          = Decimal(str(item.price)),
            original_price = Decimal(str(getattr(item, "original_price", None) or item.price)),
            total          = Decimal(str(round(item.price * item.qty, 2))),
            image_url      = getattr(item, "image", None),
        ))

    await db.flush()
    await _maybe_upsert_customer_account(db, data)
    return order


async def _maybe_upsert_customer_account(db: AsyncSession, data: "CheckoutBase") -> None:
    """
    If the customer typed a password in Section 5, upsert their account row
    (email + hash + saved profile) so a return visit can prefill the form
    via /api/customer/lookup. Best-effort — any failure is swallowed.

    Runs INSIDE the same DB transaction as the order create so the row is
    only committed if the order itself commits. That keeps us from leaving
    orphan customer rows when the customer abandons before paying.
    """
    pwd = (getattr(data, "account_password", None) or "").strip()
    if not pwd:
        return
    try:
        from services.customer_accounts import upsert_account
        await upsert_account(
            db,
            email    = data.email or "",
            password = pwd,
            profile  = {
                "first_name":  data.first_name  or "",
                "last_name":   data.last_name   or "",
                "phone":       data.phone       or "",
                "address1":    data.address1    or "",
                "address2":    data.address2    or "",
                "city":        data.city        or "",
                "province":    data.province    or "",
                "postal_code": data.postal_code or "",
                "country":     data.country     or "",
            },
        )
    except Exception as e:
        logger.warning(f"[customer_accounts] upsert hook failed for {data.email!r}: {e}")


# ─── POST /api/checkout/reserve ──────────────────────────────────────────────

@router.post("/reserve")
async def checkout_reserve(
    payload: ReserveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Reserves an order ID on page load. Creates a bare pending Order with items
    but no customer details yet. The customer's details are filled in later
    when they submit via /api/checkout/{card,interac,crypto}.

    This prevents duplicate orders when the customer switches payment methods
    or double-clicks Pay Now.
    """
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    brand = _get_brand(request)

    order_id = generate_order_id()
    while True:
        existing = await db.execute(select(Order).where(Order.id == order_id))
        if not existing.scalar_one_or_none():
            break
        order_id = generate_order_id()

    order = Order(
        id             = order_id,
        brand_id       = brand.id if brand else 1,
        store_name     = brand.store_name if brand else "Checkout",
        email          = "",                            # filled on submit
        first_name     = None,
        last_name      = "",                            # filled on submit
        subtotal       = Decimal(str(payload.subtotal)),
        total          = Decimal(str(payload.subtotal)),  # before discount
        discount_pct   = Decimal("0"),
        discount_amount= Decimal("0"),
        currency       = payload.currency,
        payment_method = PaymentMethod.card,            # placeholder, overwritten on submit
        payment_status = PaymentStatus.pending,
        ip_address     = request.client.host if request.client else None,
        user_agent     = request.headers.get("user-agent", ""),
        source_domain  = payload.source_domain or request.query_params.get("source") or request.headers.get("host", ""),
    )
    db.add(order)

    for item in payload.items:
        db.add(OrderItem(
            order_id   = order_id,
            product_id = item.product_id,
            title      = item.title,
            variant    = item.variant,
            qty        = item.qty,
            price      = Decimal(str(item.price)),
            total      = Decimal(str(round(item.price * item.qty, 2))),
        ))

    await db.commit()
    logger.info(f"Reserved order {order_id}")
    return {"order_id": order_id}


# ─── POST /api/checkout/update ───────────────────────────────────────────────
# Auto-save individual form fields as the customer types. Frontend calls this
# on field blur (and email keystroke, debounced) so we capture customer info
# progressively. Protects against customers who pay externally (e.g. via
# Interac e-Transfer) but forget to click Place Order — we still have their
# email/name/address to match the payment to a customer and ship the order.

class AutoSaveRequest(BaseModel):
    order_id: str = Field(..., max_length=50)
    field:    str = Field(..., max_length=50)
    value:    str = Field(..., max_length=255)


# Whitelist of frontend field names → DB column names.
# Anything not in this map is rejected.
AUTOSAVE_FIELD_MAP = {
    "email":          "email",
    "firstName":      "first_name",
    "lastName":       "last_name",
    "phone":          "phone",
    "address1":       "address1",
    "address2":       "address2",
    "city":           "city",
    "zone":           "province",
    "postalCode":     "postal_code",
    "country":        "country",
    "billAddress1":   "bill_address1",
    "billCity":       "bill_city",
    "billZone":       "bill_province",
    "billPostal":     "bill_postal",
    "billCountry":    "bill_country",
    "payment_method": "payment_method",
}


@router.post("/update")
async def autosave_order_field(
    payload: AutoSaveRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-save a single field of a reserved order as the customer types.
    Fire-and-forget — never blocks the customer, never raises HTTP errors.

    Behavior:
      • Empty values rejected (we never overwrite with empty)
      • Order must be in pending state (won't touch paid/failed/cancelled)
      • Only whitelisted fields are accepted (security)
      • payment_method strings are converted to the PaymentMethod enum
      • Any DB error is swallowed silently and logged
    """
    try:
        order_id = payload.order_id.strip()
        js_field = payload.field.strip()
        v        = payload.value.strip()

        if not order_id or not js_field or not v:
            return {"ok": False, "error": "missing_or_empty"}

        # Map JS field name → DB column. Reject unknown fields.
        db_column = AUTOSAVE_FIELD_MAP.get(js_field)
        if not db_column:
            return {"ok": False, "error": "unknown_field"}

        # Look up the reserved order
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            return {"ok": False, "error": "order_not_found"}

        # Only allow updates while the order is still pending
        if order.payment_status != PaymentStatus.pending:
            return {"ok": False, "error": "order_locked"}

        # Special handling for payment_method — recompute discount + total too
        # so the admin dashboard reflects the right amount as customer changes
        # methods (each method has a different discount %).
        if db_column == "payment_method":
            # Whop is a card-variant (cloaked) — gets stored as PaymentMethod.card
            # in the DB but is distinguished by payment_ref starting with "ch_".
            v_normalized = "card" if v == "whop" else v
            try:
                new_method = PaymentMethod(v_normalized)
            except (ValueError, KeyError):
                return {"ok": False, "error": "invalid_payment_method"}

            # Look up the brand for discount percentages (default fallbacks if not set)
            brand_result = await db.execute(
                select(Brand).where(Brand.id == order.brand_id)
            )
            brand = brand_result.scalar_one_or_none()

            if new_method == PaymentMethod.interac:
                pct = float(brand.interac_discount) if brand and brand.interac_discount else 10.0
            elif new_method == PaymentMethod.zelle:
                pct = float(getattr(brand, "zelle_discount", None) or 5.0)
            elif new_method == PaymentMethod.crypto:
                pct = float(brand.crypto_discount) if brand and brand.crypto_discount else 10.0
            elif new_method == PaymentMethod.altcoin:
                pct = 7.0   # NowPayments altcoin discount — matches /altcoin endpoint
            else:  # card
                pct = 0.0

            sub        = float(order.subtotal or 0)
            disc_amt   = round(sub * pct / 100, 2)
            new_total  = round(sub - disc_amt, 2)

            try:
                order.payment_method  = new_method
                order.discount_pct    = Decimal(str(pct))
                order.discount_amount = Decimal(str(disc_amt))
                order.total           = Decimal(str(new_total))
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.warning(f"[autosave] payment_method update failed for {order_id}: {e}")
                return {"ok": False, "error": "db_error"}

            return {"ok": True}

        # All other fields — simple setattr
        try:
            setattr(order, db_column, v)
            await db.commit()
        except Exception as e:
            await db.rollback()
            logger.warning(f"[autosave] DB update failed for {order_id}.{db_column}: {e}")
            return {"ok": False, "error": "db_error"}

        return {"ok": True}

    except Exception as e:
        # Catch-all — never throw, always return ok:false silently
        logger.warning(f"[autosave] unexpected error: {e}")
        return {"ok": False, "error": "server_error"}


# ─── POST /api/checkout/card ─────────────────────────────────────────────────

@router.post("/card")
async def checkout_card(
    payload: CardCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Credit card via pymtz hosted payment page.

    Creates the pending order, then creates a pymtz payment intent and returns
    the hosted payment_url. The frontend redirects the customer there; pymtz
    fires /webhooks/pymtz on completion, which marks the order paid and
    triggers Shopify order creation + affiliate webhook.
    """
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    description = f"Order {order.id}"
    if payload.items:
        first = payload.items[0].title
        extra = len(payload.items) - 1
        description = first + (f" +{extra} more" if extra > 0 else "")

    # Server-side redirects from pymtz can't see client-side state, so inject
    # v2 + country into return_url here based on the Referer.
    return_url = f"{settings.BASE_URL}/order/{order.id}/confirmation{_v2_referer_suffix(request)}"
    cancel_url = f"{settings.BASE_URL}/"

    # pymtz settles in USD. Convert CAD totals so the customer is charged the
    # USD equivalent shown by the "≈ $X USD" badge on checkout — not the raw
    # CAD number relabeled as USD.
    if (order.currency or "").upper() == "CAD":
        pymtz_amount   = round(float(order.total) / USD_CONVERSION_RATE, 2)
        pymtz_currency = "USD"
    else:
        pymtz_amount   = float(order.total)
        pymtz_currency = order.currency

    try:
        pymtz_country = "US" if (order.currency or "").upper() == "USD" else "CA"
        client  = PymtzClient(country=pymtz_country)

        # Use billing address if the customer entered one, else fall back to
        # shipping. payload.bill_same == "1" means "billing == shipping".
        bill_same = (payload.bill_same or "1") == "1"
        b_addr1   = (payload.address1 if bill_same else payload.bill_address1) or ""
        b_addr2   = (payload.address2 if bill_same else payload.bill_address2) or ""
        b_city    = (payload.city     if bill_same else payload.bill_city)     or ""
        b_state   = (payload.province if bill_same else payload.bill_province) or ""
        b_zip     = (payload.postal_code if bill_same else payload.bill_postal) or ""
        b_country = (payload.country  if bill_same else payload.bill_country)  or "CA"

        payment = await client.create_payment(
            order_id    = order.id,
            amount      = pymtz_amount,
            currency    = pymtz_currency,
            description = description,
            email       = payload.email,
            return_url  = return_url,
            cancel_url  = cancel_url,
            first_name  = payload.first_name or "",
            last_name   = payload.last_name  or "",
            phone       = payload.phone      or "",
            address1    = b_addr1,
            address2    = b_addr2,
            city        = b_city,
            state       = b_state,
            postal_code = b_zip,
            country     = b_country,
            metadata    = {
                "source_domain":    payload.source_domain or "",
                "store_name":       order.store_name or "",
                "order_currency":   order.currency or "",
                "order_total_cad":  str(order.total),
                "pymtz_account":    pymtz_country,
            },
        )

        order.payment_ref   = payment.get("id", "")
        order.payment_notes = f"pymtz payment {payment.get('id', '')} → {payment.get('payment_url', '')}"
        await db.commit()

        return {
            "success":     True,
            "orderId":     order.id,
            "redirectUrl": payment["payment_url"],
            "paymentId":   payment.get("id", ""),
        }

    except PymtzError as e:
        logger.exception(f"pymtz payment creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = str(e)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Could not start card payment: {e}")


# ─── POST /api/checkout/authnet ──────────────────────────────────────────────
#
# Customer-facing endpoint for the "Credit Card (Auth.net)" option. Customer
# tokenizes their card via Accept.js in the browser; we receive the nonce
# and immediately charge via Auth.net's createTransactionRequest API.
#
# Test Mode is controlled in the Auth.net dashboard, NOT here. Same code
# path works for test and live charges; the dashboard decides if real money
# moves.

@router.post("/authnet")
async def checkout_authnet(
    payload: AuthnetCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronous card charge via Authorize.net.

    Unlike pymtz (which holds in pending) or onramp (which redirects), this
    endpoint succeeds or fails on the SAME HTTP call. On success we mark
    the order paid, trigger Shopify + affiliate webhook, and return a
    redirectUrl. On failure we surface the bank decline reason to the user.
    """
    from services.authnet import AuthnetClient, AuthnetError, DATA_DESCRIPTOR_CARD

    if not bool(getattr(settings, "AUTHNET_ENABLED", False)):
        raise HTTPException(503, "Authorize.net not enabled")

    # Per-store gate (mirrors HIGHRISKIFY_STORES semantic): only stores in
    # AUTHNET_STORES are allowed to hit this endpoint. Prevents a direct
    # POST from bypassing the per-store rule the frontend enforces via the
    # authnet_enabled template flag.
    _authnet_raw = (getattr(settings, "AUTHNET_STORES", "") or "").strip()
    _src_domain  = (payload.source_domain or "").strip().lower()
    if _authnet_raw == "*":
        _authnet_allowed = True
    elif _authnet_raw == "":
        _authnet_allowed = False
    else:
        _authnet_allowlist = {s.strip().lower() for s in _authnet_raw.split(",") if s.strip()}
        _authnet_allowed = _src_domain in _authnet_allowlist
    if not _authnet_allowed:
        raise HTTPException(403, f"Authorize.net not enabled for store '{_src_domain}'")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # Create / fetch the pending order — same pattern as the other card paths.
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    # Build billTo block for AVS / fraud scoring. Auth.net likes complete
    # billing info → better AVS match → lower interchange + better acceptance.
    bill_same = (payload.bill_same or "1") == "1"
    billing = {
        "firstName":   (payload.first_name or "")[:50],
        "lastName":    (payload.last_name  or "")[:50],
        "address":     ((payload.address1 if bill_same else payload.bill_address1) or "")[:60],
        "city":        ((payload.city     if bill_same else payload.bill_city)     or "")[:40],
        "state":       ((payload.province if bill_same else payload.bill_province) or "")[:40],
        "zip":         ((payload.postal_code if bill_same else payload.bill_postal) or "")[:20],
        "country":     ((payload.country  if bill_same else payload.bill_country)  or "")[:60],
        "phoneNumber": (payload.phone or "")[:25],
    }

    # Customer's IP for AFDS fraud scoring (helps with chargeback dispute
    # representment too — proves cardholder was at the device they claim).
    client_ip = request.client.host if request.client else None

    try:
        client = AuthnetClient()
        result = await client.charge_card(
            opaque_data_value      = payload.opaque_data_value,
            opaque_data_descriptor = payload.opaque_data_descriptor or DATA_DESCRIPTOR_CARD,
            amount                 = float(order.total),
            order_id               = order.id,
            invoice_number         = order.id,
            description            = f"Order {order.id}",
            billing                = billing,
            customer_email         = payload.email,
            customer_ip            = client_ip,
        )
    except AuthnetError as e:
        logger.error(f"[authnet] charge transport error for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"authnet error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Card payment failed: {e}")

    # Always log the response — useful for debugging declines.
    logger.info(
        f"[authnet] order={order.id} response_code={result['response_code']} "
        f"trans_id={result['transaction_id']} msg={result['message'][:80]}"
    )

    if not result["success"]:
        # Decline / error / held-for-review — surface to user, mark failed.
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = (
            f"authnet declined · code={result['response_code']} "
            f"trans={result['transaction_id']} · {result['message'][:200]}"
        )[:1000]
        await db.commit()
        # Use 402 (Payment Required) so the frontend can distinguish
        # bank declines from server-side errors (502/500).
        raise HTTPException(402, detail={
            "code":    result["response_code"],
            "message": result["message"],
            "trans_id": result["transaction_id"],
        })

    # ── Charge succeeded — mark paid + downstream Shopify/affiliate ────────
    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.utcnow()
    # Prefix `an:` so the admin dashboard classifier recognizes this as
    # Auth.net (see models/order.py:_classify_processor).
    order.payment_ref    = f"an:{result['transaction_id']}"
    order.payment_notes  = (
        f"authnet paid · trans={result['transaction_id']} "
        f"auth={result['auth_code']} card=*{result['account_number'][-4:]} "
        f"avs={result['avs_result']} cvv={result['cvv_result']}"
    )[:1000]
    await db.commit()
    logger.info(f"✅ Card payment confirmed (authnet): order {order.id}")

    # Fire Shopify create + affiliate webhook — same downstream path as
    # other card processors (pymtz, highriskify webhook handler).
    try:
        from sqlalchemy.orm import selectinload
        result_q = await db.execute(
            select(Order).where(Order.id == order.id).options(selectinload(Order.items))
        )
        fresh_order = result_q.scalar_one_or_none()
        if fresh_order:
            try:
                from services.shopify import create_shopify_order
                await create_shopify_order(fresh_order)
            except Exception as e:
                logger.error(f"Shopify create failed for {order.id} (authnet): {e}")
            try:
                # Lazy import — _send_affiliate_webhook lives in routes/webhooks.py
                # to keep it next to the other webhook handlers that use it. Top-
                # level import would create a circular dependency.
                from routes.webhooks import _send_affiliate_webhook
                await _send_affiliate_webhook(fresh_order)
            except Exception as e:
                logger.error(f"Affiliate webhook failed for {order.id} (authnet): {e}")
    except Exception as e:
        logger.error(f"[authnet] post-payment hooks failed for {order.id}: {e}")

    return {
        "success":     True,
        "orderId":     order.id,
        "redirectUrl": f"/order/{order.id}/confirmation",
        "paymentId":   f"an:{result['transaction_id']}",
    }


# ─── POST /api/checkout/stripe_direct ────────────────────────────────────────
#
# Customer-facing endpoint for the "Credit Card (Stripe)" option. Parallel
# to /api/checkout/authnet — Stripe is its own card processor, not a fallback.
# Customer's card is tokenized via Stripe Elements in the browser; we receive
# the PaymentMethod ID and create+confirm a PaymentIntent server-side.
#
# Test mode is controlled by the API KEY (sk_test_xxx vs sk_live_xxx) — no
# dashboard toggle like Auth.net. Set STRIPE_SECRET_KEY in .env appropriately.
#
# Cloaking discipline:
#   * Description sent to Stripe is neutral ("Order ORD-XXX"); no product names
#   * Metadata only contains internal IDs (order_id, source_domain)
#   * Auto-receipts to customer are disabled at Stripe dashboard level — we
#     send our own branded confirmation via Resend
#   * Statement descriptor (what customer sees on bank statement) is the
#     Stripe account's cloaked DBA — configured in dashboard, not here

@router.post("/stripe_direct")
async def checkout_stripe_direct(
    payload: StripeDirectCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronous card charge via Stripe PaymentIntents API.

    Like Auth.net, succeeds/fails on the SAME HTTP call. Returns:
      - success → mark paid, trigger Shopify + affiliate webhook, redirect
      - decline → 402 with bank reason
      - requires_action → 402 with the 3DS challenge URL (frontend handles)
    """
    from services.stripe_direct import StripeDirectClient, StripeError

    if not bool(getattr(settings, "STRIPE_DIRECT_ENABLED", False)):
        raise HTTPException(503, "Stripe direct not enabled")

    # Per-store gate (mirrors AUTHNET_STORES semantic): explicit allowlist.
    # ""    → no stores
    # "*"   → all stores
    # "..." → only listed domains
    _stripe_raw = (getattr(settings, "STRIPE_DIRECT_STORES", "") or "").strip()
    _src_domain = (payload.source_domain or "").strip().lower()
    if _stripe_raw == "*":
        _stripe_allowed = True
    elif _stripe_raw == "":
        _stripe_allowed = False
    else:
        _stripe_allowlist = {s.strip().lower() for s in _stripe_raw.split(",") if s.strip()}
        _stripe_allowed = _src_domain in _stripe_allowlist
    if not _stripe_allowed:
        raise HTTPException(403, f"Stripe not enabled for store '{_src_domain}'")

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # Create / fetch the pending order — same pattern as the other card paths.
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    client_ip = request.client.host if request.client else None

    try:
        client = StripeDirectClient()
        result = await client.create_and_confirm_payment(
            payment_method_id = payload.payment_method_id,
            amount            = float(order.total),
            currency          = (order.currency or "USD"),
            order_id          = order.id,
            customer_email    = payload.email,
            customer_ip       = client_ip,
            source_domain     = payload.source_domain,
            # Neutral description — visible in Stripe dashboard only, but
            # we still keep it clean (no product names, no peptide refs).
            description       = f"Order {order.id}",
            # Use order ID as the idempotency key so accidental double-submit
            # from the same order won't double-charge.
            idempotency_key   = f"order-{order.id}",
        )
    except StripeError as e:
        logger.error(f"[stripe_direct] transport error for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"stripe error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Card payment failed: {e}")

    logger.info(
        f"[stripe_direct] order={order.id} status={result['status']} "
        f"pi={result['payment_intent_id']} msg={result['message'][:80]}"
    )

    # ── 3DS / SCA challenge required ──────────────────────────────────────
    # Stripe returned requires_action — the customer needs to complete a
    # 3DS challenge before we can finalize. Pass the next_action info back
    # to the frontend; Stripe.js handles the challenge UI.
    if result["status"] == "requires_action" and result.get("next_action"):
        order.payment_status = PaymentStatus.pending
        order.payment_ref    = f"pi:{result['payment_intent_id']}"
        order.payment_notes  = (
            f"stripe 3DS required · pi={result['payment_intent_id']}"
        )[:1000]
        await db.commit()
        return {
            "success":         False,
            "requires_action": True,
            "payment_intent_client_secret": result["raw"].get("client_secret", ""),
            "orderId":         order.id,
        }

    if not result["success"]:
        # Decline / error — surface to user, mark failed.
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = (
            f"stripe declined · status={result['status']} "
            f"pi={result['payment_intent_id']} · {result['message'][:200]}"
        )[:1000]
        await db.commit()
        raise HTTPException(402, detail={
            "code":    result["status"],
            "message": result["message"],
            "pi":      result["payment_intent_id"],
        })

    # ── Charge succeeded — mark paid + downstream Shopify/affiliate ────────
    order.payment_status = PaymentStatus.paid
    order.paid_at        = datetime.utcnow()
    # Prefix `pi:` so the admin dashboard classifier recognizes this as
    # Stripe direct (see models/order.py:_classify_processor → `pi_` prefix
    # already maps to "stripe").
    order.payment_ref    = f"pi_{result['payment_intent_id'].replace('pi_', '')}"
    order.payment_notes  = (
        f"stripe paid · pi={result['payment_intent_id']} "
        f"charge={result['charge_id']} card={result['brand']}*{result['last4']}"
    )[:1000]
    await db.commit()
    logger.info(f"✅ Card payment confirmed (stripe_direct): order {order.id}")

    # Downstream — Shopify create + affiliate webhook. Same path as Auth.net.
    try:
        from sqlalchemy.orm import selectinload
        result_q = await db.execute(
            select(Order).where(Order.id == order.id).options(selectinload(Order.items))
        )
        fresh_order = result_q.scalar_one_or_none()
        if fresh_order:
            try:
                from services.shopify import create_shopify_order
                await create_shopify_order(fresh_order)
            except Exception as e:
                logger.error(f"Shopify create failed for {order.id} (stripe_direct): {e}")
            try:
                from routes.webhooks import _send_affiliate_webhook
                await _send_affiliate_webhook(fresh_order)
            except Exception as e:
                logger.error(f"Affiliate webhook failed for {order.id} (stripe_direct): {e}")
    except Exception as e:
        logger.error(f"[stripe_direct] post-payment hooks failed for {order.id}: {e}")

    return {
        "success":     True,
        "orderId":     order.id,
        "redirectUrl": f"/order/{order.id}/confirmation",
        "paymentId":   f"pi:{result['payment_intent_id']}",
    }


# ─── POST /api/checkout/interac ──────────────────────────────────────────────

# ─── POST /api/checkout/onramp_wp ────────────────────────────────────────────
#
# Customer-facing endpoint for the "Card (Alt)" option. Dispatches to either:
#   1. Highriskify direct API   (HIGHRISKIFY_ENABLED=true) → preferred
#   2. WordPress + 2530gateway plugin  (ONRAMP_WP_ENABLED=true) → legacy
#
# Both produce the same response shape ({success, orderId, redirectUrl}) so
# the frontend doesn't need to know which backend handled the request.

@router.post("/onramp_wp")
async def checkout_onramp_wp(
    payload: CardCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Card payment via an onramp provider — Highriskify direct API preferred,
    falls back to WP plugin if Highriskify isn't configured.

    Creates the pending order, then either:
      A) Calls Highriskify wallet.php → builds process-payment.php redirect
      B) Creates a WC order via the plugin's WC REST API

    Customer is redirected to the returned URL. Payment confirmation arrives
    via /webhooks/highriskify (path A) or /webhooks/onramp_wp (path B), which
    marks the order paid and runs the same downstream flow as pymtz
    (Shopify create + affiliate webhook).
    """
    hr_enabled = bool(getattr(settings, "HIGHRISKIFY_ENABLED", False))
    wp_enabled = bool(getattr(settings, "ONRAMP_WP_ENABLED",  False))

    if not (hr_enabled or wp_enabled):
        raise HTTPException(503, "Onramp not enabled")

    # Block onramp for US stores. Mirrors the UI gate in main.py — even if a
    # US customer tries to call this endpoint directly, refuse the request.
    if (payload.store_country or "").upper() == "US":
        raise HTTPException(403, "Onramp is not available for US stores")

    # Per-store gate — explicit allowlist. Only domains in HIGHRISKIFY_STORES
    # route through the new Transak/MoonPay picker. Everyone else uses the
    # WP plugin path.
    #   ""    (empty)  → NO stores use Highriskify
    #   "*"            → ALL stores use Highriskify
    #   "a.com,b.com"  → only those domains
    hr_stores_raw = (getattr(settings, "HIGHRISKIFY_STORES", "") or "").strip()
    src_domain    = (payload.source_domain or "").strip().lower()
    if hr_stores_raw == "*":
        hr_for_this_store = True
    elif hr_stores_raw == "":
        hr_for_this_store = False
    else:
        hr_allowlist     = {s.strip().lower() for s in hr_stores_raw.split(",") if s.strip()}
        hr_for_this_store = src_domain in hr_allowlist

    use_highriskify = hr_enabled and hr_for_this_store

    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    # Reuse the `card` PaymentMethod — the customer-facing UX is still a card,
    # we disambiguate via payment_notes/payment_ref.
    order = await _create_base_order(db, payload, PaymentMethod.card, brand, 0.0, request)
    await db.commit()

    # ── Path A: Highriskify direct API ────────────────────────────────────
    if use_highriskify:
        from services.highriskify import HighriskifyClient, HighriskifyError

        # Customer-picked provider (from the modal on checkout) — validate
        # against an allowlist so a hostile payload can't push us to a broken
        # or attack-surface provider. Unknown / missing → fall back to the
        # configured default (settings.HIGHRISKIFY_PROVIDER, usually transak).
        ONRAMP_PROVIDERS = {"transak", "moonpay", "topper"}
        chosen_provider = (payload.provider or "").strip().lower()
        if chosen_provider not in ONRAMP_PROVIDERS:
            chosen_provider = None  # let the client use its default

        # Build the callback URL Highriskify will GET on payment completion.
        # Includes the order_id so the webhook handler can find the order.
        callback_url = f"{settings.BASE_URL}/webhooks/highriskify?number={order.id}"

        try:
            client = HighriskifyClient()
            # Step 1 — create encrypted wallet
            wallet_resp = await client.create_wallet(
                order_id     = order.id,
                callback_url = callback_url,
            )
            address_in        = wallet_resp["address_in"]
            polygon_address   = wallet_resp.get("polygon_address_in", "")
            ipn_token         = wallet_resp.get("ipn_token", "")

            # Step 2 — build hosted-checkout redirect URL with provider pinned
            redirect_url = client.build_checkout_url(
                address_in = address_in,
                amount     = float(order.total),
                currency   = (order.currency or "USD").upper(),
                email      = payload.email or "",
                provider   = chosen_provider,
            )
            effective_provider = chosen_provider or client.provider

            order.payment_ref   = f"hr:{polygon_address}"   # the temp wallet — used to match webhook
            order.payment_notes = (
                f"highriskify via {effective_provider} → temp={polygon_address} "
                f"ipn={ipn_token[:20]}..."
            )[:1000]
            await db.commit()

            # Fire IPT tracking (non-blocking, won't fail the checkout)
            await client.ipt_track({
                "event_type":      "wallet_created",
                "platform":        "custom-api",
                "merchant_site":   (payload.source_domain or "").lower(),
                "api_domain":      (settings.BASE_URL or "").replace("https://", "").replace("http://", ""),
                "checkout_domain": (settings.BASE_URL or "").replace("https://", "").replace("http://", ""),
                "order_id":        order.id,
                "client_email":    payload.email or "",
                "temp_wallet":     polygon_address,
                "network":         "polygon",
                "token_symbol":    "USDC",
                "expected_amount": float(order.total),
                "order_total_usd": float(order.total),
                "fiat_currency":   (order.currency or "USD").upper(),
                "gateway_name":    f"Highriskify ({effective_provider})",
                "merchant_wallet": client.merchant_wallet,
                "billing_name":    f"{payload.first_name or ''} {payload.last_name or ''}".strip(),
                "billing_phone":   payload.phone or "",
            })

            return {
                "success":     True,
                "orderId":     order.id,
                "redirectUrl": redirect_url,
                "paymentId":   f"hr:{polygon_address}",
            }

        except HighriskifyError as e:
            logger.error(f"[highriskify] order create failed for {order.id}: {e}")
            order.payment_status = PaymentStatus.failed
            order.payment_notes  = f"highriskify error: {str(e)[:300]}"
            await db.commit()
            raise HTTPException(502, f"Onramp payment setup failed: {e}")

    # ── Path B: WP plugin fallback ────────────────────────────────────────
    from services.onramp_wp import OnrampWPClient, OnrampWPError

    # Send the cart amount + currency through to WC as-is. WooCommerce + the
    # 2530gateway plugin handle FX themselves — pre-converting CAD→USD here
    # caused the onramp UI (Kryptonim et al.) to label the USD value as CAD,
    # undercharging the customer by ~28%.
    wc_amount   = float(order.total)
    wc_currency = (order.currency or "USD").upper()

    bill_same = (payload.bill_same or "1") == "1"
    b_addr1   = (payload.address1 if bill_same else payload.bill_address1) or ""
    b_addr2   = (payload.address2 if bill_same else payload.bill_address2) or ""
    b_city    = (payload.city     if bill_same else payload.bill_city)     or ""
    b_state   = (payload.province if bill_same else payload.bill_province) or ""
    b_zip     = (payload.postal_code if bill_same else payload.bill_postal) or ""
    b_country = (payload.country  if bill_same else payload.bill_country)  or "CA"

    try:
        client  = OnrampWPClient()
        wc_resp = await client.create_order(
            external_order_id = order.id,
            amount      = wc_amount,
            currency    = wc_currency,
            first_name  = payload.first_name or "",
            last_name   = payload.last_name  or "",
            email       = payload.email,
            phone       = payload.phone      or "",
            address1    = b_addr1,
            address2    = b_addr2,
            city        = b_city,
            state       = b_state,
            postal_code = b_zip,
            country     = b_country,
        )
        wc_order_id = wc_resp.get("id")
        pay_url     = wc_resp["payment_url"]

        order.payment_ref   = f"wc:{wc_order_id}"
        order.payment_notes = f"onramp_wp via WC order #{wc_order_id} → {pay_url}"
        await db.commit()

        return {
            "success":     True,
            "orderId":     order.id,
            "redirectUrl": pay_url,
            "paymentId":   f"wc:{wc_order_id}",
        }

    except OnrampWPError as e:
        logger.error(f"[onramp_wp] order create failed for {order.id}: {e}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"onramp_wp error: {str(e)[:300]}"
        await db.commit()
        raise HTTPException(502, f"Onramp payment setup failed: {e}")


@router.post("/interac")
async def checkout_interac(
    payload: InteracCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    discount_pct = float(brand.interac_discount if brand else 10.0)

    order = await _create_base_order(db, payload, PaymentMethod.interac, brand, discount_pct, request)

    # Reuse existing InteracPayment if present (customer re-submitted); else create
    existing = await db.execute(
        select(InteracPayment).where(InteracPayment.order_id == order.id)
    )
    ip = existing.scalar_one_or_none()
    if ip:
        if ip.status != "matched":
            ip.expected_amount = order.total
            ip.status          = "waiting"
    else:
        db.add(InteracPayment(
            order_id        = order.id,
            expected_amount = order.total,
            status          = "waiting",
        ))

    await db.commit()

    interac_email = (
        brand.interac_email if brand and brand.interac_email
        else settings.INTERAC_DEFAULT_EMAIL
    )

    return {
        "success":      True,
        "orderId":      order.id,
        "total":        float(order.total),
        "currency":     order.currency,
        "interacEmail": interac_email,
        "discountPct":  discount_pct,
        "instructions": (
            f"Send ${float(order.total):.2f} {order.currency} via Interac e-Transfer to "
            f"{interac_email}. In the message/note field, enter your Order ID: {order.id}"
        ),
    }


# ─── POST /api/checkout/zelle ────────────────────────────────────────────────

class ZelleCheckoutRequest(CheckoutBase):
    pass


@router.post("/zelle")
async def checkout_zelle(
    payload: ZelleCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    US equivalent of Interac. Customer sends Zelle manually to our US email.
    Admin matches the payment manually via the admin dashboard.
    """
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    # Zelle keeps a 5% discount (Interac is 10%)
    discount_pct = float(getattr(brand, "zelle_discount", None) or 5.0)

    order = await _create_base_order(db, payload, PaymentMethod.zelle, brand, discount_pct, request)

    # Reuse existing ZellePayment if present (customer re-submitted); else create
    existing = await db.execute(
        select(ZellePayment).where(ZellePayment.order_id == order.id)
    )
    zp = existing.scalar_one_or_none()
    if zp:
        if zp.status != "matched":
            zp.expected_amount = order.total
            zp.status          = "waiting"
    else:
        db.add(ZellePayment(
            order_id        = order.id,
            expected_amount = order.total,
            status          = "waiting",
        ))

    await db.commit()

    zelle_email = settings.ZELLE_DEFAULT_EMAIL or ""

    return {
        "success":     True,
        "orderId":     order.id,
        "total":       float(order.total),
        "currency":    order.currency,
        "zelleEmail":  zelle_email,
        "discountPct": discount_pct,
        "instructions": (
            f"Send ${float(order.total):.2f} {order.currency} via Zelle to "
            f"{zelle_email}. In the memo/note field, enter your Order ID: {order.id}"
        ),
    }


# ─── POST /api/checkout/crypto ───────────────────────────────────────────────

@router.post("/crypto")
async def checkout_crypto(
    payload: CryptoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand        = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    discount_pct = float(brand.crypto_discount if brand else 10.0)

    order = await _create_base_order(db, payload, PaymentMethod.crypto, brand, discount_pct, request)

    # Use brand-specific BTCPay store if configured
    btcpay_store = (brand.btcpay_store_id if brand and brand.btcpay_store_id else None)
    client       = BTCPayClient(store_id=btcpay_store)

    webhook_url = f"{settings.BASE_URL}/webhooks/btcpay"

    try:
        invoice = await client.create_invoice(
            order_id       = order.id,
            amount         = float(order.total),
            currency       = order.currency,
            customer_email = payload.email,
            customer_name  = f"{payload.first_name or ''} {payload.last_name}".strip(),
            webhook_url    = webhook_url,
        )

        btcpay_id  = invoice["id"]
        invoice_url = invoice.get("checkoutLink", "")

        # Reuse existing CryptoInvoice if present (customer re-submitted); else create
        existing = await db.execute(
            select(CryptoInvoice).where(CryptoInvoice.order_id == order.id)
        )
        ci = existing.scalar_one_or_none()
        if ci:
            ci.btcpay_invoice_id  = btcpay_id
            ci.btcpay_invoice_url = invoice_url
            ci.amount_fiat        = order.total
            ci.status             = "New"
        else:
            db.add(CryptoInvoice(
                order_id           = order.id,
                btcpay_invoice_id  = btcpay_id,
                btcpay_invoice_url = invoice_url,
                amount_fiat        = order.total,
                status             = "New",
            ))

        order.payment_ref = btcpay_id
        await db.commit()

        # Kick off background polling as webhook fallback
        from tasks.celery_app import check_btcpay_invoice
        check_btcpay_invoice.apply_async(
            args=[order.id, btcpay_id],
            countdown=120,   # start checking after 2 minutes
        )

        return {
            "success":          True,
            "orderId":          order.id,
            "btcpayInvoiceUrl": invoice_url,
            "discountPct":      discount_pct,
            "total":            float(order.total),
            "currency":         order.currency,
        }

    except BTCPayError as e:
        logger.exception(f"BTCPay invoice creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Crypto payment unavailable: {e}")


# ─── POST /api/checkout/altcoin ───────────────────────────────────────────────

@router.post("/altcoin")
async def checkout_altcoin(
    payload: CryptoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))
    discount_pct = 7.0  # NowPayments altcoin discount fixed at 7%

    order = await _create_base_order(db, payload, PaymentMethod.altcoin, brand, discount_pct, request)

    client      = NowPaymentsClient()
    ipn_url     = f"{settings.BASE_URL}/webhooks/nowpayments"
    # Mirror v2 + country onto the post-payment confirmation page if the
    # customer started on the v2 checkout. NOWPayments does its own redirect
    # after invoice settles, so the flag must be baked into the URL now.
    success_url = f"{settings.BASE_URL}/order/{order.id}/confirmation{_v2_referer_suffix(request)}"
    cancel_url  = f"{settings.BASE_URL}/"

    try:
        invoice = await client.create_invoice(
            order_id         = order.id,
            amount           = float(order.total),
            currency         = order.currency,
            ipn_callback_url = ipn_url,
            success_url      = success_url,
            cancel_url       = cancel_url,
        )

        np_invoice_id = str(invoice["id"])
        invoice_url   = invoice.get("invoice_url", "")

        db.add(NowPaymentsInvoice(
            order_id      = order.id,
            np_invoice_id = np_invoice_id,
            invoice_url   = invoice_url,
            amount_fiat   = order.total,
            status        = "waiting",
        ))
        order.payment_ref = np_invoice_id
        await db.commit()

        return {
            "success":     True,
            "orderId":     order.id,
            "invoiceUrl":  invoice_url,
            "discountPct": discount_pct,
            "total":       float(order.total),
            "currency":    order.currency,
        }

    except NowPaymentsError as e:
        logger.exception(f"NowPayments invoice creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Altcoin payment unavailable: {e}")


# ─── POST /api/checkout/lasso ────────────────────────────────────────────────
# Cloaked CC checkout via Lasso → Whop payment rails.
#
# Flow:
#   1. Create order in our DB with REAL product titles (for fulfillment)
#   2. Cloak all item titles → universal decoy before calling Lasso
#   3. POST cloaked cart to Lasso API → get session_id
#   4. Return redirect URL to frontend → customer lands on Lasso checkout
#   5. Whop fires /webhooks/whop on payment completion → marks order paid

class LassoCheckoutRequest(CheckoutBase):
    pass


@router.post("/lasso")
async def checkout_lasso(
    payload: LassoCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # 1. Create order with REAL item titles — our DB always has the truth
    order = await _create_base_order(
        db, payload, PaymentMethod.card, brand, 0.0, request
    )
    await db.commit()

    # 2. Cloak items → Lasso-specific mapping (peptide → dedicated decoy)
    cloaked     = cloak_items_lasso(payload.items)
    lasso_cart  = build_lasso_cart(cloaked)

    # 3. Create Lasso session
    try:
        client     = LassoClient()
        session_id = await client.create_session(
            cart      = lasso_cart,
            currency  = payload.currency,
            country   = payload.store_country,
            order_id  = order.id,
        )
        redirect_url = client.build_redirect_url(session_id)

        order.payment_ref   = session_id
        order.payment_notes = f"lasso session {session_id}"
        await db.commit()

        logger.info(f"[Lasso] Order {order.id} → session {session_id}")

        return {
            "success":     True,
            "orderId":     order.id,
            "sessionId":   session_id,
            "redirectUrl": redirect_url,
        }

    except LassoError as e:
        logger.exception(f"[Lasso] Session creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = str(e)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Could not start card payment: {e}")


# ─── POST /api/checkout/whop-embed ───────────────────────────────────────────
# Cloaked CC checkout via Whop's embedded checkout widget → direct integration,
# no Lasso, no bridge worker. This is PARALLEL to /api/checkout/card and /lasso:
# customers see "Card (WHOP)" as a separate payment option on the frontend.
#
# Flow:
#   1. Create order in our DB with REAL product titles (for fulfillment)
#   2. Call Whop API /checkout_configurations → creates a one-time plan at the
#      customer's actual cart total. Plan title is the cloaked decoy name.
#      metadata.order_id = our portal order_id so the webhook can match back.
#   3. Return { plan_id, session_id, whop_email } to the frontend, which mounts
#      <div data-whop-checkout-plan-id=... data-whop-checkout-session=...>
#      so the customer pays inside an iframe (PCI stays on whop.com).
#   4. Whop fires /webhooks/whop on payment completion → marks order paid,
#      auto-creates Shopify fulfillment order, sends affiliate webhook.

class WhopEmbedCheckoutRequest(CheckoutBase):
    # email / last_name made optional because the frontend creates the Whop
    # session AS SOON as the customer picks "Card (WHOP)" → before they've
    # filled the form. Real values are synced into the iframe later via
    # wco.setEmail / wco.setAddress (and into our DB via autosave). Whop
    # doesn't require email at session creation; it's collected inside the
    # iframe (which we hide and populate programmatically).
    email:     str = ""
    last_name: str = ""


@router.post("/whop-embed")
async def checkout_whop_embed(
    payload: WhopEmbedCheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    brand = _get_brand(request)
    _validate_cart(payload.items, payload.subtotal, getattr(payload, "discount_amount", 0.0))

    # ── Master kill-switch ───────────────────────────────────────────────
    # WHOP_ENABLED=false in .env disables this endpoint without touching keys
    # or limits. Also hides the frontend option (checked in main.py).
    if not bool(getattr(settings, "WHOP_ENABLED", True)):
        logger.info("[Whop] WHOP_ENABLED=false → refusing whop-embed request")
        return {
            "success":  False,
            "fallback": True,
            "reason":   "whop_disabled",
            "detail": (
                "This payment option is temporarily unavailable. "
                "Please choose Credit Card, Interac, or Crypto."
            ),
        }

    # ── Daily volume cap on Whop ──────────────────────────────────────────
    # Sum today's UTC-day card orders that were routed through Whop (their
    # payment_ref starts with the Whop session prefix "ch_"). We only refuse
    # NEW orders once today's running total has ALREADY reached the cap →
    # an order that would tip us over is still allowed through. So with a
    # $300 cap and $0 used today, a $500 order goes through (becomes $500
    # used); the NEXT order is rejected because we're already over.
    # Self-imposed throttle to keep Whop volume predictable and reduce
    # compliance review surface.
    daily_limit = float(getattr(settings, "WHOP_DAILY_LIMIT", 0) or 0)
    if daily_limit > 0:
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        result = await db.execute(
            select(func.coalesce(func.sum(Order.total), 0))
            .where(Order.created_at >= today_start)
            .where(Order.payment_method == PaymentMethod.card)
            .where(Order.payment_ref.like("ch_%"))
        )
        today_total = float(result.scalar() or 0)
        new_order_total = float(payload.subtotal)

        # Reject only if we're ALREADY at/over the cap. Lets the first order
        # of the day go through even if it's larger than the cap.
        if today_total >= daily_limit:
            logger.warning(
                f"[Whop] Daily limit reached: today={today_total:.2f} >= "
                f"limit={daily_limit:.2f} → refusing whop-embed for {payload.email} "
                f"(would-be new order: {new_order_total:.2f})"
            )
            return {
                "success":  False,
                "fallback": True,
                "reason":   "daily_limit_reached",
                "detail": (
                    "This payment option is temporarily at capacity. "
                    "Please choose Credit Card, Interac, or Crypto."
                ),
                "today_total":  round(today_total, 2),
                "daily_limit":  daily_limit,
            }
        elif (today_total + new_order_total) > daily_limit:
            # Allowed but we're about to tip over → log it so you know.
            logger.info(
                f"[Whop] This order will push past daily limit: "
                f"today={today_total:.2f} + new={new_order_total:.2f} > limit={daily_limit:.2f}. "
                f"Allowing (last order of the day on Whop)."
            )

    # 1. Create order with REAL item titles in our DB → single source of truth
    order = await _create_base_order(
        db, payload, PaymentMethod.card, brand, 0.0, request
    )
    await db.commit()

    # 2. Build cloaked items (informational — we don't pass them to Whop because
    #    Whop only sees a single inline plan, but cloaking the title is what
    #    keeps peptide names off Whop's records).
    _ = cloak_items(payload.items)  # noqa

    # 3. Create a Whop checkout configuration at the order's actual total.
    #    Deliberately omit return_url (BASE_URL would leak in Whop's records)
    #    and don't pass identifying metadata (source_domain / store_name).
    #    The frontend uses skip-redirect + onCheckoutComplete callback to
    #    redirect to /order/{id}/confirmation locally.
    return_url = settings.WHOP_RETURN_URL or None

    try:
        client  = WhopClient()
        session = await client.create_checkout_session(
            order_id   = order.id,
            amount     = float(order.total),
            email      = payload.email,
            currency   = order.currency,
            return_url = return_url,
            extra_meta = None,
        )
    except WhopError as e:
        logger.exception(f"[Whop-embed] Whop session creation failed for {order.id}")
        order.payment_status = PaymentStatus.failed
        order.payment_notes  = f"whop-embed failed: {e}"
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Could not start card payment: {e}")

    # If a tier plan was used, the actual amount Whop will charge is the tier
    # price, NOT the original cart total. Reconcile order.total so our DB
    # records the true charged amount (matches what shows on customer's
    # statement and our confirmation page). Track the original cart subtotal
    # in payment_notes for audit / reconciliation.
    charged_amount = float(session.get("charged_amount", session["amount"]))
    original_total = float(order.total)
    tier_was_used  = bool(session.get("tier_used", False))

    order.payment_ref = session["session_id"]
    if tier_was_used and abs(charged_amount - original_total) > 0.005:
        # Update both subtotal and total so they stay consistent in the DB
        order.total    = Decimal(str(charged_amount))
        order.subtotal = Decimal(str(charged_amount))
        order.payment_notes = (
            f"Whop embedded → session {session['session_id']} (tier "
            f"plan {session['plan_id']}). Cart was ${original_total:.2f}, "
            f"customer charged ${charged_amount:.2f} (tier match)."
        )
        logger.info(
            f"[Whop-embed] Tier reconciliation: order {order.id} "
            f"cart=${original_total:.2f} → charged=${charged_amount:.2f}"
        )
    else:
        order.payment_notes = (
            f"Whop embedded checkout → session {session['session_id']} "
            f"(plan {session['plan_id']})"
        )
    await db.commit()

    return {
        "success":         True,
        "orderId":         order.id,
        "whop_hosted":     True,
        "purchase_url":    session["purchase_url"],
        "session_id":      session["session_id"],
        "plan_id":         session["plan_id"],
        "amount":          session["amount"],          # original cart amount
        "charged_amount":  charged_amount,             # what Whop will actually charge
        "tier_used":       tier_was_used,
        "currency":        session["currency"],
        "sandbox":         session.get("sandbox", False),
        "whop_email":      session.get("whop_email", ""),
    }


# ─── GET /api/checkout/pymtz-verify/{order_id} ───────────────────────────────
# Called by confirmation.html when the customer returns from pymtz's hosted
# payment page. pymtz has no webhooks, so we poll their API here to learn
# the real outcome, mark the order paid if confirmed, and fire all side
# effects (MPC Shopify order, affiliate log, Resend email).
#
# Idempotent — safe to call multiple times. If the order is already paid/
# failed/expired we return immediately without hitting pymtz's API again.

@router.get("/pymtz-verify/{order_id}")
async def pymtz_verify(
    order_id: str,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload
    from services.pymtz import PymtzClient, PymtzError, PYMTZ_STATUS_MAP
    from datetime import datetime, timezone
    import httpx as _httpx

    result = await db.execute(
        select(Order).where(Order.id == order_id)
        .options(selectinload(Order.items))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Order not found")

    # Already resolved → return current status without touching pymtz API
    if order.payment_status != PaymentStatus.pending:
        return {
            "orderId":       order.id,
            "paymentStatus": order.payment_status.value,
        }

    if not order.payment_ref:
        return {"orderId": order.id, "paymentStatus": "pending"}

    # ── Ask pymtz for current payment status ──────────────────────────────────
    # Use the same per-country account the payment was created on, otherwise the
    # GET hits the wrong merchant and returns 404 / 401.
    pymtz_country = "US" if (order.currency or "").upper() == "USD" else "CA"
    try:
        payment = await PymtzClient(country=pymtz_country).get_payment(order.payment_ref)
    except Exception as e:
        logger.warning(f"[pymtz-verify] get_payment failed for {order.id}: {e}")
        return {"orderId": order.id, "paymentStatus": "pending"}

    pymtz_status = payment.get("status", "")
    our_status   = PYMTZ_STATUS_MAP.get(pymtz_status)

    if not our_status or our_status == "pending":
        return {"orderId": order.id, "paymentStatus": "pending"}

    # ── Update order ──────────────────────────────────────────────────────────
    order.payment_status = PaymentStatus(our_status)
    if our_status == "paid":
        order.paid_at       = datetime.utcnow()
        order.payment_notes = f"pymtz {order.payment_ref} confirmed via return-url verify."
    elif our_status == "failed":
        order.payment_notes = f"pymtz {order.payment_ref} failed (verify check)."
    elif our_status == "expired":
        order.payment_notes = f"pymtz {order.payment_ref} expired (verify check)."
    await db.commit()

    logger.info(f"[pymtz-verify] Order {order.id} → {our_status} (pymtz status: {pymtz_status})")

    # ── Side effects — only on paid ───────────────────────────────────────────
    if our_status == "paid":
        from database import AsyncSessionLocal
        async with AsyncSessionLocal() as db2:
            res2 = await db2.execute(
                select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items))
            )
            order2 = res2.scalar_one_or_none()
            if order2:
                # 1. MPC Shopify order
                shopify_order_number = None
                try:
                    from services.shopify import create_shopify_order
                    shopify_order = await create_shopify_order(order2)
                    if shopify_order:
                        shopify_order_number = str(shopify_order.get("order_number", ""))
                        logger.info(
                            f"✅ Shopify order #{shopify_order_number} "
                            f"auto-created for {order2.id} (pymtz verify)"
                        )
                    else:
                        logger.error(f"Shopify auto-create returned None for {order2.id} (pymtz verify)")
                except Exception as e:
                    logger.exception(f"Shopify auto-create failed for {order2.id} (pymtz verify): {e}")

                # 2. Affiliate log — delegate to shared helper so behavior
                # stays in lockstep with webhook-driven paid transitions.
                from routes.webhooks import _send_affiliate_webhook
                await _send_affiliate_webhook(order2)

                # 3. Resend confirmation email
                if order2.email:
                    try:
                        from models.brand import Brand
                        brand_res = await db2.execute(
                            select(Brand).where(Brand.id == order2.brand_id)
                        )
                        brand  = brand_res.scalar_one_or_none()
                        accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
                        from services.email import send_confirmation_email
                        await send_confirmation_email(
                            order2,
                            shopify_order_number=shopify_order_number,
                            accent=accent,
                        )
                        logger.info(
                            f"✉️  Confirmation email sent for {order2.id} "
                            f"(pymtz verify) → {order2.email}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Confirmation email failed for {order2.id} (pymtz verify): {e}"
                        )

    return {"orderId": order.id, "paymentStatus": our_status}


# ─── GET /api/checkout/status/{order_id} ──────────────────────────────────────

@router.get("/status/{order_id}")
async def order_status(order_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order  = result.scalar_one_or_none()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    return {
        "orderId":       order.id,
        "storeName":     order.store_name,
        "paymentMethod": order.payment_method.value if hasattr(order.payment_method, 'value') else str(order.payment_method),
        "paymentStatus": order.payment_status.value if hasattr(order.payment_status, 'value') else str(order.payment_status),
        "total":         float(order.total),
        "currency":      order.currency,
        "createdAt":     order.created_at.isoformat() if order.created_at else None,
        "paidAt":        order.paid_at.isoformat() if order.paid_at else None,
    }
