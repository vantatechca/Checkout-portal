-- ─────────────────────────────────────────────────────────────────
-- Fix schema drift + add customer email feature columns
-- Safe to run multiple times (uses IF NOT EXISTS)
-- ─────────────────────────────────────────────────────────────────

-- 1. Pre-existing missing discount columns on orders
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS discount_code VARCHAR(100) DEFAULT NULL AFTER subtotal;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS discount_pct DECIMAL(5,2) DEFAULT 0 AFTER discount_code;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS discount_amount DECIMAL(10,2) DEFAULT 0 AFTER discount_pct;

-- 2. Customer email tracking on orders (for reminder/underpaid feature)
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS last_customer_email_at DATETIME DEFAULT NULL;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS customer_emails_sent INT DEFAULT 0;

-- 3. Received amount + underpaid status on Interac payments
ALTER TABLE interac_payments
    ADD COLUMN IF NOT EXISTS received_amount DECIMAL(10,2) DEFAULT NULL AFTER expected_amount;

ALTER TABLE interac_payments
    MODIFY COLUMN status ENUM('waiting','matched','unmatched','manual','underpaid')
    DEFAULT 'waiting';

-- 4. Received amount + underpaid status on Zelle payments
ALTER TABLE zelle_payments
    ADD COLUMN IF NOT EXISTS received_amount DECIMAL(10,2) DEFAULT NULL AFTER expected_amount;

ALTER TABLE zelle_payments
    MODIFY COLUMN status ENUM('waiting','matched','unmatched','manual','underpaid')
    DEFAULT 'waiting';