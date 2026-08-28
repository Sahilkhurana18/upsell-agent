"""
UpsellAgent - Database layer
================================
SQLite, same reasoning as the RecoverAI build: zero setup, zero cost,
swappable to Postgres later without touching the models or queries.
"""

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "upsell_agent.db"
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH.as_posix()}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Order(Base):
    __tablename__ = "orders"

    order_id = Column(String, primary_key=True)          # Razorpay order id
    razorpay_payment_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    product_id = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    amount_paise = Column(Integer, nullable=False)
    customer_name = Column(String, nullable=True)
    customer_contact = Column(String, nullable=True)
    customer_email = Column(String, nullable=True)
    status = Column(String, default="created")            # created | paid | failed


class UpsellOffer(Base):
    __tablename__ = "upsell_offers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_order_id = Column(String, nullable=False, index=True)
    offered_product_id = Column(String, nullable=False)
    offered_product_name = Column(String, nullable=False)
    offer_price_paise = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    guardrail_passed = Column(Boolean, nullable=False)
    requires_human_approval = Column(Boolean, default=False)
    guardrail_reasons = Column(Text)   # JSON-encoded list

    payment_link_id = Column(String, nullable=True)
    payment_link_url = Column(String, nullable=True)
    message_text = Column(Text, nullable=True)             # LLM-generated offer copy

    status = Column(String, default="skipped")              # sent | skipped | blocked | pending_approval | converted | expired
    converted_amount_paise = Column(Integer, default=0)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    stage = Column(String, nullable=False)   # webhook_received | candidate_found | guardrail_checked | link_created | api_failure | conversion_checked
    detail = Column(Text, nullable=False)
    success = Column(Boolean, default=True)


class GuardrailConfigRow(Base):
    __tablename__ = "guardrail_config"

    id = Column(Integer, primary_key=True, default=1)
    max_upsell_price_ratio = Column(Float, default=0.5)
    max_offers_per_customer_per_day = Column(Integer, default=1)
    min_order_value_paise = Column(Integer, default=0)
    require_manual_approval_above_paise = Column(Integer, default=200000)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
