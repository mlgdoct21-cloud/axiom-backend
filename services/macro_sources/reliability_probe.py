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
from services.macro_sources.bls_api import SERIES as BLS_SERIES, fetch_bls_multi
from services.macro_sources.fed_rss import fetch_fed_rss

logger = get_logger("macro.reliability_probe")

# Outer tick — supervisor wakes this often. Per-source intervals below decide
# whether each source actually fires on a given tick.
TICK_INTERVAL = timedelta(minutes=5)
RETRY_INTERVAL = timedelta(seconds=60)

# Per-source minimum spacing. BLS unregistered limit is 25 calls/day per IP;
# 60-min cadence keeps total BLS calls at 24/day even when CPI+NFP combined.
SOURCE_INTERVAL: dict[str, timedelta] = {
    "fed_rss": timedelta(minutes=5),
    "bls_cpi": timedelta(minutes=60),
    "bls_nfp": timedelta(minutes=60),
}


@dataclass
class _SourceState:
    name: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_probed_at: Optional[datetime] = None


_STATES: dict[str, _SourceState] = {
    "fed_rss": _SourceState(name="fed_rss"),
    "bls_cpi": _SourceState(name="bls_cpi"),
    "bls_nfp": _SourceState(name="bls_nfp"),
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


async def _probe_bls_combined() -> list[dict]:
    """Single combined POST for all BLS series; returns one probe row per series.

    Cuts BLS daily quota usage in half (vs separate calls per series).
    """
    series_ids = [BLS_SERIES["bls_cpi"], BLS_SERIES["bls_nfp"]]
    t0 = time.monotonic()
    result = await fetch_bls_multi(series_ids)
    latency_ms = int((time.monotonic() - t0) * 1000)

    rows: list[dict] = []
    for source_name, series_id in (("bls_cpi", BLS_SERIES["bls_cpi"]),
                                   ("bls_nfp", BLS_SERIES["bls_nfp"])):
        per = result.series.get(series_id)
        success = bool(per and per.success)
        # data_points doubles as "events_extracted" — closest analog (count of
        # observations returned). Real release-event detection lands in Day 5.
        rows.append({
            "source": source_name,
            "success": success,
            "latency_ms": latency_ms,
            "http_status": result.http_status,
            "not_modified": False,  # BLS API has no ETag
            "payload_bytes": result.payload_bytes,
            "events_extracted": per.data_points if (per and per.success) else None,
            "error_msg": (per.error if per else None) or result.error,
        })
    return rows


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
    return (now - state.last_probed_at) >= SOURCE_INTERVAL[name]


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

    bls_due = [s for s in ("bls_cpi", "bls_nfp") if force or _is_due(s, now)]
    if bls_due:
        rows = await _probe_bls_combined()
        # Only record/mark the ones that were actually due — but combined call
        # returns both; record both anyway since the cost is already paid.
        for row in rows:
            await _record(row)
            _log_row(row)
            _mark(row["source"], now)
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
    # Initial probe a few seconds after boot, not immediately, to let DB settle.
    try:
        await asyncio.sleep(15)
        await probe_once(force=True)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Initial probe error: {e}")

    while True:
        try:
            await asyncio.sleep(TICK_INTERVAL.total_seconds())
            await probe_once()
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
