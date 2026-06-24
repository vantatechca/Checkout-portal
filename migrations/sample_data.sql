-- ─────────────────────────────────────────────────────────────────
-- Sample test data for email feature
-- Creates 1 brand + 4 test orders covering all email scenarios
-- IMPORTANT: Replace the email addresses below with YOUR Resend
-- signup email — onboarding@resend.dev only delivers to that address
-- ─────────────────────────────────────────────────────────────────

-- Wipe any existing test data (safe — only touches ORD-TEST*)
DELETE FROM order_items      WHERE order_id LIKE 'ORD-TEST%';
DELETE FROM interac_payments WHERE order_id LIKE 'ORD-TEST%';
DELETE FROM zelle_payments   WHERE order_id LIKE 'ORD-TEST%';
DELETE FROM orders           WHERE id       LIKE 'ORD-TEST%';
DELETE FROM brands           WHERE domain = 'localhost';

-- ─── Brand bound to localhost ────────────────────────────────────
INSERT INTO brands (
  domain, store_name, accent_color, accent_hover,
  interac_email, interac_discount, crypto_discount, active
) VALUES (
  'localhost', 'Local Test Peptides', '#dd1d1d', '#b01515',
  'payments@floridapeps.uk', 5.00, 10.00, 1
);

-- ─── Order 1: Pending Interac (CAD) — for "Send Reminder" test ───
INSERT INTO orders (
  id, brand_id, store_name, email, first_name, last_name, phone,
  address1, city, province, postal_code, country,
  subtotal, total, currency, payment_method, payment_status, source_domain
) VALUES (
  'ORD-TEST0001', 1, 'Local Test Peptides',
  'vantatechca@gmail.com',
  'Tiffany', 'Pierce', '+1-416-555-0101',
  '123 Yonge St', 'Toronto', 'ON', 'M5B 2H1', 'CA',
  185.00, 185.00, 'CAD', 'interac', 'pending', 'localhost'
);

INSERT INTO order_items (order_id, product_id, title, variant, qty, price, total) VALUES
('ORD-TEST0001', 'bpc-157', 'BPC-157',  '5mg',  1, 65.00,  65.00),
('ORD-TEST0001', 'tb-500',  'TB-500',   '5mg',  1, 90.00,  90.00),
('ORD-TEST0001', 'bac-h2o', 'BAC Water','30ml', 2, 15.00,  30.00);

INSERT INTO interac_payments (order_id, expected_amount, status)
VALUES ('ORD-TEST0001', 185.00, 'waiting');

-- ─── Order 2: Pending Zelle (USD) — for US "Send Reminder" test ──
INSERT INTO orders (
  id, brand_id, store_name, email, first_name, last_name, phone,
  address1, city, province, postal_code, country,
  subtotal, total, currency, payment_method, payment_status, source_domain
) VALUES (
  'ORD-TEST0002', 1, 'Local Test Peptides',
  'YOUR-RESEND-EMAIL@gmail.com',
  'Marcus', 'Johnson', '+1-305-555-0202',
  '500 Ocean Dr', 'Miami', 'FL', '33139', 'US',
  240.00, 240.00, 'USD', 'zelle', 'pending', 'localhost'
);

INSERT INTO order_items (order_id, product_id, title, variant, qty, price, total) VALUES
('ORD-TEST0002', 'sema',     'Semaglutide',  '5mg',  1, 145.00, 145.00),
('ORD-TEST0002', 'tirz',     'Tirzepatide',  '10mg', 1,  85.00,  85.00),
('ORD-TEST0002', 'bac-h2o',  'BAC Water',    '30ml', 2,   5.00,  10.00);

INSERT INTO zelle_payments (order_id, expected_amount, status)
VALUES ('ORD-TEST0002', 240.00, 'waiting');

-- ─── Order 3: Pending Interac — for "Mark Underpaid" test ────────
INSERT INTO orders (
  id, brand_id, store_name, email, first_name, last_name, phone,
  address1, city, province, postal_code, country,
  subtotal, total, currency, payment_method, payment_status, source_domain
) VALUES (
  'ORD-TEST0003', 1, 'Local Test Peptides',
  'YOUR-RESEND-EMAIL@gmail.com',
  'Sarah', 'Williams', '+1-604-555-0303',
  '789 Granville St', 'Vancouver', 'BC', 'V6Z 1K3', 'CA',
  320.00, 320.00, 'CAD', 'interac', 'pending', 'localhost'
);

INSERT INTO order_items (order_id, product_id, title, variant, qty, price, total) VALUES
('ORD-TEST0003', 'ghk-cu',  'GHK-Cu',     '50mg', 1, 110.00, 110.00),
('ORD-TEST0003', 'ipamor',  'Ipamorelin', '5mg',  2, 75.00,  150.00),
('ORD-TEST0003', 'bac-h2o', 'BAC Water',  '30ml', 4, 15.00,   60.00);

INSERT INTO interac_payments (order_id, expected_amount, status)
VALUES ('ORD-TEST0003', 320.00, 'waiting');

-- ─── Order 4: Pending Zelle — already emailed once (history test) ─
INSERT INTO orders (
  id, brand_id, store_name, email, first_name, last_name, phone,
  address1, city, province, postal_code, country,
  subtotal, total, currency, payment_method, payment_status, source_domain,
  last_customer_email_at, customer_emails_sent
) VALUES (
  'ORD-TEST0004', 1, 'Local Test Peptides',
  'vantatechca@gmail.com',
  'David', 'Chen', '+1-212-555-0404',
  '350 5th Ave', 'New York', 'NY', '10118', 'US',
  155.00, 155.00, 'USD', 'zelle', 'pending', 'localhost',
  DATE_SUB(NOW(), INTERVAL 2 DAY), 1
);

INSERT INTO order_items (order_id, product_id, title, variant, qty, price, total) VALUES
('ORD-TEST0004', 'mots-c',  'MOTS-c',    '10mg', 1, 95.00, 95.00),
('ORD-TEST0004', 'bac-h2o', 'BAC Water', '30ml', 4, 15.00, 60.00);

INSERT INTO zelle_payments (order_id, expected_amount, status)
VALUES ('ORD-TEST0004', 155.00, 'waiting');