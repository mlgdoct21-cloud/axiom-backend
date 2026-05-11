"""Stripe billing — checkout session creation + webhook handling.

Auth model is intentionally minimal: the dashboard has no NextAuth/Supabase
yet, so we lean on Telegram identity. The /upgrade Telegram command calls
into create_checkout_session(telegram_id, tier), which spins up a Checkout
Session with `metadata = {telegram_id, tier}` and returns the hosted URL.
After the user pays, Stripe POSTs to /billing/webhook; we verify the signed
payload, look up the row by metadata.telegram_id, and flip users.tier.

Tier mapping is derived from the active subscription's price ID (mapped
against STRIPE_PRICE_*), NOT from metadata — this way Stripe Customer Portal
plan-swaps (Premium ↔ Advance) update the local tier correctly. Metadata
is kept only as a fallback for `checkout.session.completed`.

Webhook idempotency is enforced via stripe_webhook_events(event_id PK):
INSERT … ON CONFLICT DO NOTHING short-circuits replays. Transient errors
(DB / network) propagate up so the router returns 500 and Stripe retries;
logic errors (unknown event types, missing telegram_id) return 200 quietly.

Env vars (all required for production checkout):
- STRIPE_SECRET_KEY              → sk_test_… or sk_live_…
- STRIPE_WEBHOOK_SECRET          → whsec_…
- STRIPE_PRICE_PREMIUM           → price_…  ($1.99/mo recurring price ID)
- STRIPE_PRICE_ADVANCE           → price_…  ($4.99/mo recurring price ID)
- STRIPE_SUCCESS_URL             → defaults to dashboard URL
- STRIPE_CANCEL_URL              → defaults to dashboard URL
- STRIPE_PORTAL_RETURN_URL       → Customer Portal return URL
- STRIPE_AUTOMATIC_TAX_ENABLED   → "true" once Stripe Tax is configured
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.future import select

from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.user import User
from services.telegram_login_token import create_token as _create_login_token

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


_DEFAULT_SUCCESS_URL = (
    "https://axiom-dashboard-sigma.vercel.app/dashboard"
    "?upgrade=success&session_id={CHECKOUT_SESSION_ID}"
)
_DEFAULT_CANCEL_URL = "https://axiom-dashboard-sigma.vercel.app/dashboard?upgrade=cancel"
# Stripe replaces the literal `{CHECKOUT_SESSION_ID}` token in success_url at
# redirect time with the actual cs_… session ID. This lets the dashboard
# call GET /billing/checkout-status?session_id=… and poll until the webhook
# has applied the tier flip, masking webhook delay from the user.


@dataclass
class CheckoutResult:
    url: Optional[str]
    session_id: Optional[str]
    error: Optional[str] = None


def is_configured() -> bool:
    """True iff Stripe SDK is installed AND the minimum env vars are set.
    Used by /upgrade Telegram command to decide whether to render a Stripe
    link or fall back to admin contact.

    STRIPE_WEBHOOK_SECRET is also required — without it, payments go through
    but webhook signature verification rejects all events with a 400 and the
    user's tier never flips. Refusing to create checkout sessions in this
    state is safer than silently taking money we can't reconcile.
    """
    return (
        _STRIPE_OK
        and bool(os.getenv("STRIPE_SECRET_KEY", "").strip())
        and bool(os.getenv("STRIPE_PRICE_PREMIUM", "").strip())
        and bool(os.getenv("STRIPE_WEBHOOK_SECRET", "").strip())
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


def _tier_for_price_id(price_id: Optional[str]) -> Optional[str]:
    """Inverse of _price_id_for: map a Stripe price ID back to our tier label.

    Canonical tier source after the initial checkout — used by
    customer.subscription.* handlers so a Stripe Portal plan swap (Premium ↔
    Advance) updates users.tier correctly. metadata.tier on the subscription
    is set once at creation and Stripe does NOT rewrite it on portal swaps.
    """
    if not price_id:
        return None
    pid = str(price_id).strip()
    if pid and pid == os.getenv("STRIPE_PRICE_PREMIUM", "").strip():
        return "premium"
    if pid and pid == os.getenv("STRIPE_PRICE_ADVANCE", "").strip():
        return "advance"
    return None


def _extract_price_id_from_subscription(obj) -> Optional[str]:
    """Pull the active recurring price ID from a Stripe subscription event
    object. Looks at items.data[0].price.id. Returns None when missing or
    when the subscription has multiple items (we don't sell bundles)."""
    if not obj:
        return None
    try:
        items = obj.get("items") if hasattr(obj, "get") else None
        data = (items or {}).get("data") if items else None
        if data and isinstance(data, list) and len(data) >= 1:
            price = (data[0] or {}).get("price") or {}
            pid = price.get("id")
            if pid:
                return str(pid)
    except Exception:
        pass
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

    # Mint a one-time login token and append to success_url so the user lands
    # on /dashboard already authenticated. Without this, a paying user opening
    # the Stripe link from Telegram's in-app browser (mobile) has no JWT in
    # that browser's localStorage and gets bounced to /auth/login right after
    # paying — common cause of "I paid but tier still Free" support tickets.
    # The token is 5-min single-use; dashboard exchanges it before rendering
    # the success interstitial. Failure is non-fatal — checkout still works,
    # the user just sees the login screen instead of the success animation.
    try:
        login_token = await _create_login_token(str(telegram_id))
        if login_token:
            sep = "&" if "?" in success_url else "?"
            success_url = f"{success_url}{sep}login_token={login_token}"
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(f"create_checkout: login token mint failed for {telegram_id}: {e}")

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
            # Address required to compute tax + produce a compliant invoice.
            # Romania merchant + EU OSS → customer's country determines the
            # VAT rate; without an address Stripe can't apply the right rate.
            "billing_address_collection": "required",
            # Let business customers provide their VAT ID at checkout (B2B
            # reverse-charge handling under EU OSS). Optional for individuals.
            "tax_id_collection": {"enabled": True},
        }
        if customer_id:
            params["customer"] = customer_id

        # Stripe Tax: requires the merchant's VAT registration to be configured
        # in Stripe Dashboard (Romania VAT + EU OSS opt-in) before flipping
        # this on. While disabled, address + VAT ID are still collected so
        # nothing changes on the customer's side when we eventually enable it.
        if os.getenv("STRIPE_AUTOMATIC_TAX_ENABLED", "").strip().lower() == "true":
            params["automatic_tax"] = {"enabled": True}
            if customer_id:
                # Required by Stripe when `customer` is passed with automatic_tax
                # — lets Stripe sync the address back onto the Customer record.
                params["customer_update"] = {"address": "auto", "name": "auto"}

        sess = stripe.checkout.Session.create(**params)  # type: ignore[union-attr]
    except Exception as e:
        logger.error(f"stripe checkout.Session.create failed: {e}")
        return CheckoutResult(url=None, session_id=None, error=str(e))

    return CheckoutResult(url=sess.url, session_id=sess.id)


_DEFAULT_PORTAL_RETURN_URL = "https://axiom-dashboard-sigma.vercel.app/dashboard/settings"


@dataclass
class PortalResult:
    url: Optional[str]
    error: Optional[str] = None


async def create_portal_session(telegram_id: str) -> PortalResult:
    """Create a Stripe Customer Portal Session URL for `telegram_id`.

    Returns 503-style error when Stripe isn't configured or when the user
    has no stripe_customer_id (i.e. they never upgraded). Caller maps to
    HTTP 400/503 as appropriate.
    """
    if not _set_api_key():
        return PortalResult(url=None, error="stripe_not_configured")

    customer_id: Optional[str] = None
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.telegram_id == str(telegram_id))
            )
            user = result.scalars().first()
            if user is not None:
                customer_id = getattr(user, "stripe_customer_id", None) or None
    except Exception as e:
        logger.warning(f"create_portal: customer lookup failed for {telegram_id}: {e}")
        return PortalResult(url=None, error="lookup_failed")

    if not customer_id:
        return PortalResult(url=None, error="no_customer")

    return_url = os.getenv("STRIPE_PORTAL_RETURN_URL", _DEFAULT_PORTAL_RETURN_URL)
    try:
        sess = stripe.billing_portal.Session.create(  # type: ignore[union-attr]
            customer=customer_id,
            return_url=return_url,
        )
    except Exception as e:
        logger.error(f"stripe billing_portal.Session.create failed: {e}")
        return PortalResult(url=None, error=str(e))

    return PortalResult(url=sess.url)


@dataclass
class CheckoutStatusResult:
    """Snapshot of a Checkout Session reconciled with our local user row.

    `ready` is True only when Stripe says payment_status='paid' AND our local
    User row has the matching paid tier — i.e. the webhook has landed. The
    dashboard polls this every ~2s after redirect; while webhook is in flight
    `ready=False` even though Stripe payment succeeded, so we keep showing
    the interstitial spinner instead of an "already paid but Free" UI race.
    """
    found: bool
    ready: bool
    stripe_payment_status: Optional[str] = None
    stripe_session_status: Optional[str] = None
    local_tier: Optional[str] = None
    local_subscription_status: Optional[str] = None
    error: Optional[str] = None


async def fetch_checkout_status(session_id: str) -> CheckoutStatusResult:
    """Retrieve a Checkout Session from Stripe and reconcile against our DB.

    Used by the dashboard's post-payment interstitial to avoid the race
    where Stripe redirects the user to ?upgrade=success before our webhook
    has fired. We trust Stripe's payment_status as the source of truth for
    "did the money move"; we additionally check the local users row so the
    UI doesn't flip to "Premium" until our system actually knows about it.
    """
    if not _set_api_key():
        return CheckoutStatusResult(found=False, ready=False, error="stripe_not_configured")
    if not session_id or not session_id.startswith("cs_"):
        return CheckoutStatusResult(found=False, ready=False, error="invalid_session_id")
    try:
        sess = stripe.checkout.Session.retrieve(session_id)  # type: ignore[union-attr]
    except Exception as e:
        logger.warning(f"checkout-status retrieve failed for {session_id}: {e}")
        return CheckoutStatusResult(found=False, ready=False, error=str(e))

    payment_status = (sess.get("payment_status") if hasattr(sess, "get") else None) or None
    session_status = (sess.get("status") if hasattr(sess, "get") else None) or None
    telegram_id = (sess.get("metadata") or {}).get("telegram_id") if hasattr(sess, "get") else None

    local_tier: Optional[str] = None
    local_status: Optional[str] = None
    if telegram_id:
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(User).where(User.telegram_id == str(telegram_id))
                )
                user = result.scalars().first()
                if user is not None:
                    local_tier = getattr(user, "tier", None) or None
                    local_status = getattr(user, "subscription_status", None) or None
        except Exception as e:
            logger.warning(f"checkout-status local lookup failed for {telegram_id}: {e}")

    paid_tiers = {"premium", "advance"}
    ready = (
        payment_status == "paid"
        and local_status in ("active", "trialing")
        and local_tier in paid_tiers
    )

    return CheckoutStatusResult(
        found=True,
        ready=ready,
        stripe_payment_status=payment_status,
        stripe_session_status=session_status,
        local_tier=local_tier,
        local_subscription_status=local_status,
    )


async def _apply_subscription_state(
    telegram_id: str,
    *,
    customer_id: Optional[str],
    subscription_id: Optional[str],
    status: Optional[str],
    tier: Optional[str],
    current_period_end: Optional[datetime] = None,
    _session=None,
) -> None:
    """Idempotent UPDATE on the users row. Tier is bumped only when the
    subscription status is 'active' / 'trialing'; on cancel/incomplete_expired/
    unpaid we set tier back to 'free' so a lapsed sub stops getting paid-tier
    broadcasts. `past_due` is treated as "no tier change yet" — Stripe will
    retry the invoice and we only downgrade on the eventual subscription.deleted.

    When `_session` is provided, executes on that session WITHOUT committing
    (caller is responsible for the commit). This lets the webhook handler put
    the dedupe insert and the state update in the same transaction so they
    succeed-or-fail atomically. Without this, a partial failure could leave
    the event_id recorded as processed while the state update was lost.
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
    if current_period_end is not None:
        sets.append("current_period_end = :cpe")
        params["cpe"] = current_period_end
    if not sets:
        return
    sql = text(f"UPDATE users SET {', '.join(sets)} WHERE telegram_id = :tid")
    if _session is not None:
        await _session.execute(sql, params)
    else:
        async with AsyncSessionLocal() as session:
            await session.execute(sql, params)
            await session.commit()
    logger.info(
        f"stripe webhook: user {telegram_id} → tier={eff_tier or 'unchanged'} status={status}"
    )


def _period_end_from(obj) -> Optional[datetime]:
    """Pull `current_period_end` (Unix ts) from a Stripe subscription object
    and convert to a UTC datetime. Returns None when missing or unparseable."""
    if not obj:
        return None
    try:
        ts = obj.get("current_period_end") if hasattr(obj, "get") else None
    except Exception:
        return None
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


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


_DEDUPE_INSERT_SQL = text(
    "INSERT INTO stripe_webhook_events (event_id, event_type) "
    "VALUES (:eid, :et) "
    "ON CONFLICT (event_id) DO NOTHING "
    "RETURNING event_id"
)


async def handle_webhook_event(event: dict) -> dict:
    """Dispatch a verified Stripe event to its handler.

    Idempotency: dedupes by event.id via stripe_webhook_events. The dedupe
    insert and the state mutations run in the SAME DB transaction so they
    commit atomically — if any handler step raises, the event_id is NOT
    recorded as processed and Stripe's retry will redeliver cleanly. Without
    this atomicity a transient DB error after the dedupe insert would mark
    the event as "done" while the state update was lost.

    Error policy: transient errors (DB / network) propagate up so the router
    returns 500 and Stripe retries. Logic-level no-ops (unknown event type,
    missing telegram_id) return 200 quietly so Stripe doesn't retry forever
    on something that's not a bug.
    """
    event_id = (event.get("id") or "").strip()
    et = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    async with AsyncSessionLocal() as session:
        # Atomic dedupe — if INSERT didn't happen (conflict), this event was
        # already processed in a prior delivery. Return early without commit;
        # the session exit rolls back any unflushed state (no-op here).
        if event_id:
            result = await session.execute(
                _DEDUPE_INSERT_SQL, {"eid": event_id, "et": et}
            )
            if result.first() is None:
                logger.info(
                    f"stripe webhook: event {event_id} ({et}) already processed; skipping"
                )
                return {"received": True, "event_type": et, "duplicate": True}

        if et == "checkout.session.completed":
            telegram_id = (obj.get("metadata") or {}).get("telegram_id")
            tier = (obj.get("metadata") or {}).get("tier")
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            if telegram_id:
                # checkout.session.completed doesn't carry current_period_end
                # on the session itself; the customer.subscription.created
                # event that follows will populate it.
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    status="active",
                    tier=tier,
                    _session=session,
                )

        elif et in ("customer.subscription.updated", "customer.subscription.created"):
            telegram_id = (obj.get("metadata") or {}).get("telegram_id")
            if not telegram_id:
                # Subscriptions may not carry our metadata if created outside
                # checkout — fall back to looking up via customer_id.
                telegram_id = await _telegram_id_for_customer(
                    obj.get("customer"), _session=session
                )
            if telegram_id:
                # Canonical tier source: derive from the active price ID, NOT
                # from subscription metadata. Stripe Customer Portal plan-swaps
                # (Premium ↔ Advance) update the price item but leave the
                # original metadata.tier untouched — without this mapping the
                # local tier would silently disagree with what we're billing.
                price_id = _extract_price_id_from_subscription(obj)
                derived_tier = _tier_for_price_id(price_id)
                tier = derived_tier or (obj.get("metadata") or {}).get("tier")
                if price_id and not derived_tier:
                    # Unknown price ID — could indicate a price was rotated in
                    # Stripe Dashboard but env vars weren't updated. Log loudly
                    # so we notice before more users get the wrong tier.
                    logger.error(
                        f"stripe webhook: subscription {obj.get('id')} uses unknown "
                        f"price_id={price_id} (not in STRIPE_PRICE_PREMIUM/ADVANCE); "
                        f"falling back to metadata.tier={tier!r}"
                    )
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=obj.get("customer"),
                    subscription_id=obj.get("id"),
                    status=obj.get("status"),
                    tier=tier,
                    current_period_end=_period_end_from(obj),
                    _session=session,
                )

        elif et == "customer.subscription.deleted":
            telegram_id = (obj.get("metadata") or {}).get("telegram_id")
            if not telegram_id:
                telegram_id = await _telegram_id_for_customer(
                    obj.get("customer"), _session=session
                )
            if telegram_id:
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=obj.get("customer"),
                    subscription_id=obj.get("id"),
                    status="canceled",
                    tier="free",  # explicit downgrade
                    current_period_end=_period_end_from(obj),
                    _session=session,
                )

        elif et == "invoice.payment_failed":
            # Card declined / renewal failed. We mark status='past_due' but
            # do NOT downgrade the tier yet — Stripe's smart-retry policy
            # will keep attempting for ~3 weeks. The eventual
            # subscription.deleted (on final failure) is what drops the user
            # to free. Marking past_due lets the dashboard surface a "update
            # payment method" CTA.
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            telegram_id = await _telegram_id_for_customer(
                customer_id, _session=session
            )
            if telegram_id:
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    status="past_due",
                    tier=None,  # explicit: no tier change on payment failure
                    _session=session,
                )

        elif et == "invoice.paid":
            # Successful renewal — cure past_due. current_period_end is
            # bumped by the customer.subscription.updated event that follows,
            # so here we only need to flip status back to 'active'. tier is
            # left alone: the subscription event handles plan-swap correctly.
            customer_id = obj.get("customer")
            subscription_id = obj.get("subscription")
            telegram_id = await _telegram_id_for_customer(
                customer_id, _session=session
            )
            if telegram_id:
                await _apply_subscription_state(
                    telegram_id,
                    customer_id=customer_id,
                    subscription_id=subscription_id,
                    status="active",
                    tier=None,  # tier flip is the subscription event's job
                    _session=session,
                )

        else:
            logger.debug(f"stripe webhook: ignored event type {et}")

        # Commit everything atomically — dedupe row + state mutations together.
        await session.commit()

    return {"received": True, "event_type": et}


async def _telegram_id_for_customer(
    customer_id: Optional[str],
    _session=None,
) -> Optional[str]:
    """Look up telegram_id from stripe_customer_id. Used by webhook handlers
    when Stripe event metadata is missing (e.g. subscriptions created outside
    checkout, or events where Stripe doesn't echo metadata back).

    When `_session` is provided, runs inside that session — required when the
    webhook handler is mid-transaction and asyncpg's transaction pooler
    refuses to open a second connection from the same async task.
    """
    if not customer_id:
        return None
    stmt = select(User).where(User.stripe_customer_id == str(customer_id))
    try:
        if _session is not None:
            result = await _session.execute(stmt)
            user = result.scalars().first()
            return user.telegram_id if user else None
        async with AsyncSessionLocal() as session:
            result = await session.execute(stmt)
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
