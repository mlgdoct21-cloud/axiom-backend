"""Telegram inline-keyboard callbacks for macro broadcasts.

Each broadcast attaches two buttons:
  📊 Tarihsel kıyaslama  → callback_data = "macro_hist:<event_id>"
  💼 Etkilenen hisseler → callback_data = "macro_stocks:<event_id>"

The polling loop in services/telegram_bot.py routes these prefixes here.
We send a follow-up message (rather than editing the original) so the
broadcast stays intact and users can drill in multiple times.

Two protections against spam / accidental re-clicks:
- Per-(callback_data) result cache, 60s TTL — same DB query isn't repeated
  if 100 users mash the button at once after a broadcast.
- Per-chat_id rate-limit, 1 callback every 3s — keeps a single user from
  firing rapid repeats that would still pass through to FRED/Telegram.
"""
from __future__ import annotations

import asyncio
import html
import time
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.sector_labels import (
    bist_tickers_for_sectors,
    label_tr,
    tickers_for_sectors,
)
from services.telegram_bot import (
    answer_callback_query,
    send_telegram_message,
)

logger = get_logger("macro.callback")

_HIST_MONTHS = 6  # how many months back to show in the comparison
_CACHE_TTL_SECONDS = 60
_RATE_LIMIT_SECONDS = 3

# (callback_data) → (expires_at_monotonic, payload_text)
_PAYLOAD_CACHE: dict[str, tuple[float, str]] = {}
# chat_id → last_invoked_at_monotonic
_LAST_INVOKED: dict[str, float] = {}


def _cache_get(key: str) -> Optional[str]:
    entry = _PAYLOAD_CACHE.get(key)
    if not entry:
        return None
    expires, payload = entry
    if time.monotonic() > expires:
        _PAYLOAD_CACHE.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload: str) -> None:
    _PAYLOAD_CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, payload)


def _rate_limited(chat_id) -> bool:
    """Returns True when the chat called us within the rate-limit window."""
    cid = str(chat_id)
    now = time.monotonic()
    last = _LAST_INVOKED.get(cid)
    if last is not None and (now - last) < _RATE_LIMIT_SECONDS:
        return True
    _LAST_INVOKED[cid] = now
    return False


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.2f}%"


def _fmt_jobs_k(v) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.0f}K"


_TR_MONTHS = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]


def _fmt_period(released_at) -> str:
    if released_at is None:
        return "?"
    try:
        return f"{_TR_MONTHS[released_at.month]} {released_at.year}"
    except Exception:
        return "?"


async def _hist_payload(event_id: str) -> str:
    """Build the 'Tarihsel kıyaslama' reply text for one event_id."""
    parts = event_id.split(":")
    if len(parts) < 3 or parts[0] != "fred":
        return "Bu release için tarihsel veri yok."
    et = parts[1]
    sql = text("""
        SELECT released_at, actual_value, prior_value
        FROM macro_releases
        WHERE event_type = :et AND source = 'fred' AND actual_value IS NOT NULL
        ORDER BY released_at DESC
        LIMIT :limit
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {"et": et, "limit": _HIST_MONTHS})).mappings().all()
    if not rows:
        return f"{et} için tarihsel veri bulunamadı."
    rows = list(reversed(rows))  # oldest first
    lines = [f"📊 <b>{html.escape(et)} — son {_HIST_MONTHS} ay</b>", ""]
    for i, r in enumerate(rows):
        actual = float(r["actual_value"]) if r["actual_value"] is not None else None
        prior = float(r["prior_value"]) if r["prior_value"] is not None else None
        period = _fmt_period(r["released_at"])
        if et == "NFP":
            change = (actual - prior) if (actual is not None and prior is not None) else None
            val = _fmt_jobs_k(change)
        else:
            pct = ((actual - prior) / abs(prior) * 100) if (actual is not None and prior is not None and prior != 0) else None
            val = _fmt_pct(pct)
        marker = " ← bu ay" if i == len(rows) - 1 else ""
        lines.append(f"  {html.escape(period)}: <b>{val}</b>{marker}")
    return "\n".join(lines)


async def _stocks_payload(event_id: str) -> str:
    """Build the 'Etkilenen hisseler' reply text for one event_id."""
    sql = text("""
        SELECT event_type, sectors_positive, sectors_negative
        FROM macro_releases WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
    if not row:
        return "Release bulunamadı."

    def _coerce(v) -> list:
        if isinstance(v, list):
            return [str(x) for x in v if x]
        if isinstance(v, str):
            try:
                import json as _json
                parsed = _json.loads(v)
                return [str(x) for x in parsed if x] if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    pos = _coerce(row["sectors_positive"])
    neg = _coerce(row["sectors_negative"])

    def _block(direction_label: str, emoji: str, sectors: list) -> list:
        if not sectors:
            return []
        sector_label = ", ".join(label_tr(s) for s in sectors)
        us = tickers_for_sectors(sectors)
        bist = bist_tickers_for_sectors(sectors)
        out = [f"{emoji} <b>{direction_label}</b> ({html.escape(sector_label)}):"]
        if us:
            out.append("  🇺🇸 " + "  ".join(f"<code>{html.escape(t)}</code>" for t in us))
        if bist:
            out.append("  🇹🇷 " + "  ".join(f"<code>{html.escape(t)}</code>" for t in bist))
        if not us and not bist:
            out.append("  (hisse listesi boş)")
        out.append("")
        return out

    lines = [f"💼 <b>{html.escape(row['event_type'])} — Etkilenen hisseler</b>", ""]
    lines += _block("Olumlu", "🟢", pos)
    lines += _block("Olumsuz", "🔴", neg)
    if not pos and not neg:
        lines.append("Bu release için sektör etkisi henüz kaydedilmedi.")
        lines.append("")
    lines.append("<i>⚠️ Yatırım tavsiyesi değildir.</i>")
    return "\n".join(lines)


async def handle_callback(callback_query_id: str, chat_id, data: str) -> None:
    """Route a macro_* callback to the matching payload builder + reply.
    Always acknowledges the callback so the Telegram spinner clears.
    Rate-limited per chat_id; result cached per callback_data.
    """
    try:
        if ":" not in data:
            answer_callback_query(callback_query_id)
            return
        if _rate_limited(chat_id):
            logger.debug(f"rate-limited callback from chat={chat_id}")
            answer_callback_query(callback_query_id)
            return

        cached = _cache_get(data)
        if cached is not None:
            await asyncio.to_thread(send_telegram_message, chat_id, cached)
            return

        prefix, event_id = data.split(":", 1)
        if prefix == "macro_hist":
            text_out = await _hist_payload(event_id)
        elif prefix == "macro_stocks":
            text_out = await _stocks_payload(event_id)
        else:
            answer_callback_query(callback_query_id)
            return
        _cache_put(data, text_out)
        await asyncio.to_thread(send_telegram_message, chat_id, text_out)
    except Exception as e:
        logger.error(f"handle_callback failed for {data}: {e}")
    finally:
        answer_callback_query(callback_query_id)
