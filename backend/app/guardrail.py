"""
UpsellAgent - Guardrail Engine
==================================
Sits between "the decision engine found a compatible upsell product"
and "an actual Razorpay Payment Link gets created and sent". No
LLM involvement -- every check here is a plain function on plain data,
directly answering the brief's "Safety & Control: every financial
transaction must be bounded, explainable, and human-gated/permissioned"
requirement.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class GuardrailConfig:
    max_upsell_price_ratio: float = 0.5   # upsell price <= this fraction of original order value
    max_offers_per_customer_per_day: int = 1
    min_order_value_paise: int = 0        # skip upsell entirely below this order size
    require_manual_approval_above_paise: int = 200000  # Rs 2,000+ upsell needs human approval


@dataclass
class GuardrailResult:
    passed: bool
    requires_human_approval: bool
    reasons: list[str]


def evaluate_upsell_guardrail(
    order_value_paise: int,
    upsell_price_paise: int,
    offers_to_this_customer_today: int,
    config: GuardrailConfig,
) -> GuardrailResult:
    reasons: list[str] = []
    passed = True
    requires_human_approval = False

    if order_value_paise < config.min_order_value_paise:
        passed = False
        reasons.append(
            f"Order value ({order_value_paise/100:.2f}) is below the minimum "
            f"threshold for triggering an upsell ({config.min_order_value_paise/100:.2f})."
        )

    if offers_to_this_customer_today >= config.max_offers_per_customer_per_day:
        passed = False
        reasons.append(
            f"Customer has already received {offers_to_this_customer_today} upsell "
            f"offer(s) today (limit: {config.max_offers_per_customer_per_day})."
        )

    max_allowed_upsell = order_value_paise * config.max_upsell_price_ratio
    if upsell_price_paise > max_allowed_upsell:
        passed = False
        reasons.append(
            f"Upsell price ({upsell_price_paise/100:.2f}) exceeds "
            f"{config.max_upsell_price_ratio:.0%} of the original order value "
            f"({order_value_paise/100:.2f}) -- capped at {max_allowed_upsell/100:.2f}."
        )

    if passed and upsell_price_paise >= config.require_manual_approval_above_paise:
        requires_human_approval = True
        reasons.append(
            f"Upsell price ({upsell_price_paise/100:.2f}) meets the manual-approval "
            f"threshold ({config.require_manual_approval_above_paise/100:.2f}) -- "
            f"routing to merchant for approval before sending."
        )

    if passed and not reasons:
        reasons.append("All guardrail checks passed.")

    return GuardrailResult(passed=passed, requires_human_approval=requires_human_approval, reasons=reasons)
