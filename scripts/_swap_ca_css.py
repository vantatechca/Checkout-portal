"""
One-shot helper: replace the CA override CSS block with single-column
centered layout + dark prominent header.

Run from project root:
    python scripts/_swap_ca_css.py
"""
from pathlib import Path

TARGET = Path("templates/checkout-ca.html")

START_MARKER = "/* ══════════════════════════════════════════════════════════════════════\n   CHECKOUT CA"
END_MARKER   = "\n</style>"

NEW_CSS = """/* ══════════════════════════════════════════════════════════════════════
   CHECKOUT CA · Warm Editorial · Single-Column Centered
   ──────────────────────────────────────────────────────────────────────
   Structural shift from v1/v2's two-column splits:
   • Single centered column, max-width ~76rem (like Stripe Checkout)
   • Dark warm header with cream text — prominent, not subtle
   • Order summary as a full-width banner at the top of the body
   • Form sections stacked vertically below
   • Pay button at the bottom — full container width
   • Same Cormorant Garamond serif + cognac accent from prior iteration
   ══════════════════════════════════════════════════════════════════════ */

:root{
  --ca-paper:       #F8F2E5;
  --ca-paper-2:     #F1E8D5;
  --ca-card:        #FFFCF5;
  --ca-card-soft:   #FBF6E8;
  --ca-ink:         #2A1F12;     /* deep warm brown */
  --ca-ink-2:       #594833;
  --ca-muted:       #9E8A6F;
  --ca-line:        #E6DBC0;
  --ca-line-2:      #D2C49E;
  --ca-success:     #4A6E2F;
  --ca-success-bg:  #EFF3DD;
  --ca-r:           6px;
  --ca-r-sm:        4px;
  --ca-shadow-sm:   0 1px 2px rgba(42, 31, 18, 0.04);
  --ca-shadow:      0 4px 16px rgba(42, 31, 18, 0.06);
  --serif:          'DM Sans', system-ui, sans-serif;
}

/* Brand accent — driven by the store's accent color (from URL ?accent=
   and/or the brands DB row). The base template already sets --red and
   --red-dark from those values; we just pipe them through. Transparency
   variants use color-mix so they re-tint automatically when --red changes. */
:root{
  --ca-accent:      var(--red);
  --ca-accent-deep: var(--red-dark);
  --ca-accent-soft: color-mix(in srgb, var(--red) 12%, transparent);
  --ca-accent-tint: color-mix(in srgb, var(--red) 6%,  transparent);
  --ca-accent-warm: var(--red);
}

::selection{ background: var(--ca-accent-soft); color: var(--ca-ink); }

body{
  background: var(--ca-paper) !important;
  font-family: 'DM Sans', system-ui, sans-serif !important;
  color: var(--ca-ink) !important;
  -webkit-font-smoothing: antialiased;
}

@media(min-width:981px){
  html, body{ height: 100vh !important; overflow: hidden !important; }
  .shell{ height: 100vh !important; display: flex !important; flex-direction: column !important; overflow: hidden !important; }
}

/* ══════════════════════════════════════════════════════════════════════
   HEADER — minimal text-only strip. No logo, no chrome. Store name
   on the left, "Checkout" label on the right. Slim bar at the top.
   ══════════════════════════════════════════════════════════════════════ */
.header{
  background: transparent !important;
  border-bottom: 1px solid var(--ca-line-2) !important;
  padding: 0.9rem 2.4rem !important;
  height: auto !important;
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: space-between;
  position: relative;
}
.header::before, .header::after{ display: none !important; }
.header-brand{ gap: 0 !important; }
.header-logo{ display: none !important; }
.header-name{
  font-family: var(--serif) !important;
  font-weight: 600 !important;
  font-style: italic !important;
  font-size: 1.45rem !important;
  letter-spacing: -0.01em !important;
  color: var(--ca-ink) !important;
}
.header-secure{
  color: var(--ca-ink-2) !important;
  font-family: 'DM Mono', monospace !important;
  font-size: 1.05rem !important;
  font-weight: 600 !important;
  letter-spacing: 0.2em !important;
  text-transform: uppercase !important;
  gap: 0;
}
.header-secure svg{ display: none !important; }
.header-secure .secure-text::after{
  content: 'CHECKOUT';
}
.header-secure .secure-text{
  font-size: 0;
  letter-spacing: 0;
}
.header-secure .secure-text::after{
  font-size: 1.05rem;
  letter-spacing: 0.2em;
}
.progress-bar{ display: none !important; }

/* ══════════════════════════════════════════════════════════════════════
   BODY — 2-col, wide. Form LEFT (dominant), order summary RIGHT (sticky).
   Uses more screen than v1/v2 (132rem cap vs ~120rem).
   ══════════════════════════════════════════════════════════════════════ */
.body{
  display: grid !important;
  grid-template-columns: minmax(0, 1fr) minmax(0, 36rem) !important;
  grid-template-areas: "form sidebar" !important;
  gap: 2.4rem !important;
  max-width: 132rem !important;
  margin: 0 auto !important;
  padding: 1.4rem 2.4rem 2rem !important;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  width: 100%;
  align-items: stretch;
}

.sidebar{
  grid-area: sidebar !important;
  background: transparent !important;
  border: none !important;
  border-radius: 0 !important;
  padding: 0 !important;
  position: static !important;
  top: auto !important;
  width: auto !important;
  height: 100% !important;
  display: flex !important;
  flex-direction: column;
  gap: 0;
  min-height: 0;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--ca-line-2) transparent;
}
.sidebar::-webkit-scrollbar{ width: 6px; }
.sidebar::-webkit-scrollbar-track{ background: transparent; }
.sidebar::-webkit-scrollbar-thumb{ background: var(--ca-line-2); border-radius: 3px; }

.form-col{
  grid-area: form !important;
  background: transparent !important;
  border: none !important;
  border-radius: 0 !important;
  padding: 0 !important;
  box-shadow: none !important;
  display: flex !important; flex-direction: column !important; gap: 0.8rem;
  overflow-y: auto;
  min-height: 0;
  scrollbar-width: thin;
  scrollbar-color: var(--ca-line-2) transparent;
}
.form-col::-webkit-scrollbar{ width: 6px; }
.form-col::-webkit-scrollbar-track{ background: transparent; }
.form-col::-webkit-scrollbar-thumb{ background: var(--ca-line-2); border-radius: 3px; }

/* ══════════════════════════════════════════════════════════════════════
   FORM SECTIONS — cards stacked, each with cognac left rule
   ══════════════════════════════════════════════════════════════════════ */
.section{
  background: transparent !important;
  border: 1px solid var(--ca-line-2) !important;
  border-left: 3px solid var(--ca-accent) !important;
  border-radius: var(--ca-r) !important;
  box-shadow: none !important;
  padding: 1.4rem 1.8rem !important;
  margin: 0 !important;
  position: relative;
}

.section-heading{
  font-family: var(--serif) !important;
  font-size: 1.8rem !important;
  font-weight: 600 !important;
  color: var(--ca-ink) !important;
  letter-spacing: -0.02em !important;
  margin-bottom: 0.25rem !important;
  display: flex; align-items: baseline; gap: 1rem;
  line-height: 1.1;
}
.step-num{
  background: transparent !important;
  color: var(--ca-accent) !important;
  font-family: var(--serif) !important;
  font-weight: 700 !important;
  font-style: italic !important;
  font-size: 2.8rem !important;
  width: auto !important;
  height: auto !important;
  border-radius: 0 !important;
  padding: 0 1rem 0 0 !important;
  border-right: 1px solid var(--ca-line-2);
  margin-right: 0.2rem !important;
}
.section-sub{
  color: var(--ca-ink-2) !important;
  font-size: 1.15rem !important;
  margin-bottom: 0.9rem !important;
  margin-top: 0.2rem !important;
  font-style: italic;
}

/* FORM FIELDS — tighter heights to fit more on screen */
.field-row{ gap: 0.8rem !important; margin-bottom: 0.8rem !important; }
.field input,
.field select{
  border: 1px solid var(--ca-line-2) !important;
  border-radius: var(--ca-r) !important;
  background: var(--ca-card-soft) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: 1.35rem !important;
  color: var(--ca-ink) !important;
  height: 4.2rem !important;
  padding: 1.4rem 1.2rem 0.4rem !important;
  transition: border-color .15s, background .15s, box-shadow .15s !important;
}
.field input:focus,
.field select:focus{
  border-color: var(--ca-accent) !important;
  background: var(--ca-card) !important;
  box-shadow: 0 0 0 3px var(--ca-accent-soft) !important;
  outline: none !important;
}
.field input.error{ border-color: #B23A3A !important; }
.field label{
  background: var(--ca-card-soft) !important;
  color: var(--ca-muted) !important;
}
.field input:focus + label,
.field input:not(:placeholder-shown) + label,
.field select:focus + label,
.field select:valid + label,
.field .label-up{
  background: var(--ca-card) !important;
  color: var(--ca-accent) !important;
  font-weight: 500 !important;
}
.field-error{ color: #B23A3A !important; font-size: 1.15rem !important; }
.select-wrap::after{ border-top-color: var(--ca-muted) !important; }

/* PAYMENT METHODS */
.pay-methods{
  border: 1px solid var(--ca-line) !important;
  border-radius: var(--ca-r) !important;
  overflow: hidden !important;
  background: var(--ca-card-soft) !important;
}
.pay-method{
  background: transparent !important;
  border-bottom: 1px solid var(--ca-line) !important;
  padding: 1.7rem 2rem !important;
  transition: background .15s !important;
}
.pay-method:last-child{ border-bottom: none !important; }
.pay-method:hover{ background: var(--ca-card) !important; }
.pay-method.active{ background: var(--ca-card) !important; }
.pay-method.active::before{
  content: '' !important;
  position: absolute !important; left: 0; top: 0; bottom: 0;
  width: 3px !important;
  background: var(--ca-accent) !important;
}
.pay-method input[type=radio]{ accent-color: var(--ca-accent) !important; }
.pay-name{
  font-family: var(--serif) !important;
  font-size: 1.8rem !important;
  font-weight: 600 !important;
  color: var(--ca-ink) !important;
  letter-spacing: -0.01em !important;
}
.pay-desc{
  color: var(--ca-ink-2) !important;
  font-size: 1.3rem !important;
  font-style: italic;
}
.save-badge{
  background: var(--ca-success-bg) !important;
  color: var(--ca-success) !important;
  font-family: 'DM Mono', monospace !important;
  font-size: 1.05rem !important;
  font-weight: 700 !important;
  padding: 0.3rem 0.9rem !important;
  border-radius: 100px !important;
  letter-spacing: 0.06em !important;
  text-transform: uppercase !important;
}

/* INTERAC BOX */
.interac-box{
  background: var(--ca-card-soft) !important;
  border: 1px solid var(--ca-line) !important;
  border-left: 3px solid var(--ca-accent) !important;
  border-radius: var(--ca-r) !important;
  color: var(--ca-ink-2) !important;
  padding: 1.8rem !important;
  font-size: 1.35rem !important;
  line-height: 1.65 !important;
}
.interac-box strong{ color: var(--ca-ink) !important; font-family: var(--serif) !important; font-size: 1.6rem !important; }
.interac-email-pill{
  background: var(--ca-card) !important;
  border: 1px solid var(--ca-line-2) !important;
  border-radius: var(--ca-r-sm) !important;
  color: var(--ca-ink) !important;
  font-family: 'DM Mono', monospace !important;
  font-size: 1.3rem !important;
  font-weight: 500 !important;
  padding: 0.5rem 1rem !important;
}
.copy-btn{
  background: var(--ca-card) !important;
  border: 1px solid var(--ca-line-2) !important;
  color: var(--ca-ink) !important;
  border-radius: var(--ca-r-sm) !important;
  padding: 0.4rem 1rem !important;
  font-size: 1.1rem !important;
  font-weight: 500 !important;
}
.copy-btn:hover{ background: var(--ca-accent-tint) !important; border-color: var(--ca-accent) !important; color: var(--ca-accent) !important; }
.interac-highlight{
  color: var(--ca-accent) !important;
  font-family: 'DM Mono', monospace !important;
  font-weight: 600 !important;
}

/* CRYPTO BOX */
.crypto-box{
  background: var(--ca-card-soft) !important;
  border: 1px solid var(--ca-line) !important;
  border-left: 3px solid var(--ca-accent) !important;
  border-radius: var(--ca-r) !important;
  color: var(--ca-ink-2) !important;
  padding: 1.8rem !important;
}
.crypto-box strong{ color: var(--ca-ink) !important; font-family: var(--serif) !important; }
.coin-chip{
  background: var(--ca-card) !important;
  border: 1px solid var(--ca-line-2) !important;
  color: var(--ca-ink-2) !important;
  border-radius: 100px !important;
  font-family: 'DM Mono', monospace !important;
}

/* CHECKBOX */
.checkbox-row{ color: var(--ca-ink-2) !important; font-size: 1.35rem !important; }
.checkbox-row input{ accent-color: var(--ca-accent) !important; }

/* ══════════════════════════════════════════════════════════════════════
   SUBMIT BUTTON — full-width, prominent at the bottom of the column
   ══════════════════════════════════════════════════════════════════════ */
.submit-btn{
  background: var(--ca-ink) !important;
  color: var(--ca-paper) !important;
  border: none !important;
  border-radius: var(--ca-r) !important;
  font-family: var(--serif) !important;
  font-style: italic !important;
  font-size: 2rem !important;
  font-weight: 600 !important;
  letter-spacing: 0 !important;
  height: 6.4rem !important;
  margin-top: 0.6rem !important;
  transition: background .15s, transform .1s, box-shadow .15s !important;
  box-shadow: 0 2px 6px rgba(42, 31, 18, 0.2);
}
.submit-btn:hover{
  background: #3d2e1b !important;
  box-shadow: 0 6px 18px rgba(42, 31, 18, 0.3);
}
.submit-btn:active{ transform: translateY(1px) scale(0.997); box-shadow: 0 1px 2px rgba(42, 31, 18, 0.15) !important; }
.submit-btn:disabled{ background: var(--ca-line-2) !important; box-shadow: none !important; }
.submit-btn .btn-icon{ stroke: var(--ca-paper) !important; }

/* TRUST ROW */
.trust-row{
  border-top: 1px dashed var(--ca-line-2) !important;
  padding-top: 1.6rem !important;
  margin-top: 1.6rem !important;
  color: var(--ca-muted) !important;
  font-size: 1.2rem !important;
  font-style: italic;
  gap: 2.4rem;
  justify-content: center;
}
.trust-item{ color: var(--ca-muted) !important; font-size: 1.2rem !important; font-style: italic; }
.trust-item svg{ stroke: var(--ca-muted) !important; }

/* ══════════════════════════════════════════════════════════════════════
   ORDER SUMMARY — full-width banner at top, premium feel
   ══════════════════════════════════════════════════════════════════════ */
.sidebar-title{
  background: transparent !important;
  border: 1px solid var(--ca-line-2) !important;
  border-bottom: 1px solid var(--ca-line-2) !important;
  border-radius: var(--ca-r) var(--ca-r) 0 0 !important;
  box-shadow: none !important;
  padding: 1.2rem 1.8rem !important;
  margin: 0 !important;
  font-family: var(--serif) !important;
  font-weight: 600 !important;
  font-size: 1.65rem !important;
  letter-spacing: -0.015em !important;
  text-transform: none !important;
  color: var(--ca-ink) !important;
  flex-shrink: 0;
  display: flex; align-items: center; gap: 0.7rem;
}
.sidebar-title::after{ display: none !important; }
.sidebar-title svg{
  stroke: var(--ca-accent) !important;
  width: 1.9rem !important;
  height: 1.9rem !important;
}

.items{
  background: transparent !important;
  border-left: 1px solid var(--ca-line-2) !important;
  border-right: 1px solid var(--ca-line-2) !important;
  border-top: none !important;
  border-bottom: none !important;
  padding: 0.2rem 1.8rem !important;
  margin: 0 !important;
  overflow: visible !important;
  flex: 0 0 auto;
}
.item{
  display: grid !important;
  grid-template-columns: 4.4rem 1fr auto !important;
  gap: 1.2rem !important;
  padding: 0.9rem 0 !important;
  border-bottom: 1px dotted var(--ca-line-2) !important;
  border-top: none !important;
  align-items: center;
}
.item:first-child{ padding-top: 0.9rem !important; }
.item:last-child{ border-bottom: none !important; padding-bottom: 0.9rem !important; }
.item-img,
.item-img-wrap{
  width: 4.4rem !important;
  height: 4.4rem !important;
  background: var(--ca-paper) !important;
  border: 1px solid var(--ca-line) !important;
  border-radius: var(--ca-r) !important;
  position: relative;
}
.item-img svg{
  stroke: var(--ca-accent) !important;
  width: 2.4rem !important;
  height: 2.4rem !important;
  opacity: 0.7;
}
.item-name{
  color: var(--ca-ink) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-weight: 600 !important;
  font-size: 1.25rem !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
  line-height: 1.3 !important;
}
.item-variant{
  color: var(--ca-ink-2) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: 1.2rem !important;
  font-style: italic !important;
  font-weight: 400 !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
  margin-top: 0.2rem !important;
}
.item-price{
  color: var(--ca-ink) !important;
  font-family: 'DM Mono', monospace !important;
  font-weight: 600 !important;
  font-size: 1.3rem !important;
  white-space: nowrap;
}
.item-price.free{ color: var(--ca-success) !important; font-style: italic; }
.item-qty{
  background: var(--ca-accent) !important;
  color: #fff !important;
  border: 2px solid var(--ca-card) !important;
  min-width: 2.6rem !important;
  height: 2.6rem !important;
  font-family: 'DM Mono', monospace !important;
  font-size: 1.2rem !important;
  font-weight: 700 !important;
  top: -0.75rem !important;
  right: -0.75rem !important;
  padding: 0 0.5rem !important;
  box-shadow: 0 2px 4px rgba(168, 90, 42, 0.3);
}

/* DISCOUNT CODE */
.discount-code{
  background: transparent !important;
  border-left: 1px solid var(--ca-line-2) !important;
  border-right: 1px solid var(--ca-line-2) !important;
  border-top: 1px solid var(--ca-line-2) !important;
  border-bottom: none !important;
  border-radius: 0 !important;
  box-shadow: none !important;
  padding: 1.4rem 1.8rem !important;
  margin: 0 !important;
  flex-shrink: 0;
}
.discount-code-row{ gap: 0.6rem !important; display: flex; }
.discount-code-row input{
  flex: 1; min-width: 0;
  padding: 1rem 1.2rem !important;
  border: 1px solid var(--ca-line-2) !important;
  border-radius: var(--ca-r) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: 1.3rem !important;
  background: var(--ca-card-soft) !important;
  color: var(--ca-ink) !important;
  height: auto !important;
  transition: border-color .15s, background .15s;
}
.discount-code-row input::placeholder{ color: var(--ca-muted) !important; font-style: italic; }
.discount-code-row input:focus{
  outline: none !important;
  border-color: var(--ca-accent) !important;
  background: var(--ca-card) !important;
  box-shadow: 0 0 0 3px var(--ca-accent-soft) !important;
}
.discount-code-row button{
  flex: none;
  padding: 0 1.8rem !important;
  background: var(--ca-accent) !important;
  color: #fff !important;
  border: 1px solid var(--ca-accent) !important;
  border-radius: var(--ca-r) !important;
  font-family: var(--serif) !important;
  font-style: italic !important;
  font-weight: 600 !important;
  font-size: 1.5rem !important;
  height: auto !important;
  cursor: pointer;
  transition: background .15s;
}
.discount-code-row button:hover{ background: var(--ca-accent-deep) !important; }
.discount-code-row button.applied{
  background: var(--ca-success) !important;
  border-color: var(--ca-success) !important;
}
.discount-code-row button.applied:hover{ background: #3a5524 !important; }
.discount-code-msg.error{ color: #B23A3A !important; font-size: 1.15rem !important; font-weight: 500 !important; font-style: italic; }
.discount-code-msg.success{ color: var(--ca-success) !important; font-size: 1.15rem !important; font-weight: 500 !important; font-style: italic; }

/* TOTALS */
.totals{
  background: transparent !important;
  border: 1px solid var(--ca-line-2) !important;
  border-top: 1px solid var(--ca-line-2) !important;
  border-radius: 0 0 var(--ca-r) var(--ca-r) !important;
  box-shadow: none !important;
  padding: 1.4rem 1.8rem 1.8rem !important;
  margin: 0 !important;
  flex-shrink: 0;
}
.total-row{
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.5rem 0 !important;
  border: none !important;
  font-size: 1.35rem !important;
  color: var(--ca-ink-2) !important;
}
.total-label{
  color: var(--ca-ink-2) !important;
  font-family: 'DM Sans', sans-serif !important;
  font-weight: 400 !important;
}
.total-value{
  color: var(--ca-ink) !important;
  font-family: 'DM Mono', monospace !important;
  font-weight: 500 !important;
  font-feature-settings: "tnum";
}
.total-row.discount .total-label,
.total-row.discount .total-value{
  color: var(--ca-success) !important;
  font-weight: 600 !important;
}
.shipping-free{
  color: var(--ca-success) !important;
  font-family: var(--serif) !important;
  font-style: italic !important;
  font-size: 1.35rem !important;
  font-weight: 600 !important;
}

.total-row.grand{
  display: flex; align-items: baseline;
  margin-top: 0.8rem !important;
  padding: 1.3rem 0 0 !important;
  border-top: 1px solid var(--ca-line) !important;
}
.total-row.grand .total-label{
  font-family: var(--serif) !important;
  font-weight: 600 !important;
  font-size: 2rem !important;
  color: var(--ca-ink) !important;
  text-transform: none !important;
  letter-spacing: -0.01em !important;
}
.total-row.grand .total-value{
  font-family: var(--serif) !important;
  font-weight: 700 !important;
  font-size: 2.8rem !important;
  color: var(--ca-ink) !important;
  letter-spacing: -0.02em !important;
}
.currency-code{
  color: var(--ca-muted) !important;
  font-size: 1rem !important;
  font-weight: 600 !important;
  letter-spacing: 0.1em !important;
  margin-right: 0.5rem;
  font-family: 'DM Mono', monospace !important;
  text-transform: uppercase !important;
}

/* "Why choose us?" — at the bottom of the column */
.why-mobile{ display: none !important; }
.mobile-cart{ display: none !important; }
.why-desktop{
  display: block !important;
  margin: 0 !important;
  background: transparent !important;
  border: 1px solid var(--ca-line-2) !important;
  border-left: 3px solid var(--ca-accent) !important;
  border-radius: var(--ca-r) !important;
  box-shadow: none !important;
  padding: 2rem 2.4rem !important;
  flex-shrink: 0;
}
.why-desktop > div:first-child{
  font-family: var(--serif) !important;
  font-weight: 600 !important;
  font-size: 1.9rem !important;
  letter-spacing: -0.01em !important;
  color: var(--ca-ink) !important;
  text-align: left !important;
  margin-bottom: 1.3rem !important;
  text-transform: none !important;
}
.why-desktop ul{
  display: flex !important;
  flex-direction: column !important;
  gap: 1.1rem !important;
  padding: 0 !important;
  margin: 0 !important;
  list-style: none !important;
}
.why-desktop ul li{
  display: flex !important;
  align-items: flex-start !important;
  gap: 1.2rem !important;
  font-family: 'DM Sans', sans-serif !important;
  font-size: 1.25rem !important;
  line-height: 1.5 !important;
  color: var(--ca-ink-2) !important;
}
.why-desktop ul li svg{
  width: 2.6rem !important;
  height: 2.6rem !important;
  flex-shrink: 0 !important;
  margin-top: 0 !important;
  padding: 0.5rem !important;
  background: var(--ca-accent-tint) !important;
  border-radius: var(--ca-r-sm) !important;
  box-sizing: border-box;
  color: var(--ca-accent) !important;
}
.why-desktop ul li svg path,
.why-desktop ul li svg circle{ fill: var(--ca-accent) !important; }
.why-desktop ul li strong{ color: var(--ca-ink) !important; font-family: var(--serif) !important; }

/* ══════════════════════════════════════════════════════════════════════
   MOBILE — stack to single column (sidebar on top, form below)
   ══════════════════════════════════════════════════════════════════════ */
@media(max-width:980px){
  /* Hide decorative SVG illustration on mobile */
  .v2-art{ display: none !important; }
  html, body{ height: auto !important; overflow: auto !important; }
  .shell{ height: auto !important; display: block !important; overflow: visible !important; }
  .body{
    display: flex !important;
    flex-direction: column !important;
    max-width: none !important;
    padding: 1.6rem 1.4rem 4rem !important;
    overflow: visible !important;
    height: auto !important;
    min-height: 0;
    gap: 1.2rem !important;
  }
  .sidebar{
    position: static !important;
    top: auto !important;
    order: 1 !important;
    margin-bottom: 0 !important;
    height: auto !important;
    overflow: visible !important;
  }
  .items{ overflow: visible !important; max-height: none !important; }
  .form-col{
    order: 2 !important;
    margin-top: 0 !important;
    overflow: visible !important;
    height: auto !important;
    padding: 0 !important;
    gap: 1.2rem;
  }
  .section{ padding: 2rem 1.8rem !important; }
  .section-heading{ font-size: 2rem !important; }
  .step-num{ font-size: 2.2rem !important; }
  .sidebar-title{ font-size: 1.8rem !important; padding: 1.6rem 1.8rem !important; }
  .items{ padding: 0.4rem 1.8rem !important; }
  .discount-code{ padding: 1.4rem 1.8rem !important; }
  .totals{ padding: 1.4rem 1.8rem 1.8rem !important; }
}
@media(max-width:560px){
  .header{ padding: 1.6rem 1.6rem !important; }
  .header-name{ font-size: 1.85rem !important; }
  .header-logo{ width: 3.2rem !important; height: 3.2rem !important; }
  .header-secure .secure-text{ display: none !important; }
  .field-row{ display: block !important; }
  .field-row > .field{ margin-bottom: 1.2rem !important; }
  .field-row.cols-2.keep-cols{ display: grid !important; }
  .total-row.grand .total-value{ font-size: 2.4rem !important; }
  .total-row.grand .total-label{ font-size: 1.8rem !important; }
  .submit-btn{ font-size: 1.75rem !important; height: 5.8rem !important; }
}
"""


def main():
    txt = TARGET.read_text(encoding="utf-8")

    s = txt.find(START_MARKER)
    if s == -1:
        raise SystemExit("Start marker not found.")
    e = txt.find(END_MARKER, s)
    if e == -1:
        raise SystemExit("End marker (</style>) not found after start.")

    new_txt = txt[:s] + NEW_CSS + txt[e:]
    TARGET.write_text(new_txt, encoding="utf-8")
    print(f"OK — wrote {TARGET} ({len(new_txt)} bytes, {new_txt.count(chr(10))} lines)")


if __name__ == "__main__":
    main()
