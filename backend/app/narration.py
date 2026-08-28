"""
UpsellAgent - Narration Layer
=================================
The ONLY place an LLM is called. Its job: write a short, personalized
upsell offer message, and explain the reasoning in plain English for
the audit trail. It is given the product names and prices as fixed
facts -- it cannot alter them, and it never picks which product to
offer (that's catalog.py) or whether the offer is allowed to send
(that's guardrail.py). Falls back to a template if no API key is set
or the call fails, so the pipeline never blocks on an LLM outage.
"""

import os
import sys

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

_gemini_client = None


def _get_gemini():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    if not GEMINI_API_KEY:
        print("[narration.py] GEMINI_API_KEY not set -- using template fallback.", file=sys.stderr)
        return None
    try:
        from google import genai
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        return _gemini_client
    except Exception as e:
        print(f"[narration.py] Failed to initialize Gemini client: {e!r}", file=sys.stderr)
        return None


def _call_llm(prompt: str) -> str | None:
    client = _get_gemini()
    if client is None:
        return None
    try:
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return response.text.strip()
    except Exception as e:
        print(f"[narration.py] Gemini call failed, falling back to template: {e!r}", file=sys.stderr)
        return None


def _template_offer_message(purchased_name: str, offer_name: str, offer_price_display: str) -> str:
    return (
        f"Since you just picked up the {purchased_name}, you might like the "
        f"{offer_name} to go with it -- available now for {offer_price_display}."
    )


def generate_offer_message(purchased_product_name: str, offer_product_name: str,
                            offer_price_display: str) -> str:
    """Personalized customer-facing message for the upsell offer."""
    prompt = (
        "Write a short (1-2 sentence), friendly, non-pushy upsell message for an "
        "e-commerce customer who just bought a product. Do not invent a price or "
        "product name beyond what's given below -- use exactly what's provided. "
        "No preamble, just the message text.\n\n"
        f"Customer just bought: {purchased_product_name}\n"
        f"Complementary product being offered: {offer_product_name}\n"
        f"Offer price: {offer_price_display}\n"
    )
    result = _call_llm(prompt)
    return result if result else _template_offer_message(purchased_product_name, offer_product_name, offer_price_display)


def _template_reasoning(purchased_name: str, offer_name: str, guardrail_passed: bool, reasons: list[str]) -> str:
    base = f"Customer bought {purchased_name}. Compatibility map suggests {offer_name} as a complementary product."
    if guardrail_passed:
        return base + " Guardrail approved: " + " ".join(reasons)
    return base + " Guardrail blocked this offer: " + " ".join(reasons)


def generate_audit_reasoning(purchased_product_name: str, offer_product_name: str,
                              guardrail_passed: bool, guardrail_reasons: list[str]) -> str:
    """Explanation shown in the audit trail -- for a human reviewing why
    this offer was (or wasn't) sent."""
    prompt = (
        "In 2-3 plain sentences, explain why this upsell offer decision was made, "
        "for an internal audit log. Use only the facts given -- do not add "
        "speculation.\n\n"
        f"Purchased product: {purchased_product_name}\n"
        f"Candidate upsell product: {offer_product_name}\n"
        f"Guardrail passed: {guardrail_passed}\n"
        f"Guardrail reasons: {'; '.join(guardrail_reasons)}\n"
    )
    result = _call_llm(prompt)
    return result if result else _template_reasoning(purchased_product_name, offer_product_name, guardrail_passed, guardrail_reasons)
