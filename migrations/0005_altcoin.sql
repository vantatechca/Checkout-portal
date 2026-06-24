docker exec checkout-server-vps-db-1 mariadb -u checkout_user -pPepsCheckout123*** checkout_db -e "SHOW TABLES LIKE 'nowpayments%'; DESCRIBE nowpayments_invoices; DESCRIBE orders;"-- Add NowPayments altcoin integration
CREATE TABLE IF NOT EXISTS nowpayments_invoices (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    order_id      VARCHAR(20) UNIQUE,
    np_invoice_id VARCHAR(255) UNIQUE NOT NULL,
    np_payment_id VARCHAR(255) NULL,
    invoice_url   TEXT NULL,
    coin          VARCHAR(50) NULL,
    amount_fiat   DECIMAL(10,2) NOT NULL,
    received_fiat DECIMAL(10,2) NULL,
    status        VARCHAR(50) DEFAULT 'waiting',
    settled_at    DATETIME NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE orders 
  MODIFY COLUMN payment_method ENUM('card','interac','crypto','zelle','altcoin') NOT NULL;

SELECT 'Altcoin migration applied' AS result;
