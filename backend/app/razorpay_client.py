"""
UpsellAgent - Razorpay API Client
=====================================
Thin wrapper around Razorpay's REST API (Orders, Payment Links,
webhook signature verification). Uses plain `requests` against
https://api.razorpay.com/v1 directly rather than the official SDK,
so the actual HTTP calls are transparent and easy to reason about.

Includes deliberate retry-with-backoff logic and a demo failure-
injection hook (RAZORPAY_SIMULATE_FAILURE env var) -- this exists
specifically to satisfy the brief's "Failure Handling" requirement:
demonstrate at least one failure mode (API drop) handled gracefully,
not just claimed in a slide.
"""

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET")

BASE_URL = "https://api.razorpay.com/v1"

# Demo/testing hook only -- when set to a positive integer N, the next N
# calls to `_request` raise a simulated timeout before actually calling
# Razorpay, so the retry/backoff path can be demonstrated on demand
# without needing Razorpay's real API to fail on cue.
_SIMULATED_FAILURES_REMAINING = {"count": 0}


def arm_simulated_failure(n: int = 1):
    """Call this from a demo/testing endpoint to make the next N Razorpay
    API calls raise a simulated timeout, so the retry+backoff path and
    the audit-logged failure can be shown live."""
    _SIMULATED_FAILURES_REMAINING["count"] = n


class RazorpayAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _auth():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RazorpayAPIError("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not configured.")
    return (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)


def _request(method: str, path: str, json_body: dict | None = None,
             max_retries: int = 3, base_delay_seconds: float = 1.0) -> dict:
    """
    Makes a Razorpay API call with exponential backoff on failure.
    Raises RazorpayAPIError if all retries are exhausted -- callers
    (pipeline.py) are responsible for catching this, logging an audited
    failure, and notifying the merchant rather than crashing.
    """
    url = f"{BASE_URL}{path}"
    last_error: Optional[Exception] = None

    for attempt in range(max_retries):
        try:
            if _SIMULATED_FAILURES_REMAINING["count"] > 0:
                _SIMULATED_FAILURES_REMAINING["count"] -= 1
                raise requests.exceptions.Timeout("Simulated timeout (demo failure injection)")

            response = requests.request(
                method, url, auth=_auth(), json=json_body, timeout=10
            )
            if response.status_code >= 500:
                raise RazorpayAPIError(
                    f"Razorpay server error {response.status_code}",
                    status_code=response.status_code, response_body=response.text,
                )
            if response.status_code >= 400:
                # Client errors (bad request, auth failure) are not retried --
                # retrying an invalid payload just wastes time and quota.
                raise RazorpayAPIError(
                    f"Razorpay client error {response.status_code}: {response.text}",
                    status_code=response.status_code, response_body=response.text,
                )
            return response.json()

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, RazorpayAPIError) as e:
            last_error = e
            is_client_error = isinstance(e, RazorpayAPIError) and e.status_code and e.status_code < 500
            if is_client_error:
                raise  # don't retry 4xx errors
            if attempt < max_retries - 1:
                delay = base_delay_seconds * (2 ** attempt)
                time.sleep(delay)
            continue

    raise RazorpayAPIError(f"Razorpay API call failed after {max_retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Orders API
# ---------------------------------------------------------------------------

def create_order(amount_paise: int, currency: str = "INR", receipt: Optional[str] = None,
                  notes: Optional[dict] = None) -> dict:
    """Creates a Razorpay Order -- the first step before a customer pays
    via Checkout. Returns the order object including its `id`."""
    body = {"amount": amount_paise, "currency": currency}
    if receipt:
        body["receipt"] = receipt
    if notes:
        body["notes"] = notes
    return _request("POST", "/orders", json_body=body)


def fetch_order(order_id: str) -> dict:
    return _request("GET", f"/orders/{order_id}")


# ---------------------------------------------------------------------------
# Payment Links API
# ---------------------------------------------------------------------------

def create_payment_link(amount_paise: int, description: str, customer_name: Optional[str] = None,
                         customer_contact: Optional[str] = None, customer_email: Optional[str] = None,
                         notes: Optional[dict] = None) -> dict:
    """Creates a real Razorpay test-mode Payment Link for the upsell offer.
    Returns the link object including `short_url` (what you'd actually
    send the customer) and `id` (used to check payment status later)."""
    body: dict = {
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "notify": {"sms": False, "email": False},  # we simulate the send channel ourselves in the demo
    }
    customer = {}
    if customer_name:
        customer["name"] = customer_name
    if customer_contact:
        customer["contact"] = customer_contact
    if customer_email:
        customer["email"] = customer_email
    if customer:
        body["customer"] = customer
    if notes:
        body["notes"] = notes
    return _request("POST", "/payment_links", json_body=body)


def fetch_payment_link(payment_link_id: str) -> dict:
    """Returns the current status of a payment link: 'created', 'paid',
    'cancelled', or 'expired'. Used to check whether an upsell converted."""
    return _request("GET", f"/payment_links/{payment_link_id}")


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload_body: bytes, received_signature: str) -> bool:
    """
    Verifies that a webhook actually came from Razorpay, per their
    documented HMAC-SHA256 scheme: signature = HMAC_SHA256(payload, webhook_secret).
    NEVER process a webhook payload without this check -- otherwise
    anyone who finds the endpoint URL could inject fake "payment
    completed" events.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        raise RazorpayAPIError("RAZORPAY_WEBHOOK_SECRET not configured.")
    expected_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, received_signature)
