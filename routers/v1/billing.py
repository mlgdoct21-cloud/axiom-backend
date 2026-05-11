"""Billing router — Stripe checkout + webhook.

Two endpoints:
- POST /billing/checkout (auth=BOT_INTERNAL_SECRET): the Telegram bot calls
  this from the /upgrade command to mint a hosted Checkout URL for a tier.
- POST /billing/webhook  (auth=Stripe signature): Stripe POSTs subscription
  lifecycle events here; signature verified by services/stripe_billing.

The webhook intentionally always returns 200 once the signature passes —
Stripe's retry policy is aggressive on non-2xx responses, so we swallow
handler errors and let our logger flag them.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from core.logger import get_logger
from core.rate_limit import limiter
from core.security import (
    assert_internal_secret,
    get_current_user as get_authenticated_user,
)
from services.stripe_billing import (
    create_checkout_session,
    create_portal_session,
    fetch_checkout_status,
    handle_webhook_event,
    is_configured,
    verify_webhook_signature,
)

logger = get_logger("billing_router")

router = APIRouter(prefix="/billing", tags=["billing"])


_VALID_TIERS = ("premium", "advance")


@router.post("/checkout")
@limiter.limit("20/minute")
async def checkout(
    request: Request,
    telegram_id: str,
    tier: str,
    x_internal_secret: Optional[str] = Header(None),
):
    """Mint a Stripe Checkout Session URL for `telegram_id` upgrading to `tier`.
    Returns 503 when Stripe env vars aren't set so the bot can fall back to
    the admin-contact path without retrying.

    Rate-limited to 20/min per IP — Telegram bot itself can hit this multiple
    times during a /upgrade flow, but a flood from a single source is suspicious.
    """
    assert_internal_secret(x_internal_secret)
    tier_norm = (tier or "").lower()
    if tier_norm not in _VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"tier must be one of {_VALID_TIERS}")
    if not is_configured():
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    res = await create_checkout_session(str(telegram_id), tier_norm)
    if res.error or not res.url:
        raise HTTPException(status_code=502, detail=f"checkout_failed: {res.error or 'no_url'}")
    return {"url": res.url, "session_id": res.session_id, "tier": tier_norm}


@router.post("/customer-portal")
async def customer_portal(current_user = Depends(get_authenticated_user)):
    """Mint a Stripe Customer Portal session URL for the authenticated user.

    Frontend calls this from Settings → "Manage Subscription" and redirects
    `window.location` to the returned URL. Returns 400 when the user has
    never upgraded (no stripe_customer_id) so the frontend can route them
    to the Telegram /upgrade flow instead. 503 when Stripe isn't configured.
    """
    if not is_configured():
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    res = await create_portal_session(str(current_user.telegram_id))
    if res.error == "no_customer":
        raise HTTPException(status_code=400, detail="no_subscription")
    if res.error or not res.url:
        raise HTTPException(status_code=502, detail=f"portal_failed: {res.error or 'no_url'}")
    return {"url": res.url}


@router.get("/checkout-status")
@limiter.limit("60/minute")
async def checkout_status(request: Request, session_id: str):
    """Reconcile a Stripe Checkout Session with the local user row.

    Called by the dashboard right after redirect to `?upgrade=success` —
    polls every ~2s until `ready=true`, masking the webhook delivery delay
    (typically 1-3s, occasionally 10s+). Without this, a fast user lands on
    the success page before the webhook flips their tier and briefly sees
    themselves still as Free, which generates support tickets.

    Public (no auth) — the session_id is a per-checkout opaque token; an
    attacker without it cannot enumerate or pivot. Rate-limited to 60/min
    (one poll every second per IP) which covers polling cadence + headroom.
    """
    if not is_configured():
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    res = await fetch_checkout_status(session_id)
    if not res.found:
        # invalid session_id or Stripe API error — return 404 not 500 so the
        # dashboard can stop polling cleanly.
        raise HTTPException(status_code=404, detail=res.error or "not_found")
    return {
        "ready": res.ready,
        "stripe_payment_status": res.stripe_payment_status,
        "stripe_session_status": res.stripe_session_status,
        "local_tier": res.local_tier,
        "local_subscription_status": res.local_subscription_status,
    }


@router.post("/webhook")
async def webhook(request: Request):
    """Stripe-signed event handler. Always 200 once the signature passes.
    400 on missing/invalid signature so Stripe will retry."""
    sig_header = request.headers.get("stripe-signature", "")
    payload = await request.body()
    event = verify_webhook_signature(payload, sig_header)
    if event is None:
        raise HTTPException(status_code=400, detail="bad_signature")
    return await handle_webhook_event(event)
