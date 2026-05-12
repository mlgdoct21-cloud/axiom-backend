"""T-5dk pre-announcement broadcast — kullanıcı engagement + "biz izliyoruz" sinyali.

Major release event'lerden (CPI/NFP/PCE/PPI/FOMC + TR TÜFE/PPK) 5 dakika önce
tüm kullanıcılara (Free + Premium + Advance) "5 dakika içinde X açıklanacak,
biz takip ediyoruz, rakam çıkar çıkmaz analiz Telegram'ınıza düşecek" mesajı
fırlatılır.

Faydası:
- Free user'lar pre-announce'tan "vay biz takip ediyoruz" der → engagement +
  Premium'a geçme motivasyonu (asıl analiz Premium/Advance'ta detaylı düşer).
- Premium/Advance kullanıcı odaklanır, ekranını açar, rakam geldiğinde
  hemen analiz okur.

Idempotency: `macro_pre_announcements` tablosunda her (event_type, scheduled_at)
deterministik key. Reliability_probe tick'inde (30s) çağrılır; release window
(now+4min, now+6min) içindeki announced-olmamış event'ler için fire eder.

Kill-switch: `MACRO_PRE_ANNOUNCE_ENABLED=false` env var.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text

from core.database import AsyncSessionLocal, engine
from core.logger import get_logger
from models.user import User
from services.macro_calendar import CalendarEvent, load_calendar
from services.telegram_bot import send_telegram_message

logger = get_logger("macro.pre_announce")

# Hangi event_type'ler için pre-announcement gönderilir. Major releases sadece;
# JOBLESS_CLAIMS haftalık (her perşembe), HOUSING_STARTS, GDP gibi düşük-priority
# event'ler için noise yaratmasın.
_PRE_ANNOUNCE_EVENT_TYPES = frozenset({
    "CPI", "NFP", "PCE", "PPI", "FOMC_STATEMENT",
    "TR_TUFE", "TR_POLICY_RATE",
})

# Window: T-5dk hedeflenir ama scheduler tick'i 30s olduğundan ±1dk tolerans.
# (now + 4min, now + 6min) aralığındaki event'leri yakalar.
_ANNOUNCE_WINDOW_AHEAD_LO = timedelta(minutes=4)
_ANNOUNCE_WINDOW_AHEAD_HI = timedelta(minutes=6)

# event_type → Türkçe label (label kalitesi macro_calendar.yaml'da var ama
# pre-announce kısa hap için kendi mapping'imiz daha kontrollü).
_EVENT_LABELS_TR = {
    "CPI":             "ABD Tüketici Fiyat Endeksi (TÜFE)",
    "NFP":             "ABD Tarım Dışı İstihdam (NFP)",
    "PCE":             "ABD PCE Fiyat Endeksi",
    "PPI":             "ABD Üretici Fiyat Endeksi",
    "FOMC_STATEMENT":  "Fed FOMC Faiz Kararı",
    "TR_TUFE":         "Türkiye TÜFE",
    "TR_POLICY_RATE":  "TCMB Politika Faizi Kararı",
}


def _event_key(ev: CalendarEvent) -> str:
    """Deterministic idempotency key — same period collapses to one row."""
    return f"{ev.event_type}:{ev.scheduled_at.date().isoformat()}"


async def _is_announced(event_key: str) -> bool:
    sql = text("SELECT 1 FROM macro_pre_announcements WHERE event_key = :k")
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"k": event_key})).first()
    return row is not None


async def _stamp_announced(event_key: str, ev: CalendarEvent, recipients: int) -> None:
    sql = text("""
        INSERT INTO macro_pre_announcements
        (event_key, event_type, scheduled_at, sent_at, recipients_count)
        VALUES (:k, :et, :ts, NOW(), :n)
        ON CONFLICT (event_key) DO NOTHING
    """)
    async with engine.begin() as conn:
        await conn.execute(sql, {
            "k": event_key,
            "et": ev.event_type,
            "ts": ev.scheduled_at,
            "n": recipients,
        })


def _format_pre_announce_message(ev: CalendarEvent, minutes_to_go: int) -> str:
    label = _EVENT_LABELS_TR.get(ev.event_type, ev.label or ev.event_type)
    # HTML format — bold + emoji.
    tr_time_hint = ev.scheduled_at.astimezone(timezone(timedelta(hours=3)))  # UTC+3 TR time
    hh_mm = tr_time_hint.strftime("%H:%M")
    return (
        f"🔔 <b>Yaklaşan veri: {label}</b>\n\n"
        f"⏰ {minutes_to_go} dakika içinde açıklanıyor (saat <b>{hh_mm}</b> TR).\n\n"
        f"📡 AXIOM piyasayı şu an aktif takip ediyor. Rakam yayımlanır "
        f"yayımlanmaz, Premium ve Advance kullanıcılarımıza analiz "
        f"mesajı gelecek.\n\n"
        f"☕ Hazırlanın, ekranı bırakmayın."
    )


async def _send_to_all_users(message: str) -> int:
    """Free + Premium + Advance kullanıcıların hepsine gönderir."""
    try:
        async with AsyncSessionLocal() as session:
            users = list((await session.execute(select(User))).scalars().all())
    except Exception as e:
        logger.error(f"pre-announce user query failed: {e}")
        return 0

    sent = 0
    for user in users:
        try:
            await asyncio.to_thread(send_telegram_message, user.telegram_id, message)
            sent += 1
        except Exception as e:
            if "chat not found" not in str(e).lower():
                logger.warning(f"pre-announce send to {user.telegram_id}: {e}")
    return sent


async def fire_due_pre_announcements() -> dict:
    """Reliability_probe tick'inden çağrılır (30s cadence).

    Kill-switch + idempotency + window-based filter. Hata olursa loglar,
    raise etmez (probe loop kırılmasın).
    """
    enabled = os.getenv("MACRO_PRE_ANNOUNCE_ENABLED", "true").strip().lower()
    if enabled in ("false", "0", "no"):
        return {"fired": 0, "skipped_disabled": True}

    try:
        events = await load_calendar()
    except Exception as e:
        logger.warning(f"pre-announce calendar load failed: {e}")
        return {"fired": 0, "error": str(e)}

    now = datetime.now(timezone.utc)
    lo = now + _ANNOUNCE_WINDOW_AHEAD_LO
    hi = now + _ANNOUNCE_WINDOW_AHEAD_HI

    candidates = [
        e for e in events
        if e.event_type in _PRE_ANNOUNCE_EVENT_TYPES
        and lo <= e.scheduled_at <= hi
    ]
    if not candidates:
        return {"fired": 0}

    fired = 0
    for ev in candidates:
        key = _event_key(ev)
        if await _is_announced(key):
            continue
        minutes_to_go = max(1, int((ev.scheduled_at - now).total_seconds() // 60))
        message = _format_pre_announce_message(ev, minutes_to_go)
        n = await _send_to_all_users(message)
        await _stamp_announced(key, ev, n)
        logger.info(
            f"📣 pre-announce fired: {key} ({ev.event_type}) "
            f"{minutes_to_go}min ahead → {n} users"
        )
        fired += 1
    return {"fired": fired}


async def fire_due_pre_announcements_safe() -> None:
    """Probe loop'tan fire-and-forget wrapper — raise YASAK."""
    try:
        await fire_due_pre_announcements()
    except Exception as e:
        logger.error(f"pre-announce dispatcher unexpected: {e}")
