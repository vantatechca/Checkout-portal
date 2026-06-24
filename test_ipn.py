import hmac
import hashlib
import json
import requests

secret = "oo0I2B63CEkl0x6cV2PL+SY9HyGoiVGK"

# Replace with a real order ID from your DB
payload = {
    "payment_id": "test123",
    "payment_status": "finished",
    "order_id": "ORD-Q40YQ924",   # ← put a real pending altcoin order ID here
    "price_amount": "100.00",
    "actually_paid_amount": "100.00",
    "pay_currency": "eth"
}

sorted_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
sig = hmac.new(secret.encode(), sorted_str.encode(), hashlib.sha512).hexdigest()

resp = requests.post(
    "https://pepscheckoutportal.com/webhooks/nowpayments",
    json=payload,
    headers={
        "Content-Type": "application/json",
        "x-nowpayments-sig": sig
    }
)
print(resp.status_code, resp.text)