"""Macro revision broadcaster — Advance tier'a özel "geçmiş ay revize edildi"
push'u.

Akış:
- release_detect._upsert_release_with_revision delta saptayıp
  macro_release_revisions audit satırı yazıyor.
- _trigger_revision_broadcast → bu modüldeki broadcast_revision'ı fire-and-forget
  çağırıyor.
- Eşik kontrolü burada: küçük revizyon (örn NFP +2K) noise; sadece anlamlı
  delta (NFP |Δ|≥10K, CPI |Δ|≥0.1pp, vb) Advance push'u tetikler.
- Hedef kitle: SADECE tier='advance' (Premium kullanıcı revizyon almıyor —
  trader edge satıyoruz).
- Idempotency: macro_release_revisions.broadcasted_at stamp.
- Kill-switch: MACRO_REVISION_BROADCAST_ENABLED=false.

Faz 3 sonrası genişletme alanı: revize edilen rakamlar mevcut storyteller
hikayesini tutarsızlaştırabilir; orta vadede revision sonrası ilgili
macro_stories satırını regen flag'lemek mantıklı.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.future import select

from core.database import AsyncSessionLocal, engine
from core.logger import get_logger
from models.user import User
from services.telegram_bot import send_message_with_keyboard

logger = get_logger("macro.revisions")


# Anlamlı revizyon eşikleri — event_type bazlı. Birim event_type'ın doğal
# birimi (NFP = bin kişi, CPI/PCE/PPI = index level, UNRATE = yüzde puan).
# Eşik altı revizyonları "noise" olarak yutuyoruz — trader'a değer üretmiyor.
_REVISION_THRESHOLDS_ABS = {
    "NFP": Decimal("10"),               # 10K kişi (FRED PAYEMS düzeyi 158K civarında, 10K ≈ %0.006)
    "UNRATE": Decimal("0.1"),           # 0.1 puan
    "CPI": Decimal("0.3"),              # index level 327 civarında, 0.3 ≈ %0.09
    "CORE_CPI": Decimal("0.3"),
    "PCE": Decimal("0.15"),
    "CORE_PCE": Decimal("0.15"),
    "PPI": Decimal("0.5"),              # PPI volatil, eşik daha yüksek
    "RETAIL_SALES": Decimal("0.2"),
    "GDP": Decimal("0.1"),              # quarterly, küçük revize bile önemli
    "HOUSING_STARTS": Decimal("20"),    # bin konut
    "JOBLESS_INITIAL": Decimal("5"),    # bin kişi
    "JOBLESS_CONTINUING": Decimal("20"),
}
# Default eşik — eventte tanımı yoksa Decimal("0") (tüm revizyonlar geçer).
# Belirsizlik avantajına oynuyoruz: bilmediğimiz event_type için yine de
# haberdar etmek yanlış sessizlikten iyi.


def _emoji_for(event_type: str) -> str:
    return {
        "CPI": "📊", "CORE_CPI": "📊",
        "PCE": "📈", "CORE_PCE": "📈",
        "PPI": "🏭",
        "NFP": "👷", "UNRATE": "📉",
        "RETAIL_SALES": "🛍️", "GDP": "🏛️",
        "HOUSING_STARTS": "🏠",
        "JOBLESS_INITIAL": "📋", "JOBLESS_CONTINUING": "📋",
    }.get(event_type, "📊")


def _headline_for(event_type: str) -> str:
    return {
        "CPI": "ABD Tüketici Fiyat Endeksi (CPI)",
        "CORE_CPI": "ABD Çekirdek CPI",
        "PCE": "ABD PCE",
        "CORE_PCE": "ABD Çekirdek PCE",
        "PPI": "ABD Üretici Fiyat Endeksi (PPI)",
        "NFP": "ABD Tarım Dışı İstihdam (NFP)",
        "UNRATE": "ABD İşsizlik Oranı",
        "RETAIL_SALES": "ABD Perakende Satışlar",
        "GDP": "ABD GSYİH",
        "HOUSING_STARTS": "ABD Konut Başlangıçları",
        "JOBLESS_INITIAL": "ABD İlk İşsizlik Talepleri",
        "JOBLESS_CONTINUING": "ABD Devam Eden İşsizlik Talepleri",
    }.get(event_type, event_type)


def _format_value(event_type: str, value: Decimal) -> str:
    """Doğru birimde format. NFP/JOBLESS = '92K', UNRATE = '%4.3', diğerleri
    index level → 1 ondalıkla."""
    if event_type in ("NFP", "JOBLESS_INITIAL", "JOBLESS_CONTINUING",
                      "HOUSING_STARTS"):
        # FRED level (bin cinsinden) — kullanıcıya da 'bin' olarak göster
        return f"{int(value):,}K".replace(",", ".")
    if event_type == "UNRATE":
        return f"%{value.quantize(Decimal('0.1'))}"
    # Index düzeyi (CPI/PCE/PPI/RETAIL/GDP) — 1 ondalık
    return str(value.quantize(Decimal("0.1")))


def _format_delta(event_type: str, old: Decimal, new: Decimal) -> str:
    """Trader-okur format: '+15K aşağı revize' yerine '+15K yukarı revize'.
    Yön gösterir + birimi koruyor."""
    delta = new - old
    yon = "yukarı" if delta > 0 else "aşağı"
    if event_type in ("NFP", "JOBLESS_INITIAL", "JOBLESS_CONTINUING",
                      "HOUSING_STARTS"):
        return f"{'+' if delta > 0 else ''}{int(delta):,}K {yon}".replace(",", ".")
    if event_type == "UNRATE":
        return f"{'+' if delta > 0 else ''}{delta.quantize(Decimal('0.1'))} puan {yon}"
    return f"{'+' if delta > 0 else ''}{delta.quantize(Decimal('0.1'))} {yon}"


def _format_period(released_at) -> str:
    """'2026-04-01' → 'Nisan 2026'. Trader release ayını okur."""
    if released_at is None:
        return ""
    try:
        months_tr = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                     "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
        return f"{months_tr[released_at.month - 1]} {released_at.year}"
    except (AttributeError, IndexError):
        return ""


def _is_meaningful_revision(event_type: str, delta_abs: Decimal) -> bool:
    """Eşik üstü mü? Default Decimal("0") tüm revizyonları geçirir."""
    threshold = _REVISION_THRESHOLDS_ABS.get(event_type, Decimal("0"))
    return abs(delta_abs) >= threshold


def _build_revision_keyboard(event_id: str) -> dict:
    rows = [[
        {"text": "📊 Tarihsel kıyaslama", "callback_data": f"macro_hist:{event_id}"},
        {"text": "💼 Etkilenen hisseler", "callback_data": f"macro_stocks:{event_id}"},
    ]]
    dashboard_base = (
        os.getenv("DASHBOARD_URL", "").strip()
        or os.getenv("DASHBOARD_BASE_URL", "").strip()
    ).rstrip("/")
    if dashboard_base:
        rows.append([{
            "text": "📈 Dashboard'da tam analiz",
            "url": f"{dashboard_base}/tr?event={event_id}",
        }])
    return {"inline_keyboard": rows}


def _format_revision_message(
    event_type: str,
    period_label: str,
    old: Decimal,
    new: Decimal,
) -> str:
    emoji = _emoji_for(event_type)
    head = _headline_for(event_type)
    delta_str = _format_delta(event_type, old, new)
    old_str = _format_value(event_type, old)
    new_str = _format_value(event_type, new)
    return (
        f"🔄 <b>REVİZYON</b> · {emoji} {head}\n"
        f"🚀 ADVANCE\n\n"
        f"<b>{period_label}</b> revize edildi:\n"
        f"  Eski: <code>{old_str}</code>\n"
        f"  Yeni: <code>{new_str}</code>\n"
        f"  Δ: <b>{delta_str}</b>\n\n"
        f"Orijinal yorumumuzu güncelleyecek bir veri — Advance kullanıcısı "
        f"olarak ilk gören sensin. Dashboard'da güncellenmiş story_md "
        f"kısa süre içinde yayınlanacak."
    )


async def _load_revision_context(event_id: str) -> Optional[dict]:
    """Son revision satırını + ilgili release'in event_type/released_at'ini çek."""
    sql = text("""
        SELECT v.id, v.event_id, v.event_type, v.old_actual_value, v.new_actual_value,
               v.delta_abs, v.delta_pct, v.broadcasted_at,
               r.released_at
        FROM macro_release_revisions v
        LEFT JOIN macro_releases r ON r.event_id = v.event_id
        WHERE v.event_id = :eid
        ORDER BY v.detected_at DESC
        LIMIT 1
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
    if not row:
        return None
    return {k: row[k] for k in row.keys()}


async def _stamp_revision_broadcast(revision_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE macro_release_revisions SET broadcasted_at = NOW() WHERE id = :id"),
            {"id": revision_id},
        )


async def broadcast_revision(event_id: str, *, force: bool = False) -> dict:
    """Latest revision for event_id'i Advance kullanıcılara push'la.

    Eşik kontrolü:
    - _REVISION_THRESHOLDS_ABS altı → skip (noise)
    - delta None → skip (audit row corrupt)

    Idempotency: revision row'un broadcasted_at stamp'i.
    Kill-switch: MACRO_REVISION_BROADCAST_ENABLED=false.
    """
    enabled = os.getenv(
        "MACRO_REVISION_BROADCAST_ENABLED", "true",
    ).strip().lower()
    if enabled in ("false", "0", "no"):
        logger.info(f"revision broadcast disabled, skipping {event_id}")
        return {"sent": 0, "failed": 0, "skipped_disabled": True}

    ctx = await _load_revision_context(event_id)
    if not ctx:
        return {"sent": 0, "failed": 0, "missing_revision": True}

    if not force and ctx.get("broadcasted_at"):
        return {"sent": 0, "failed": 0, "skipped_already_broadcasted": True}

    delta_abs = ctx.get("delta_abs")
    if delta_abs is None:
        return {"sent": 0, "failed": 0, "missing_delta": True}

    event_type = (ctx.get("event_type") or "").upper()
    if not _is_meaningful_revision(event_type, delta_abs):
        # Stamp anyway so we don't keep re-evaluating the same noise.
        await _stamp_revision_broadcast(ctx["id"])
        logger.info(
            f"revision below threshold: {event_id} |Δ|={abs(delta_abs)} "
            f"<{_REVISION_THRESHOLDS_ABS.get(event_type, Decimal('0'))} — skip"
        )
        return {"sent": 0, "failed": 0, "below_threshold": True}

    # Stale-period guard (2026-05-12): backfill operations (e.g. SA→NSA series
    # switch, retroactive data fix) trigger revision detection on historical
    # observations. Sending Advance users 14 messages for "revision" of January
    # data they never saw is noise. Skip broadcast if observation period is
    # older than 35 days — keeps real intra-cycle revisions (BLS often revises
    # last 2-3 months' NFP within first 5-10 days of next release) firing.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    released_at = ctx.get("released_at")
    if released_at is not None:
        try:
            age = _dt.now(_tz.utc) - released_at
            if age > _td(days=35):
                await _stamp_revision_broadcast(ctx["id"])
                logger.info(
                    f"revision stale-period skip: {event_id} "
                    f"released={released_at.isoformat()[:10]} age={age.days}d > 35d"
                )
                return {"sent": 0, "failed": 0, "stale_period": True}
        except Exception as e:
            logger.warning(f"revision stale check failed for {event_id}: {e}")

    period_label = _format_period(ctx.get("released_at"))
    message = _format_revision_message(
        event_type, period_label,
        old=ctx["old_actual_value"], new=ctx["new_actual_value"],
    )

    try:
        async with AsyncSessionLocal() as session:
            all_users = list((await session.execute(select(User))).scalars().all())
    except Exception as e:
        logger.error(f"revision broadcast user list query failed: {e}")
        return {"sent": 0, "failed": 0, "user_query_error": str(e)}

    recipients = [
        u for u in all_users
        if (getattr(u, "tier", "free") or "free").lower() == "advance"
    ]

    # Stamp first — intent matters for idempotency, partial failure
    # shouldn't trigger storms.
    await _stamp_revision_broadcast(ctx["id"])

    keyboard = _build_revision_keyboard(event_id)
    sent = 0
    failed = 0
    import asyncio as _asyncio
    for user in recipients:
        try:
            await _asyncio.to_thread(
                send_message_with_keyboard, user.telegram_id, message, keyboard,
            )
            sent += 1
        except Exception as e:
            failed += 1
            if "chat not found" not in str(e).lower():
                logger.warning(f"revision broadcast {event_id} -> {user.telegram_id}: {e}")

    logger.info(
        f"📣 revision broadcast {event_id}: "
        f"{sent} sent / {failed} failed / {len(recipients)} advance users"
    )
    return {
        "sent": sent,
        "failed": failed,
        "total_users": len(all_users),
        "advance_users": len(recipients),
        "event_type": event_type,
        "delta_abs": float(delta_abs),
        "threshold": float(_REVISION_THRESHOLDS_ABS.get(event_type, Decimal("0"))),
    }


async def broadcast_revision_safe(event_id: str, *, force: bool = False) -> None:
    """Fire-and-forget wrapper — never raises."""
    try:
        await broadcast_revision(event_id, force=force)
    except Exception as e:
        logger.error(f"broadcast_revision crashed for {event_id}: {e}")
