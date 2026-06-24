-- ─────────────────────────────────────────────────────────────────
-- WIPE ALL ORDER DATA (local dev only)
-- Resets auto-increment IDs back to 1
-- ─────────────────────────────────────────────────────────────────

-- Disable FK checks so order of deletion doesn't matter
SET FOREIGN_KEY_CHECKS = 0;

DELETE FROM order_items;
DELETE FROM interac_payments;
DELETE FROM zelle_payments;
DELETE FROM crypto_invoices;
DELETE FROM orders;

-- Reset auto-increment counters
ALTER TABLE order_items      AUTO_INCREMENT = 1;
ALTER TABLE interac_payments AUTO_INCREMENT = 1;
ALTER TABLE zelle_payments   AUTO_INCREMENT = 1;
ALTER TABLE crypto_invoices  AUTO_INCREMENT = 1;

SET FOREIGN_KEY_CHECKS = 1;

-- Confirmation counts (should all return 0)
SELECT 'orders'           AS table_name, COUNT(*) AS remaining FROM orders
UNION ALL
SELECT 'order_items',      COUNT(*) FROM order_items
UNION ALL
SELECT 'interac_payments', COUNT(*) FROM interac_payments
UNION ALL
SELECT 'zelle_payments',   COUNT(*) FROM zelle_payments
UNION ALL
SELECT 'crypto_invoices',  COUNT(*) FROM crypto_invoices;