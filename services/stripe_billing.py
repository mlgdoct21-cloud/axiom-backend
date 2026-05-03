"""Stripe billing — checkout session creation + webhook handling.

Auth model is intentionally minimal: the dashboard has no NextAuth/Supabase
yet, so we lean on Telegram identity. The /upgrade Telegram command calls
into create_checkout_session(telegram_id, tier), which spins up a Checkout
Session with `metadata = {telegram_id, tier}` and returns the hosted URL.
After the user pays, Stripe POSTs to /billing/webhook; we verify the signed
payload, look up the row by metadata.telegram_id, and flip users.tier.

Env vars (all optional — without them /upgrade falls back to admin contact):
- STRIPE_SECRET_KEY        → sk_test_… or sk_live_…
- STRIPE_WEBHOOK_SECRET    → whsec_…
- STRIPE_PRICE_PREMIUM     → price_…  ($2/mo recurring price ID)
- STRIPE_PRICE_ADVANCE     → price_…  ($5/mo recurring price ID)
- STRIPE_SUCCESS_URL       → defaults to dashboard URL
- STRIPE_CANCEL_URL        → defaults to dashboard URL
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.future import select

from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.user import User

logger = get_logger("stripe_billing")

# Lazy import — the SDK is pulled in only when the module is actually used,
# so a missing pip install during a partial deploy doesn't crash imports.
try:
    import stripe
    _STRIPE_OK = True
except Exception as e:  # pragma: no cover
    stripe = None  # type: ignore
    _STRIPE_OK = False
    logger.warning(f"stripe SDK import failed: {e}")


_DEFAULT_SUCCESS_URL = "https://axiom-dashboard-sigma.vercel.app/dashboard?upgrade=success"
_DEFAULT_CANCEL_URL = "https://axiom-dashboard-sigma.vercel.app/dashboard?upgrade=cancel"


@dataclass
class CheckoutResult:
    url: Optional[str]
    session_id: Optional[str]
    error: Optional[str] = None


def is_configured() -> bool:
    """True iff Stripe SDK is installed AND the minimum env vars are set.
    Used by /upgrade Telegram command to decide whether to render a Stripe
    link or fall back to admin contact.
    """
    return (
        _STRIPE_OK
        and bool(os.getenv("STRIPE_SECRET_KEY", "").strip())
        and bool(os.getenv("STRIPE_PRICE_PREMIUM", "").strip())
    )


def _set_api_key() -> bool:
    """Configure the SDK at call time. Returns False if no key — caller
    should fall back to the admin-contact path and log nothing user-facing.
    """
    if not _STRIPE_OK:
        return False
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return False
    stripe.api_key = key
    return True


def _price_id_for(tier: str) -> Optional[str]:
    tier = (tier or "").lower()
    if tier == "premium":
        return os.getenv("STRIPE_PRICE_PREMIUM", "").strip() or None
    if tier == "advance":
        return os.getenv("STRIPE_PRICE_ADVANCE", "").strip() or None
    return None


async def create_checkout_session(telegram_id: str, tier: str) -> CheckoutResult:
    """Create a Checkout Session for `telegram_id` upgrading to `tier`.

    Reuses an existing stripe_customer_id from the User row when present so
    repeat upgrades don't spawn duplicate Stripe customers. metadata is the
    canonical link back from webhook → user — never trust client_reference_id.
    """
    if not _set_api_key():
        return CheckoutResult(url=None, session_id=None, error="stripe_not_configured")
    price_id = _price_id_for(tier)
    if not price_id:
        return CheckoutResult(url=None, session_id=None, error=f"no_price_for_{tier}")

    success_url = os.getenv("STRIPE_SUCCESS_URL", _DEFAULT_SUCCESS_URL)
    cancel_url = os.getenv("STRIPE_CANCEL_URL", _DEFAULT_CANCEL_URL)

    # Look up existing customer to avoid duplicates on a second upgrade.
    customer_id: Optional[str] = None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.telegram_id == str(telegram_id)))
            user = result.scalars().first()
            if user is not None:
                customer_id = getattr(user, "stripe_customer_id", None) or None
    except Exception as e:
        logger.warning(f"create_checkout: customer lookup failed for {telegram_id}: {e}")

    try:
        params: dict = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"telegram_id": str(telegram_id), "tier": tier},
            "subscription_data": {
                "metadata": {"telegram_id": str(telegram_id), "tier": tier},
            },
            "allow_promotion_codes": True,
        }
        if customer_id:
            params["customer"] = customer_id
        sess = stripe.checkout.Session.create(**params)  # type: ignore[union-attr]
    except Exception as e:
        logger.error(f"stripe checkout.Session.create failed: {e}")
        return CheckoutResult(url=None, session_id=None, error=str(e))

    return CheckoutResult(url=sess.url, session_id=sess.id)


async def _apply_subscription_state(
    telegram_id: str,
    *,
    customer_id: Optional[str],
    subscription_id: Optional[str],
    status: Optional[str],
    tier: Optional[str],
) -> None:
    """Idempotent UPDATE on the users row. Tier is bumped only when the
    subscription status is 'active' / 'trialing'; on cancel/past_due we set
    tier back to 'free' so a lapsed sub stops getting paid-tier broadcasts.
    """
    eff_tier: Optional[str] = None
    if tier:
        if status in ("active", "trialing"):
            eff_tier = tier
        elif status in ("canceled", "incomplete_expired", "unpaid"):
            eff_tier = "free"
    sets: list[str] = []
    params: dict = {"tid": str(telegram_id)}
    if customer_id is not None:
        sets.append("stripe_customer_id = :cid")
        params["cid"] = customer_id
    if subscription_id is not None:
        sets.append("stripe_subscription_id = :sid")
        params["sid"] = subscription_id
    if status is not None:
        sets.append("subscription_status = :st")
        params["st"] = status
    if eff_tier is not None:
        sets.append("tier = :tier")
        params["tier"] = eff_tier
    if not sets:
        return
    sql = text(f"UPDATE users SET {', '.join(sets)} WHERE telegram_id = :tid")
    async with AsyncSessionLocal() as session:
        await session.execute(sql, params)
        await session.commit()
    logger.info(
        f"stripe webhook: user {telegram_id} → tier={eff_tier or 'unchanged'} status={status}"
    )


def _meta_get(obj, key: str) -> Optional[str]:
    """Pull a metadata key from either the top-level object or its nested
    subscription/checkout payload — Stripe puts it in different places
    depending on the event type."""
    try:
        md = getattr(obj, "metadata", None) or {}
        v = md.get(key) if hasattr(md, "get") else None
        if v:
            return str(v)
    except Exception:
        pass
    return None


async def handle_webhook_event(event: dict) -> dict:
    """Dispatch a verified Stripe event to its handler. Returns a dict that
    becomes the 200 response body; never raises (Stripe retries on non-2xx
    so we want to swallow our own bugs and return success after best-effort)."""
    et = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    try:
        if et == "checkout.session.completed":
            telegram_id = (obj.get("metadata") or {}).get("telegram_id")
            tier = (obj.get("metadata") or {}).get("tier")
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            if telegram_id:
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    status="active",
                    tier=tier,
                )
        elif et in ("customer.subscription.updated", "customer.subscription.created"):
            telegram_id = (obj.get("metadata") or {}).get("telegram_id")
            if not telegram_id:
                # Subscriptions may not carry our metadata if created outside
                # checkout — fall back to looking up via customer_id.
                telegram_id = await _telegram_id_for_customer(obj.get("customer"))
            if telegram_id:
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=obj.get("customer"),
                    subscription_id=obj.get("id"),
                    status=obj.get("status"),
                    tier=(obj.get("metadata") or {}).get("tier"),
                )
        elif et == "customer.subscription.deleted":
            telegram_id = (obj.get("metadata") or {}).get("telegram_id")
            if not telegram_id:
                telegram_id = await _telegram_id_for_customer(obj.get("customer"))
            if telegram_id:
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=obj.get("customer"),
                    subscription_id=obj.get("id"),
                    status="canceled",
                    tier="free",  # explicit downgrade
                )
        else:
            logger.debug(f"stripe webhook: ignored event type {et}")
    except Exception as e:
        logger.error(f"stripe webhook handler crashed for {et}: {e}")
    return {"received": True, "event_type": et}


async def _telegram_id_for_customer(customer_id: Optional[str]) -> Optional[str]:
    if not customer_id:
        return None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.stripe_customer_id == str(customer_id))
            )
            user = result.scalars().first()
            return user.telegram_id if user else None
    except Exception as e:
        logger.warning(f"stripe webhook: customer lookup failed for {customer_id}: {e}")
        return None


def verify_webhook_signature(payload: bytes, sig_header: str) -> Optional[dict]:
    """Returns the parsed event dict on a valid signature, None otherwise.
    Caller responds 400 on None so Stripe retries."""
    if not _STRIPE_OK:
        return None
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.warning("stripe webhook: STRIPE_WEBHOOK_SECRET not set; rejecting")
        return None
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)  # type: ignore[union-attr]
    except Exception as e:
        logger.warning(f"stripe webhook signature verify failed: {e}")
        return None
    # construct_event returns a stripe.Event object; coerce to plain dict.
    try:
        return event.to_dict()  # type: ignore[union-attr]
    except Exception:
        return dict(event)
