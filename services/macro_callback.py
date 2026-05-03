"""Telegram inline-keyboard callbacks for macro broadcasts.

Each broadcast attaches two buttons:
  📊 Tarihsel kıyaslama  → callback_data = "macro_hist:<event_id>"
  💼 Etkilenen hisseler → callback_data = "macro_stocks:<event_id>"

The polling loop in services/telegram_bot.py routes these prefixes here.
We send a follow-up message (rather than editing the original) so the
broadcast stays intact and users can drill in multiple times.
"""
from __future__ import annotations

import asyncio
import html
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.sector_labels import label_tr, tickers_for_sectors
from services.telegram_bot import (
    answer_callback_query,
    send_telegram_message,
)

logger = get_logger("macro.callback")

_HIST_MONTHS = 6  # how many months back to show in the comparison


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
    pos_tickers = tickers_for_sectors(pos)
    neg_tickers = tickers_for_sectors(neg)

    lines = [f"💼 <b>{html.escape(row['event_type'])} — Etkilenen hisseler</b>", ""]
    if pos:
        sector_label = ", ".join(label_tr(s) for s in pos)
        lines.append(f"🟢 <b>Olumlu</b> ({html.escape(sector_label)}):")
        if pos_tickers:
            lines.append("  " + "  ".join(f"<code>{html.escape(t)}</code>" for t in pos_tickers))
        else:
            lines.append("  (hisse listesi boş)")
        lines.append("")
    if neg:
        sector_label = ", ".join(label_tr(s) for s in neg)
        lines.append(f"🔴 <b>Olumsuz</b> ({html.escape(sector_label)}):")
        if neg_tickers:
            lines.append("  " + "  ".join(f"<code>{html.escape(t)}</code>" for t in neg_tickers))
        else:
            lines.append("  (hisse listesi boş)")
    if not pos and not neg:
        lines.append("Bu release için sektör etkisi henüz kaydedilmedi.")
    lines.append("")
    lines.append("<i>⚠️ Yatırım tavsiyesi değildir.</i>")
    return "\n".join(lines)


async def handle_callback(callback_query_id: str, chat_id, data: str) -> None:
    """Route a macro_* callback to the matching payload builder + reply.
    Always acknowledges the callback so the Telegram spinner clears."""
    try:
        if ":" not in data:
            answer_callback_query(callback_query_id)
            return
        prefix, event_id = data.split(":", 1)
        if prefix == "macro_hist":
            text_out = await _hist_payload(event_id)
        elif prefix == "macro_stocks":
            text_out = await _stocks_payload(event_id)
        else:
            answer_callback_query(callback_query_id)
            return
        await asyncio.to_thread(send_telegram_message, chat_id, text_out)
    except Exception as e:
        logger.error(f"handle_callback failed for {data}: {e}")
    finally:
        answer_callback_query(callback_query_id)
