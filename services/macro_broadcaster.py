"""Macro release Telegram broadcaster.

Fired from `macro_narrative.generate_narrative` once a brand-new narrative_md
has been persisted (idempotency is inherited — UPDATE only writes when the
column was NULL, so we cannot double-broadcast a single event).

Recipients: all users (no tag filter for v0; macro is a global signal).
Tier guard / Free-tier 5min delay queue lands in a later commit.

Format mirrors the news broadcaster's "emoji + bold headline + body + link"
shape so users get a familiar message; sentiment chip appended when known.
A `MACRO_BROADCAST_ENABLED` env var lets us kill-switch the path without a
deploy if Gemini misbehaves in early rollout.
"""
from __future__ import annotations

import asyncio
import html
import os
from typing import Optional

from sqlalchemy import text
from sqlalchemy.future import select

from core.database import AsyncSessionLocal, engine
from core.logger import get_logger
from models.user import User
from services.telegram_bot import send_telegram_message

logger = get_logger("macro.broadcaster")

# Per event_type cosmetic prefix. Keep emojis in one place so the broadcast
# style stays consistent without scattering literals through the codebase.
_EMOJI = {
    "CPI": "📊",
    "NFP": "👷",
    "PCE": "📈",
    "FOMC_STATEMENT": "🏛️",
    "FOMC_MINUTES": "📜",
    "FOMC_PROJECTIONS": "🔮",
    "RATE_DECISION": "⚖️",
}

_HEADLINE = {
    "CPI": "ABD Tüketici Fiyat Endeksi (CPI)",
    "NFP": "ABD Tarım Dışı İstihdam (NFP)",
    "PCE": "ABD PCE Çekirdek Enflasyon",
    "FOMC_STATEMENT": "Fed FOMC Bildirisi",
    "FOMC_MINUTES": "Fed FOMC Tutanakları",
    "FOMC_PROJECTIONS": "Fed Ekonomik Projeksiyonlar (SEP)",
    "RATE_DECISION": "Fed Faiz Kararı",
}


def _sentiment_chip(score: Optional[float]) -> str:
    """Map 0..1 sentiment to a short coloured-emoji label.
    Score sense: 1 = bullish/dovish to risk, 0 = bearish/hawkish.
    """
    if score is None:
        return ""
    if score >= 0.66:
        return " 🟢 Güvercin / risk-on"
    if score <= 0.33:
        return " 🔴 Şahin / risk-off"
    return " 🟡 Karışık"


def _format_message(release: dict) -> str:
    et = (release.get("event_type") or "").upper()
    emoji = _EMOJI.get(et, "📰")
    headline = _HEADLINE.get(et, et or "Makro Veri")
    narrative = (release.get("narrative_md") or "").strip()
    sentiment = _sentiment_chip(_to_float(release.get("sentiment_score")))
    src_url = release.get("source_url") or ""

    parts = [
        f"{emoji} <b>{html.escape(headline)}</b>",
        "",
        html.escape(narrative),
    ]
    if sentiment:
        parts.append("")
        parts.append(f"<i>Piyasa tonu:{sentiment}</i>")
    if src_url:
        safe = html.escape(src_url, quote=True)
        parts.append("")
        parts.append(f"🔗 <a href='{safe}'>Resmi kaynak</a>")
    return "\n".join(parts)


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _load_release(event_id: str) -> Optional[dict]:
    sql = text("""
        SELECT event_id, event_type, source, released_at,
               narrative_md, sentiment_score, source_url
        FROM macro_releases WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
    return dict(row) if row else None


async def broadcast_release(event_id: str) -> dict:
    """Fan out one macro release to every user. Returns per-call counters.

    Honours `MACRO_BROADCAST_ENABLED=false` as a kill-switch (default on).
    Logs network/Telegram errors per user but never raises.
    """
    if os.getenv("MACRO_BROADCAST_ENABLED", "true").strip().lower() in ("false", "0", "no"):
        logger.info(f"macro broadcast disabled, skipping {event_id}")
        return {"sent": 0, "failed": 0, "skipped_disabled": True}

    release = await _load_release(event_id)
    if not release:
        logger.warning(f"macro broadcast: release row not found for {event_id}")
        return {"sent": 0, "failed": 0, "missing_row": True}
    if not release.get("narrative_md"):
        logger.warning(f"macro broadcast: empty narrative for {event_id}")
        return {"sent": 0, "failed": 0, "no_narrative": True}

    message = _format_message(release)

    try:
        async with AsyncSessionLocal() as session:
            users = list((await session.execute(select(User))).scalars().all())
    except Exception as e:
        logger.error(f"macro broadcast: user list query failed: {e}")
        return {"sent": 0, "failed": 0, "user_query_error": str(e)}

    sent = 0
    failed = 0
    for user in users:
        chat_id = user.telegram_id
        try:
            await asyncio.to_thread(send_telegram_message, chat_id, message)
            sent += 1
        except Exception as e:
            failed += 1
            if "chat not found" not in str(e).lower():
                logger.warning(f"macro broadcast {event_id} -> {chat_id}: {e}")

    logger.info(f"📣 macro broadcast {event_id}: {sent} sent / {failed} failed / {len(users)} total")
    return {"sent": sent, "failed": failed, "total_users": len(users)}


async def broadcast_release_safe(event_id: str) -> None:
    """Fire-and-forget wrapper used by macro_narrative — never raises."""
    try:
        await broadcast_release(event_id)
    except Exception as e:
        logger.error(f"broadcast_release crashed for {event_id}: {e}")
