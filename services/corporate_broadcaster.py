"""Kurumsal Sentez Telegram broadcaster — Commit 3.

macro_broadcaster.py (broadcast_story) forku. KORUNAN: tier-strict
recipient, STAMP-BEFORE-FANOUT idempotency, Free 5dk gecikmeli watermark,
strong-ref set (GC guard). DEĞİŞEN: kaynak tablo = corporate_syntheses,
kill-switch **default OFF**, 24h defense-in-depth cap.

EMNİYET: `CORPORATE_SYNTH_BROADCAST_ENABLED` default "false". Açıkça
true yapılana kadar broadcast YOK — sentez yalnız DB'ye yazılır. Bu,
Commit 3'ün canlı kullanıcıya spam atmadan ship edilmesini sağlar.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.future import select

from core.database import AsyncSessionLocal, engine
from core.logger import get_logger
from models.user import User
from services.telegram_bot import send_telegram_message

logger = get_logger("corporate.broadcaster")

_ENABLED_DEFAULT = "false"  # macro "true"; kurumsal sentez OFF (emniyet)
_TELEGRAM_LIMIT = 3600       # 4096 cap; başlık + watermark + SSL payı
_FREE_DELAY_SECONDS = 300
_24H_SECONDS = 24 * 3600

_FREE_WATERMARK = (
    "🔒 <i>Bu AXIOM Kurumsal Sentez özetidir. Premium/Advance üyeler "
    "Pazartesi sabahı erken ve tam sürüm alır.</i>\n\n"
)
# Free tier yalnız kısa teaser görür (tam premium gövdesi DEĞİL).
# Dashboard corporate_public._TEASER_CHARS ile tutarlı.
_FREE_TEASER_CHARS = 280
_FREE_UPGRADE_CTA = (
    "\n\n💎 <i>Tam haftalık kurumsal sentez Premium/Advance "
    "üyelere açıktır.</i>"
)

# Strong-ref set — gecikmeli free fanout task'i GC silmesin (memory feedback).
_CORP_INFLIGHT: set = set()


def _enabled() -> bool:
    return os.getenv("CORPORATE_SYNTH_BROADCAST_ENABLED", _ENABLED_DEFAULT) \
        .strip().lower() not in ("false", "0", "no", "")


async def _load_row(event_id: str, tier: str) -> Optional[dict]:
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(
                text(
                    "SELECT event_id, tier, week_start, synthesis_md, "
                    "broadcasted_premium_at, broadcasted_advance_at "
                    "FROM corporate_syntheses WHERE event_id=:e AND tier=:t"
                ),
                {"e": event_id, "t": tier},
            )).mappings().first()
        return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"corp broadcast load row failed {event_id}/{tier}: {e}")
        return None


# 24h içinde bu kadar FARKLI event broadcast edilmişse → storm say, blokla.
# 1 bitişik meşru yayına (geçen haftanın catch-up'ı + bu haftanın normal
# yayını, ~10h arayla) izin verir; gerçek koruma zaten per-event stamp.
_STORM_DISTINCT_THRESHOLD = 2


async def _recent_broadcast_within_24h(event_id: str) -> bool:
    """Defense-in-depth storm guard: son 24 saatte (bu event hariç) KAÇ
    FARKLI kurumsal event broadcast edilmiş? Eşik (2) ve üzeri → storm
    (örn. event_id churn bug'ı) → blokla. Tek bitişik meşru broadcast
    ENGELLENMEZ. Aynı event'in tekrarı zaten per-event stamp ile bloklanır."""
    try:
        async with engine.begin() as conn:
            cnt = (await conn.execute(
                text(
                    "SELECT COUNT(DISTINCT event_id) FROM corporate_syntheses "
                    "WHERE event_id <> :e AND ("
                    "  broadcasted_premium_at > NOW() - INTERVAL '24 hours' "
                    "  OR broadcasted_advance_at > NOW() - INTERVAL '24 hours')"
                ),
                {"e": event_id},
            )).scalar()
        return (cnt or 0) >= _STORM_DISTINCT_THRESHOLD
    except Exception as e:  # noqa: BLE001
        logger.info(f"corp 24h-cap check skip: {e}")
        return False


async def _stamp(event_id: str, tier: str) -> None:
    col = "broadcasted_premium_at" if tier == "premium" else "broadcasted_advance_at"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(f"UPDATE corporate_syntheses SET {col} = NOW() "
                     "WHERE event_id=:e AND tier=:t"),
                {"e": event_id, "t": tier},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"corp stamp failed {event_id}/{tier}: {e}")


def _md_to_tg_html(md: str) -> str:
    """Markdown gövdesini Telegram-HTML'e çevir. ÖNCE html.escape (içerik
    güvenliği: <>&), SONRA güvenli işaret dönüşümü:
      - satır başı '## Başlık' → <b>Başlık</b>
      - '**kalın**' (eşli) → <b>kalın</b>
    Üretilen <b> etiketleri DAİMA dengeli: başlık tüm satırı sarar, kalın
    yalnız eşli ** ikilisini eşler (tek kalan ** literal kalır). Malformed
    HTML → Telegram 400 → send_telegram_message sessizce yutar (mesaj
    gitmez) olduğu için denge kritik. Aksi halde synthesis_md ham '## ...'
    olarak görünür."""
    safe = html.escape(md or "")
    safe = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*(.+?)[ \t]*$", r"<b>\1</b>", safe)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    return safe


def _format_message(tier: str, week_start, synthesis_md: str) -> str:
    badge = "💎 PREMIUM" if tier == "premium" else "🚀 ADVANCE"
    head = (f"🏛️ <b>AXIOM Kurumsal Sentez</b> · {badge}\n"
            f"<i>Hafta: {html.escape(str(week_start))}</i>\n\n")
    body = synthesis_md or ""
    budget = _TELEGRAM_LIMIT - len(head)
    if len(body) > budget:
        body = body[:budget - 20].rstrip() + "\n…(devamı dashboard'da)"
    return head + _md_to_tg_html(body)


def _format_free_teaser(week_start, synthesis_md: str) -> str:
    """Free tier: marka başlığı + ilk ~280 char alıntı + watermark + CTA.
    Tam premium gövdesi GÖNDERİLMEZ (dashboard teaser deseniyle aynı)."""
    head = (f"🏛️ <b>AXIOM Kurumsal Sentez</b>\n"
            f"<i>Hafta: {html.escape(str(week_start))}</i>\n\n")
    full = (synthesis_md or "").strip()
    excerpt = full[:_FREE_TEASER_CHARS].rstrip()
    if len(full) > _FREE_TEASER_CHARS:
        excerpt += "…"
    return _FREE_WATERMARK + head + _md_to_tg_html(excerpt) + _FREE_UPGRADE_CTA


async def _fanout(message: str, users: list) -> tuple[int, int]:
    sent = failed = 0
    for u in users:
        try:
            await asyncio.to_thread(send_telegram_message, u.telegram_id, message)
            sent += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if "chat not found" not in str(e).lower():
                logger.warning(f"corp broadcast -> {u.telegram_id}: {e}")
    return sent, failed


async def _delayed_free_fanout(message: str, free_users: list) -> None:
    try:
        await asyncio.sleep(_FREE_DELAY_SECONDS)
        sent, failed = await _fanout(message, free_users)
        logger.info(f"📣 corp broadcast [FREE +5min]: {sent}/{failed}/"
                    f"{len(free_users)}")
    except asyncio.CancelledError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"corp delayed free fanout error: {e}")


async def broadcast_synthesis(
    event_id: str, tier: str, *, force: bool = False,
) -> dict:
    """Bir kurumsal sentezi tier-strict yayınla. Kill-switch default OFF.

    Idempotency: corporate_syntheses.broadcasted_<tier>_at stamp. 24h cap
    defense-in-depth. Free 5dk gecikmeli watermark.
    """
    if tier not in ("premium", "advance"):
        return {"sent": 0, "failed": 0, "invalid_tier": tier}
    if not _enabled():
        logger.info(f"corp broadcast disabled (default OFF), skip {event_id}/{tier}")
        return {"sent": 0, "failed": 0, "skipped_disabled": True}

    row = await _load_row(event_id, tier)
    if not row:
        return {"sent": 0, "failed": 0, "missing_row": True}

    stamp_col = "broadcasted_premium_at" if tier == "premium" else "broadcasted_advance_at"
    if not force and row.get(stamp_col):
        return {"sent": 0, "failed": 0, "skipped_already_broadcasted": True}
    if not force and await _recent_broadcast_within_24h(event_id):
        logger.info(f"corp broadcast 24h-cap, skip {event_id}/{tier}")
        return {"sent": 0, "failed": 0, "skipped_24h_cap": True}

    msg = _format_message(tier, row.get("week_start"), row.get("synthesis_md") or "")

    try:
        async with AsyncSessionLocal() as session:
            all_users = list((await session.execute(select(User))).scalars().all())
    except Exception as e:  # noqa: BLE001
        logger.error(f"corp broadcast user query failed: {e}")
        return {"sent": 0, "failed": 0, "user_query_error": str(e)}

    recipients = [
        u for u in all_users
        if (getattr(u, "tier", "free") or "free").lower() == tier
    ]
    free_users = [
        u for u in all_users
        if (getattr(u, "tier", "free") or "free").lower() == "free"
    ]

    # STAMP BEFORE FANOUT — niyet idempotency (partial fail re-broadcast storm engeli).
    await _stamp(event_id, tier)

    sent, failed = await _fanout(msg, recipients)
    logger.info(f"📣 corp broadcast {event_id}/{tier}: {sent}/{failed}/"
                f"{len(recipients)} eligible")

    # Free-tier yalnız premium broadcast'inde teaser alır (advance'i tekrar
    # göndermeyiz — çift mesaj engeli).
    if tier == "premium" and free_users:
        try:
            free_msg = _format_free_teaser(
                row.get("week_start"), row.get("synthesis_md") or ""
            )
            task = asyncio.create_task(
                _delayed_free_fanout(free_msg, free_users)
            )
            _CORP_INFLIGHT.add(task)
            task.add_done_callback(_CORP_INFLIGHT.discard)
        except Exception as e:  # noqa: BLE001
            logger.error(f"corp free delay schedule fail: {e}")

    return {
        "sent": sent, "failed": failed, "tier": tier,
        "eligible_users": len(recipients), "free_delayed": len(free_users),
    }


async def broadcast_synthesis_safe(
    event_id: str, tier: str, *, force: bool = False,
) -> None:
    """Fire-and-forget wrapper — asla raise etmez (supervisor güvenliği)."""
    try:
        await broadcast_synthesis(event_id, tier, force=force)
    except Exception as e:  # noqa: BLE001
        logger.error(f"broadcast_synthesis crashed {event_id}/{tier}: {e}")
