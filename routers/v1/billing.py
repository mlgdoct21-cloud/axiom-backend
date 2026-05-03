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
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from core.logger import get_logger
from services.stripe_billing import (
    create_checkout_session,
    handle_webhook_event,
    is_configured,
    verify_webhook_signature,
)

logger = get_logger("billing_router")

router = APIRouter(prefix="/billing", tags=["billing"])


def _check_internal_auth(secret: Optional[str]) -> None:
    expected = os.getenv("BOT_INTERNAL_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=403, detail="forbidden")


_VALID_TIERS = ("premium", "advance")


@router.post("/checkout")
async def checkout(
    telegram_id: str,
    tier: str,
    x_internal_secret: Optional[str] = Header(None),
):
    """Mint a Stripe Checkout Session URL for `telegram_id` upgrading to `tier`.
    Returns 503 when Stripe env vars aren't set so the bot can fall back to
    the admin-contact path without retrying."""
    _check_internal_auth(x_internal_secret)
    tier_norm = (tier or "").lower()
    if tier_norm not in _VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"tier must be one of {_VALID_TIERS}")
    if not is_configured():
        raise HTTPException(status_code=503, detail="stripe_not_configured")
    res = await create_checkout_session(str(telegram_id), tier_norm)
    if res.error or not res.url:
        raise HTTPException(status_code=502, detail=f"checkout_failed: {res.error or 'no_url'}")
    return {"url": res.url, "session_id": res.session_id, "tier": tier_norm}


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
