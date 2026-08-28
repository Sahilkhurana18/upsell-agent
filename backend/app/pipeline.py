"""
UpsellAgent - Pipeline Orchestrator
=======================================
Runs when a real Razorpay 'order.paid' / 'payment.captured' webhook
arrives. Deterministic sequence, same design philosophy as before:

  1. Look up the purchased product
  2. Find a compatible upsell candidate (catalog.py -- deterministic)
  3. Check the guardrail (guardrail.py -- deterministic)
  4. If approved: create a REAL Razorpay Payment Link (razorpay_client.py)
  5. Generate customer-facing copy + audit reasoning (narration.py -- LLM,
     narration only, never the decision)
  6. Log every stage to the audit trail, including any API failure

Every external Razorpay call is wrapped in try/except so an API
failure produces an audited, graceful failure record -- never a crash.
"""

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

import catalog
import narration
import razorpay_client as rc
from database import AuditLog, GuardrailConfigRow, Order, UpsellOffer
from guardrail import GuardrailConfig, evaluate_upsell_guardrail


def _log(db: Session, order_id: str, stage: str, detail: str, success: bool = True):
    db.add(AuditLog(order_id=order_id, stage=stage, detail=detail, success=success))


def get_current_guardrail_config(db: Session) -> GuardrailConfig:
    row = db.query(GuardrailConfigRow).first()
    if row is None:
        return GuardrailConfig()
    return GuardrailConfig(
        max_upsell_price_ratio=row.max_upsell_price_ratio,
        max_offers_per_customer_per_day=row.max_offers_per_customer_per_day,
        min_order_value_paise=row.min_order_value_paise,
        require_manual_approval_above_paise=row.require_manual_approval_above_paise,
    )


def _offers_to_customer_today(db: Session, customer_contact: str | None, customer_email: str | None) -> int:
    if not customer_contact and not customer_email:
        return 0
    since = datetime.utcnow() - timedelta(days=1)
    q = db.query(UpsellOffer).join(Order, UpsellOffer.source_order_id == Order.order_id).filter(
        UpsellOffer.created_at >= since,
        UpsellOffer.status == "sent",
    )
    if customer_contact:
        q = q.filter(Order.customer_contact == customer_contact)
    elif customer_email:
        q = q.filter(Order.customer_email == customer_email)
    return q.count()


def process_paid_order(db: Session, order: Order) -> UpsellOffer:
    """
    Main entry point, called after a webhook confirms an order was paid.
    Always returns an UpsellOffer row (even if skipped/blocked), and
    always writes a full audit trail regardless of outcome.
    """
    _log(db, order.order_id, "webhook_received",
         f"Order {order.order_id} confirmed paid: {order.product_name}, "
         f"Rs {order.amount_paise/100:.2f}")

    candidate = catalog.find_upsell_candidate(order.product_id)
    if candidate is None:
        _log(db, order.order_id, "candidate_found",
             f"No compatible upsell product found for '{order.product_id}'.", success=True)
        offer = UpsellOffer(
            source_order_id=order.order_id,
            offered_product_id="none",
            offered_product_name="none",
            offer_price_paise=0,
            guardrail_passed=False,
            guardrail_reasons=json.dumps(["No compatible product in catalog."]),
            status="skipped",
        )
        db.add(offer)
        db.commit()
        return offer

    _log(db, order.order_id, "candidate_found",
         f"Compatible upsell candidate: {candidate.name} ({catalog.paise_to_rupees_display(candidate.price_paise)})")

    config = get_current_guardrail_config(db)
    offers_today = _offers_to_customer_today(db, order.customer_contact, order.customer_email)
    guardrail_result = evaluate_upsell_guardrail(
        order_value_paise=order.amount_paise,
        upsell_price_paise=candidate.price_paise,
        offers_to_this_customer_today=offers_today,
        config=config,
    )

    _log(db, order.order_id, "guardrail_checked",
         f"Guardrail passed={guardrail_result.passed}, "
         f"requires_approval={guardrail_result.requires_human_approval}: "
         f"{'; '.join(guardrail_result.reasons)}")

    reasoning = narration.generate_audit_reasoning(
        order.product_name, candidate.name, guardrail_result.passed, guardrail_result.reasons
    )

    offer = UpsellOffer(
        source_order_id=order.order_id,
        offered_product_id=candidate.id,
        offered_product_name=candidate.name,
        offer_price_paise=candidate.price_paise,
        guardrail_passed=guardrail_result.passed,
        requires_human_approval=guardrail_result.requires_human_approval,
        guardrail_reasons=json.dumps(guardrail_result.reasons),
    )

    if not guardrail_result.passed:
        offer.status = "blocked"
        offer.message_text = reasoning
        db.add(offer)
        db.commit()
        return offer

    if guardrail_result.requires_human_approval:
        offer.status = "pending_approval"
        offer.message_text = reasoning
        db.add(offer)
        db.commit()
        return offer

    # Guardrail approved -- create a REAL Razorpay test-mode Payment Link
    try:
        link = rc.create_payment_link(
            amount_paise=candidate.price_paise,
            description=f"Complete your order: {candidate.name}",
            customer_name=order.customer_name,
            customer_contact=order.customer_contact,
            customer_email=order.customer_email,
            notes={"source_order_id": order.order_id, "type": "upsell_offer"},
        )
        message = narration.generate_offer_message(
            order.product_name, candidate.name, catalog.paise_to_rupees_display(candidate.price_paise)
        )
        offer.payment_link_id = link["id"]
        offer.payment_link_url = link["short_url"]
        offer.message_text = message
        offer.status = "sent"
        _log(db, order.order_id, "link_created",
             f"Payment Link created: {link['id']} -> {link['short_url']}")
    except rc.RazorpayAPIError as e:
        offer.status = "blocked"
        offer.message_text = reasoning
        _log(db, order.order_id, "api_failure",
             f"Failed to create Payment Link after retries: {e}", success=False)

    db.add(offer)
    db.commit()
    return offer


def check_offer_conversion(db: Session, offer: UpsellOffer) -> UpsellOffer:
    """Polls Razorpay for whether a sent upsell Payment Link has been paid.
    Call this periodically (or on-demand from the dashboard) since Payment
    Link payment confirmation isn't pushed back through the original
    order's webhook."""
    if offer.status != "sent" or not offer.payment_link_id:
        return offer
    try:
        link = rc.fetch_payment_link(offer.payment_link_id)
        _log(db, offer.source_order_id, "conversion_checked",
             f"Payment link {offer.payment_link_id} status: {link.get('status')}")
        if link.get("status") == "paid":
            offer.status = "converted"
            offer.converted_amount_paise = offer.offer_price_paise
        elif link.get("status") in ("cancelled", "expired"):
            offer.status = link["status"]
        db.commit()
    except rc.RazorpayAPIError as e:
        _log(db, offer.source_order_id, "api_failure",
             f"Failed to check payment link status: {e}", success=False)
        db.commit()
    return offer
