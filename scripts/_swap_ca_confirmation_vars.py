"""
One-shot helper: apply CA design tokens (cognac + parchment) to the
confirmation/success -ca templates by swapping the :root CSS variables.

The v2 confirmation templates use a self-contained variable scheme
(--paper, --card, --ink, --accent, etc.). This swaps them in place
without rewriting the rest of the CSS — that way every rule using
those vars still works, just renders in the CA palette.

Also strips the {% if store_country == 'US' %} per-country accent block
since CA pages are CA-only.

Run from project root:
    python scripts/_swap_ca_confirmation_vars.py
"""
import re
from pathlib import Path

TEMPLATES = [
    "templates/order-success-ca.html",
    "templates/confirmation-ca.html",
    "templates/confirmation_crypto-ca.html",
    "templates/confirmation_altcoin-ca.html",
]

# CA design tokens — same palette as checkout-ca.html
CA_VARS = """:root{
  /* CA palette — warm editorial (cognac on parchment) */
  --paper:        #F8F2E5;
  --card:         #FFFCF5;
  --card-soft:    #FBF6E8;
  --ink:          #2A1F12;
  --ink-2:        #594833;
  --muted:        #9E8A6F;
  --dim:          #C2B59A;
  --line:         #E6DBC0;
  --line-2:       #D2C49E;
  --coral:        #A85A2A;
  --coral-s:      #F4E4D3;
  --success:      #4A6E2F;
  --success-bg:   #EFF3DD;
  --shadow-sm:    0 1px 2px rgba(42, 31, 18, 0.04);
  --shadow:       0 4px 16px rgba(42, 31, 18, 0.06);
  --r:            6px;
  --r-sm:         4px;
  --d:            'DM Sans', system-ui, sans-serif;
  --b:            'DM Sans', system-ui, sans-serif;
  --m:            'DM Mono', monospace;

  /* Brand accent — driven by the store's accent_color/accent_hover (URL or
     brand DB). Same vars the v2 per-country block sets. Transparency
     variants use color-mix so they re-tint when accent changes. */
  --accent:       {{ accent_color }};
  --accent-dark:  {{ accent_hover }};
  --accent-soft:  color-mix(in srgb, {{ accent_color }} 12%, transparent);
  --accent-tint:  color-mix(in srgb, {{ accent_color }} 6%,  transparent);
  --accent-glow:  color-mix(in srgb, {{ accent_color }} 28%, transparent);
  --accent-grad:  {{ accent_color }};
}

"""

# Matches the entire original :root block (lines 14-34 in confirmation-v2.html
# and equivalents) plus the per-country {% if %}{% else %}{% endif %} block.
ROOT_BLOCK = re.compile(
    r":root\{[^}]+?--m:\s*'Spline Sans Mono',monospace;\s*\}\s*"
    r"(?:\{%\s*if\s+store_country\s*==\s*'US'\s*%\}\s*:root\{[^}]+?\}\s*"
    r"\{%\s*else\s*%\}\s*:root\{[^}]+?\}\s*\{%\s*endif\s*%\}\s*)?",
    re.DOTALL,
)


def main():
    for path_str in TEMPLATES:
        p = Path(path_str)
        if not p.exists():
            print(f"SKIP {p} — does not exist")
            continue
        txt = p.read_text(encoding="utf-8")
        new_txt, n = ROOT_BLOCK.subn(CA_VARS, txt, count=1)
        if n == 0:
            print(f"WARN {p} — :root block not matched, no change")
            continue
        p.write_text(new_txt, encoding="utf-8")
        print(f"OK {p} ({len(new_txt)} bytes)")


if __name__ == "__main__":
    main()
