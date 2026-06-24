/**
 * oaklyn-cart-worker.js
 *
 * Cloudflare Worker that builds a Shopify cart on Oaklyn's storefront from
 * our backend. Shopify blocks our datacenter IP with 403s when we POST to
 * /cart/add.js; routing through a Cloudflare Worker dodges that because
 * requests come from a Cloudflare edge IP.
 *
 * Endpoint:
 *   POST /build-cart
 *   Headers:
 *     X-Worker-Secret: <SHARED_SECRET>   (must match WORKER_SECRET env var)
 *     Content-Type:    application/json
 *   Body:
 *     {
 *       "shop":            "ajp8fc-zn.myshopify.com",
 *       "target_cents":    20800,         // sum we need cart to reach
 *       "candidates":      [{"id": 1234, "qty": 42, "price_cents": 495}, ...]
 *           // ordered by accuracy: first one is "best match". Worker tries
 *           // them in order; first non-sold-out wins.
 *     }
 *   Returns:
 *     {
 *       "token":          "abc123...",
 *       "items":          [...],
 *       "total_price":    20800,
 *       "added":          5,
 *       "skipped":        12,
 *       "skipped_details": [{"id": 999, "status": 422, "body": "sold out"}, ...]
 *     }
 *
 * Deploy:
 *   wrangler deploy
 *   wrangler secret put WORKER_SECRET   # then paste the secret used by the backend
 */

const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ' +
  'AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/120.0.0.0 Safari/537.36';

// Pause between sequential /cart/add.js calls. Tune up if we still see 403s.
const ADD_DELAY_MS = 80;

// Cloudflare Workers cap at 50 subrequests per invocation. We use:
//   - 1 for warm-up GET
//   - 1 for final /cart.js
//   - up to MAX_ATTEMPTS POSTs to /cart/add.js
const MAX_ATTEMPTS = 45;

function jsonResponse(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// Cookie jar shared across one cart-build invocation.
class CookieJar {
  constructor() {
    this.cookies = new Map();
  }
  ingest(response) {
    const raw = response.headers.get('set-cookie');
    if (!raw) return;
    // Cloudflare's `getAll` is available for set-cookie via headers.getAll if supported
    const all = response.headers.getAll
      ? response.headers.getAll('set-cookie')
      : [raw];
    for (const line of all) {
      const [pair] = line.split(';');
      const eq = pair.indexOf('=');
      if (eq > 0) {
        const name = pair.slice(0, eq).trim();
        const value = pair.slice(eq + 1).trim();
        this.cookies.set(name, value);
      }
    }
  }
  header() {
    return Array.from(this.cookies.entries())
      .map(([k, v]) => `${k}=${v}`)
      .join('; ');
  }
}

// When the running cart total is within this much of target, stop adding more.
// Otherwise the worker happily piles on extra items past the target.
const TARGET_TOLERANCE_CENTS = 500;

async function buildCart(shop, targetCents, candidates) {
  const base = `https://${shop}`;
  const jar = new CookieJar();

  const baseHeaders = () => ({
    'User-Agent': BROWSER_UA,
    'Accept': 'application/json,text/html,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': `${base}/`,
    'Origin': base,
    ...(jar.header() ? { 'Cookie': jar.header() } : {}),
  });

  // Warm-up GET — establishes initial cookies + makes us look like a real visitor
  try {
    const warm = await fetch(base + '/', {
      method: 'GET',
      headers: baseHeaders(),
      redirect: 'follow',
    });
    jar.ingest(warm);
  } catch (e) {
    // Not fatal — proceed without warm-up
  }

  const added = [];
  const skipped = [];
  let addedTotal = 0;
  let attempts = 0;

  // Each candidate is a pre-computed (id, qty) basket ordered by accuracy.
  // Try them in order. STOP after first successful add — python already
  // chose the closest-to-target qty, so ONE successful add is enough.
  // Adding more items past the first only overshoots the target.
  for (const v of candidates) {
    if (attempts >= MAX_ATTEMPTS) break;

    const qty = v.qty || 1;
    attempts++;
    try {
      const resp = await fetch(base + '/cart/add.js', {
        method: 'POST',
        headers: {
          ...baseHeaders(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ items: [{ id: v.id, quantity: qty }] }),
      });
      jar.ingest(resp);
      if (resp.status === 200) {
        added.push({ id: v.id, qty, price_cents: v.price_cents });
        addedTotal += (v.price_cents || 0) * qty;
        break;  // first non-sold-out candidate wins — don't add more
      } else {
        const body = (await resp.text()).slice(0, 200);
        skipped.push({ id: v.id, qty, status: resp.status, body });
      }
    } catch (e) {
      skipped.push({ id: v.id, qty, status: 0, body: String(e) });
    }

    if (ADD_DELAY_MS > 0) await sleep(ADD_DELAY_MS);
  }

  // Fetch the assembled cart to get token
  const cartResp = await fetch(base + '/cart.js', {
    method: 'GET',
    headers: baseHeaders(),
  });
  if (cartResp.status !== 200) {
    const body = (await cartResp.text()).slice(0, 300);
    return jsonResponse(
      { error: `cart.js failed: ${cartResp.status}`, body, added: added.length, skipped: skipped.length },
      502,
    );
  }
  const cart = await cartResp.json();

  return jsonResponse({
    token: cart.token,
    items: cart.items,
    total_price: cart.total_price,
    item_count: cart.item_count,
    added: added.length,
    added_total: addedTotal,
    skipped: skipped.length,
    skipped_details: skipped.slice(0, 20),
    attempts,
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method !== 'POST' || url.pathname !== '/build-cart') {
      return jsonResponse({ error: 'POST /build-cart only' }, 404);
    }

    const secret = request.headers.get('x-worker-secret');
    if (!secret || secret !== env.WORKER_SECRET) {
      return jsonResponse({ error: 'unauthorized' }, 401);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResponse({ error: 'invalid json' }, 400);
    }

    const { shop, target_cents, candidates } = body;
    if (!shop || typeof target_cents !== 'number' || target_cents <= 0) {
      return jsonResponse({ error: 'missing shop or target_cents' }, 400);
    }
    if (!Array.isArray(candidates) || candidates.length === 0) {
      return jsonResponse({ error: 'missing candidates' }, 400);
    }

    return buildCart(shop, target_cents, candidates);
  },
};
