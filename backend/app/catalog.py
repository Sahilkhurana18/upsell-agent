"""
UpsellAgent - Product Catalog & Compatibility Map
=====================================================
Deliberately static and deterministic. The LLM never chooses which
product to offer or at what price -- that would be exactly the kind
of ungoverned financial decision the brief's "Safety & Control"
requirement warns against. This module is the single source of truth
for "what pairs with what", editable by a human (the merchant), not
inferred by a model.

Prices are in paise (Razorpay's smallest currency unit for INR) to
match what the Orders/Payment Links APIs expect directly -- e.g.
199900 = Rs 1,999.00. Helper functions below convert for display.
"""

from dataclasses import dataclass


@dataclass
class Product:
    id: str
    name: str
    category: str
    price_paise: int  # e.g. 199900 = Rs 1,999.00
    description: str


CATALOG: dict[str, Product] = {
    "phone-x1": Product("phone-x1", "Aster Phone X1", "phone", 2499900,
                         "Flagship smartphone, 128GB, triple camera."),
    "laptop-air": Product("laptop-air", "Aster Laptop Air", "laptop", 5499900,
                           "Lightweight 14-inch laptop, 16GB RAM."),
    "watch-fit": Product("watch-fit", "Aster Watch Fit", "watch", 799900,
                          "Fitness smartwatch with heart-rate tracking."),

    "phone-case": Product("phone-case", "Shockproof Phone Case", "phone_accessory", 59900,
                           "Drop-protection case, fits Aster Phone X1."),
    "phone-charger": Product("phone-charger", "65W Fast Charger", "phone_accessory", 129900,
                              "Fast-charging adapter, USB-C."),
    "laptop-bag": Product("laptop-bag", "Padded Laptop Sleeve", "laptop_accessory", 149900,
                           "14-inch padded sleeve, water-resistant."),
    "laptop-mouse": Product("laptop-mouse", "Wireless Travel Mouse", "laptop_accessory", 89900,
                             "Compact wireless mouse, USB-C receiver."),
    "watch-band": Product("watch-band", "Extra Watch Band", "watch_accessory", 39900,
                           "Silicone replacement band, 3 colors."),
    "watch-charger": Product("watch-charger", "Magnetic Watch Charger", "watch_accessory", 49900,
                              "Magnetic charging dock for Aster Watch Fit."),
}

# category -> list of complementary categories, ordered by priority.
# The FIRST available product in the first matching category is offered.
# This ordering is a merchant/business decision, not something the AI infers.
COMPATIBILITY_MAP: dict[str, list[str]] = {
    "phone": ["phone_accessory"],
    "laptop": ["laptop_accessory"],
    "watch": ["watch_accessory"],
}


def get_product(product_id: str) -> Product | None:
    return CATALOG.get(product_id)


def find_upsell_candidate(purchased_product_id: str, variation_seed: str | None = None) -> Product | None:
    """
    Returns an upsell candidate for a purchased product, or None if no
    compatible product exists. Still fully deterministic and auditable --
    no LLM or randomness involved -- but when a category has more than
    one compatible product (e.g. phone case AND charger), the choice
    varies per `variation_seed` (typically the order_id) via a stable
    hash, rather than always returning the same first match. The same
    seed always reproduces the same result, which keeps the audit trail
    reproducible, while different customers see some variety across the
    guardrail-approved candidate set.
    """
    import hashlib

    purchased = CATALOG.get(purchased_product_id)
    if purchased is None:
        return None

    compatible_categories = COMPATIBILITY_MAP.get(purchased.category, [])
    for category in compatible_categories:
        candidates = [p for p in CATALOG.values() if p.category == category]
        if not candidates:
            continue
        if len(candidates) == 1 or not variation_seed:
            return candidates[0]
        # stable per-seed selection, not random -- same order_id always
        # yields the same candidate, reproducible for the audit trail
        index = int(hashlib.md5(variation_seed.encode()).hexdigest(), 16) % len(candidates)
        return candidates[index]
    return None


def paise_to_rupees_display(paise: int) -> str:
    return f"Rs {paise / 100:,.2f}"
