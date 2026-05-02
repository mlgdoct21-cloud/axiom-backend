"""Macro source reliability probe — periodic health-check + rolling stats.

Runs as a FastAPI lifespan background task. Every PROBE_INTERVAL it pings
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
from datetime import timedelta
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.fed_rss import fetch_fed_rss

logger = get_logger("macro.reliability_probe")

PROBE_INTERVAL = timedelta(minutes=5)
RETRY_INTERVAL = timedelta(seconds=60)


@dataclass
class _SourceState:
    name: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None


_STATES: dict[str, _SourceState] = {
    "fed_rss": _SourceState(name="fed_rss"),
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


async def probe_once() -> dict:
    """Probe every registered source once. Returns a summary dict for logs/tests."""
    probe = await _probe_fed_rss()
    await _record(probe)
    if probe["success"]:
        tag = "304" if probe["not_modified"] else "200"
        logger.info(
            f"probe fed_rss ok [{tag}] {probe['latency_ms']}ms events={probe['events_extracted']}"
        )
    else:
        logger.warning(f"probe fed_rss FAIL {probe['latency_ms']}ms err={probe['error_msg']}")
    return probe


async def reliability_probe_supervisor() -> None:
    """Background supervisor — same shape as etf_scraper_supervisor."""
    logger.info("Reliability probe supervisor started")
    # Initial probe a few seconds after boot, not immediately, to let DB settle.
    try:
        await asyncio.sleep(15)
        await probe_once()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Initial probe error: {e}")

    while True:
        try:
            await asyncio.sleep(PROBE_INTERVAL.total_seconds())
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
