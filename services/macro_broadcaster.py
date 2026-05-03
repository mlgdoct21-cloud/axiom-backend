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
from services.macro_sources.sector_labels import label_tr
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


_GAUGE_CELLS = 8

# Event types where actual/prior are price indices and MoM% is the headline
# reading. NFP is jobs added (raw count); FOMC has no actual_value at all.
_PCT_DELTA_EVENTS = frozenset({"CPI", "PCE", "CORE_CPI", "CORE_PCE"})

# Turkish month names indexed 1..12 — date.strftime("%B") gives English in
# the Railway container regardless of locale config, and we want a clean
# "Mart 2026" without yanking the locale module.
_TR_MONTHS = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]


def _mom_pct(actual: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Compute month-on-month % change. Returns None when not computable."""
    if actual is None or prior is None:
        return None
    try:
        a = float(actual)
        p = float(prior)
    except (TypeError, ValueError):
        return None
    if p == 0:
        return None
    return (a - p) / abs(p) * 100.0


def _format_period(released_at) -> Optional[str]:
    """'Mart 2026' from a released_at date (or None)."""
    if released_at is None:
        return None
    month = getattr(released_at, "month", None)
    year = getattr(released_at, "year", None)
    if not month or not year:
        return None
    return f"{_TR_MONTHS[month]} {year}"


def _render_headline_metric(release: dict) -> Optional[str]:
    """For % event types, render 'Mart 2026 MoM +0.87%'. None otherwise."""
    et = (release.get("event_type") or "").upper()
    if et not in _PCT_DELTA_EVENTS:
        return None
    pct = _mom_pct(release.get("actual_value"), release.get("prior_value"))
    if pct is None:
        return None
    period = _format_period(release.get("released_at")) or ""
    sign = "+" if pct >= 0 else ""
    if period:
        return f"📈 {period} MoM <b>{sign}{pct:.2f}%</b>"
    return f"📈 MoM <b>{sign}{pct:.2f}%</b>"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{float(v):.2f}%"


def _render_metric_row(label: str, prior: Optional[float], expected: Optional[float], actual: Optional[float]) -> Optional[str]:
    """One row of the prior | expected | actual block. None when nothing
    is computable (all three missing).
    """
    if prior is None and expected is None and actual is None:
        return None
    return (
        f"<b>{html.escape(label)}</b>  "
        f"Önceki {_fmt_pct(prior)} · Beklenti {_fmt_pct(expected)} · "
        f"Gelen {_fmt_pct(actual)}"
    )


def _render_metrics_block(headline: dict, core: Optional[dict]) -> Optional[str]:
    """4-row prior | expected | actual table, headline + paired Core if any.
    Skips rows that are entirely empty."""
    et = (headline.get("event_type") or "").upper()
    if et not in _PCT_DELTA_EVENTS:
        return None
    head_label_mom = f"{_short_label(et)} MoM"
    head_label_yoy = f"{_short_label(et)} YoY"
    rows = []
    r = _render_metric_row(
        head_label_mom,
        headline.get("prior_mom_pct"),
        headline.get("expected_mom_pct"),
        headline.get("mom_pct"),
    )
    if r:
        rows.append(r)
    if core:
        r = _render_metric_row(
            f"{_short_label((core.get('event_type') or '').upper())} MoM",
            core.get("prior_mom_pct"),
            core.get("expected_mom_pct"),
            core.get("mom_pct"),
        )
        if r:
            rows.append(r)
    r = _render_metric_row(
        head_label_yoy,
        headline.get("prior_yoy_pct"),
        headline.get("expected_yoy_pct"),
        headline.get("yoy_pct"),
    )
    if r:
        rows.append(r)
    if core:
        r = _render_metric_row(
            f"{_short_label((core.get('event_type') or '').upper())} YoY",
            core.get("prior_yoy_pct"),
            core.get("expected_yoy_pct"),
            core.get("yoy_pct"),
        )
        if r:
            rows.append(r)
    return "\n".join(rows) if rows else None


_SHORT_LABEL = {
    "CPI": "CPI",
    "PCE": "PCE",
    "CORE_CPI": "Çekirdek CPI",
    "CORE_PCE": "Çekirdek PCE",
    "NFP": "NFP",
}


def _short_label(et: str) -> str:
    return _SHORT_LABEL.get(et, et)


def _render_market_reaction(reaction: Optional[dict]) -> Optional[str]:
    """'📉 Piyasa tepkisi (T+5dk): DXY +0.4% | SPY -0.6% | US10Y +6bp'.
    Returns None when neither delta is computable.
    """
    if not reaction:
        return None
    dxy = reaction.get("dxy_change_pct")
    spy = reaction.get("spy_change_pct")
    us10y = reaction.get("us10y_change_bp")
    if dxy is None and spy is None and us10y is None:
        return None
    parts = []
    if dxy is not None:
        sign = "+" if dxy >= 0 else ""
        parts.append(f"DXY {sign}{dxy:.2f}%")
    if spy is not None:
        sign = "+" if spy >= 0 else ""
        parts.append(f"SPY {sign}{spy:.2f}%")
    if us10y is not None:
        sign = "+" if us10y >= 0 else ""
        parts.append(f"US10Y {sign}{us10y:.0f}bp")
    return f"📉 Piyasa tepkisi (T+5dk): {' | '.join(parts)}"


def _render_gauge(score: Optional[float]) -> Optional[str]:
    """ASCII gauge for a 0..1 sentiment score: ━━━●━━━━ 0.42.
    Returns None when there's nothing to render (no score yet).
    """
    if score is None:
        return None
    s = max(0.0, min(1.0, float(score)))
    pos = int(round(s * (_GAUGE_CELLS - 1)))
    cells = ["━"] * _GAUGE_CELLS
    cells[pos] = "●"
    return f"{''.join(cells)} {s:.2f}"


def _render_sectors(positive, negative) -> Optional[str]:
    """Two-line sector chip block. Returns None when both lists are empty.
    Inputs may be JSON strings (Postgres JSONB round-tripped) or lists.
    """
    pos = [label_tr(s) for s in _coerce_list(positive)]
    neg = [label_tr(s) for s in _coerce_list(negative)]
    if not pos and not neg:
        return None
    lines = []
    if pos:
        lines.append(f"↑ {html.escape(', '.join(pos))}")
    if neg:
        lines.append(f"↓ {html.escape(', '.join(neg))}")
    return "\n".join(lines)


def _coerce_list(v) -> list:
    if v is None:
        return []
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


def _format_message(release: dict, core: Optional[dict] = None) -> str:
    et = (release.get("event_type") or "").upper()
    emoji = _EMOJI.get(et, "📰")
    headline = _HEADLINE.get(et, et or "Makro Veri")
    narrative = (release.get("narrative_md") or "").strip()
    score = _to_float(release.get("sentiment_score"))
    sentiment = _sentiment_chip(score)
    gauge = _render_gauge(score)
    sectors = _render_sectors(
        release.get("sectors_positive"), release.get("sectors_negative"),
    )
    src_url = release.get("source_url") or ""

    parts = [
        f"{emoji} <b>{html.escape(headline)}</b>",
    ]
    period = _format_period(release.get("released_at"))
    if period:
        parts.append(f"📅 {period}")
    metrics_block = _render_metrics_block(release, core)
    if metrics_block:
        parts.append("")
        parts.append(metrics_block)
    parts.append("")
    parts.append(html.escape(narrative))
    if sectors:
        parts.append("")
        parts.append(sectors)
    market_line = _render_market_reaction(release.get("market_reaction"))
    if market_line:
        parts.append("")
        parts.append(f"<i>{market_line}</i>")
    if sentiment or gauge:
        parts.append("")
        if sentiment:
            parts.append(f"<i>Piyasa tonu:{sentiment}</i>")
        if gauge:
            parts.append(f"<code>{gauge}</code>")
    if src_url:
        safe = html.escape(src_url, quote=True)
        parts.append("")
        parts.append(f"🔗 <a href='{safe}'>Resmi kaynak</a>")
    parts.append("")
    parts.append("<i>⚠️ Yatırım tavsiyesi değildir.</i>")
    return "\n".join(parts)


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _load_release(event_id: str) -> Optional[dict]:
    """Load the row + delegate to macro_public's enricher so the broadcaster
    sees the same MoM/YoY/expected/prior derivatives as the dashboard.
    """
    from routers.v1.macro_public import _enrich_release, _fetch_paired_core, _SELECT_COLS
    sql = text(f"SELECT {_SELECT_COLS} FROM macro_releases WHERE event_id = :eid")
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
        if not row:
            return None
        enriched = await _enrich_release(conn, row)
        core = await _fetch_paired_core(conn, row)
    enriched["_core_release"] = core
    return enriched


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

    core = release.pop("_core_release", None)
    message = _format_message(release, core=core)

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
