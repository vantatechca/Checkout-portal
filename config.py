from pydantic_settings import BaseSettings
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


def _to_sqlalchemy_url(raw: str, *, async_: bool) -> str:
    """Normalize a Postgres (Neon) connection string into a SQLAlchemy URL.

    - `postgres://`            → `postgresql://`        (Heroku/Neon shorthand)
    - `postgresql://`          → `postgresql+asyncpg://` (async)  or
                                 `postgresql+psycopg2://` (sync)
    - For the async (asyncpg) driver, strips libpq-only query params
      (`sslmode`, `channel_binding`) because asyncpg rejects them in the URL —
      SSL is supplied via connect_args in database.py instead. psycopg2 (sync)
      understands `sslmode`, so it's left intact there.

    Anything that isn't a Postgres URL (e.g. a legacy `mysql+...` string) is
    returned unchanged.
    """
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    parts = urlsplit(url)
    base_scheme = parts.scheme.split("+", 1)[0]
    if base_scheme not in ("postgresql", "postgres"):
        return url  # not Postgres — leave as-is

    scheme = "postgresql+asyncpg" if async_ else "postgresql+psycopg2"
    drop = {"sslmode", "channel_binding"} if async_ else set()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in drop])
    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


class Settings(BaseSettings):
    # Database
    # Production (Neon/Render): set DATABASE_URL to the Neon connection string,
    # e.g. postgresql://user:pass@ep-xxx-pooler.neon.tech/dbname?sslmode=require
    # When DATABASE_URL is empty the DB_* parts below are used to build a
    # legacy local MySQL URL (dev fallback only).
    DATABASE_URL: str = ""
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_NAME: str = "checkout_db"
    DB_USER: str = "checkout_user"
    DB_PASSWORD: str = ""

    # App
    SECRET_KEY: str = "changeme"
    ENVIRONMENT: str = "production"
    BASE_URL: str = "https://checkout.yourdomain.com"

    # Redis
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    # Helcim
    HELCIM_API_TOKEN: str = ""
    HELCIM_API_URL: str = "https://api.helcim.com/v2"

    # BTCPay
    BTCPAY_URL: str = ""
    BTCPAY_API_KEY: str = ""
    BTCPAY_STORE_ID: str = ""
    BTCPAY_WEBHOOK_SECRET: str = ""

    RESEND_API_KEY: str = ""

    # Gmail
    GMAIL_CREDENTIALS_FILE: str = "./gmail_credentials.json"
    GMAIL_TOKEN_FILE: str = "./gmail_token.json"
    GMAIL_WATCH_EMAIL: str = ""

    # Interac / Zelle
    INTERAC_DEFAULT_EMAIL: str = ""
    ZELLE_DEFAULT_EMAIL: str = ""

    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: str = ""

    BRIDGE_SECRET: str = ""
    BRIDGE_URL: str = "https://bridge-7.flystarcafe7.workers.dev/s2s"
    BRIDGE_SECRET_US: str = ""
    BRIDGE_URL_US:    str = ""

    SHOPIFY_STORE_DOMAIN: str = ""
    SHOPIFY_API_TOKEN: str = ""
    SHOPIFY_STORE_DOMAIN_US: str = ""
    SHOPIFY_API_TOKEN_US: str = ""
    SHOPIFY_WEBHOOK_SECRET: str = ""

    MPC_CHECKOUT_SHOP:  str = ""
    MPC_CHECKOUT_TOKEN: str = ""
    STORE_1_SHOP:       str = ""
    STORE_1_TOKEN:      str = ""

    MPC_WEBHOOK_SECRET:     str = ""
    STORE_1_WEBHOOK_SECRET: str = ""
    FROPEP_CHECKOUT_SHOP: str = ""
    FROPEP_CHECKOUT_TOKEN: str = ""
    FROPEP_WEBHOOK_SECRET: str = ""
    LUKPEP_CHECKOUT_SHOP: str = ""
    LUKPEP_CHECKOUT_TOKEN: str = ""
    LUKPEP_WEBHOOK_SECRET: str = ""
    TOPPEP_CHECKOUT_SHOP: str = ""
    TOPPEP_CHECKOUT_TOKEN: str = ""
    TOPPEP_WEBHOOK_SECRET: str = ""
    CANPEP_CHECKOUT_SHOP: str = ""
    CANPEP_CHECKOUT_TOKEN: str = ""
    CANPEP_WEBHOOK_SECRET: str = ""
    CRAPEP_CHECKOUT_SHOP: str = ""
    CRAPEP_CHECKOUT_TOKEN: str = ""
    CRAPEP_WEBHOOK_SECRET: str = ""
    SAKPEP_CHECKOUT_SHOP: str = ""
    SAKPEP_CHECKOUT_TOKEN: str = ""
    SAKPEP_WEBHOOK_SECRET: str = ""
    PLUPEP_CHECKOUT_SHOP: str = ""
    PLUPEP_CHECKOUT_TOKEN: str = ""
    PLUPEP_WEBHOOK_SECRET: str = ""
    LIPEP_CHECKOUT_SHOP: str = ""
    LIPEP_CHECKOUT_TOKEN: str = ""
    LIPEP_WEBHOOK_SECRET: str = ""
    MAXPEP_CHECKOUT_SHOP: str = ""
    MAXPEP_CHECKOUT_TOKEN: str = ""
    MAXPEP_WEBHOOK_SECRET: str = ""
    COLPEP_CHECKOUT_SHOP: str = ""
    COLPEP_CHECKOUT_TOKEN: str = ""
    COLPEP_WEBHOOK_SECRET: str = ""
    JAMPEP_CHECKOUT_SHOP: str = ""
    JAMPEP_CHECKOUT_TOKEN: str = ""
    JAMPEP_WEBHOOK_SECRET: str = ""
    SWOPEP_CHECKOUT_SHOP: str = ""
    SWOPEP_CHECKOUT_TOKEN: str = ""
    SWOPEP_WEBHOOK_SECRET: str = ""

    # NowPayments
    NOWPAYMENTS_API_KEY:     str = ""
    NOWPAYMENTS_IPN_SECRET:  str = ""
    NOWPAYMENTS_SUCCESS_URL: str = ""
    # Master kill-switch for the "Altcoins" payment option (NowPayments). Set
    # to False to hide it from the checkout without wiping API keys.
    ALTCOIN_ENABLED:         bool = True

    # Polling
    INTERAC_POLL_INTERVAL: int = 300

    # Order expiration
    ORDER_EXPIRY_CARD_MINUTES:    int = 60
    ORDER_EXPIRY_CRYPTO_MINUTES:  int = 60
    ORDER_EXPIRY_INTERAC_MINUTES: int = 2880

    # Affiliate dashboard
    AFFILIATE_DASHBOARD_URL: str = "https://peps-affiliate.onrender.com"

    # Card-enabled stores (comma-separated source domains) — legacy fallback
    # when no per-store row exists in STORE_CONFIG_CSV.
    CARD_ENABLED_STORES: str = ""

    # Per-source-store payment-method config (CSV file). See
    # services/store_config.py for format details. If the file doesn't
    # exist, gating falls back to the global env flags below.
    STORE_CONFIG_CSV: str = "data/source_stores.csv"

    # Stores that should be served the V2 checkout template
    # (templates/checkout-v2.html). One source domain per line in the file;
    # blank lines and # comments are ignored. Missing file = no v2 stores.
    CHECKOUT_V2_STORES_FILE: str = "data/checkout_v2_stores.txt"

    # Stripe — for embedded checkout in modal
    STRIPE_PUBLISHABLE_KEY: str = ""    # pk_test_... (test) or pk_live_... (live)
    STRIPE_WORKER_URL:      str = "https://stripe-worker.flystarcafe7.workers.dev"

    # Helcim — worker URL for thank-you page order lookup
    HELCIM_WORKER_URL:      str = "https://hc-worker.flystarcafe7.workers.dev"

    # pymtz — credit card via hosted payment page (replaces bridge card flow).
    # Two merchant accounts: one for Canada (charged in USD via the 1.38
    # conversion shown on checkout) and one for the US. The country is
    # selected from order.currency: CAD→CA account, USD→US account. The legacy
    # single-account keys below are kept as fallbacks so a partially-configured
    # deploy still works.
    PYMTZ_API_KEY:        str = ""   # legacy / fallback if per-country unset
    PYMTZ_WEBHOOK_SECRET: str = ""   # legacy / fallback if per-country unset
    PYMTZ_API_KEY_CA:        str = ""   # pymtz_live_... for the Canada account
    PYMTZ_WEBHOOK_SECRET_CA: str = ""   # whsec_... for the Canada account
    PYMTZ_API_KEY_US:        str = ""   # pymtz_live_... for the US account
    PYMTZ_WEBHOOK_SECRET_US: str = ""   # whsec_... for the US account

    # Onramp via WordPress + 2530gateway plugin.
    # See services/onramp_wp.py for the architecture overview.
    ONRAMP_WP_ENABLED:         bool = False  # master kill-switch — flip to true to show the option
    ONRAMP_WP_URL:             str = ""   # e.g. http://23.137.251.62:8083 (no trailing /)
    ONRAMP_WP_CONSUMER_KEY:    str = ""   # ck_... from WC → Settings → Advanced → REST API (HTTPS only)
    ONRAMP_WP_CONSUMER_SECRET: str = ""   # cs_...
    # Application Password auth — preferred over WC REST keys when the
    # site is HTTP. Create at WP admin → Users → admin → Application Passwords.
    ONRAMP_WP_USERNAME:        str = ""   # WP username (e.g. "admin")
    ONRAMP_WP_APP_PASSWORD:    str = ""   # 24-char app password "aBcD eFgH ..."
    ONRAMP_WP_PRODUCT_ID:      str = ""   # leave blank to use fee_lines (no product needed)
    ONRAMP_WP_GATEWAY_ID:      str = ""   # leave blank for the default hosted gateway
    ONRAMP_WP_WEBHOOK_SECRET:  str = ""   # signing secret from the WC webhook config

    # Highriskify (a.k.a. 2530gateway) DIRECT API. Replaces ONRAMP_WP_* —
    # talks straight to api.2530gateway.com without the WordPress middleman.
    # No API key needed; the payout wallet identifies the merchant.
    HIGHRISKIFY_ENABLED:    bool = False  # master kill-switch
    HIGHRISKIFY_WALLET:     str = ""      # 0x... merchant USDC Polygon payout wallet
    HIGHRISKIFY_PROVIDER:   str = "transak"  # pinned provider — transak / banxa / topper / etc.
    HIGHRISKIFY_IPT_KEY:    str = ""      # IPT tracking key (blank = use public default)
    # Comma-separated list of source-store domains that should use the
    # Highriskify direct API (the new Transak/MoonPay picker). All other
    # onramp-enabled stores keep using the legacy WP-plugin path. Empty or
    # "*" → Highriskify for ALL onramp stores (matches old global behavior).
    HIGHRISKIFY_STORES:     str = ""

    # Authorize.net direct card processor — SWISSCO merchant via TSYS (MCC
    # 7299 cloak). Customer cards tokenized client-side via Accept.js, then
    # charged via api.authorize.net/xml/v1/request.api. Test Mode is toggled
    # in the Auth.net DASHBOARD (Account → Security Settings → Transaction
    # Processing Mode), NOT here — same credentials, dashboard decides
    # whether real cards are charged.
    AUTHNET_ENABLED:           bool = False
    AUTHNET_LOGIN_ID:          str  = ""      # API Login ID (<=25 chars)
    AUTHNET_TRANSACTION_KEY:   str  = ""      # secret — never expose to frontend
    AUTHNET_PUBLIC_CLIENT_KEY: str  = ""      # safe for frontend Accept.js
    AUTHNET_SIGNATURE_KEY:     str  = ""      # 128-char hex for webhook HMAC-SHA512
    AUTHNET_SANDBOX:           bool = False   # true = apitest.authorize.net (rarely needed; use dashboard Test Mode instead)
    # Per-store allowlist (mirrors HIGHRISKIFY_STORES semantic):
    #   ""    (empty)  → Auth.net shown on NO stores (current default)
    #   "*"            → Auth.net shown on ALL stores (when AUTHNET_ENABLED=true)
    #   "a.com,b.com"  → Auth.net shown only on those domains
    AUTHNET_STORES:            str  = ""

    # Stripe direct card processor — parallel to Auth.net, NOT a fallback.
    # Per team architecture: customer card → Stripe OR Auth.net (each settles
    # to its own bank) → manual transfer to Grey wallet.
    #
    # STRIPE_PUBLISHABLE_KEY is defined elsewhere in this Settings class
    # (used by the legacy Stripe Elements bridge path). For Stripe Direct,
    # we reuse it on the frontend.
    STRIPE_DIRECT_ENABLED:                bool = False
    STRIPE_SECRET_KEY:                    str  = ""   # sk_live_xxx or sk_test_xxx — server-side, never exposed
    STRIPE_WEBHOOK_SECRET:                str  = ""   # whsec_xxx — for /webhooks/stripe_direct HMAC verification
    STRIPE_STATEMENT_DESCRIPTOR_SUFFIX:   str  = ""   # appended to merchant name on bank statement; neutral text only (no pharma references)
    # Per-store allowlist (same semantic as AUTHNET_STORES):
    #   ""    → no stores see Stripe option
    #   "*"   → all stores
    #   "..." → only listed domains
    STRIPE_DIRECT_STORES:                 str  = ""

    # Lasso — cloaked CC checkout via Whop payment rails
    LASSO_STORE_ID:           str = ""   # data-store-id from your Lasso merchant dashboard
    LASSO_CHECKOUT_URL:       str = ""   # set to https://pepscheckoutportal.com/pay for proxy mode
    LASSO_REAL_CHECKOUT_URL:  str = ""   # actual Lasso URL e.g. https://checkout.yourdomain.com/checkout
    LASSO_WHOP_SECRET:        str = ""   # webhook signing secret from Whop dashboard (legacy — fallback for /webhooks/whop)

    # Whop — direct embedded checkout (parallel option to the existing Card flow).
    # Each order creates a one-time checkout configuration with a per-order plan
    # at the customer's actual cart total. The plan title is cloaked so peptide
    # names never reach Whop.
    #
    # Production credentials (whop.com dashboard)
    WHOP_API_KEY:        str = ""  # Company API key from Whop dashboard → Developer → API Keys
    WHOP_COMPANY_ID:     str = ""  # biz_xxxxxxxxxxxxx — the Whop company that owns the plan
    WHOP_PRODUCT_ID:     str = ""  # prod_xxxxxxxxxxxxx — existing product to attach plans to (so dashboard Product column populates)
    WHOP_WEBHOOK_SECRET: str = ""  # whsec_... or ws_... — Developer → Webhooks → Signing secret

    # Sandbox credentials (sandbox.whop.com — completely separate from prod).
    # Only used when WHOP_SANDBOX=true. Leave blank if not testing in sandbox.
    WHOP_SANDBOX_API_KEY:        str = ""
    WHOP_SANDBOX_COMPANY_ID:     str = ""
    WHOP_SANDBOX_PRODUCT_ID:     str = ""
    WHOP_SANDBOX_WEBHOOK_SECRET: str = ""

    # Shared settings (same for both environments)
    WHOP_CURRENCY:       str = "cad"  # lowercase ISO 4217 — must match what Whop supports for this company
    WHOP_PLAN_TITLE:     str = "DigiTech SecureSync"  # cloaked title shown to customer on Whop checkout
    WHOP_RETURN_URL:     str = ""  # optional — leave blank to NOT send redirect_url to Whop (skip-redirect handles it)
    WHOP_SANDBOX:        bool = False  # True → use WHOP_SANDBOX_* credentials and route API calls to sandbox-api.whop.com

    # Master kill-switch for the Card (WHOP) payment option. Set to False to
    # hide the option from the checkout page AND reject any direct API calls
    # to /api/checkout/whop-embed, without having to wipe API keys or limits.
    # Useful for quick on/off without touching credentials.
    WHOP_ENABLED:        bool = True

    # Volume cap — refuse new Whop checkouts once this CAD amount has been
    # routed through Whop today (UTC day). Counts both pending and paid
    # orders so a flood of abandoned attempts also throttles us. Set to 0
    # to disable. Recommended: start at 100 week 1, ramp to 300.
    WHOP_DAILY_LIMIT: float = 300.0

    # Optional email sink — if set, customer emails sent to Whop are
    # rewritten to {user}+{order_id}@{domain}. NOTE: not recommended at
    # volume (pattern detection flag). Leave empty to pass the customer's
    # real email through (Whop's send_customer_emails=false handles
    # email suppression on Whop's side).
    WHOP_SINK_EMAIL: str = ""

    # ── Tier plans (big risk reducer for production volume) ─────────────
    # Instead of creating a new inline plan per order (→ 600+ unique plans
    # per month, bot-like pattern), pre-create N fixed-price plans in the
    # Whop dashboard and route each cart to the closest one. From Whop's
    # POV the account now looks like a normal SaaS pricing ladder.
    #
    # Format: comma-separated "price:plan_id" pairs (price in major units).
    # Example: WHOP_TIER_PLANS=49:plan_aaa,99:plan_bbb,199:plan_ccc,299:plan_ddd
    # Leave empty to disable tiers and fall back to inline plan creation.
    WHOP_TIER_PLANS:         str = ""  # production tier plans
    WHOP_SANDBOX_TIER_PLANS: str = ""  # sandbox tier plans (different plan_ids)

    # How to map a cart total to a tier when tiers are configured:
    #   "round_down" — use the largest tier ≤ cart amount (customer pays
    #                  same or less than cart; you absorb any delta).
    #                  Safest UX — no surprise charges.
    #   "nearest"    — use whichever tier is closest (customer might pay
    #                  slightly more if cart is between two tiers).
    #   "round_up"   — use smallest tier ≥ cart amount (customer always
    #                  pays same or more). Margin gain but dispute risk.
    # If cart is outside the tier range (smaller than smallest or larger
    # than largest), falls back to inline plan creation for that one order.
    WHOP_TIER_STRATEGY: str = "round_down"

    @property
    def async_database_url(self) -> str:
        """Async SQLAlchemy URL (asyncpg for Postgres/Neon, aiomysql fallback)."""
        raw = (self.DATABASE_URL or "").strip()
        if raw:
            return _to_sqlalchemy_url(raw, async_=True)
        return f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def sync_database_url(self) -> str:
        """Sync SQLAlchemy URL (psycopg2 for Postgres, used by alembic/scripts)."""
        raw = (self.DATABASE_URL or "").strip()
        if raw:
            return _to_sqlalchemy_url(raw, async_=False)
        return f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "allow"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()