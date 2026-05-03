"""Macro source reliability probe — periodic health-check + rolling stats.

Runs as a FastAPI lifespan background task. Every TICK_INTERVAL it pings
each registered source, records latency / success / payload metrics into
`macro_source_health`, and keeps in-memory ETag/Last-Modified state so
sequential probes can hit 304s.

Reporter (`rolling_health_report`) computes uptime% and p95 latency over a
trailing window — feeds the Hafta 1 verification criterion (>=99%, p95<3s).

In-memory ETag is intentional v0: process-local, resets on restart. Promoted
to Postgres if/when we run multi-replica.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_calendar import effective_interval
from services.macro_sources.fed_rss import fetch_fed_rss
from services.macro_sources.fred_api import SERIES as FRED_SERIES, fetch_fred_multi
from services.macro_sources.kalshi_fed import fetch_kalshi_fed
from services.macro_sources.fred_api import fetch_fred_series
from services.macro_sources.release_detect import (
    backfill_fred_series,
    record_fed_rss_events,
    record_fred_observation,
    record_kalshi_snapshot,
)

logger = get_logger("macro.reliability_probe")

# Outer tick — supervisor wakes this often. Per-source intervals below decide
# whether each source actually fires on a given tick. 30s tick gives the
# adaptive calendar enough resolution to honour HOT_INTERVAL=10s during the
# ±30 min release window without falling further than ~30s behind.
TICK_INTERVAL = timedelta(seconds=30)
RETRY_INTERVAL = timedelta(seconds=60)

# Per-source minimum spacing. FRED quota is 50,000/day with key — generous,
# so we still gate at 60-min for parity with the broader release-detection
# cadence (a faster probe gains nothing for monthly data points).
SOURCE_INTERVAL: dict[str, timedelta] = {
    "fed_rss": timedelta(minutes=5),
    "fred_cpi": timedelta(minutes=60),
    "fred_core_cpi": timedelta(minutes=60),
    "fred_nfp": timedelta(minutes=60),
    "fred_unrate": timedelta(minutes=60),
    "fred_pce": timedelta(minutes=60),
    "fred_core_pce": timedelta(minutes=60),
    "kalshi_fed": timedelta(minutes=60),
}

# All FRED sources we probe in one batched call. Order is stable for
# deterministic probe rows.
_FRED_SOURCES = ("fred_cpi", "fred_core_cpi", "fred_nfp", "fred_unrate", "fred_pce", "fred_core_pce")

# Number of historical observations to fetch per probe. 13 lets the public
# endpoint compute YoY (current vs 12-mo-prior) and the previous-period
# MoM% display ("prior" column) without a second FRED call. The first
# probe backfills the 12 history rows quietly; subsequent probes are
# idempotent (ON CONFLICT DO NOTHING) and almost always insert just the
# newest row when a fresh observation lands.
_FRED_FETCH_LIMIT = 15


@dataclass
class _SourceState:
    name: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_probed_at: Optional[datetime] = None


_STATES: dict[str, _SourceState] = {
    "fed_rss": _SourceState(name="fed_rss"),
    "fred_cpi": _SourceState(name="fred_cpi"),
    "fred_core_cpi": _SourceState(name="fred_core_cpi"),
    "fred_nfp": _SourceState(name="fred_nfp"),
    "fred_unrate": _SourceState(name="fred_unrate"),
    "fred_pce": _SourceState(name="fred_pce"),
    "fred_core_pce": _SourceState(name="fred_core_pce"),
    "kalshi_fed": _SourceState(name="kalshi_fed"),
}


async def _probe_fed_rss() -> dict:
    state = _STATES["fed_rss"]
    t0 = time.monotonic()
    result = await fetch_fed_rss(etag=state.etag, last_modified=state.last_modified)
    latency_ms = int((time.monotonic() - t0) * 1000)

    # Update etag for next probe (only on 200; on 304 server confirmed our cached one)
    if result.etag:
        state.etag = result.etag
    if result.last_modified:
        state.last_modified = result.last_modified

    success = result.error is None

    # Persist any newly seen FOMC events into macro_releases (idempotent).
    if success and not result.not_modified and result.events:
        try:
            inserted = await record_fed_rss_events(result.events)
            if inserted:
                logger.info(f"fed_rss: {inserted} new release(s) recorded")
        except Exception as e:
            logger.error(f"fed_rss release persist failed: {e}")

    return {
        "source": "fed_rss",
        "success": success,
        "latency_ms": latency_ms,
        "http_status": 304 if result.not_modified else (200 if success else None),
        "not_modified": result.not_modified,
        "payload_bytes": None if result.not_modified else _approx_payload_size(result.events),
        "events_extracted": len(result.events) if not result.not_modified else None,
        "error_msg": result.error,
    }


def _approx_payload_size(events) -> Optional[int]:
    if not events:
        return None
    # Rough proxy: sum of raw_summary lengths. Avoids carrying full bytes.
    return sum(len(e.raw_summary or "") + len(e.title or "") for e in events) or None


async def _probe_fred_all(due_sources: tuple[str, ...] = _FRED_SOURCES) -> list[dict]:
    """Per-series GET for due FRED series; returns one probe row per series.

    Each call independent — one series down doesn't taint the others.
    Fetches `_FRED_FETCH_LIMIT` observations so YoY / prior-MoM% computations
    have enough history without an extra round trip.
    """
    rows: list[dict] = []
    for source_name in due_sources:
        series_id = FRED_SERIES.get(source_name)
        if not series_id:
            continue
        t0 = time.monotonic()
        per = await fetch_fred_series(series_id, limit=_FRED_FETCH_LIMIT)
        latency_ms = int((time.monotonic() - t0) * 1000)
        success = bool(per and per.success)
        rows.append({
            "source": source_name,
            "success": success,
            "latency_ms": latency_ms,
            "http_status": per.http_status if per else None,
            "not_modified": False,
            "payload_bytes": per.payload_bytes if per else 0,
            "events_extracted": per.data_points if success else None,
            "error_msg": per.error if per else None,
        })

        # Persist into macro_releases — backfill historical rows quietly,
        # narrative+broadcast only fire for the freshest observation.
        if success and per and per.observations:
            try:
                inserted = await backfill_fred_series(source_name, per.observations)
                if inserted:
                    logger.info(f"fred {source_name}: {inserted} new release(s) recorded")
            except Exception as e:
                logger.error(f"fred release persist failed for {source_name}: {e}")
    return rows


async def _probe_kalshi() -> dict:
    """One Kalshi KXFED snapshot — also writes the distribution to macro_market_pricing."""
    t0 = time.monotonic()
    snap = await fetch_kalshi_fed()
    latency_ms = int((time.monotonic() - t0) * 1000)

    if snap.success and snap.meeting_ticker:
        try:
            await record_kalshi_snapshot(snap)
        except Exception as e:
            logger.error(f"kalshi snapshot persist failed: {e}")

    return {
        "source": "kalshi_fed",
        "success": snap.success,
        "latency_ms": latency_ms,
        "http_status": snap.http_status,
        "not_modified": False,
        "payload_bytes": snap.payload_bytes or None,
        "events_extracted": len(snap.distribution) if snap.success else None,
        "error_msg": snap.error,
    }


async def _record(probe: dict) -> None:
    sql = text("""
        INSERT INTO macro_source_health
        (source, success, latency_ms, http_status, not_modified, payload_bytes, events_extracted, error_msg)
        VALUES
        (:source, :success, :latency_ms, :http_status, :not_modified, :payload_bytes, :events_extracted, :error_msg)
    """)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, probe)
    except Exception as e:
        logger.error(f"reliability_probe insert failed: {e}")


def _is_due(name: str, now: datetime) -> bool:
    state = _STATES[name]
    if state.last_probed_at is None:
        return True
    interval = effective_interval(name, now, default=SOURCE_INTERVAL[name])
    return (now - state.last_probed_at) >= interval


def _mark(name: str, now: datetime) -> None:
    _STATES[name].last_probed_at = now


async def probe_once(*, force: bool = False) -> dict:
    """Run probes for any sources whose interval has elapsed (or all on force).

    BLS series are batched into a single combined POST when both are due.
    Returns the per-source probe rows that fired this tick.
    """
    now = datetime.now(timezone.utc)
    fired: list[dict] = []

    if force or _is_due("fed_rss", now):
        row = await _probe_fed_rss()
        await _record(row)
        _log_row(row)
        _mark("fed_rss", now)
        fired.append(row)

    fred_due = tuple(s for s in _FRED_SOURCES if force or _is_due(s, now))
    if fred_due:
        rows = await _probe_fred_all(fred_due)
        for row in rows:
            await _record(row)
            _log_row(row)
            _mark(row["source"], now)
            fired.append(row)

    if force or _is_due("kalshi_fed", now):
        row = await _probe_kalshi()
        await _record(row)
        _log_row(row)
        _mark("kalshi_fed", now)
        fired.append(row)

    return {"fired": [r["source"] for r in fired], "rows": fired}


def _log_row(row: dict) -> None:
    if row["success"]:
        tag = "304" if row.get("not_modified") else str(row.get("http_status") or "ok")
        logger.info(
            f"probe {row['source']} ok [{tag}] {row['latency_ms']}ms "
            f"events={row.get('events_extracted')} bytes={row.get('payload_bytes')}"
        )
    else:
        logger.warning(
            f"probe {row['source']} FAIL {row['latency_ms']}ms err={row.get('error_msg')}"
        )


async def reliability_probe_supervisor() -> None:
    """Background supervisor — same shape as etf_scraper_supervisor."""
    logger.info("Reliability probe supervisor started")
    # Prime the merged calendar (YAML + FRED) so is_in_hot_window has fresh
    # data on the very first probe instead of falling back to YAML-only.
    try:
        from services.macro_calendar import load_calendar
        events = await load_calendar()
        logger.info(f"Calendar primed: {len(events)} upcoming events (YAML + FRED merged)")
    except Exception as e:
        logger.warning(f"Calendar prime failed (degrading to YAML-only): {e}")

    # Initial probe a few seconds after boot, not immediately, to let DB settle.
    try:
        await asyncio.sleep(15)
        await probe_once(force=True)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Initial probe error: {e}")

    last_calendar_refresh = time.monotonic()
    while True:
        try:
            await asyncio.sleep(TICK_INTERVAL.total_seconds())
            await probe_once()
            # Re-prime once a day so a calendar update doesn't wait 24h to
            # show up in `is_in_hot_window`.
            if time.monotonic() - last_calendar_refresh > 24 * 3600:
                try:
                    from services.macro_calendar import load_calendar
                    await load_calendar(force_refresh=True)
                    last_calendar_refresh = time.monotonic()
                except Exception as e:
                    logger.warning(f"Calendar daily refresh failed: {e}")
        except asyncio.CancelledError:
            logger.info("Reliability probe supervisor cancelled")
            break
        except Exception as e:
            logger.error(f"probe loop error: {e}; retry in {RETRY_INTERVAL.total_seconds()}s")
            try:
                await asyncio.sleep(RETRY_INTERVAL.total_seconds())
            except asyncio.CancelledError:
                break


async def rolling_health_report(source: str = "fed_rss", hours: int = 168) -> dict:
    """Trailing window stats. Defaults to fed_rss over 7 days."""
    sql = text("""
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE success) AS ok,
          COUNT(*) FILTER (WHERE not_modified) AS not_modified,
          COALESCE(
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE success),
            0
          ) AS p95_latency_ms,
          COALESCE(
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE success),
            0
          ) AS p50_latency_ms,
          MAX(probed_at) AS last_probe_at,
          MIN(probed_at) AS first_probe_at
        FROM macro_source_health
        WHERE source = :source AND probed_at >= NOW() - make_interval(hours => :hours)
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"source": source, "hours": hours})).mappings().first()
    if not row or row["total"] == 0:
        return {
            "source": source,
            "window_hours": hours,
            "total": 0,
            "uptime_pct": None,
            "p50_latency_ms": None,
            "p95_latency_ms": None,
            "not_modified": 0,
            "first_probe_at": None,
            "last_probe_at": None,
        }
    return {
        "source": source,
        "window_hours": hours,
        "total": row["total"],
        "ok": row["ok"],
        "uptime_pct": round(100.0 * row["ok"] / row["total"], 3),
        "p50_latency_ms": int(row["p50_latency_ms"] or 0),
        "p95_latency_ms": int(row["p95_latency_ms"] or 0),
        "not_modified": row["not_modified"],
        "first_probe_at": row["first_probe_at"].isoformat() if row["first_probe_at"] else None,
        "last_probe_at": row["last_probe_at"].isoformat() if row["last_probe_at"] else None,
    }
