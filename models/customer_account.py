"""
Customer "soft account" model — backs the optional password-prefill feature.

A customer can set a password during checkout (US stores). On a return visit
they sign in via /api/customer/lookup and we prefill their saved shipping
profile. This is NOT a Shopify account and is NOT used for order viewing —
it only stores contact/address fields + a PBKDF2 password hash.

Password hashing/verification lives in services/customer_accounts.py. The
stored `password_hash` is a self-describing string:
    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""
from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from database import Base


class CustomerAccount(Base):
    __tablename__ = "customer_accounts"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    email         = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)

    # Saved shipping/contact profile — latest checkout wins on update.
    first_name    = Column(String(100), nullable=True)
    last_name     = Column(String(100), nullable=True)
    phone         = Column(String(50),  nullable=True)
    address1      = Column(String(255), nullable=True)
    address2      = Column(String(255), nullable=True)
    city          = Column(String(100), nullable=True)
    province      = Column(String(100), nullable=True)
    postal_code   = Column(String(20),  nullable=True)
    country       = Column(String(2),   nullable=True)

    created_at    = Column(DateTime, server_default=func.now())
    updated_at    = Column(DateTime, server_default=func.now(), onupdate=func.now())
