"""
POST /webhooks/btcpay         — BTCPay Server payment notifications
POST /webhooks/shopify-paid   — Shopify order paid (from bridge stores)
"""
import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Request, HTTPException, Header
from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal
from models.order import Order, CryptoInvoice, NowPaymentsInvoice, PaymentMethod, PaymentStatus
from services.btcpay import verify_btcpay_webhook, BTCPAY_STATUS_MAP, BTCPayClient
from services.nowpayments import verify_nowpayments_ipn, NOWPAYMENTS_STATUS_MAP
from services.pymtz import PYMTZ_STATUS_MAP
import httpx

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


async def _send_affiliate_webhook(order) -> None:
    """
    Notify the rosicteam dashboard whenever an order transitions to paid.

    Sends for EVERY paid order — with or without a discount_code. Orders
    placed without an affiliate code still post; the `discount_code` field is
    an empty string in that case so rosicteam can record the sale under "no
    affiliate" while still tracking total order flow.

    Bails only if AFFILIATE_DASHBOARD_URL isn't configured (dev/local).
    Errors are caught + logged; no retry — failed sends are surfaced in the
    log only. Callers are expected to use the helper idempotently.
    """
    try:
        affiliate_url = getattr(settings, "AFFILIATE_DASHBOARD_URL", "")
        if not affiliate_url:
            return
        items_summary = ", ".join(
            f"{item.qty}x {item.title}" for item in (order.items or [])
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{affiliate_url}/api/webhooks/order-paid",
                json={
                    "customer_first_name": order.first_name or order.last_name or "Customer",
                    "items_summary":       items_summary,
                    "order_total":         float(order.total),
                    "discount_code":       (order.discount_code or "").upper(),
                    "source":              "portal",
                    "external_order_id":   order.id,
                    "source_store":        order.source_domain or order.store_name or "",
                    "currency":            order.currency or "CAD",
                },
            )
        logger.info(f"Affiliate webhook sent for {order.id}: {resp.status_code}")
    except Exception as e:
        logger.warning(f"Affiliate webhook failed for {order.id}: {e}")


# ─── POST /webhooks/btcpay ────────────────────────────────────────────────────

@router.post("/btcpay")
async def btcpay_webhook(
    request: Request,
    btcpay_sig: str = Header(None, alias="BTCPay-Sig"),
):
    raw_body = await request.body()

    # Verify HMAC signature from BTCPay
    if not verify_btcpay_webhook(raw_body, btcpay_sig or ""):
        logger.warning("BTCPay webhook: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    event_type    = data.get("type", "")
    btcpay_id     = data.get("invoiceId", "")

    # Map event type to status
    TYPE_TO_STATUS = {
        "InvoiceSettled":         "Complete",
        "InvoicePaymentSettled":  "Processing",
        "InvoiceExpired":         "Expired",
        "InvoiceInvalid":         "Invalid",
        "InvoiceCreated":         "New",
        "InvoiceProcessing":      "Processing",
    }
    btcpay_status = TYPE_TO_STATUS.get(event_type, "New")

    logger.info(f"BTCPay webhook: {event_type} | invoice {btcpay_id} | status {btcpay_status}")

    # Only act on terminal states
    our_status = BTCPAY_STATUS_MAP.get(btcpay_status)
    if not our_status or our_status == "pending":
        return {"received": True, "action": "none"}

    async with AsyncSessionLocal() as db:
        # Find the crypto invoice record
        inv_result = await db.execute(
            select(CryptoInvoice).where(CryptoInvoice.btcpay_invoice_id == btcpay_id)
        )
        inv_rec = inv_result.scalar_one_or_none()

        if not inv_rec:
            logger.warning(f"BTCPay webhook: no CryptoInvoice found for {btcpay_id}")
            return {"received": True, "action": "not_found"}

        # Update crypto invoice
        inv_rec.status = btcpay_status
        if our_status == "paid":
            inv_rec.settled_at = datetime.now(timezone.utc)

        # Update parent order
        order_result = await db.execute(select(Order).where(Order.id == inv_rec.order_id))
        order = order_result.scalar_one_or_none()

        should_create_shopify = False
        if order and order.payment_status == PaymentStatus.pending:
            order.payment_status = PaymentStatus(our_status)
            if our_status == "paid":
                order.paid_at = datetime.now(timezone.utc)
                order.payment_notes = f"BTCPay invoice {btcpay_id} settled."
                logger.info(f"✅ Crypto payment confirmed: order {order.id}")
                should_create_shopify = True
            elif our_status == "expired":
                order.payment_notes = f"BTCPay invoice {btcpay_id} expired."
            elif our_status == "failed":
                order.payment_notes = f"BTCPay invoice {btcpay_id} invalid/failed."

        await db.commit()

    # Shopify order creation happens outside the DB transaction so a Shopify
    # failure doesn't roll back our own "paid" status.
    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == inv_rec.order_id)
                .options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                try:
                    from services.shopify import create_shopify_order
                    shopify_order = await create_shopify_order(order)
                    if shopify_order:
                        logger.info(
                            f"✅ Shopify order #{shopify_order.get('order_number')} "
                            f"auto-created for {order.id} (crypto)"
                        )
                        await _send_affiliate_webhook(order)
                    else:
                        logger.error(f"Shopify auto-create returned None for {order.id}")
                except Exception as e:
                    logger.exception(f"Shopify auto-create failed for {order.id}: {e}")

    return {"received": True, "action": our_status}


# ─── POST /webhooks/shopify-paid ──────────────────────────────────────────────

@router.post("/shopify-paid")
async def shopify_paid_webhook(request: Request):
    """
    Fired by Shopify when an order is paid on any of the bridge stores (MPC,
    FRT Chek, ONEPEPSCHECK, TWE Chek). Matches the Shopify order back to our
    DB record by the ORD-XXXXXXXX reference the bridge adds to note_attributes,
    and marks it paid.

    If no match is found (e.g. order placed directly on Shopify, not through
    our custom checkout), the webhook is silently ignored — the Revenue tab's
    live Shopify fetch still shows it.
    """
    raw_body      = await request.body()
    shopify_hmac  = request.headers.get("X-Shopify-Hmac-Sha256", "")
    shop_domain   = request.headers.get("X-Shopify-Shop-Domain", "")

    if not _verify_shopify_hmac(raw_body, shopify_hmac, shop_domain):
        logger.warning(f"Shopify webhook: invalid HMAC from {shop_domain}")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        order_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    shopify_order_id = str(order_data.get("id", ""))
    shopify_name     = order_data.get("name", "")      # e.g. "#1099FT"
    email            = (order_data.get("email") or "").lower()
    total_price      = order_data.get("total_price", "0")

    logger.info(
        f"Shopify webhook: paid order {shopify_name} ({shopify_order_id}) "
        f"from {shop_domain} — {email} / ${total_price}"
    )

    # Look for our ref in note_attributes (the bridge worker sets it as
    # `_src` with a value like "peptideslab.ca | ref:ORD-K3M9P2QA").
    bridge_order_id = None
    for attr in (order_data.get("note_attributes") or []):
        if attr.get("name") == "_src":
            m = re.search(r"ref:(ORD-[A-Z0-9]+)", attr.get("value", ""))
            if m:
                bridge_order_id = m.group(1)
                break

    async with AsyncSessionLocal() as db:
        if bridge_order_id:
            # Exact match by order ID — most reliable
            stmt = select(Order).where(Order.id == bridge_order_id)
        else:
            # Fallback: match by email + total + pending + card
            stmt = (
                select(Order)
                .where(
                    Order.email == email,
                    Order.total == Decimal(str(total_price)),
                    Order.payment_status == PaymentStatus.pending,
                    Order.payment_method == PaymentMethod.card,
                )
                .order_by(Order.created_at.desc())
                .limit(1)
            )
        result = await db.execute(stmt)
        order = result.scalar_one_or_none()

        if not order:
            logger.info(
                f"No matching DB order for {shopify_name} "
                f"(ref:{bridge_order_id or 'none'}) "
                f"— likely placed directly on Shopify, not via custom checkout"
            )
            return {"received": True, "action": "no_match"}

        if order.payment_status == PaymentStatus.paid:
            logger.info(f"Order {order.id} already marked paid — webhook replay, ignoring")
            return {"received": True, "action": "already_paid"}

        order.payment_status = PaymentStatus.paid
        order.paid_at        = datetime.now(timezone.utc)
        order.payment_ref    = shopify_order_id
        order.payment_notes  = f"Matched to Shopify order {shopify_name} on {shop_domain}"
        await db.commit()

        logger.info(f"✅ Order {order.id} matched and marked paid")
        return {"received": True, "action": "matched", "orderId": order.id}


def _verify_shopify_hmac(body: bytes, hmac_header: str, shop_domain: str) -> bool:
    """
    Each Shopify store has its own webhook signing secret, shown on each
    store's Shopify admin → Settings → Notifications → Webhooks page.

    Configure stores in .env using paired vars with any prefix:
        MPC_CHECKOUT_SHOP=mpc-store.myshopify.com
        MPC_WEBHOOK_SECRET=<secret>

        US_CHECKOUT_SHOP=76vpwc-g9.myshopify.com
        US_WEBHOOK_SECRET=<secret>

        STORE_X_CHECKOUT_SHOP=...
        STORE_X_WEBHOOK_SECRET=...

    All pairs ending in `_CHECKOUT_SHOP` + `_WEBHOOK_SECRET` are auto-detected.
    Adding a new store = add 2 env vars and restart, no code change.
    """
    if not hmac_header:
        return False

    secrets_by_shop = {}
    # Walk all settings fields ending in _CHECKOUT_SHOP, find the matching _WEBHOOK_SECRET
    for attr in dir(settings):
        if not attr.endswith("_CHECKOUT_SHOP"):
            continue
        prefix = attr[: -len("_CHECKOUT_SHOP")]
        secret_attr = f"{prefix}_WEBHOOK_SECRET"
        shop   = getattr(settings, attr,        "")
        secret = getattr(settings, secret_attr, "")
        if shop and secret:
            secrets_by_shop[shop] = secret

    secret = secrets_by_shop.get(shop_domain, "")
    if not secret:
        logger.warning(f"No webhook secret configured for shop: {shop_domain}")
        return False

    expected = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(expected, hmac_header)


@router.post("/nowpayments")
async def nowpayments_ipn(
    request: Request,
    x_nowpayments_sig: str = Header(None, alias="x-nowpayments-sig"),
):
    raw_body = await request.body()

    if not verify_nowpayments_ipn(raw_body, x_nowpayments_sig or ""):
        logger.warning("NowPayments IPN: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid IPN signature.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    np_payment_id = str(data.get("payment_id", ""))
    np_status     = data.get("payment_status", "")
    order_id      = data.get("order_id", "")
    price_amount  = Decimal(str(data.get("price_amount", "0")))
    actually_paid = Decimal(str(data.get("actually_paid_amount", data.get("actually_paid", "0"))))
    pay_currency  = data.get("pay_currency", "")

    logger.info(f"NowPayments IPN: payment {np_payment_id} | status={np_status} | order={order_id} | paid={actually_paid} {pay_currency}")

    our_status = NOWPAYMENTS_STATUS_MAP.get(np_status)
    if not our_status or our_status == "pending":
        async with AsyncSessionLocal() as db:
            inv_result = await db.execute(select(NowPaymentsInvoice).where(NowPaymentsInvoice.order_id == order_id))
            inv_rec = inv_result.scalar_one_or_none()
            if inv_rec:
                inv_rec.np_payment_id = np_payment_id
                inv_rec.status        = np_status
                inv_rec.coin          = pay_currency
                await db.commit()
        return {"received": True, "action": "none"}

    # Underpayment — trust NowPayments' own status. They emit "partially_paid"
    # when the customer sent less than the invoice required (within their
    # tolerance settings). DO NOT compare actually_paid (crypto) to
    # price_amount (fiat) — different units, comparison is meaningless.
    if np_status == "partially_paid":
        pay_amount = Decimal(str(data.get("pay_amount", "0")))
        underpay_pct = 0.0
        if pay_amount > 0 and actually_paid > 0:
            underpay_pct = float((pay_amount - actually_paid) / pay_amount * 100)
        logger.warning(
            f"⚠️  NowPayments order {order_id} partially paid "
            f"({actually_paid}/{pay_amount} {pay_currency}, ~{underpay_pct:.2f}% short)"
        )
        # Approximate fiat equivalent for the admin "Amount received" column
        received_fiat_approx = Decimal("0")
        if pay_amount > 0:
            received_fiat_approx = (price_amount * actually_paid / pay_amount).quantize(Decimal("0.01"))
        async with AsyncSessionLocal() as db:
            inv_result = await db.execute(select(NowPaymentsInvoice).where(NowPaymentsInvoice.order_id == order_id))
            inv_rec = inv_result.scalar_one_or_none()
            if inv_rec:
                inv_rec.np_payment_id = np_payment_id
                inv_rec.received_fiat = received_fiat_approx
                inv_rec.status        = "underpaid"
                inv_rec.coin          = pay_currency
            await db.commit()
        return {"received": True, "action": "underpaid", "underpay_pct": round(underpay_pct, 2)}

    async with AsyncSessionLocal() as db:
        inv_result = await db.execute(select(NowPaymentsInvoice).where(NowPaymentsInvoice.order_id == order_id))
        inv_rec = inv_result.scalar_one_or_none()

        if not inv_rec:
            logger.warning(f"NowPayments IPN: no invoice found for order {order_id}")
            return {"received": True, "action": "not_found"}

        inv_rec.np_payment_id = np_payment_id
        inv_rec.status        = np_status
        inv_rec.coin          = pay_currency
        if our_status == "paid":
            inv_rec.settled_at = datetime.now(timezone.utc)

        order_result = await db.execute(select(Order).where(Order.id == order_id))
        order = order_result.scalar_one_or_none()

        should_create_shopify = False
        if order and order.payment_status == PaymentStatus.pending:
            order.payment_status = PaymentStatus(our_status)
            if our_status == "paid":
                order.paid_at       = datetime.now(timezone.utc)
                order.payment_notes = f"NowPayments {np_payment_id} finished ({pay_currency})."
                logger.info(f"✅ Altcoin payment confirmed: order {order.id}")
                should_create_shopify = True
            elif our_status == "expired":
                order.payment_notes = f"NowPayments {np_payment_id} expired."
            elif our_status == "failed":
                order.payment_notes = f"NowPayments {np_payment_id} failed."
        await db.commit()

    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(select(Order).where(Order.id == order_id).options(selectinload(Order.items)))
            order = result.scalar_one_or_none()
            if order:
                try:
                    from services.shopify import create_shopify_order
                    shopify_order = await create_shopify_order(order)
                    if shopify_order:
                        logger.info(f"✅ Shopify order #{shopify_order.get('order_number')} auto-created for {order.id} (altcoin)")
                        await _send_affiliate_webhook(order)
                    else:
                        logger.error(f"Shopify auto-create returned None for {order.id}")
                except Exception as e:
                    logger.exception(f"Shopify auto-create failed for {order.id}: {e}")

    return {"received": True, "action": our_status}

# ─── POST /webhooks/pymtz ─────────────────────────────────────────────────────
# Based on https://pymtz.co/api-guide.html (the simpler page). pymtz's two doc
# pages disagree on the event name — api-guide.html shows "payment.succeeded",
# api-docs.html shows "payment.completed". We accept BOTH so we're not at the
# mercy of which one pymtz's production actually fires.
#
# FORGERY PROTECTION
# pymtz documents webhook signing but never tells us how to get the signing
# secret, so we can't verify HMAC signatures. Instead, before marking ANY
# order paid based on a webhook, we call pymtz's authenticated GET
# /payments/{payment_id} endpoint using our API key. The forger doesn't have
# our API key, so they can't fake pymtz's response — and pymtz will only
# report status="completed" for payments that were actually paid. A forged
# webhook gets rejected because pymtz itself says the payment isn't complete.
#
# What this still gives up vs api-docs.html coverage:
#   • No failure/refund handler — declines stay `pending` until expiry;
#     refunds get flipped manually in /peps-admin-2026.

@router.post("/pymtz")
async def pymtz_webhook(request: Request):
    """
    pymtz webhook handler — docs-literal per api-guide.html, plus an
    authenticated cross-verify against pymtz to defeat webhook forgery.

    Documented payload (their Node example):
        { "event": "payment.succeeded", "data": { ... } }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    # Webhook events may use the "event" key (api-guide.html style) or the
    # "type" key (api-docs.html style). Read both.
    event = body.get("event") or body.get("type") or ""
    data  = body.get("data", {}) or {}

    # Accept both event-name styles. Anything else → log + ack.
    if event not in ("payment.succeeded", "payment.completed"):
        logger.info(f"pymtz webhook: ignoring event {event!r}")
        return {"received": True}

    payment_id = str(data.get("payment_id") or data.get("id") or "")
    metadata   = data.get("metadata", {}) or {}
    order_id   = metadata.get("order_id", "")

    # Recover order via payment_ref if metadata didn't carry order_id.
    if not order_id and payment_id:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Order).where(Order.payment_ref == payment_id))
            match = res.scalar_one_or_none()
            if match:
                order_id = match.id

    logger.info(
        f"pymtz webhook: event={event} payment={payment_id} order={order_id}"
    )

    if not order_id:
        logger.warning(f"pymtz webhook: could not resolve order for payment {payment_id}")
        return {"received": True}

    # ── Pre-read order for currency + idempotency ────────────────────────────
    # We do this BEFORE the cross-verify HTTP call so we (a) can skip pymtz
    # entirely if the order is already paid, and (b) know which pymtz account
    # (CA vs US) to authenticate against.
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order_pre = result.scalar_one_or_none()
    if not order_pre:
        logger.warning(f"pymtz webhook: no order found for {order_id}")
        return {"received": True}
    if order_pre.payment_status == PaymentStatus.paid:
        return {"received": True}   # already paid — skip

    # ── Forgery protection #1: payment_id must belong to THIS order ───────────
    # When /api/checkout/card creates a pymtz payment, we stash the returned
    # pymtz payment ID on order.payment_ref. A legitimate webhook for this
    # order must therefore have payment_id == order.payment_ref. This blocks
    # the "use someone else's paid payment_id to mark other orders paid"
    # attack — the forger would need the exact pymtz payment ID that pymtz
    # created for THIS order, which they could only know if they were the
    # one who initiated checkout for it.
    if not payment_id:
        logger.warning(f"pymtz webhook: REJECTED — no payment_id for order {order_id}")
        return {"received": True}
    if not order_pre.payment_ref:
        logger.warning(
            f"pymtz webhook: REJECTED — order {order_id} has no payment_ref "
            f"(no pymtz payment ever created for it?)"
        )
        return {"received": True}
    if order_pre.payment_ref != payment_id:
        logger.warning(
            f"pymtz webhook: REJECTED — payment_id mismatch (webhook payment_id="
            f"{payment_id}, order.payment_ref={order_pre.payment_ref!r}). Order "
            f"{order_id} likely targeted by forgery using another order's payment ID."
        )
        return {"received": True}

    # ── Forgery protection #2: cross-verify with pymtz API ────────────────────
    # Confirms the payment actually completed by asking pymtz directly using
    # our authenticated API key. Catches the (now narrow) cases where the
    # webhook is forged for a payment_id that genuinely belongs to this order
    # but hasn't actually completed yet.
    try:
        pymtz_country = "US" if (order_pre.currency or "").upper() == "USD" else "CA"
        from services.pymtz import PymtzClient
        pymtz_payment = await PymtzClient(country=pymtz_country).get_payment(payment_id)
    except Exception as e:
        logger.warning(
            f"pymtz webhook: cross-verify call failed for payment={payment_id} "
            f"order={order_id}: {e}"
        )
        return {"received": True}

    pymtz_status = str(pymtz_payment.get("status") or "").lower()
    if pymtz_status not in ("completed", "succeeded", "paid"):
        logger.warning(
            f"pymtz webhook: REJECTED — pymtz reports status={pymtz_status!r} for "
            f"payment={payment_id} order={order_id}. Likely forged or stale."
        )
        return {"received": True}

    # ── Mark paid (idempotent — second guard in case of race) ────────────────
    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            return {"received": True}

        if order.payment_status == PaymentStatus.paid:
            return {"received": True}   # raced with another path — skip

        if order.payment_status == PaymentStatus.pending:
            order.payment_status = PaymentStatus.paid
            order.paid_at        = datetime.now(timezone.utc)
            order.payment_ref    = payment_id or order.payment_ref
            order.payment_notes  = f"pymtz {payment_id} succeeded (cross-verified)."
            should_create_shopify = True
            logger.info(f"✅ Card payment confirmed (pymtz): order {order.id}")
        await db.commit()

    # ── Downstream — Shopify create + affiliate webhook ──────────────────────
    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                try:
                    from services.shopify import create_shopify_order
                    shopify_order = await create_shopify_order(order)
                    if shopify_order:
                        logger.info(f"✅ Shopify order #{shopify_order.get('order_number')} auto-created for {order.id} (pymtz)")
                        await _send_affiliate_webhook(order)
                    else:
                        logger.error(f"Shopify auto-create returned None for {order.id} (pymtz)")
                except Exception as e:
                    logger.exception(f"Shopify auto-create failed for {order.id} (pymtz): {e}")

    return {"received": True}

# ─── POST /webhooks/whop ──────────────────────────────────────────────────────
# Whop fires this when an embedded-checkout payment completes/fails/refunds.
# We match back to our order via metadata.order_id (passed when creating the
# Whop checkout configuration), mark it paid, auto-create the Shopify order,
# and send the affiliate webhook — same post-payment pipeline as pymtz/btcpay.
#
# Two signature formats supported:
#   1. Standard Webhooks spec (Whop's current format)
#        Headers: webhook-id, webhook-timestamp, webhook-signature ("v1,<b64>")
#        Signed: "{id}.{timestamp}.{body}"  HMAC-SHA256 base64-decoded secret
#   2. Legacy "x-whop-signature: sha256=<hex>" (older Lasso-routed setup)
#
# Secret is read from WHOP_WEBHOOK_SECRET first, then falls back to
# WHOP_SANDBOX_WEBHOOK_SECRET, then legacy LASSO_WHOP_SECRET.

def _decode_whop_secret_candidates(secret: str) -> list[bytes]:
    """
    Whop uses `ws_<hex>` for sandbox/prod webhook signing secrets, while
    Standard Webhooks spec assumes `whsec_<base64>`. We don't know which
    format a given account is on, so we try every plausible decoding and
    return all candidates. Verification will try each until one matches.
    """
    candidates: list[bytes] = []

    # Strip known prefixes (try both stripped and full versions)
    stripped = secret
    for prefix in ("whsec_", "ws_"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break

    # 1) Hex decode (Whop's actual format — ws_<64hex>)
    try:
        candidates.append(bytes.fromhex(stripped))
    except ValueError:
        pass

    # 2) Standard base64 decode (Standard Webhooks spec for whsec_<base64>)
    try:
        # Pad if needed
        padded = stripped + "=" * (-len(stripped) % 4)
        candidates.append(base64.b64decode(padded))
    except Exception:
        pass

    # 3) URL-safe base64 decode
    try:
        padded = stripped + "=" * (-len(stripped) % 4)
        candidates.append(base64.urlsafe_b64decode(padded))
    except Exception:
        pass

    # 4) Raw bytes (whole secret as UTF-8, including prefix)
    candidates.append(secret.encode())

    # 5) Stripped raw bytes (UTF-8, without prefix)
    candidates.append(stripped.encode())

    # De-dup while preserving order
    seen: set = set()
    unique: list[bytes] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _verify_whop_signature_standard(
    raw_body: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    secret: str,
) -> bool:
    """
    Standard Webhooks spec verification.
        signed_content = "{id}.{timestamp}.{body}"
        expected = base64( HMAC-SHA256(key, signed_content) )
        header = "v1,<expected>" (may include multiple comma/space-separated entries)

    Tries multiple ways to decode the secret since Whop's format differs
    from Standard Webhooks reference (ws_<hex> vs whsec_<base64>).
    """
    if not (secret and webhook_id and webhook_timestamp and webhook_signature):
        return False
    try:
        signed_content = f"{webhook_id}.{webhook_timestamp}.{raw_body.decode('utf-8', errors='replace')}"

        # Parse incoming signature entries once.
        # Header format per Standard Webhooks spec: "v1,<sig> v1a,<sig> ..."
        # Entries are space-separated; within an entry, version and sig are
        # comma-separated. DO NOT split on commas in the top-level split —
        # that would destroy the version/sig pairing.
        incoming_v1: list[str] = []
        for entry in re.split(r"\s+", webhook_signature.strip()):
            if "," not in entry:
                continue
            version, sig = entry.split(",", 1)
            if version.strip() == "v1":
                incoming_v1.append(sig.strip())

        if not incoming_v1:
            return False

        # Try every candidate key derived from the secret
        for key in _decode_whop_secret_candidates(secret):
            try:
                expected = base64.b64encode(
                    hmac.new(key, signed_content.encode(), hashlib.sha256).digest()
                ).decode()
            except Exception:
                continue
            for sig in incoming_v1:
                if hmac.compare_digest(expected, sig):
                    return True
        return False
    except Exception:
        logger.exception("[Whop] Standard-webhooks signature verification raised")
        return False


def _whop_fill_customer_from_payload(order, payload: dict, top_level: dict) -> None:
    """
    Whop's webhook payload typically includes customer/user info. If our
    order has empty email/name/address fields (because the customer skipped
    our form and typed only inside the iframe), pull what we can from the
    webhook payload so Shopify create + Resend email still work.

    We check multiple likely paths since Whop's payload shape has rotated
    across API versions:
        data.user.email / data.user.username
        data.customer.email / data.customer.first_name / last_name
        data.email (root)
        data.billing_address.{name, address1, city, country, ...}
    Only fills fields that are currently empty — never overwrites existing.
    """
    if not isinstance(payload, dict):
        return

    sources: list[dict] = []
    for key in ("user", "customer", "buyer", "member"):
        s = payload.get(key)
        if isinstance(s, dict):
            sources.append(s)
    # Sometimes top-level has user/customer
    for key in ("user", "customer"):
        s = top_level.get(key) if isinstance(top_level, dict) else None
        if isinstance(s, dict):
            sources.append(s)
    sources.append(payload)
    if isinstance(top_level, dict):
        sources.append(top_level)

    def first_truthy(keys: tuple[str, ...]) -> str:
        for src in sources:
            for k in keys:
                v = src.get(k) if isinstance(src, dict) else None
                if v and isinstance(v, str):
                    return v.strip()
        return ""

    # Email — most important
    if not (getattr(order, "email", None) or "").strip():
        email = first_truthy(("email", "user_email", "customer_email"))
        if email:
            order.email = email
            logger.info(f"[Whop] Backfilled order {order.id} email from webhook: {email}")

    # Name — Shopify requires last_name at minimum
    if not (getattr(order, "first_name", None) or "").strip():
        first = first_truthy(("first_name", "firstName", "given_name"))
        if first:
            order.first_name = first
    if not (getattr(order, "last_name", None) or "").strip():
        last = first_truthy(("last_name", "lastName", "family_name", "surname"))
        if not last:
            # Whop sometimes only has "name" or "username"; split on first space
            full = first_truthy(("name", "full_name", "username", "display_name"))
            if full and " " in full:
                first, last = full.split(" ", 1)
                if not (getattr(order, "first_name", None) or "").strip():
                    order.first_name = first.strip()
            elif full:
                last = full  # username as last name fallback
        if last:
            order.last_name = last
            logger.info(f"[Whop] Backfilled order {order.id} last_name from webhook: {last}")

    # Billing address — if completely empty
    ba_sources = []
    for src in sources:
        for key in ("billing_address", "billingAddress", "address"):
            v = src.get(key) if isinstance(src, dict) else None
            if isinstance(v, dict):
                ba_sources.append(v)

    def addr_field(keys: tuple[str, ...]) -> str:
        for src in ba_sources:
            for k in keys:
                v = src.get(k)
                if v and isinstance(v, str):
                    return v.strip()
        return ""

    if ba_sources:
        if not (getattr(order, "address1", None) or "").strip():
            v = addr_field(("line1", "address1", "street1", "street"))
            if v: order.address1 = v
        if not (getattr(order, "city", None) or "").strip():
            v = addr_field(("city", "locality"))
            if v: order.city = v
        if not (getattr(order, "province", None) or "").strip():
            v = addr_field(("state", "province", "region"))
            if v: order.province = v
        if not (getattr(order, "postal_code", None) or "").strip():
            v = addr_field(("postal_code", "postalCode", "zip", "postcode"))
            if v: order.postal_code = v
        if not (getattr(order, "country", None) or "").strip() or order.country == "CA":
            # Replace only if currently empty or default 'CA' came from the
            # form default and we have something better
            v = addr_field(("country", "country_code"))
            if v: order.country = v


# ──────────────────────────────────────────────────────────────────────────────
# Onramp via WordPress + 2530gateway plugin
# ──────────────────────────────────────────────────────────────────────────────
# Highriskify callback (direct API path)
# Per docs §7 — this is a server-to-server GET. Highriskify hits our
# callback URL with query params (number, value_coin, coin, txid_in,
# txid_out, address_in) when a payment completes and USDC has landed
# at our merchant wallet.
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/highriskify")
async def highriskify_callback(
    request: Request,
    number:     str = "",
    value_coin: str = "0",
    coin:       str = "USDC",
    txid_in:    str = "",
    txid_out:   str = "",
    address_in: str = "",
):
    """
    Authoritative payment confirmation from Highriskify. Mark the order paid,
    record the txids, fire downstream Shopify create + affiliate webhook.

    Per docs §7.4–7.5:
      - Locate order by `number` (our order_id)
      - Idempotency required — repeated callbacks must be safe
      - Return HTTP 200 OK on success (any non-200 = Highriskify may retry)
    """
    order_id = (number or "").strip()
    logger.info(
        f"highriskify callback: order={order_id} value={value_coin} {coin} "
        f"txid_in={txid_in[:14]}... txid_out={txid_out[:14]}... temp={address_in[:14]}..."
    )

    if not order_id:
        logger.warning("highriskify callback: missing number param")
        return {"received": True, "action": "no_order"}

    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            logger.warning(f"highriskify callback: no order found for {order_id}")
            return {"received": True, "action": "not_found"}

        # Idempotency — if already paid, just return 200 silently.
        if order.payment_status == PaymentStatus.paid:
            return {"received": True, "action": "already_paid"}

        # Mark paid + record txids in payment_notes for reconciliation
        order.payment_status = PaymentStatus.paid
        order.paid_at        = datetime.now(timezone.utc)
        order.payment_ref    = f"hr:{address_in or order.payment_ref or ''}"
        order.payment_notes  = (
            f"highriskify paid · value_coin={value_coin} {coin} · "
            f"txid_in={txid_in} · txid_out={txid_out} · temp={address_in}"
        )[:1000]
        await db.commit()
        should_create_shopify = True
        logger.info(f"✅ Card payment confirmed (highriskify): order {order.id}")

    # Downstream — Shopify create + affiliate ping. Same as pymtz/btcpay path.
    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                try:
                    from services.shopify import create_shopify_order
                    await create_shopify_order(order)
                except Exception as e:
                    logger.error(f"Shopify create failed for {order_id} (highriskify): {e}")
                try:
                    await _send_affiliate_webhook(order)
                except Exception as e:
                    logger.error(f"Affiliate webhook failed for {order_id} (highriskify): {e}")

                # Fire IPT payment_confirmed event (non-blocking)
                try:
                    from services.highriskify import HighriskifyClient
                    hr = HighriskifyClient()
                    await hr.ipt_track({
                        "event_type":       "payment_confirmed",
                        "platform":         "custom-api",
                        "order_id":         order_id,
                        "temp_wallet":      address_in,
                        "network":          "polygon",
                        "token_symbol":     coin or "USDC",
                        "amount_paid":      float(value_coin or 0),
                        "amount_paid_usdc": float(value_coin or 0),
                        "txid_in":          txid_in,
                        "txid_out":         txid_out,
                        "merchant_wallet":  hr.merchant_wallet,
                        "confirmed_at":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception as e:
                    logger.warning(f"highriskify IPT confirm failed: {e}")

    return {"received": True, "action": "paid", "order_id": order_id}


# ──────────────────────────────────────────────────────────────────────────────
# Authorize.net webhook handler
#
# Most events for our flow fire AFTER the synchronous /api/checkout/authnet
# charge already marked the order paid — so the webhook is primarily a
# secondary safety net (in case the synchronous response was lost) and a
# notification channel for after-the-fact events: refunds issued via the
# Auth.net dashboard, fraud holds released/declined, etc.
#
# Auth.net events we care about:
#   net.authorize.payment.authcapture.created  → charge completed
#   net.authorize.payment.refund.created       → refund issued
#   net.authorize.payment.void.created         → void issued
#   net.authorize.payment.fraud.held           → held by AFDS
#   net.authorize.payment.fraud.approved       → admin released the hold
#   net.authorize.payment.fraud.declined       → admin rejected the hold
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/authnet")
async def authnet_webhook(request: Request):
    """
    Verify HMAC-SHA512 signature, decode the event, and act on it.
    Idempotent — repeated webhooks for the same event are no-ops.
    """
    from services.authnet import AuthnetClient

    raw_body = await request.body()
    signature = request.headers.get("X-ANET-Signature", "")

    client = AuthnetClient()
    if not client.verify_webhook_signature(raw_body, signature):
        logger.warning("[authnet webhook] invalid signature — rejecting")
        raise HTTPException(401, "Invalid signature")

    try:
        event = json.loads(raw_body)
    except Exception:
        logger.error(f"[authnet webhook] invalid JSON: {raw_body[:200]!r}")
        raise HTTPException(400, "Invalid JSON")

    event_type = event.get("eventType", "")
    payload    = event.get("payload", {}) or {}
    auth_trans_id = str(payload.get("id", "") or "")

    logger.info(f"[authnet webhook] event={event_type} trans={auth_trans_id}")

    # Find the order by stored payment_ref (we stored as "an:<trans_id>")
    payment_ref = f"an:{auth_trans_id}"
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.payment_ref == payment_ref))
        order = result.scalar_one_or_none()

        if not order:
            # Not necessarily an error — Auth.net can fire test events or
            # events for transactions we don't own (if our merchant ID is
            # somehow shared). Just log and return 200.
            logger.info(f"[authnet webhook] no order found for trans={auth_trans_id} (event={event_type})")
            return {"received": True, "action": "no_order"}

        # ── Handle each event type ───────────────────────────────────────────
        if event_type == "net.authorize.payment.authcapture.created":
            # Charge already marked paid by the synchronous endpoint. This is
            # belt-and-suspenders for the case where the sync response got lost
            # between server and customer.
            if order.payment_status != PaymentStatus.paid:
                order.payment_status = PaymentStatus.paid
                order.paid_at        = datetime.now(timezone.utc)
                order.payment_notes  = (
                    (order.payment_notes or "") +
                    f" | [webhook] authcapture.created confirmed at {datetime.now(timezone.utc).isoformat()}"
                )[:1000]
                await db.commit()
                logger.info(f"✅ [authnet webhook] order {order.id} marked paid (sync had failed)")
            else:
                logger.info(f"[authnet webhook] order {order.id} already paid — webhook is duplicate")

        elif event_type == "net.authorize.payment.refund.created":
            # Full or partial refund issued via the Auth.net dashboard. Mark
            # status accordingly so the admin dashboard reflects it.
            refund_amount = payload.get("authAmount") or payload.get("settleAmount") or 0
            order.payment_status = PaymentStatus.refunded
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] refund.created amount={refund_amount} at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[authnet webhook] order {order.id} marked refunded ({refund_amount})")

        elif event_type == "net.authorize.payment.void.created":
            # Pre-settlement void — reverse the charge entirely.
            order.payment_status = PaymentStatus.refunded   # we don't have a 'voided' status
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] void.created at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[authnet webhook] order {order.id} marked refunded (void)")

        elif event_type == "net.authorize.payment.fraud.held":
            # AFDS held the transaction for manual review. Customer's card is
            # authorized but funds aren't captured yet. Mark pending.
            order.payment_status = PaymentStatus.pending
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] fraud.held at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[authnet webhook] order {order.id} held for fraud review")

        elif event_type == "net.authorize.payment.fraud.approved":
            # Admin released the hold — charge proceeds.
            order.payment_status = PaymentStatus.paid
            order.paid_at        = datetime.now(timezone.utc)
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] fraud.approved at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[authnet webhook] order {order.id} fraud hold released → paid")

        elif event_type == "net.authorize.payment.fraud.declined":
            # Admin rejected the hold — charge voided.
            order.payment_status = PaymentStatus.failed
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] fraud.declined at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[authnet webhook] order {order.id} fraud hold rejected → failed")

        else:
            logger.info(f"[authnet webhook] unhandled event type: {event_type}")

    return {"received": True, "action": event_type, "order_id": order.id if order else None}


# ──────────────────────────────────────────────────────────────────────────────
# Stripe direct webhook handler
#
# Like Auth.net, most events fire AFTER the synchronous /api/checkout/stripe_direct
# call already marked the order paid. The webhook serves as:
#   1. Secondary safety net if the synchronous response was lost
#   2. Notification for after-the-fact events (refunds from dashboard, etc.)
#
# Stripe events we care about:
#   payment_intent.succeeded             → charge completed
#   payment_intent.payment_failed        → charge failed
#   payment_intent.canceled              → charge canceled
#   charge.refunded                      → refund issued
#   charge.dispute.created               → chargeback opened
# ──────────────────────────────────────────────────────────────────────────────

@router.post("/stripe_direct")
async def stripe_direct_webhook(request: Request):
    """
    Verify Stripe webhook signature (HMAC-SHA256 with timestamp + raw body),
    decode the event, and act on it. Idempotent.
    """
    from services.stripe_direct import StripeDirectClient

    raw_body = await request.body()
    signature = request.headers.get("Stripe-Signature", "")

    client = StripeDirectClient()
    if not client.verify_webhook(raw_body, signature):
        logger.warning("[stripe_direct webhook] invalid signature — rejecting")
        raise HTTPException(401, "Invalid signature")

    try:
        event = json.loads(raw_body)
    except Exception:
        logger.error(f"[stripe_direct webhook] invalid JSON: {raw_body[:200]!r}")
        raise HTTPException(400, "Invalid JSON")

    event_type = event.get("type", "")
    payload    = event.get("data", {}).get("object", {}) or {}
    pi_id      = payload.get("id") or payload.get("payment_intent", "")

    logger.info(f"[stripe_direct webhook] event={event_type} pi={pi_id}")

    # Find the order by stored payment_ref. We store as "pi_<intent_id>" so
    # the existing classifier can recognize it; match accordingly.
    if not pi_id:
        return {"received": True, "action": "no_pi"}

    async with AsyncSessionLocal() as db:
        # payment_ref stored as "pi_<the-id>" — Stripe's IDs already start
        # with "pi_" so this looks like e.g. "pi_pi_3xxxxx" depending on
        # how we wrote it. Match by suffix to be safe.
        suffix = pi_id.replace("pi_", "")
        result = await db.execute(
            select(Order).where(Order.payment_ref.ilike(f"%{suffix}%"))
        )
        order = result.scalar_one_or_none()

        if not order:
            logger.info(f"[stripe_direct webhook] no order found for pi={pi_id} (event={event_type})")
            return {"received": True, "action": "no_order"}

        # ── Handle each event type ───────────────────────────────────────────
        if event_type == "payment_intent.succeeded":
            if order.payment_status != PaymentStatus.paid:
                order.payment_status = PaymentStatus.paid
                order.paid_at        = datetime.now(timezone.utc)
                order.payment_notes  = (
                    (order.payment_notes or "") +
                    f" | [webhook] payment_intent.succeeded at {datetime.now(timezone.utc).isoformat()}"
                )[:1000]
                await db.commit()
                logger.info(f"✅ [stripe_direct webhook] order {order.id} marked paid (sync had failed)")
            else:
                logger.info(f"[stripe_direct webhook] order {order.id} already paid — webhook is duplicate")

        elif event_type == "payment_intent.payment_failed":
            if order.payment_status == PaymentStatus.pending:
                order.payment_status = PaymentStatus.failed
                err = payload.get("last_payment_error") or {}
                order.payment_notes  = (
                    (order.payment_notes or "") +
                    f" | [webhook] payment_intent.payment_failed: {err.get('message', 'unknown')[:200]}"
                )[:1000]
                await db.commit()
                logger.info(f"[stripe_direct webhook] order {order.id} marked failed")

        elif event_type == "payment_intent.canceled":
            order.payment_status = PaymentStatus.failed
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] payment_intent.canceled at {datetime.now(timezone.utc).isoformat()}"
            )[:1000]
            await db.commit()
            logger.info(f"[stripe_direct webhook] order {order.id} marked failed (canceled)")

        elif event_type == "charge.refunded":
            order.payment_status = PaymentStatus.refunded
            refund_amount = (payload.get("amount_refunded") or 0) / 100.0
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | [webhook] charge.refunded amount=${refund_amount:.2f}"
            )[:1000]
            await db.commit()
            logger.info(f"[stripe_direct webhook] order {order.id} marked refunded (${refund_amount:.2f})")

        elif event_type == "charge.dispute.created":
            # Chargeback — log but don't change status. The merchant team
            # needs to know but the order is still "paid" until the dispute resolves.
            dispute_amount = (payload.get("amount") or 0) / 100.0
            order.payment_notes  = (
                (order.payment_notes or "") +
                f" | ⚠️ [webhook] DISPUTE OPENED amount=${dispute_amount:.2f} reason={payload.get('reason', 'unknown')}"
            )[:1000]
            await db.commit()
            logger.warning(f"⚠️ [stripe_direct webhook] CHARGEBACK on order {order.id}: ${dispute_amount:.2f}")

        else:
            logger.info(f"[stripe_direct webhook] unhandled event type: {event_type}")

    return {"received": True, "action": event_type, "order_id": order.id if order else None}


# ──────────────────────────────────────────────────────────────────────────────
# Onramp via WordPress + 2530gateway plugin (legacy path, kept dormant)
# ──────────────────────────────────────────────────────────────────────────────

def _verify_wc_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """
    WooCommerce webhooks sign the raw body with HMAC-SHA256 + base64 encoded.
    Header: X-WC-Webhook-Signature: <base64-hmac-sha256-of-body>
    Secret: the value you set in WP admin → WooCommerce → Settings → Advanced
            → Webhooks → (your webhook) → Secret
    """
    if not secret or not signature:
        return False
    try:
        digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(expected, signature.strip())
    except Exception:
        return False


@router.post("/onramp_wp")
async def onramp_wp_webhook(
    request: Request,
    x_wc_webhook_signature: str = Header(None, alias="x-wc-webhook-signature"),
    x_wc_webhook_topic:     str = Header(None, alias="x-wc-webhook-topic"),
):
    """
    Receive WooCommerce webhook from the WP site running the 2530gateway plugin.
    Topics we care about: `order.updated`, `order.created`. We act on status
    transitions to "processing" or "completed" (both = paid in WC parlance).
    """
    raw_body = await request.body()

    secret = getattr(settings, "ONRAMP_WP_WEBHOOK_SECRET", "") or ""
    if secret:
        if not _verify_wc_signature(raw_body, x_wc_webhook_signature or "", secret):
            logger.warning("onramp_wp webhook: invalid signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    elif settings.ENVIRONMENT == "production":
        logger.warning("onramp_wp webhook: ONRAMP_WP_WEBHOOK_SECRET not set in production")
        raise HTTPException(status_code=401, detail="Webhook secret not configured.")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    wc_order_id = data.get("id", "")
    wc_status   = (data.get("status") or "").lower()

    # Pull our external order_id out of meta_data
    order_id = ""
    for meta in (data.get("meta_data") or []):
        if meta.get("key") in ("_external_order_id", "external_order_id"):
            order_id = str(meta.get("value") or "")
            break

    # Fallback — match by stored payment_ref `wc:<id>`
    if not order_id and wc_order_id:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(Order).where(Order.payment_ref == f"wc:{wc_order_id}")
            )
            ord_match = res.scalar_one_or_none()
            if ord_match:
                order_id = ord_match.id

    logger.info(
        f"onramp_wp webhook: topic={x_wc_webhook_topic} wc_order={wc_order_id} "
        f"wc_status={wc_status} order={order_id}"
    )

    if not order_id:
        logger.warning(f"onramp_wp webhook: could not resolve order for WC order {wc_order_id}")
        return {"received": True, "action": "no_order"}

    from services.onramp_wp import WC_STATUS_MAP
    our_status = WC_STATUS_MAP.get(wc_status)
    if not our_status or our_status == "pending":
        return {"received": True, "action": "none", "status": wc_status}

    should_create_shopify = False
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            logger.warning(f"onramp_wp webhook: no order found for {order_id}")
            return {"received": True, "action": "not_found"}

        if order.payment_status == PaymentStatus.pending:
            order.payment_status = PaymentStatus(our_status)
            order.payment_ref    = f"wc:{wc_order_id}"
            if our_status == "paid":
                order.paid_at       = datetime.now(timezone.utc)
                order.payment_notes = f"onramp_wp WC #{wc_order_id} {wc_status}."
                should_create_shopify = True
                logger.info(f"✅ Card payment confirmed (onramp_wp): order {order.id} / WC #{wc_order_id}")
            elif our_status == "failed":
                order.payment_notes = f"onramp_wp WC #{wc_order_id} failed."
            elif our_status == "cancelled":
                order.payment_notes = f"onramp_wp WC #{wc_order_id} cancelled."
            elif our_status == "refunded":
                order.payment_notes = f"onramp_wp WC #{wc_order_id} refunded."
        await db.commit()

    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id).options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                try:
                    from services.shopify import create_shopify_order
                    await create_shopify_order(order)
                except Exception as e:
                    logger.error(f"Shopify create failed for {order_id} (onramp_wp): {e}")
                try:
                    await _send_affiliate_webhook(order)
                except Exception as e:
                    logger.error(f"Affiliate webhook failed for {order_id} (onramp_wp): {e}")

    return {"received": True, "action": "processed", "order_id": order_id, "status": our_status}


# ──────────────────────────────────────────────────────────────────────────────


def _verify_whop_signature_legacy(raw_body: bytes, signature: str, secret: str) -> bool:
    """Legacy 'x-whop-signature: sha256=<hex>' format."""
    if not secret or not signature:
        return False
    try:
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        incoming = signature.replace("sha256=", "").strip()
        return hmac.compare_digest(expected, incoming)
    except Exception:
        return False


@router.post("/whop")
async def whop_webhook(
    request: Request,
    webhook_id:        str | None = Header(None, alias="webhook-id"),
    webhook_timestamp: str | None = Header(None, alias="webhook-timestamp"),
    webhook_signature: str | None = Header(None, alias="webhook-signature"),
    x_whop_signature:  str | None = Header(None, alias="x-whop-signature"),  # legacy
):
    raw_body = await request.body()

    # Collect every secret that's configured. Try each one until one matches.
    sandbox_secret = getattr(settings, "WHOP_SANDBOX_WEBHOOK_SECRET", "") or ""
    prod_secret    = getattr(settings, "WHOP_WEBHOOK_SECRET", "") or ""
    legacy_secret  = getattr(settings, "LASSO_WHOP_SECRET", "") or ""

    if bool(getattr(settings, "WHOP_SANDBOX", False)):
        candidate_secrets = [sandbox_secret, prod_secret, legacy_secret]
    else:
        candidate_secrets = [prod_secret, sandbox_secret, legacy_secret]
    candidate_secrets = [s for s in candidate_secrets if s]

    if candidate_secrets:
        ok = False
        for secret in candidate_secrets:
            if _verify_whop_signature_standard(
                raw_body,
                webhook_id or "",
                webhook_timestamp or "",
                webhook_signature or "",
                secret,
            ):
                ok = True
                break
            if x_whop_signature and _verify_whop_signature_legacy(raw_body, x_whop_signature, secret):
                ok = True
                break
        if not ok:
            # Debug logging — print enough detail to diagnose the mismatch
            # WITHOUT leaking the secret. Includes header presence, body length,
            # and the first 8 chars of each candidate's hex-encoded HMAC output
            # so we can compare to what Whop sent.
            debug_lines = [
                f"[Whop] Invalid webhook signature — rejecting. Tried {len(candidate_secrets)} secret(s).",
                f"[Whop debug] webhook-id present: {bool(webhook_id)}  webhook-timestamp present: {bool(webhook_timestamp)}",
                f"[Whop debug] webhook-signature header: {webhook_signature[:80] if webhook_signature else '(missing)'}",
                f"[Whop debug] x-whop-signature header: {x_whop_signature[:80] if x_whop_signature else '(missing)'}",
                f"[Whop debug] body length: {len(raw_body)}",
                f"[Whop debug] body start: {raw_body[:200].decode('utf-8', errors='replace')}",
            ]
            # For each candidate secret, compute what we expected. Helps
            # confirm whether our decoding produced the right key.
            for i, secret in enumerate(candidate_secrets):
                try:
                    sample_keys = _decode_whop_secret_candidates(secret)
                    expectations = []
                    signed_dbg = f"{webhook_id or ''}.{webhook_timestamp or ''}.{raw_body.decode('utf-8', errors='replace')}"
                    for j, k in enumerate(sample_keys[:3]):
                        try:
                            exp = base64.b64encode(
                                hmac.new(k, signed_dbg.encode(), hashlib.sha256).digest()
                            ).decode()
                            expectations.append(f"key{j}={exp[:16]}…")
                        except Exception:
                            pass
                    debug_lines.append(
                        f"[Whop debug] secret#{i} prefix={secret[:6]} expected: {' '.join(expectations)}"
                    )
                except Exception:
                    pass
            for line in debug_lines:
                logger.warning(line)
            raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    else:
        logger.warning("[Whop] No webhook secret configured — skipping signature check")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    # Whop event shape (Standard Webhooks):
    #   { "type": "payment.succeeded", "data": { "id": "pay_xxx", "status": "succeeded",
    #     "metadata": { "order_id": "ORD-..." }, ... } }
    # We accept several spellings ("type"/"action"/"event") for safety.
    action   = (data.get("type") or data.get("action") or data.get("event") or "").lower()
    payload  = data.get("data", data) or {}
    whop_id  = str(payload.get("id") or data.get("id") or "")
    status   = (payload.get("status") or "").lower()
    metadata = (payload.get("metadata") or data.get("metadata") or {}) or {}
    order_id = metadata.get("order_id", "")

    logger.info(f"[Whop] action={action} id={whop_id} status={status} order={order_id}")

    # Fallback: match by payment_ref (session_id stored on order creation)
    if not order_id and whop_id:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(Order).where(Order.payment_ref == whop_id)
            )
            matched = res.scalar_one_or_none()
            if matched:
                order_id = matched.id

    if not order_id:
        logger.warning(f"[Whop] Could not resolve order for whop_id={whop_id}")
        return {"received": True, "action": "no_order"}

    # Map Whop event/status → our PaymentStatus. Event "type" is the most
    # reliable signal (e.g. "payment.succeeded"); data.status is a bonus check.
    # Whop emits event names in both dot- and underscore-notation depending on
    # version, and uses both "payment.*" (intent-style) and "invoice.*"
    # (subscription/invoice-style) families. For one-time purchases via
    # checkout configurations, the most common event is invoice_paid (or
    # payment_succeeded on newer accounts). We accept all spellings.
    paid_events = {
        "payment.succeeded", "payment.completed", "payment.paid",
        "payment_succeeded", "payment_completed", "payment_paid",
        "invoice.paid", "invoice_paid",
        "invoice.payment_succeeded", "invoice_payment_succeeded",
    }
    failed_events = {
        "payment.failed", "payment_failed",
        "invoice.payment_failed", "invoice_payment_failed",
        "invoice.marked_uncollectible", "invoice_marked_uncollectible",
        "invoice.voided", "invoice_voided",
    }
    refund_events = {
        "payment.refunded", "payment_refunded",
        "invoice.refunded", "invoice_refunded",
    }
    informational_events = {
        "payment.created", "payment_created",
        "payment.pending", "payment_pending",
        "invoice.created", "invoice_created",
        "invoice.past_due", "invoice_past_due",
    }

    if action in paid_events or status in ("succeeded", "paid", "completed"):
        our_status = "paid"
    elif action in failed_events or status in ("failed", "uncollectible", "voided"):
        our_status = "failed"
    elif action in refund_events or status in ("refunded",):
        our_status = "refunded"
    elif action in informational_events:
        logger.info(f"[Whop] Informational event {action} for order {order_id} — ignoring")
        return {"received": True, "action": "ignored", "event": action}
    else:
        logger.info(f"[Whop] Unhandled event '{action}' (status='{status}') for order {order_id} — ignoring")
        return {"received": True, "action": "none"}

    should_create_shopify = False

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Order).where(Order.id == order_id))
        order  = result.scalar_one_or_none()
        if not order:
            logger.warning(f"[Whop] Order {order_id} not found in DB")
            return {"received": True, "action": "not_found"}

        if order.payment_status == PaymentStatus.pending:
            order.payment_status = PaymentStatus(our_status)
            order.payment_ref    = whop_id or order.payment_ref

            # Backfill customer info from the Whop webhook payload if the
            # customer skipped our form fields (typed only inside the iframe).
            # Whop's payload includes user/customer info that we can use to
            # avoid empty email/name — Shopify create fails on missing fields,
            # and our Resend confirmation can't address the customer.
            _whop_fill_customer_from_payload(order, payload, data)

            if our_status == "paid":
                order.paid_at       = datetime.now(timezone.utc)
                order.payment_notes = f"Whop payment {whop_id} completed."
                should_create_shopify = True
                logger.info(f"✅ Whop payment confirmed: order {order.id}")
            else:
                order.payment_notes = f"Whop payment {whop_id} {status}."
                logger.info(f"[Whop] Order {order.id} marked {our_status}")

        elif our_status == "refunded" and order.payment_status == PaymentStatus.paid:
            # Refunds arrive AFTER the order is already paid, so the `pending`
            # branch above never catches them. Without this, a refunded Whop
            # order would stay marked `paid` forever. Don't undo the Shopify
            # order/email here — just reflect the refund in our status.
            order.payment_status = PaymentStatus.refunded
            order.payment_notes  = (
                (order.payment_notes or "") + f" | Whop refund {whop_id}."
            )[:1000]
            logger.info(f"[Whop] Order {order.id} refunded")

        else:
            _cur = getattr(order.payment_status, "value", order.payment_status)
            logger.info(
                f"[Whop] Order {order.id} already "
                f"{_cur}; event '{action}' "
                f"(→{our_status}) ignored"
            )

        await db.commit()

    if should_create_shopify:
        async with AsyncSessionLocal() as db:
            from sqlalchemy.orm import selectinload
            result = await db.execute(
                select(Order).where(Order.id == order_id)
                .options(selectinload(Order.items))
            )
            order = result.scalar_one_or_none()
            if order:
                # Same post-payment pipeline used by /admin mark-paid for
                # Interac/Zelle/etc.: Shopify order → affiliate webhook →
                # Resend confirmation email. Identical flow so customer
                # experience is the same regardless of payment method.

                shopify_order_number = None
                try:
                    from services.shopify import create_shopify_order
                    shopify_order = await create_shopify_order(order)
                    if shopify_order:
                        shopify_order_number = str(shopify_order.get("order_number", ""))
                        logger.info(
                            f"✅ Shopify order #{shopify_order_number} "
                            f"auto-created for {order.id} (whop)"
                        )
                        await _send_affiliate_webhook(order)
                    else:
                        logger.error(f"Shopify auto-create returned None for {order.id} (whop)")
                except Exception as e:
                    logger.exception(f"Shopify auto-create failed for {order.id} (whop): {e}")

                # Customer confirmation email via Resend — only if order has
                # an email on file. Same pattern as routes/admin.py mark-paid.
                if order.email:
                    try:
                        from models.brand import Brand
                        brand_result = await db.execute(
                            select(Brand).where(Brand.id == order.brand_id)
                        )
                        brand = brand_result.scalar_one_or_none()
                        accent = brand.accent_color if brand and brand.accent_color else "#dd1d1d"
                        from services.email import send_confirmation_email
                        await send_confirmation_email(
                            order,
                            shopify_order_number=shopify_order_number,
                            accent=accent,
                        )
                        logger.info(f"✉️  Confirmation email sent for {order.id} (whop) → {order.email}")
                    except Exception as e:
                        logger.error(f"Confirmation email failed for {order.id} (whop): {e}")
                else:
                    logger.warning(
                        f"[Whop] Order {order.id} has no email — skipping confirmation email. "
                        f"Customer likely typed email only in the Whop iframe."
                    )

    return {"received": True, "action": our_status}
