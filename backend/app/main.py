"""
UpsellAgent - FastAPI Backend
=================================
Run with: uvicorn main:app --reload --port 8000

Endpoints:
  GET  /health
  POST /checkout/create-order      customer picks a product, we create a Razorpay Order
  POST /webhook/razorpay            Razorpay calls this when payment completes (signature-verified)
  GET  /orders                      list orders (dashboard)
  GET  /offers                      list upsell offers (dashboard)
  GET  /audit/{order_id}            full audit trail for one order
  GET  /summary                     dashboard header numbers
  GET  /guardrail-config / PUT      live-editable guardrail config
  POST /demo/simulate-failure       arms N simulated Razorpay API failures (for the failure-handling demo)
  POST /offers/{id}/check-conversion  poll whether a sent upsell link was paid
  POST /offers/{id}/approve          human approves a pending_approval offer -> sends the real link
  POST /chat                        ask a free-text question about the dashboard state
"""

import json
import os
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

load_dotenv()  # must run before any module that reads env vars at import time

import catalog
import narration
import razorpay_client as rc
from database import AuditLog, GuardrailConfigRow, Order, SessionLocal, UpsellOffer, get_db, init_db
from pipeline import check_offer_conversion, get_current_guardrail_config, process_paid_order

app = FastAPI(title="UpsellAgent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# checkout (customer-facing: creates a real Razorpay Order to pay against)
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    product_id: str
    customer_name: Optional[str] = None
    customer_contact: Optional[str] = None
    customer_email: Optional[str] = None


@app.post("/checkout/create-order")
def create_order(req: CreateOrderRequest, db: Session = Depends(get_db)):
    product = catalog.get_product(req.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown product_id '{req.product_id}'")

    try:
        razorpay_order = rc.create_order(
            amount_paise=product.price_paise,
            receipt=f"order-{product.id}-{int(datetime.utcnow().timestamp())}",
            notes={"product_id": product.id},
        )
    except rc.RazorpayAPIError as e:
        raise HTTPException(status_code=502, detail=f"Razorpay order creation failed: {e}")

    order = Order(
        order_id=razorpay_order["id"],
        product_id=product.id,
        product_name=product.name,
        amount_paise=product.price_paise,
        customer_name=req.customer_name,
        customer_contact=req.customer_contact,
        customer_email=req.customer_email,
        status="created",
    )
    db.add(order)
    db.commit()

    return {
        "razorpay_order_id": razorpay_order["id"],
        "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID"),
        "amount_paise": product.price_paise,
        "currency": "INR",
        "product_name": product.name,
    }


# ---------------------------------------------------------------------------
# webhook (Razorpay -> us, signature-verified)
# ---------------------------------------------------------------------------

@app.post("/webhook/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        valid = rc.verify_webhook_signature(raw_body, signature)
    except rc.RazorpayAPIError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not valid:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    payload = json.loads(raw_body)
    event = payload.get("event")

    if event == "order.paid":
        order_entity = payload["payload"]["order"]["entity"]
        payment_entity = payload["payload"]["payment"]["entity"]
        order_id = order_entity["id"]

        order = db.query(Order).filter(Order.order_id == order_id).first()
        if order is None:
            # order created outside our own /checkout/create-order flow --
            # not expected in this demo, but handle gracefully rather than 500
            return {"status": "ignored", "reason": "unknown order_id"}

        order.status = "paid"
        order.razorpay_payment_id = payment_entity["id"]
        db.commit()

        # Idempotency guard: Razorpay retries webhook delivery with backoff
        # for up to 24 hours if a delivery isn't acknowledged cleanly, so
        # this endpoint WILL receive duplicate deliveries for the same
        # order in practice. Without this check, each retry would re-run
        # the full pipeline and create a duplicate upsell offer / duplicate
        # Payment Link -- already-processed orders are a no-op here.
        existing_offer = db.query(UpsellOffer).filter(UpsellOffer.source_order_id == order_id).first()
        if existing_offer is not None:
            return {"status": "already_processed", "order_id": order_id}

        process_paid_order(db, order)
        return {"status": "processed"}

    return {"status": "ignored", "reason": f"unhandled event type '{event}'"}


# ---------------------------------------------------------------------------
# dashboard: orders / offers / audit / summary
# ---------------------------------------------------------------------------

@app.get("/orders")
def list_orders(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(Order).order_by(Order.created_at.desc()).limit(limit).all()
    return [
        {
            "order_id": o.order_id,
            "created_at": o.created_at.isoformat(),
            "product_name": o.product_name,
            "amount_paise": o.amount_paise,
            "status": o.status,
            "customer_name": o.customer_name,
        }
        for o in rows
    ]


@app.get("/offers")
def list_offers(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(UpsellOffer).order_by(UpsellOffer.created_at.desc()).limit(limit).all()
    return [
        {
            "id": o.id,
            "source_order_id": o.source_order_id,
            "offered_product_name": o.offered_product_name,
            "offer_price_paise": o.offer_price_paise,
            "created_at": o.created_at.isoformat(),
            "guardrail_passed": o.guardrail_passed,
            "requires_human_approval": o.requires_human_approval,
            "status": o.status,
            "message_text": o.message_text,
            "payment_link_url": o.payment_link_url,
            "converted_amount_paise": o.converted_amount_paise,
        }
        for o in rows
    ]


@app.get("/summary")
def summary(db: Session = Depends(get_db)):
    orders = db.query(Order).filter(Order.status == "paid").all()
    offers = db.query(UpsellOffer).all()

    total_order_revenue = sum(o.amount_paise for o in orders)
    total_upsell_revenue = sum(o.converted_amount_paise for o in offers)
    sent_count = sum(1 for o in offers if o.status in ("sent", "converted", "expired", "cancelled"))
    converted_count = sum(1 for o in offers if o.status == "converted")

    return {
        "total_paid_orders": len(orders),
        "total_order_revenue_paise": total_order_revenue,
        "total_offers_made": len(offers),
        "total_offers_sent": sent_count,
        "total_offers_converted": converted_count,
        "total_upsell_revenue_paise": total_upsell_revenue,
        "conversion_rate": round(converted_count / sent_count, 4) if sent_count > 0 else 0.0,
    }


@app.get("/pipeline/funnel")
def pipeline_funnel(db: Session = Depends(get_db)):
    offers = db.query(UpsellOffer).all()
    return {
        "orders_processed": len(offers),
        "candidate_found": sum(1 for o in offers if o.offered_product_id != "none"),
        "guardrail_passed": sum(1 for o in offers if o.guardrail_passed),
        "sent": sum(1 for o in offers if o.status in ("sent", "converted", "expired", "cancelled")),
        "converted": sum(1 for o in offers if o.status == "converted"),
    }


@app.get("/audit/{order_id}")
def get_audit(order_id: str, db: Session = Depends(get_db)):
    entries = db.query(AuditLog).filter(AuditLog.order_id == order_id).order_by(AuditLog.created_at.asc()).all()
    if not entries:
        raise HTTPException(status_code=404, detail="No audit trail found for this order.")
    return [
        {"created_at": e.created_at.isoformat(), "stage": e.stage, "detail": e.detail, "success": e.success}
        for e in entries
    ]


# ---------------------------------------------------------------------------
# guardrail config
# ---------------------------------------------------------------------------

class GuardrailConfigUpdate(BaseModel):
    max_upsell_price_ratio: Optional[float] = None
    max_offers_per_customer_per_day: Optional[int] = None
    min_order_value_paise: Optional[int] = None
    require_manual_approval_above_paise: Optional[int] = None


@app.get("/guardrail-config")
def get_guardrail_config(db: Session = Depends(get_db)):
    return vars(get_current_guardrail_config(db))


@app.put("/guardrail-config")
def update_guardrail_config(update: GuardrailConfigUpdate, db: Session = Depends(get_db)):
    row = db.query(GuardrailConfigRow).first()
    if row is None:
        row = GuardrailConfigRow(id=1)
        db.add(row)
    for field, value in update.dict(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return {
        "max_upsell_price_ratio": row.max_upsell_price_ratio,
        "max_offers_per_customer_per_day": row.max_offers_per_customer_per_day,
        "min_order_value_paise": row.min_order_value_paise,
        "require_manual_approval_above_paise": row.require_manual_approval_above_paise,
    }


# ---------------------------------------------------------------------------
# offer actions: approve pending, check conversion
# ---------------------------------------------------------------------------

@app.post("/offers/{offer_id}/check-conversion")
def check_conversion(offer_id: int, db: Session = Depends(get_db)):
    offer = db.query(UpsellOffer).filter(UpsellOffer.id == offer_id).first()
    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    updated = check_offer_conversion(db, offer)
    return {"id": updated.id, "status": updated.status}


@app.post("/offers/{offer_id}/approve")
def approve_offer(offer_id: int, db: Session = Depends(get_db)):
    offer = db.query(UpsellOffer).filter(UpsellOffer.id == offer_id).first()
    if offer is None:
        raise HTTPException(status_code=404, detail="Offer not found.")
    if offer.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Offer is not pending approval (status: {offer.status}).")

    order = db.query(Order).filter(Order.order_id == offer.source_order_id).first()
    try:
        link = rc.create_payment_link(
            amount_paise=offer.offer_price_paise,
            description=f"Complete your order: {offer.offered_product_name}",
            customer_name=order.customer_name if order else None,
            customer_contact=order.customer_contact if order else None,
            customer_email=order.customer_email if order else None,
            notes={"source_order_id": offer.source_order_id, "type": "upsell_offer_approved"},
        )
        offer.payment_link_id = link["id"]
        offer.payment_link_url = link["short_url"]
        offer.status = "sent"
        db.add(AuditLog(order_id=offer.source_order_id, stage="link_created",
                         detail=f"Merchant-approved Payment Link created: {link['id']}"))
        db.commit()
        return {"id": offer.id, "status": offer.status, "payment_link_url": offer.payment_link_url}
    except rc.RazorpayAPIError as e:
        db.add(AuditLog(order_id=offer.source_order_id, stage="api_failure",
                         detail=f"Failed to create approved Payment Link: {e}", success=False))
        db.commit()
        raise HTTPException(status_code=502, detail=f"Razorpay call failed: {e}")


# ---------------------------------------------------------------------------
# demo: failure injection (for the Failure Handling requirement)
# ---------------------------------------------------------------------------

class SimulateFailureRequest(BaseModel):
    count: int = 3


@app.post("/demo/simulate-failure")
def simulate_failure(req: SimulateFailureRequest):
    """Arms the next N Razorpay API calls to fail with a simulated timeout,
    so the retry-with-backoff + graceful-audit-failure path can be shown
    live without needing Razorpay's real API to fail on cue."""
    rc.arm_simulated_failure(req.count)
    return {"armed_failures": req.count}


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    question: str


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    summ = summary(db)
    prompt_context = {
        "total_paid_orders": summ["total_paid_orders"],
        "total_order_revenue": summ["total_order_revenue_paise"] / 100,
        "total_upsell_revenue": summ["total_upsell_revenue_paise"] / 100,
        "conversion_rate": summ["conversion_rate"],
    }
    answer = narration._call_llm(
        "You are UpsellAgent, an assistant summarizing e-commerce upsell performance. "
        "Answer ONLY using this data, 2-3 sentences, no preamble. Always write currency "
        "as 'Rs X' (e.g. 'Rs 24,999'), never '$' or any other symbol:\n"
        f"{prompt_context}\n\nQuestion: {req.question}"
    )
    if not answer:
        answer = (
            f"So far: {summ['total_paid_orders']} paid orders, "
            f"Rs {summ['total_order_revenue_paise']/100:,.2f} in original revenue, "
            f"Rs {summ['total_upsell_revenue_paise']/100:,.2f} in upsell revenue "
            f"({summ['conversion_rate']:.1%} conversion rate on sent offers)."
        )
    return {"answer": answer}
