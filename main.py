"""
Checkout Server — FastAPI entry point.

Brand middleware:
  Every request reads the Host header, looks up the matching Brand in DB,
  and attaches it to request.state.brand. All downstream routes use this
  to serve the correct store name, logo, colors, discounts, and API keys.

Static file serving:
  GET /              → serves checkout.html (brand-injected)
  GET /order/{id}/confirmation → order confirmation page
  GET /order/success → Stripe embedded checkout thank-you page
  GET /config        → returns brand config JSON for frontend bootstrapping
"""
import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, text

from database import engine, AsyncSessionLocal
from models.brand import Brand
from models import Order  # triggers all model registrations
from models.order import NowPaymentsInvoice
import models  # noqa — ensure all models are registered with Base
from routes.checkout import router as checkout_router
from routes.webhooks import router as webhooks_router
from routes.admin    import router as admin_router
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Jinja2 template engine ───────────────────────────────────────────────────
jinja_env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"]),
)


def _get_card_enabled_stores() -> list[str]:
    """
    Parse CARD_ENABLED_STORES env var into a clean lowercase list
    (no protocol, no trailing slash).
    """
    raw = getattr(settings, "CARD_ENABLED_STORES", "") or ""
    if not raw:
        return []
    return [
        s.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
        for s in raw.split(",")
        if s.strip()
    ]


# ─── Brand color overrides via query param ───────────────────────────────────
# A store can pass its own color in the redirect URL — e.g.
#   /?source=victoriapeps.ca&accent=%237c3aed
# This wins over the brand DB row, so a store doesn't need a DB update to
# theme its checkout. Hex codes only (#abc or #aabbcc) — anything else is
# rejected to prevent CSS injection.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _validate_hex_color(raw: str) -> str:
    """Return `raw` if it's a safe hex color, else empty string."""
    if not raw:
        return ""
    raw = raw.strip()
    if _HEX_COLOR_RE.match(raw):
        return raw
    return ""


def _darken_hex(hex_color: str, factor: float = 0.78) -> str:
    """Return `hex_color` darkened by (1-factor). Used to derive a hover shade
    when the store passes a primary color but no explicit hover."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _resolve_accent(request: Request, brand, default_color: str = "",
                    default_hover: str = "") -> tuple[str, str]:
    """Resolve (accent_color, accent_hover) for any page that wants brand colors.

    Priority:
      1. ?accent / ?accent_hover URL params (store-supplied)
      2. brand DB row
      3. Country-based default: red for CA, blue for US (legacy behavior)
      4. Hardcoded red

    Shared by the checkout page, confirmation page, and Stripe success page so
    a customer keeps the same color across the entire flow.
    """
    qp_accent = _validate_hex_color(request.query_params.get("accent", ""))
    qp_hover  = _validate_hex_color(request.query_params.get("accent_hover", ""))
    if qp_accent:
        return (qp_accent, qp_hover or _darken_hex(qp_accent))

    if brand and brand.accent_color:
        return (brand.accent_color, brand.accent_hover or _darken_hex(brand.accent_color))

    # Country-based legacy default. CA → red, US → blue.
    country = (request.query_params.get("country", "CA") or "CA").upper()
    if country == "US":
        return (default_color or "#2563eb", default_hover or "#1d4ed8")
    return (default_color or "#dd1d1d", default_hover or "#b01515")


# ─── V2 checkout routing ─────────────────────────────────────────────────────
# Stores listed in CHECKOUT_V2_STORES_FILE get served the new v2 template
# (templates/checkout-v2.html). File is read once and cached in-process.
#
# Line format:
#     domain.com           ← v2 store, country unspecified (defaults to CA)
#     domain.com:US        ← v2 store, force USD currency
#     domain.com:CA        ← v2 store, force CAD currency
#     # comment
#
# Comparing "is in the file" still works via membership in the dict's keys.
_V2_STORES: dict[str, str | None] | None = None  # {domain: "US" | "CA" | None}
_V2_MTIME: float = 0.0


def _normalize_domain(d: str) -> str:
    d = (d or "").strip().lower().replace("https://", "").replace("http://", "")
    d = d.lstrip("/").rstrip("/")
    if d.startswith("www."):
        d = d[4:]
    return d


def _load_v2_stores() -> dict[str, str | None]:
    """Read the v2 stores file. Cached, refreshed on mtime change.

    Returns a {domain: country} dict — `country` is "US", "CA", or None if
    the line had no `:CC` suffix.
    """
    global _V2_STORES, _V2_MTIME
    path = getattr(settings, "CHECKOUT_V2_STORES_FILE", "") or ""
    if not path:
        return {}
    try:
        import os
        mtime = os.path.getmtime(path)
    except OSError:
        _V2_STORES = {}
        return _V2_STORES
    if _V2_STORES is not None and mtime == _V2_MTIME:
        return _V2_STORES
    stores: dict[str, str | None] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Split on `:US` / `:CA` country suffix — but only the LAST
                # colon (so `https://...` doesn't get misparsed as country).
                country: str | None = None
                if ":" in line:
                    head, _, tail = line.rpartition(":")
                    cc = tail.strip().upper()
                    if cc in ("US", "CA"):
                        country = cc
                        line = head
                stores[_normalize_domain(line)] = country
    except OSError:
        pass
    _V2_STORES = stores
    _V2_MTIME = mtime
    return _V2_STORES


def _is_v2_store(source_domain: str) -> bool:
    """True if this source-domain should be served the v2 checkout."""
    if not source_domain:
        return False
    return _normalize_domain(source_domain) in _load_v2_stores()


def _v2_store_country(source_domain: str) -> str | None:
    """Returns "US" or "CA" if the v2 store has a country pinned in the
    file, else None (unknown — caller should fall back to query param)."""
    if not source_domain:
        return None
    return _load_v2_stores().get(_normalize_domain(source_domain))


def _is_card_enabled_for_source(source_domain: str) -> bool:
    """
    Returns True if credit card payment should be enabled for this source store.

    Rules:
      - CARD_ENABLED_STORES empty or "*" → enabled for ALL source stores
      - Otherwise → source_domain must match one of the entries (substring)
    """
    raw = (getattr(settings, "CARD_ENABLED_STORES", "") or "").strip()

    # Empty or "*" → enabled for all
    if not raw or raw == "*":
        return True

    if not source_domain:
        return True  # No source given but cards are globally enabled

    src = source_domain.lower().replace("https://", "").replace("http://", "").rstrip("/")
    enabled_stores = _get_card_enabled_stores()
    if not enabled_stores:
        return True

    for store in enabled_stores:
        if store and store in src:
            return True
    return False


def _authnet_enabled_for(source_domain: str) -> bool:
    """
    Return True if the Authorize.net card option should appear on the
    checkout for the given source store.

    Three preconditions must ALL hold:
      1. AUTHNET_ENABLED=true                 (master switch)
      2. AUTHNET_LOGIN_ID + AUTHNET_PUBLIC_CLIENT_KEY are set (credentials present)
      3. source_domain is in AUTHNET_STORES allowlist

    Allowlist semantic (mirrors HIGHRISKIFY_STORES):
      ""    (empty)  → no stores
      "*"            → all stores
      "a.com,b.com"  → only listed domains
    """
    if not bool(getattr(settings, "AUTHNET_ENABLED", False)):
        return False
    if not (getattr(settings, "AUTHNET_LOGIN_ID", "") and
            getattr(settings, "AUTHNET_PUBLIC_CLIENT_KEY", "")):
        return False

    raw = (getattr(settings, "AUTHNET_STORES", "") or "").strip()
    if raw == "*":
        return True
    if raw == "":
        return False
    allowlist = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return (source_domain or "").strip().lower() in allowlist


def _stripe_direct_enabled_for(source_domain: str) -> bool:
    """
    Return True if the Stripe direct card option should appear on the
    checkout for the given source store.

    Same gating pattern as _authnet_enabled_for():
      1. STRIPE_DIRECT_ENABLED=true
      2. STRIPE_SECRET_KEY + STRIPE_PUBLISHABLE_KEY are set
      3. source_domain is in STRIPE_DIRECT_STORES allowlist
    """
    if not bool(getattr(settings, "STRIPE_DIRECT_ENABLED", False)):
        return False
    if not (getattr(settings, "STRIPE_SECRET_KEY", "") and
            getattr(settings, "STRIPE_PUBLISHABLE_KEY", "")):
        return False

    raw = (getattr(settings, "STRIPE_DIRECT_STORES", "") or "").strip()
    if raw == "*":
        return True
    if raw == "":
        return False
    allowlist = {s.strip().lower() for s in raw.split(",") if s.strip()}
    return (source_domain or "").strip().lower() in allowlist


# ─── Bridge-7 availability cache ──────────────────────────────────────────────
# Avoid hammering bridge-7's /router/status on every page load.
# Result is cached for BRIDGE_CHECK_CACHE_TTL seconds.
BRIDGE_CHECK_CACHE_TTL = 30  # seconds
_bridge_cache: dict = {"ts": 0.0, "available": True}


async def _is_bridge_card_available() -> bool:
    """
    Returns True if bridge-7 has at least one available checkout store
    (Stripe or Shopify) under its daily limit. False if all are exhausted.

    Caches the result for BRIDGE_CHECK_CACHE_TTL seconds. On any error
    (network, timeout, bad response) returns True (fail-open) so the
    checkout flow isn't blocked by transient infrastructure issues.
    """
    now = time.time()
    if (now - _bridge_cache["ts"]) < BRIDGE_CHECK_CACHE_TTL:
        return _bridge_cache["available"]

    # Derive status URL from BRIDGE_URL (replace /s2s with /router/status)
    bridge_url = getattr(settings, "BRIDGE_URL", "") or ""
    if not bridge_url:
        return True
    status_url = bridge_url.replace("/s2s", "/router/status")

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(status_url)
        if resp.status_code != 200:
            logger.warning(f"Bridge status returned {resp.status_code} — assuming cards available")
            _bridge_cache.update(ts=now, available=True)
            return True

        data = resp.json()
        available_count = int(data.get("available", 0))
        is_available = available_count > 0

        _bridge_cache.update(ts=now, available=is_available)
        if not is_available:
            logger.warning(f"🚫 Bridge reports ALL stores exhausted — disabling card option")
        return is_available

    except Exception as e:
        logger.warning(f"Bridge availability check failed ({e}) — assuming cards available")
        _bridge_cache.update(ts=now, available=True)
        return True


# ─── App lifespan (startup / shutdown) ───────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify DB connection on startup
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        logger.info("✅ Database connection OK")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")

    # Auto-create missing tables from models (dev convenience)
    try:
        from database import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Tables verified/created")
    except Exception as e:
        logger.error(f"❌ Auto-create tables failed: {e}")

    # Order expiry task DISABLED — orders stay pending until admin acts on them.
    # Customers may complete Interac/Zelle hours or days after placing the order;
    # we don't want a timer silently flipping those to expired.

    # Log card-enabled stores for visibility
    raw_enabled = (getattr(settings, "CARD_ENABLED_STORES", "") or "").strip()
    if not raw_enabled or raw_enabled == "*":
        logger.info("💳 Card payment enabled for ALL source stores")
    else:
        enabled_stores = _get_card_enabled_stores()
        logger.info(f"💳 Card payment enabled for: {enabled_stores}")

    # Log Stripe publishable key status
    if settings.STRIPE_PUBLISHABLE_KEY:
        logger.info(f"💳 Stripe publishable key configured (starts with: {settings.STRIPE_PUBLISHABLE_KEY[:10]}...)")
    else:
        logger.warning("⚠️  STRIPE_PUBLISHABLE_KEY not set — embedded Stripe checkout will fail")

    yield

    # Cleanup
    await engine.dispose()
    logger.info("Database engine disposed.")


# ─── FastAPI app ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="Checkout Server",
    version="1.0.0",
    docs_url="/api/docs" if settings.ENVIRONMENT == "development" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# CORS — only needed if checkout page is served from a different origin
# (not needed if Nginx serves everything from same domain)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pepscheckoutportal.com",
        "https://www.pepscheckoutportal.com",
        "https://eaststpaulpeptides.ca",
        "https://swiftremit.ca",
        "https://www.swiftremit.ca",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ─── Security headers ─────────────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Content-Type-Options"]    = "nosniff"
        # response.headers["X-Frame-Options"]           = "DENY"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    return response


# ─── Brand middleware ─────────────────────────────────────────────────────────
@app.middleware("http")
async def brand_middleware(request: Request, call_next):
    host = request.headers.get("host", "").split(":")[0].lower()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Brand).where(Brand.domain == host)
        )
        brand = result.scalar_one_or_none()

    # Treat an inactive brand as "no brand". We filter in Python rather than in
    # SQL (`Brand.active == True`) because Neon DBs imported from MySQL store
    # `brands.active` as smallint, not boolean — and Postgres rejects the
    # `smallint = boolean` comparison. Reading the value works regardless of
    # the underlying column type (1/True are both truthy).
    if brand is not None and not brand.active:
        brand = None

    request.state.brand = brand

    if brand is None and settings.ENVIRONMENT == "production":
        logger.warning(f"Unknown domain: {host}")
        # Still serve the page — will use defaults

    response = await call_next(request)
    return response


# ─── Routers ─────────────────────────────────────────────────────────────────
app.include_router(checkout_router)
app.include_router(webhooks_router)
app.include_router(admin_router)
from routes.auth_routes import router as auth_router
app.include_router(auth_router)
from routes.revenue import router as revenue_router
app.include_router(revenue_router)
from routes.customer import router as customer_router
app.include_router(customer_router)

# Static files (CSS, JS, images if any)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ─── Brand config endpoint ────────────────────────────────────────────────────
@app.get("/config")
async def brand_config(request: Request):
    """
    Returns brand configuration as JSON.
    Called by the checkout page JS on load to dynamically apply branding.
    """
    brand = getattr(request.state, "brand", None)

    if brand:
        config = brand.to_public_dict()
    else:
        # Fallback defaults when domain isn't registered
        config = {
            "storeName":       "Checkout",
            "logoUrl":         None,
            "headerBgUrl":     None,
            "accentColor":     "#dd1d1d",
            "accentHover":     "#b01515",
            "interacEmail":    settings.INTERAC_DEFAULT_EMAIL,
            "interacDiscount": 5.0,
            "cryptoDiscount":  10.0,
        }

    return JSONResponse(config)

# ─── Whop availability helper ─────────────────────────────────────────────────
async def _is_whop_available_today() -> bool:
    """
    Returns True if Whop is configured AND today's CAD volume routed through
    Whop is still under WHOP_DAILY_LIMIT. False otherwise.

    Used by /api/checkout/whop-embed (hard enforcement) and by the checkout
    page renderer (hide the Card (WHOP) option entirely when capacity is
    reached, so customers don't see a button that just errors).

    Counts orders where payment_method=card AND payment_ref starts with "ch_"
    (Whop session ID prefix) created since UTC midnight today.
    """
    # Master kill-switch — hides Whop without touching keys or limits.
    # Set WHOP_ENABLED=false in .env to disable, then restart.
    if not bool(getattr(settings, "WHOP_ENABLED", True)):
        return False

    # Whop not configured at all → option hidden
    if bool(getattr(settings, "WHOP_SANDBOX", False)):
        configured = bool(getattr(settings, "WHOP_SANDBOX_API_KEY", ""))
    else:
        configured = bool(getattr(settings, "WHOP_API_KEY", ""))
    if not configured:
        return False

    daily_limit = float(getattr(settings, "WHOP_DAILY_LIMIT", 0) or 0)
    if daily_limit <= 0:
        # 0 means disabled — but configured. We treat as "always available".
        # If you want 0 to mean "always hidden", flip this to `return False`.
        return True

    try:
        from datetime import datetime, timezone
        from sqlalchemy import select, func
        from models.order import Order, PaymentMethod
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(func.coalesce(func.sum(Order.total), 0))
                .where(Order.created_at >= today_start)
                .where(Order.payment_method == PaymentMethod.card)
                .where(Order.payment_ref.like("ch_%"))
            )
            today_total = float(result.scalar() or 0)
        return today_total < daily_limit
    except Exception as e:
        # On any DB error, default to AVAILABLE (fail-open). Better to show
        # the option and have a transaction fail than to silently hide it
        # because of a transient DB blip.
        logger.warning(f"[Whop availability] check failed ({e}) — defaulting to available")
        return True


# ─── Checkout page ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def checkout_page(request: Request):
    """
    Serve the checkout HTML with brand vars injected server-side.
    This avoids a visible flash/reflow from client-side brand loading.
    """
    brand = getattr(request.state, "brand", None)

    # Country from theme's query param (?country=US or CA). Defaults to CA.
    country = request.query_params.get("country", "CA").upper()

    # If this v2 store has a country pinned in data/checkout_v2_stores.txt
    # (lines like `mystore.com:US`), the file ALWAYS wins — protects against
    # a theme bridge that forgot to send `&country=US`. v1/non-listed stores
    # keep the query-param-driven behavior.
    _src_for_country = (
        request.query_params.get("source")
        or request.headers.get("host", "")
    )
    pinned = _v2_store_country(_src_for_country)
    if pinned:
        country = pinned

    currency = "USD" if country == "US" else "CAD"

    # Source store determines per-method gating. Per-store overrides come
    # from STORE_CONFIG_CSV (a CSV file). If a source has no row, fall back
    # to the global env defaults — preserves current behavior for stores
    # not yet in the file.
    source_domain = request.query_params.get("source", "")

    from services import store_config as _store_cfg

    # Defaults pulled from the existing env machinery — same as before.
    _env_card    = _is_card_enabled_for_source(source_domain)
    _env_whop    = bool(getattr(settings, "WHOP_ENABLED", False))
    _env_altcoin = (
        bool(getattr(settings, "ALTCOIN_ENABLED", True))
        and bool(settings.NOWPAYMENTS_API_KEY)
    )
    # Onramp default: prefer the direct Highriskify integration when it's
    # configured; fall back to the WP plugin path if HIGHRISKIFY isn't set.
    # The CSV `onramp` column controls per-store visibility for whichever
    # path is active.
    _hr_ready = (
        bool(getattr(settings, "HIGHRISKIFY_ENABLED", False))
        and bool(getattr(settings, "HIGHRISKIFY_WALLET", ""))
    )
    _wp_ready = (
        bool(getattr(settings, "ONRAMP_WP_ENABLED", False))
        and bool(getattr(settings, "ONRAMP_WP_URL", ""))
        and (
            bool(getattr(settings, "ONRAMP_WP_CONSUMER_KEY", ""))
            or bool(getattr(settings, "ONRAMP_WP_APP_PASSWORD", ""))
        )
    )
    _env_onramp = _hr_ready or _wp_ready

    # CSV per-store overrides (None = no override, fall back to default)
    card_enabled    = _store_cfg.is_enabled(source_domain, "card",    _env_card)
    # Card method disabled on US AND CA stores.
    #   - US: disabled per business decision (e.g. processor switch / risk)
    #   - CA: pymtz integration was originally US-only — never had a CA path
    # To re-enable for a country, remove that branch.
    if country in ("US", "CA"):
        card_enabled = False

    whop_enabled_pre = _store_cfg.is_enabled(source_domain, "whop",   _env_whop)
    altcoin_enabled = _store_cfg.is_enabled(source_domain, "altcoin", _env_altcoin)
    onramp_enabled  = _store_cfg.is_enabled(source_domain, "onramp",  _env_onramp)

    # Hard kill onramp for US stores — onramp providers (Transak/MoonPay) have
    # poor US support, and the WP-plugin path bills in USD via CAD-cloaked
    # merchant. Disabling globally avoids per-store CSV bookkeeping.
    if country == "US":
        onramp_enabled = False

    # Whop also requires daily-cap headroom — apply that *after* the CSV
    # override so a store that's enabled but over cap still gets hidden.
    whop_enabled = (
        whop_enabled_pre
        and card_enabled                       # whop UI is coupled to card
        and await _is_whop_available_today()
    )

    # Onramp UI is independent — a store can have onramp on with regular
    # card off (typical setup: hide pymtz card, show "Card (Alt)" instead).
    # No additional coupling here.

    # Per-store Highriskify gate — explicit allowlist. Only domains listed
    # in HIGHRISKIFY_STORES use the new Transak/MoonPay picker. Every other
    # onramp-enabled store falls through to the legacy WP plugin path.
    #   ""    (empty)  → NO stores use Highriskify (all use WP plugin)
    #   "*"            → ALL stores use Highriskify (wildcard)
    #   "a.com,b.com"  → only those domains use Highriskify
    _hr_stores_raw = (getattr(settings, "HIGHRISKIFY_STORES", "") or "").strip()
    if _hr_stores_raw == "*":
        _hr_for_this_store = True
    elif _hr_stores_raw == "":
        _hr_for_this_store = False
    else:
        _hr_allowlist     = {s.strip().lower() for s in _hr_stores_raw.split(",") if s.strip()}
        _hr_for_this_store = (source_domain or "").lower() in _hr_allowlist
    # Frontend flag — only show the Transak/MoonPay picker modal when the
    # backend would actually honor `provider`. Non-Highriskify stores skip
    # straight to the WP plugin redirect.
    onramp_picker_enabled = bool(_hr_ready and _hr_for_this_store and onramp_enabled)

    ctx = {
        "store_name": (
            request.query_params.get("storename") + " Checkout"
            if request.query_params.get("storename")
            else (brand.store_name if brand else "Checkout")
        ),
        "logo_url":         brand.logo_url          if brand else "",
        "header_bg_url":    brand.header_bg_url     if brand else "",
        **dict(zip(("accent_color", "accent_hover"), _resolve_accent(request, brand))),
        "interac_email":    brand.interac_email     if brand else settings.INTERAC_DEFAULT_EMAIL,
        "zelle_email":      settings.ZELLE_DEFAULT_EMAIL,
        "interac_discount": float(brand.interac_discount if brand else 10),
        "zelle_discount":   float(getattr(brand, "zelle_discount", None) or 5),
        "crypto_discount":  float(brand.crypto_discount  if brand else 10),
        "store_country":    country,
        "store_currency":   currency,
        "base_url":         settings.BASE_URL,
        "source_domain":    source_domain,
        "card_enabled":      card_enabled,
        "whop_enabled":      whop_enabled,
        "altcoin_enabled":   altcoin_enabled,
        "onramp_wp_enabled": onramp_enabled,
        "onramp_picker_enabled": onramp_picker_enabled,
        "stripe_publishable_key": settings.STRIPE_PUBLISHABLE_KEY or "",
        "helcim_worker_url": getattr(settings, "HELCIM_WORKER_URL", "https://hc-worker.flystarcafe7.workers.dev"),
        # Authorize.net — public client key is safe to expose to the browser
        # (it's used by Accept.js for client-side tokenization). The secret
        # Transaction Key stays server-side.
        #
        # Per-store gate (AUTHNET_STORES): explicit allowlist, same semantic
        # as HIGHRISKIFY_STORES.
        #   ""    → no stores see the option
        #   "*"   → all stores see it
        #   "..." → only listed domains see it
        "authnet_enabled":           _authnet_enabled_for(source_domain),
        "authnet_login_id":          getattr(settings, "AUTHNET_LOGIN_ID", "") or "",
        "authnet_public_client_key": getattr(settings, "AUTHNET_PUBLIC_CLIENT_KEY", "") or "",
        "authnet_sandbox":           bool(getattr(settings, "AUTHNET_SANDBOX", False)),
        # Stripe direct — parallel processor to Auth.net. Publishable key is
        # safe to expose to the browser; secret key stays server-side.
        "stripe_direct_enabled":     _stripe_direct_enabled_for(source_domain),
        # stripe_publishable_key already in ctx above (legacy bridge uses same).
    }

    # Template routing — opt-in via `?v=` query param.
    #   v=2  → checkout-v2.html (US — sage/mint editorial)
    #   v=ca → checkout-ca.html (Canada — slate stone, oxblood accent)
    #   else → checkout.html    (legacy v1)
    # The peptide store theme appends the right `&v=` to the checkout URL.
    v_param = request.query_params.get("v", "").strip().lower()
    if v_param == "ca":
        try:
            template = jinja_env.get_template("checkout-ca.html")
        except Exception:
            template = jinja_env.get_template("checkout.html")
    elif v_param == "2":
        try:
            template = jinja_env.get_template("checkout-v2.html")
        except Exception:
            template = jinja_env.get_template("checkout.html")
    else:
        template = jinja_env.get_template("checkout.html")
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Stripe embedded checkout — branded thank-you page ────────────────────────
@app.get("/order/success", response_class=HTMLResponse)
async def order_success_page(request: Request):
    """
    Branded thank-you page for Stripe embedded checkout success.
    Stripe redirects here with ?session_id=cs_test_xxx after payment.
    The page then fetches order details from the Stripe worker and displays
    a summary using the customer's form data (not Stripe's invoice data).
    """
    brand = getattr(request.state, "brand", None)
    session_id = request.query_params.get("session_id", "")

    _acc, _hov = _resolve_accent(request, brand, "#dc2626", "#b91c1c")
    v_param = request.query_params.get("v", "").strip().lower()
    is_v2 = v_param == "2"
    is_ca = v_param == "ca"
    ctx = {
        "store_name":        brand.store_name   if brand else "Checkout",
        "logo_url":          brand.logo_url     if brand else "",
        "accent_color":      _acc,
        "accent_hover":      _hov,
        "session_id":        session_id,
        "stripe_worker_url": settings.STRIPE_WORKER_URL,
        "helcim_worker_url": getattr(settings, "HELCIM_WORKER_URL", "https://hc-worker.flystarcafe7.workers.dev"),
        # v2 reskin flag — driven by ?v=2 (propagated from checkout-v2's
        # withBrandAccent). Used by the template to include the v2 stylesheet.
        "is_v2":             is_v2,
        "is_ca":             is_ca,
        # Country drives the v2 palette (US=sky-blue, CA=emerald). Propagated
        # from checkout-v2 via withBrandAccent; falls back to CA (matches the
        # checkout-page default).
        "store_country":     (request.query_params.get("country", "CA") or "CA").upper(),
    }

    # Template routing for /order/success — v=ca picks the CA design,
    # v=2 picks v2, anything else falls back to v1.
    if is_ca:
        template_name = "order-success-ca.html"
    elif is_v2:
        template_name = "order-success-v2.html"
    else:
        template_name = "order-success.html"
    try:
        template = jinja_env.get_template(template_name)
    except Exception:
        template = jinja_env.get_template("order-success.html")
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Order confirmation page ──────────────────────────────────────────────────
@app.get("/order/{order_id}/confirmation", response_class=HTMLResponse)
async def confirmation_page(order_id: str, request: Request):
    brand = getattr(request.state, "brand", None)

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        result = await db.execute(
            select(Order).where(Order.id == order_id)
            .options(selectinload(Order.items))
            .options(selectinload(Order.nowpayments_invoice))
        )
        order = result.scalar_one_or_none()

    if not order:
        return HTMLResponse("<h1>Order not found.</h1>", status_code=404)

    # Normalize enum values to plain strings for the template
    pm_value = order.payment_method.value if hasattr(order.payment_method, 'value') else str(order.payment_method)
    ps_value = order.payment_status.value if hasattr(order.payment_status, 'value') else str(order.payment_status)

    # pymtz card orders: payment_method=card, payment_ref starts with "pay_"
    # Need separate confirmation flow since pymtz has no webhooks — we verify
    # status on the return-url page via /api/checkout/pymtz-verify/{order_id}.
    is_pymtz = (
        pm_value == "card"
        and bool(order.payment_ref)
        and str(order.payment_ref).startswith("pay_")
    )

    _acc, _hov = _resolve_accent(request, brand)
    _v_param = request.query_params.get("v", "").strip().lower()
    is_v2 = _v_param == "2"
    is_ca = _v_param == "ca"
    ctx = {
        "store_name":    order.store_name or (brand.store_name if brand else "Checkout"),
        "logo_url":      brand.logo_url   if brand else "",
        "accent_color":  _acc,
        "accent_hover":  _hov,
        "order":         order,
        "order_id":      order_id,
        "payment_method": pm_value,
        "payment_status": ps_value,
        "subtotal":       float(order.subtotal),
        "discount_pct":            float(order.discount_pct or 0),
        "discount_amount":         float(order.discount_amount or 0),
        "total":                   float(order.total),
        "original_subtotal":       float(order.original_subtotal or order.subtotal),
        "discount_code":           order.discount_code or "",
        "voucher_discount":        float(order.promo_discount_amount or 0),
        "voucher_discount_pct":    float(order.promo_discount_pct or 0),
        "payment_method_discount": float(order.discount_amount or 0),
        # Read the percentage from the ORDER, not the brand — that way the
        # label always matches the discount_amount that was actually applied,
        # even for old orders placed before the brand's % was changed. Brand
        # value is only used as a fallback for orders missing discount_pct.
        "interac_discount_pct":    float(order.discount_pct or (brand.interac_discount if brand else 10)),
        "zelle_discount_pct":      float(order.discount_pct or (getattr(brand, "zelle_discount", None) or 5)),
        "currency":       order.currency,
        "interac_email": (
            brand.interac_email if brand and brand.interac_email
            else settings.INTERAC_DEFAULT_EMAIL
        ),
        "zelle_email":   settings.ZELLE_DEFAULT_EMAIL,
        "btcpay_url": order.payment_ref and f"{settings.BTCPAY_URL}/i/{order.payment_ref}" if pm_value == "crypto" else "",
        "items": order.items if order.items else [],
        "np_invoice_id": order.nowpayments_invoice.np_invoice_id if order.nowpayments_invoice else "",
        "is_pymtz":      is_pymtz,
        # v2 reskin flag — propagated from checkout-v2 via withBrandAccent.
        "is_v2":         is_v2,
        "is_ca":         is_ca,
        # Country drives the v2 palette. Propagated from checkout-v2 via
        # withBrandAccent (?country=US/CA). Falls back to inferring from the
        # order's currency (CAD → CA, anything else → US).
        "store_country": (
            (request.query_params.get("country") or "").upper()
            or ("CA" if (order.currency or "").upper() == "CAD" else "US")
        ),
    }

    # Confirmation template routing — picks the right variant by payment
    # method, then the right skin by `v` query param:
    #   v=ca → confirmation*-ca.html  (warm cognac)
    #   v=2  → confirmation*-v2.html  (sage/mint)
    #   else → confirmation*.html     (legacy v1)
    if pm_value == "crypto":
        ca_name, v2_name, v1_name = "confirmation_crypto-ca.html", "confirmation_crypto-v2.html", "confirmation_crypto.html"
    elif pm_value == "altcoin":
        ca_name, v2_name, v1_name = "confirmation_altcoin-ca.html", "confirmation_altcoin-v2.html", "confirmation_altcoin.html"
    else:
        ca_name, v2_name, v1_name = "confirmation-ca.html", "confirmation-v2.html", "confirmation.html"

    if is_ca:
        try:
            template = jinja_env.get_template(ca_name)
        except Exception:
            template = jinja_env.get_template(v1_name)
    elif is_v2:
        try:
            template = jinja_env.get_template(v2_name)
        except Exception:
            template = jinja_env.get_template(v1_name)
    else:
        template = jinja_env.get_template(v1_name)
    html = template.render(**ctx)
    return HTMLResponse(content=html)


# ─── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "environment": settings.ENVIRONMENT}


# ─── Lasso checkout proxy ─────────────────────────────────────────────────────
# Routes /pay?sid=XXX through our server instead of sending the customer
# directly to Lasso's checkout URL. Injects CSS to hide the order summary
# so the customer never sees the decoy product name.
#
# Usage: set LASSO_CHECKOUT_URL=https://pepscheckoutportal.com in .env
# so LassoClient.build_redirect_url() returns /pay?sid=... pointing here.
# This endpoint then fetches the real Lasso page and serves it sanitised.

_LASSO_SUPPRESS_CSS = """
<style id="lasso-portal-overrides">
  /*
   * Hides the order summary / cart review panel on Lasso's checkout page.
   * Selectors are broad to survive Lasso's CSS-module class hashing.
   * Inspect checkout DOM and add specific selectors below if needed.
   */

  /* Common order summary containers */
  [class*="order-summary"],
  [class*="OrderSummary"],
  [class*="order_summary"],
  [class*="cart-summary"],
  [class*="CartSummary"],
  [class*="cart_summary"],
  [class*="product-list"],
  [class*="ProductList"],
  [class*="line-items"],
  [class*="LineItems"],
  [class*="line_items"],
  [class*="cart-items"],
  [class*="CartItems"],
  [id*="order-summary"],
  [id*="cart-summary"],
  [id*="line-items"] {
    display: none !important;
    visibility: hidden !important;
  }

  /* Hide any expandable order toggle / accordion */
  [class*="order-toggle"],
  [class*="summary-toggle"],
  [class*="collapse-summary"],
  [aria-label*="Order summary"],
  [aria-label*="order summary"] {
    display: none !important;
  }
</style>
"""

@app.get("/pay", response_class=HTMLResponse)
async def lasso_proxy(request: Request, sid: str = ""):
    """
    Fetches Lasso's checkout page for the given session, injects CSS to
    suppress the order summary, and serves the result to the customer.
    The customer's browser still runs all of Lasso's JS normally — only
    the visual order summary panel is hidden.
    """
    if not sid:
        return HTMLResponse("<h1>Invalid checkout session.</h1>", status_code=400)

    lasso_checkout_base = getattr(settings, "LASSO_CHECKOUT_URL", "").rstrip("/")
    if not lasso_checkout_base:
        return HTMLResponse("<h1>Checkout unavailable.</h1>", status_code=503)

    # If LASSO_CHECKOUT_URL points back to us (proxy mode), we need the REAL
    # Lasso URL stored separately as LASSO_REAL_CHECKOUT_URL.
    real_checkout_url = (
        getattr(settings, "LASSO_REAL_CHECKOUT_URL", "") or lasso_checkout_base
    )
    target_url = f"{real_checkout_url}?sid={sid}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
        ) as client:
            resp = await client.get(
                target_url,
                headers={
                    "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
    except httpx.RequestError as e:
        logger.error(f"[LassoProxy] Failed to fetch {target_url}: {e}")
        return HTMLResponse("<h1>Checkout temporarily unavailable. Please try again.</h1>", status_code=502)

    html = resp.text

    # Rewrite relative asset URLs → absolute so they still resolve from our domain
    lasso_origin = real_checkout_url.split("/checkout")[0]
    html = html.replace('src="/', f'src="{lasso_origin}/')
    html = html.replace("src='/", f"src='{lasso_origin}/")
    html = html.replace('href="/', f'href="{lasso_origin}/')
    html = html.replace("href='/", f"href='{lasso_origin}/")

    # Inject suppression CSS before </head>
    if "</head>" in html:
        html = html.replace("</head>", f"{_LASSO_SUPPRESS_CSS}</head>", 1)
    else:
        # Fallback: prepend at top
        html = _LASSO_SUPPRESS_CSS + html

    return HTMLResponse(content=html, status_code=200)
