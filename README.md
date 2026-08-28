# UpsellAgent — Automated Cross-Sell Agent on Razorpay

Built for the Bennett University / Times of India Group buildathon
(AI agent using Razorpay test-mode APIs that grows a merchant's sales).

**What it does:** when a customer completes a real Razorpay test-mode
purchase, a webhook triggers this agent to evaluate whether a
complementary product is worth offering. If a guardrail-approved
match exists, it creates a REAL Razorpay test-mode Payment Link for
the upsell and tracks whether it converts. Every decision — found,
approved, blocked, sent, converted — is fully audited.

## Why this satisfies the brief's requirements

- **Uses real Razorpay test-mode APIs** — Orders API (original
  purchase), Payment Links API (the upsell offer), Webhooks
  (signature-verified) for payment confirmation. Not synthetic data.
- **Safety & Control**: the LLM never picks the upsell product (a
  fixed compatibility map does) or its price (the catalog does) or
  whether to send it (a deterministic guardrail engine does). The LLM's
  only job is writing the customer-facing message and an audit
  explanation.
- **Audit Trail**: every order that reaches this pipeline gets a full,
  timestamped, stage-by-stage log — visible in the dashboard's drawer.
- **Failure Handling**: `razorpay_client.py` implements real
  retry-with-backoff, and a `/demo/simulate-failure` endpoint lets you
  demonstrate this live — the next N Razorpay API calls fail with a
  simulated timeout, and you can watch the retry sequence followed by
  a graceful, audited failure record instead of a crash.

## Project layout

```
backend/
  requirements.txt
  app/
    catalog.py           product catalog + upsell compatibility map (static, not AI-decided)
    razorpay_client.py    real Razorpay API calls: Orders, Payment Links, webhook verification, retry+backoff
    guardrail.py           deterministic checks before any upsell offer is sent
    narration.py            LLM (Gemini) writes offer copy + audit explanations only
    pipeline.py             orchestrates: candidate -> guardrail -> Payment Link -> audit
    database.py             SQLite models: Order, UpsellOffer, AuditLog, GuardrailConfig
    main.py                 FastAPI app -- all endpoints, including the webhook
frontend/
  storefront.html            customer-facing checkout (real Razorpay Checkout.js)
  index.html                  merchant dashboard
```

## Setup

### 1. Get Razorpay test-mode keys (no KYC needed for test mode)
- Sign up at https://dashboard.razorpay.com/signup
- Switch to **Test Mode** (toggle near top of dashboard)
- Account & Settings -> API Keys -> Generate Test Key
- Save the Key ID (`rzp_test_...`) and Key Secret immediately

### 2. Set up a webhook (needed for the pipeline to trigger)
- In the Razorpay Dashboard (Test Mode): Account & Settings -> Webhooks -> Add New Webhook
- URL: `https://your-deployed-backend-url.onrender.com/webhook/razorpay`
  (must be a public HTTPS URL -- won't work with localhost; deploy first,
  or use ngrok for local testing)
- Active events: check **order.paid**
- Set a webhook secret (any string you choose) -- you'll need this below

### 3. Configure environment variables
Create `backend/app/.env`:
```
RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
RAZORPAY_KEY_SECRET=your_key_secret
RAZORPAY_WEBHOOK_SECRET=the_webhook_secret_you_set_in_step_2
GEMINI_API_KEY=your_gemini_key   # optional -- falls back to templates without it
```

### 4. Install and run
```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows; use `source venv/bin/activate` on Mac/Linux
pip install -r requirements.txt
cd app
uvicorn main:app --reload --port 8000
```

### 5. Open the storefront and dashboard
- `frontend/storefront.html` -- set `window.UPSELL_API_BASE` to your backend URL, then open it and make a test purchase
  (Razorpay test card: **4111 1111 1111 1111**, any future expiry, any CVV)
- `frontend/index.html` -- the merchant dashboard, same API_BASE config

## Demo flow for your video

1. Open the storefront, buy the "Aster Phone X1" using the Razorpay test card
2. Switch to the merchant dashboard -- within ~15 seconds (auto-refresh) you should see
   the order appear, and a real Payment Link generated for the phone case upsell
3. Click the order row to show the full audit trail
4. Click "Simulate API failure" in the dashboard, then make another purchase --
   watch the terminal show the retry-with-backoff sequence, and the dashboard
   show a "blocked" offer with an audited failure reason
5. Edit the guardrail config (e.g. lower the max price ratio), save, make another
   purchase, and show the offer get blocked or approved differently
