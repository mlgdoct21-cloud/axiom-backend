"""Watchlist supervisor — kategori-bazlı dossier yenileme + diff broadcast.

Akış (her tick = 30dk):
  - watchlist_items'ı çek (user_id, symbol, category, last_dossier_at, last_trigger_check_at)
  - Kategoriye göre yenile:
      long_term:   last_dossier_at None VEYA 7+ gün önceyse → dossier refresh
      swing:       last_dossier_at None VEYA 24+ saat önceyse → dossier refresh
                   AYRICA last_trigger_check_at 4+ saat önceyse → trigger proximity scan
  - Yeni snapshot üretildikten sonra:
      a) compute_structured_diff(prev, curr)
      b) yapısal değişiklik varsa summarize_with_gemini ile kategori+severity+özet
      c) dossier_diffs'e yaz; severity in {mid, high} ise Telegram'a kullanıya broadcast
  - last_dossier_at / last_trigger_check_at güncelle

Env:
  AXIOM_WATCHLIST_SUPERVISOR_ENABLED — default false. Açmak için Railway'de true.
  AXIOM_WATCHLIST_DIFF_SMART_ENABLED — Plan B Gemini call gate (services/watchlist_diff.py)
"""
from __future__ import annotations

import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text

from core.database import AsyncSessionLocal
from core.logger import get_logger
from services.dossier_service import get_or_build_dossier
from services.watchlist_diff import (
    compute_structured_diff,
    summarize_with_gemini,
    classify_trigger_proximity,
)

logger = get_logger("watchlist_supervisor")

TICK_SECONDS = int(os.getenv("AXIOM_WATCHLIST_TICK_SECONDS", "1800"))  # 30dk
LONG_TERM_REFRESH_HOURS = 24 * 7  # haftalık
SWING_REFRESH_HOURS = 24
SWING_TRIGGER_CHECK_HOURS = 4
STARTUP_DELAY_SECONDS = 120  # FastAPI startup'tan 2 dk sonra ilk tarama


def _gate_on(k: str, default: str = "false") -> bool:
    return os.getenv(k, default).strip().lower() in ("1", "true", "yes")


async def _fetch_prev_payload(symbol: str, before_id: Optional[int] = None) -> tuple[Optional[int], Optional[dict]]:
    """Bir önceki dossier snapshot (curr_id'den önceki). (id, payload) döner."""
    async with AsyncSessionLocal() as db:
        if before_id:
            sql = text("""
                SELECT id, payload FROM trade_dossiers
                WHERE symbol = :sym AND id < :bid
                ORDER BY id DESC LIMIT 1
            """)
            res = await db.execute(sql, {"sym": symbol, "bid": before_id})
        else:
            sql = text("""
                SELECT id, payload FROM trade_dossiers
                WHERE symbol = :sym
                ORDER BY id DESC OFFSET 1 LIMIT 1
            """)
            res = await db.execute(sql, {"sym": symbol})
        row = res.fetchone()
        if not row:
            return None, None
        return int(row[0]), row[1]


async def _fetch_latest_snapshot(symbol: str) -> tuple[Optional[int], Optional[dict]]:
    async with AsyncSessionLocal() as db:
        sql = text("""
            SELECT id, payload FROM trade_dossiers
            WHERE symbol = :sym
            ORDER BY id DESC LIMIT 1
        """)
        res = await db.execute(sql, {"sym": symbol})
        row = res.fetchone()
        if not row:
            return None, None
        return int(row[0]), row[1]


async def _persist_diff(
    *,
    user_id: int,
    symbol: str,
    prev_id: Optional[int],
    curr_id: int,
    diff_type: str,
    severity: str,
    summary: str,
    details: dict,
) -> int:
    async with AsyncSessionLocal() as db:
        sql = text("""
            INSERT INTO dossier_diffs
                (user_id, symbol, prev_snapshot_id, curr_snapshot_id,
                 diff_type, severity, summary, details, created_at)
            VALUES
                (:uid, :sym, :pid, :cid, :dt, :sv, :sm, :dd, NOW())
            RETURNING id
        """)
        res = await db.execute(sql, {
            "uid": user_id, "sym": symbol, "pid": prev_id, "cid": curr_id,
            "dt": diff_type, "sv": severity, "sm": summary,
            "dd": json.dumps(details, ensure_ascii=False),
        })
        row = res.fetchone()
        await db.commit()
        return int(row[0]) if row else 0


async def _broadcast_diff(
    *,
    user_id: int,
    symbol: str,
    diff_id: int,
    diff_type: str,
    severity: str,
    summary: str,
    curr_payload: dict,
) -> None:
    """Telegram'a kullanıya watchlist diff push'la. severity in {mid, high} için."""
    if severity not in ("mid", "high"):
        return
    try:
        from services.telegram_bot import send_telegram_message
    except Exception as e:
        logger.warning(f"telegram_bot import fail: {e}")
        return

    async with AsyncSessionLocal() as db:
        sql = text("SELECT telegram_id FROM users WHERE id = :uid")
        res = await db.execute(sql, {"uid": user_id})
        row = res.fetchone()
        if not row or not row[0]:
            return
        chat_id = row[0]

    emoji = "🚨" if severity == "high" else "🟡"
    type_label = {
        "decision_shift": "Karar değişti",
        "target_change": "Hedef güncellendi",
        "trigger_near": "Tetik seviyesi yakın",
        "risk_escalation": "Yeni risk",
        "thesis_change": "Tez güncellendi",
        "stop_change": "Stop kaydı",
        "conviction_shift": "İkna kaydı",
    }.get(diff_type, "Değişiklik")

    verdict = (curr_payload or {}).get("verdict", "?")
    conviction = (curr_payload or {}).get("conviction", "?")
    msg = (
        f"{emoji} <b>{symbol}</b> — {type_label}\n"
        f"{summary}\n\n"
        f"Şu anki dossier: <b>{verdict}</b> · ikna {conviction}/5\n"
        f"<i>Dashboard'tan dossier'ı aç → tam analiz</i>"
    )
    try:
        await asyncio.to_thread(send_telegram_message, chat_id, msg, True)
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE dossier_diffs SET sent_at = NOW() WHERE id = :did"),
                {"did": diff_id},
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"Watchlist broadcast hata {symbol}/{user_id}: {e}")


async def _process_item(item: dict, now: datetime) -> None:
    """Tek watchlist item'ı işle: refresh gerekiyorsa dossier üret, diff hesapla, persist+broadcast."""
    user_id = item["user_id"]
    symbol = item["symbol"]
    category = item["category"]
    last_dossier_at: Optional[datetime] = item.get("last_dossier_at")
    last_trigger_at: Optional[datetime] = item.get("last_trigger_check_at")

    refresh_threshold = LONG_TERM_REFRESH_HOURS if category == "long_term" else SWING_REFRESH_HOURS
    needs_refresh = (last_dossier_at is None) or (
        (now - last_dossier_at).total_seconds() / 3600.0 >= refresh_threshold
    )

    needs_trigger_scan = category == "swing" and (
        last_trigger_at is None
        or (now - last_trigger_at).total_seconds() / 3600.0 >= SWING_TRIGGER_CHECK_HOURS
    )

    if not needs_refresh and not needs_trigger_scan:
        return

    if needs_refresh:
        prev_id, prev_payload = await _fetch_latest_snapshot(symbol)
        logger.info(f"watchlist refresh start: {symbol} ({category}) user={user_id}")
        try:
            result = await get_or_build_dossier(symbol, force_refresh=True)
        except Exception as e:
            logger.error(f"watchlist dossier üretim hata {symbol}: {e}")
            return

        curr_id, curr_payload = await _fetch_latest_snapshot(symbol)
        if not curr_id or not curr_payload:
            return

        # Structured diff
        structured = compute_structured_diff(prev_payload, curr_payload)
        if structured:
            summary_obj = await summarize_with_gemini(
                symbol=symbol,
                category=category,
                structured=structured,
                prev_payload=prev_payload or {},
                curr_payload=curr_payload,
                prev_ts=(item.get("last_dossier_at") or now).isoformat() if item.get("last_dossier_at") else "ilk",
                curr_ts=now.isoformat(),
            )
            diff_id = await _persist_diff(
                user_id=user_id, symbol=symbol, prev_id=prev_id, curr_id=curr_id,
                diff_type=summary_obj.get("category", "thesis_change"),
                severity=summary_obj.get("severity", "low"),
                summary=summary_obj.get("summary", ""),
                details={"structured": structured, "model_error": summary_obj.get("model_error")},
            )
            await _broadcast_diff(
                user_id=user_id, symbol=symbol, diff_id=diff_id,
                diff_type=summary_obj.get("category", "thesis_change"),
                severity=summary_obj.get("severity", "low"),
                summary=summary_obj.get("summary", ""),
                curr_payload=curr_payload,
            )

        # last_dossier_at update
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE watchlist_items SET last_dossier_at = NOW() WHERE user_id = :uid AND symbol = :sym"),
                {"uid": user_id, "sym": symbol},
            )
            await db.commit()

    if needs_trigger_scan:
        # Trigger fiyat yakınlığı kontrolü — son snapshot'a bakar
        _id, curr_payload = await _fetch_latest_snapshot(symbol)
        if curr_payload:
            last_close = ((curr_payload.get("_data") or {}).get("ta") or {}).get("last_close")
            prox = classify_trigger_proximity(curr_payload, last_close)
            if prox:
                diff_id = await _persist_diff(
                    user_id=user_id, symbol=symbol, prev_id=None, curr_id=_id or 0,
                    diff_type=prox["type"], severity=prox["severity"],
                    summary=prox["summary"], details=prox["details"],
                )
                await _broadcast_diff(
                    user_id=user_id, symbol=symbol, diff_id=diff_id,
                    diff_type=prox["type"], severity=prox["severity"],
                    summary=prox["summary"], curr_payload=curr_payload,
                )

        async with AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE watchlist_items SET last_trigger_check_at = NOW() WHERE user_id = :uid AND symbol = :sym"),
                {"uid": user_id, "sym": symbol},
            )
            await db.commit()


async def _tick() -> None:
    """Tek tarama: tüm watchlist item'larını sırayla işle (rate-limit dostu)."""
    async with AsyncSessionLocal() as db:
        sql = text("""
            SELECT user_id, symbol, category, last_dossier_at, last_trigger_check_at
            FROM watchlist_items
            ORDER BY id ASC
        """)
        res = await db.execute(sql)
        rows = res.fetchall()

    if not rows:
        logger.info("watchlist boş — tick atlandı")
        return

    now = datetime.now(timezone.utc)
    logger.info(f"watchlist tick: {len(rows)} item taranıyor")
    processed = 0
    for r in rows:
        item = {
            "user_id": int(r[0]),
            "symbol": str(r[1]).upper(),
            "category": str(r[2]),
            "last_dossier_at": r[3],
            "last_trigger_check_at": r[4],
        }
        try:
            await _process_item(item, now)
            processed += 1
        except Exception as e:
            logger.error(f"watchlist item hata {item['symbol']}/{item['user_id']}: {e}", exc_info=True)
        # Sembol arası küçük nefes — Gemini/FMP rate-limit dostu
        await asyncio.sleep(2)
    logger.info(f"watchlist tick bitti: {processed}/{len(rows)} işlendi")


async def watchlist_supervisor() -> None:
    """Lifespan-bound supervisor. AXIOM_WATCHLIST_SUPERVISOR_ENABLED gate."""
    if not _gate_on("AXIOM_WATCHLIST_SUPERVISOR_ENABLED"):
        logger.info("🟡 Watchlist supervisor devre dışı (AXIOM_WATCHLIST_SUPERVISOR_ENABLED=false)")
        return

    logger.info(
        f"Watchlist supervisor başladı: tick={TICK_SECONDS}s, "
        f"long_term refresh ≥{LONG_TERM_REFRESH_HOURS}h, swing ≥{SWING_REFRESH_HOURS}h, "
        f"swing trigger scan ≥{SWING_TRIGGER_CHECK_HOURS}h"
    )
    try:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                await _tick()
            except Exception as e:
                logger.error(f"watchlist tick genel hata: {e}", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)
    except asyncio.CancelledError:
        logger.info("Watchlist supervisor iptal edildi")
        raise
