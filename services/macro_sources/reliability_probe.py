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
from services.macro_pre_announcement import fire_due_pre_announcements_safe
from services.macro_sources.fed_rss import fetch_fed_rss
from services.macro_sources.fmp_economic import fetch_fmp_recent_releases
from services.macro_sources.fred_api import SERIES as FRED_SERIES, fetch_fred_multi
from services.macro_sources.kalshi_fed import fetch_kalshi_fed
from services.macro_sources.fred_api import fetch_fred_series
from services.macro_sources.release_detect import (
    backfill_fred_series,
    record_fed_rss_events,
    record_fmp_events,
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
    # NFP sektör alt-serileri — aylık, NFP ile birlikte yayınlanır
    "fred_nfp_health":  timedelta(minutes=60),
    "fred_nfp_govt":    timedelta(minutes=60),
    "fred_nfp_prof":    timedelta(minutes=60),
    "fred_nfp_leisure": timedelta(minutes=60),
    "fred_nfp_mfg":     timedelta(minutes=60),
    "fred_nfp_const":   timedelta(minutes=60),
    "fred_nfp_tpu":     timedelta(minutes=60),
    "fred_nfp_info":    timedelta(minutes=60),
    "fred_pce": timedelta(minutes=60),
    "fred_core_pce": timedelta(minutes=60),
    # Day 28 part 3 — haftalık jobless claims daha sık (perşembe 13:30 UTC
    # geldiğinde hızlı yakalansın), aylıklar 60min
    "fred_jobless_initial": timedelta(minutes=30),
    "fred_jobless_continuing": timedelta(minutes=30),
    "fred_retail_sales": timedelta(minutes=60),
    "fred_ppi": timedelta(minutes=60),
    "fred_core_ppi": timedelta(minutes=60),
    "fred_housing_starts": timedelta(minutes=60),
    "fred_gdp": timedelta(minutes=60),
    # CPI sub-kalemleri (Faz D) — aylık, CPI ile aynı release
    "fred_cpi_shelter":    timedelta(minutes=60),
    "fred_cpi_energy":     timedelta(minutes=60),
    "fred_cpi_food":       timedelta(minutes=60),
    "fred_cpi_medical":    timedelta(minutes=60),
    "fred_cpi_apparel":    timedelta(minutes=60),
    "fred_cpi_transport":  timedelta(minutes=60),
    "fred_cpi_recreation": timedelta(minutes=60),
    "fred_cpi_education":  timedelta(minutes=60),
    # PPI sub-kalemleri (2026-05-12 evening) — aylık, PPI ile aynı release
    "fred_ppi_goods":     timedelta(minutes=60),
    "fred_ppi_services":  timedelta(minutes=60),
    "fred_ppi_energy":    timedelta(minutes=60),
    "fred_ppi_foods":     timedelta(minutes=60),
    "fred_ppi_trade":     timedelta(minutes=60),
    "fred_ppi_transport": timedelta(minutes=60),
    # FOMC decoder (Faz 3) — günlük seriler ama FOMC kararı dışında değer
    # değişmez; 60dk yeterli (8 toplantı/yıl, ±60dk gecikme önemsiz).
    "fred_fed_funds_upper": timedelta(minutes=60),
    "fred_fed_funds_lower": timedelta(minutes=60),
    "kalshi_fed": timedelta(minutes=60),
    # FMP economic-calendar (2026-05-12) — birincil release-detection kaynağı,
    # FRED gecikmesinin önüne geçer. T±30dk hot window'da effective_interval
    # daha agresif olur. 5dk default normal-zaman cadence; release anında
    # macro_calendar hot detection 30sn'ye kadar düşürür.
    "fmp_economic": timedelta(minutes=5),
    # Day 28 part 3 — Türkiye TCMB EVDS, hepsi aylık → 60dk yeterli
    # Day 28 part 4 — TCMB EVDS interval 60dk → 15dk (kullanıcı anlık yayın
    # istedi; 6 series × 4/saat × 24 = 576 req/gün, EVDS quota'sının çok altında).
    # Yeni veri yayınlandığı andan max 15dk içinde Telegram'a düşer.
    "tcmb_tufe":          timedelta(minutes=15),
    "tcmb_core_b":        timedelta(minutes=15),
    "tcmb_ufe":           timedelta(minutes=15),
    "tcmb_policy_rate":   timedelta(minutes=15),
    "tcmb_unemployment":  timedelta(minutes=15),
    "tcmb_current_acct":  timedelta(minutes=15),
}

# All FRED sources we probe in one batched call. Order is stable for
# deterministic probe rows.
_FRED_SOURCES = (
    "fred_cpi", "fred_core_cpi", "fred_nfp", "fred_unrate", "fred_pce", "fred_core_pce",
    "fred_jobless_initial", "fred_jobless_continuing", "fred_retail_sales",
    "fred_ppi", "fred_core_ppi", "fred_housing_starts", "fred_gdp",
    # NFP sektör alt-serileri (BLS B-1 eşdeğeri)
    "fred_nfp_health", "fred_nfp_govt", "fred_nfp_prof", "fred_nfp_leisure",
    "fred_nfp_mfg", "fred_nfp_const", "fred_nfp_tpu", "fred_nfp_info",
    # FOMC decoder (Faz 3) — fed funds target range
    "fred_fed_funds_upper", "fred_fed_funds_lower",
    # CPI sub-kalemleri (Faz D) — storyteller sektörel kırılım
    "fred_cpi_shelter", "fred_cpi_energy", "fred_cpi_food", "fred_cpi_medical",
    "fred_cpi_apparel", "fred_cpi_transport", "fred_cpi_recreation", "fred_cpi_education",
    # PPI sub-kalemleri (2026-05-12 evening) — storyteller "üretici baskısı" lens
    "fred_ppi_goods", "fred_ppi_services", "fred_ppi_energy", "fred_ppi_foods",
    "fred_ppi_trade", "fred_ppi_transport",
)

# Number of historical observations to fetch per probe. 13 lets the public
# endpoint compute YoY (current vs 12-mo-prior) and the previous-period
# MoM% display ("prior" column) without a second FRED call. The first
# probe backfills the 12 history rows quietly; subsequent probes are
# idempotent (ON CONFLICT DO NOTHING) and almost always insert just the
# newest row when a fresh observation lands.
_FRED_FETCH_LIMIT = 15

# Per-source override. FED_FUNDS_UPPER/LOWER günlük seri — varsayılan 15 satır
# sadece 15 gün geriye gidiyor, oysa FOMC kararları 6 hafta aralıklı. 2-yıl
# history = 500 satır (~8 toplantı backfill için yeterli + tüm DFEDTARU
# değişiklikleri yakalanır).
_FRED_FETCH_LIMIT_OVERRIDE: dict[str, int] = {
    "fred_fed_funds_upper": 500,
    "fred_fed_funds_lower": 500,
}


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
    "fred_nfp_health":  _SourceState(name="fred_nfp_health"),
    "fred_nfp_govt":    _SourceState(name="fred_nfp_govt"),
    "fred_nfp_prof":    _SourceState(name="fred_nfp_prof"),
    "fred_nfp_leisure": _SourceState(name="fred_nfp_leisure"),
    "fred_nfp_mfg":     _SourceState(name="fred_nfp_mfg"),
    "fred_nfp_const":   _SourceState(name="fred_nfp_const"),
    "fred_nfp_tpu":     _SourceState(name="fred_nfp_tpu"),
    "fred_nfp_info":    _SourceState(name="fred_nfp_info"),
    "fred_pce": _SourceState(name="fred_pce"),
    "fred_core_pce": _SourceState(name="fred_core_pce"),
    # Day 28 part 3
    "fred_jobless_initial": _SourceState(name="fred_jobless_initial"),
    "fred_jobless_continuing": _SourceState(name="fred_jobless_continuing"),
    "fred_retail_sales": _SourceState(name="fred_retail_sales"),
    "fred_ppi": _SourceState(name="fred_ppi"),
    "fred_core_ppi": _SourceState(name="fred_core_ppi"),
    "fred_housing_starts": _SourceState(name="fred_housing_starts"),
    "fred_gdp": _SourceState(name="fred_gdp"),
    # FOMC decoder (Faz 3)
    "fred_fed_funds_upper": _SourceState(name="fred_fed_funds_upper"),
    "fred_fed_funds_lower": _SourceState(name="fred_fed_funds_lower"),
    # CPI sub-kalemleri (Faz D)
    "fred_cpi_shelter":    _SourceState(name="fred_cpi_shelter"),
    "fred_cpi_energy":     _SourceState(name="fred_cpi_energy"),
    "fred_cpi_food":       _SourceState(name="fred_cpi_food"),
    "fred_cpi_medical":    _SourceState(name="fred_cpi_medical"),
    "fred_cpi_apparel":    _SourceState(name="fred_cpi_apparel"),
    "fred_cpi_transport":  _SourceState(name="fred_cpi_transport"),
    "fred_cpi_recreation": _SourceState(name="fred_cpi_recreation"),
    "fred_cpi_education":  _SourceState(name="fred_cpi_education"),
    # PPI sub-kalemleri (2026-05-12 evening)
    "fred_ppi_goods":     _SourceState(name="fred_ppi_goods"),
    "fred_ppi_services":  _SourceState(name="fred_ppi_services"),
    "fred_ppi_energy":    _SourceState(name="fred_ppi_energy"),
    "fred_ppi_foods":     _SourceState(name="fred_ppi_foods"),
    "fred_ppi_trade":     _SourceState(name="fred_ppi_trade"),
    "fred_ppi_transport": _SourceState(name="fred_ppi_transport"),
    "kalshi_fed": _SourceState(name="kalshi_fed"),
    "fmp_economic": _SourceState(name="fmp_economic"),
    # Day 28 part 3 — TCMB EVDS sources (3 aktif; kalan 3 kod EVDS3'te değişti)
    "tcmb_tufe":          _SourceState(name="tcmb_tufe"),
    "tcmb_core_b":        _SourceState(name="tcmb_core_b"),
    "tcmb_ufe":           _SourceState(name="tcmb_ufe"),
    "tcmb_policy_rate":   _SourceState(name="tcmb_policy_rate"),
    "tcmb_unemployment":  _SourceState(name="tcmb_unemployment"),
    "tcmb_current_acct":  _SourceState(name="tcmb_current_acct"),
}

_TCMB_SOURCES = (
    "tcmb_tufe", "tcmb_core_b", "tcmb_ufe",
    "tcmb_policy_rate", "tcmb_unemployment", "tcmb_current_acct",
)


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
        fetch_limit = _FRED_FETCH_LIMIT_OVERRIDE.get(source_name, _FRED_FETCH_LIMIT)
        per = await fetch_fred_series(series_id, limit=fetch_limit)
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


async def _probe_tcmb_all(due_sources: tuple[str, ...] = _TCMB_SOURCES) -> list[dict]:
    """TCMB EVDS probes — paralel olmayan, sırayla. Anahtar yoksa graceful skip
    (tek warning loglanır, downstream'e error mesajı gider, narrative tetiklenmez)."""
    from services.macro_sources.tcmb_evds import (
        SERIES as TCMB_SERIES,
        fetch_tcmb_series,
        _is_configured as tcmb_configured,
    )
    from services.macro_sources.release_detect import record_tcmb_observation

    if not tcmb_configured():
        # Tek warning sızdır — her tickte spamlanmasın diye state üzerinden
        # gate'lenmiyor (zaten 60dk cadence). İlk gate'lenmemiş probe'ta
        # error_msg kalıcı olur, kullanıcı log üzerinden görür.
        return [
            {
                "source": s,
                "success": False,
                "latency_ms": 0,
                "http_status": None,
                "not_modified": False,
                "payload_bytes": 0,
                "events_extracted": None,
                "error_msg": "TCMB_EVDS_API_KEY missing",
            }
            for s in due_sources
        ]

    rows: list[dict] = []
    for source_name in due_sources:
        series_code = TCMB_SERIES.get(source_name)
        if not series_code:
            continue
        t0 = time.monotonic()
        per = await fetch_tcmb_series(series_code)
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
        # Persist newest observation; older ones aren't backfilled (TCMB aylık
        # cadence + brifing zamanlama TR data için zaten ileri planda).
        if success and per and per.observations:
            try:
                ok = await record_tcmb_observation(
                    source=source_name,
                    latest_date=per.latest_date,
                    latest_value=per.latest_value,
                    prior_value=per.prior_value,
                )
                if ok:
                    logger.info(f"tcmb {source_name}: new release recorded")
            except Exception as e:
                logger.error(f"tcmb release persist failed for {source_name}: {e}")
    return rows


async def _probe_fmp_economic() -> dict:
    """One FMP economic-calendar fetch — past 7d + next 1d window.

    Persists every released event (where `actual` is non-null) matching the
    adapter's _EVENT_TYPE_MAP into macro_releases. New rows trigger narrative;
    repeats are 'unchanged' no-ops.
    """
    t0 = time.monotonic()
    result = await fetch_fmp_recent_releases()
    latency_ms = int((time.monotonic() - t0) * 1000)

    inserted = 0
    if result.success and result.events:
        try:
            inserted = await record_fmp_events(result.events)
            if inserted:
                logger.info(f"fmp_economic: {inserted} new release(s) recorded")
        except Exception as e:
            logger.error(f"fmp_economic release persist failed: {e}")

    return {
        "source": "fmp_economic",
        "success": result.success,
        "latency_ms": latency_ms,
        "http_status": result.http_status,
        "not_modified": False,
        "payload_bytes": result.payload_bytes or None,
        "events_extracted": len(result.events) if result.success else None,
        "error_msg": result.error,
    }


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

    # Day 28 part 3 — TCMB EVDS rotation (key yoksa graceful skip)
    tcmb_due = tuple(s for s in _TCMB_SOURCES if force or _is_due(s, now))
    if tcmb_due:
        rows = await _probe_tcmb_all(tcmb_due)
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

    if force or _is_due("fmp_economic", now):
        row = await _probe_fmp_economic()
        await _record(row)
        _log_row(row)
        _mark("fmp_economic", now)
        fired.append(row)

    # T-5dk pre-announcement dispatcher — fire-and-forget, probe loop'u
    # bloklamaz, hata raise etmez. Idempotency macro_pre_announcements
    # tablosunda; her tick check eder, sadece window'a uyan & henüz
    # announced-olmamış event'ler için fire.
    try:
        await fire_due_pre_announcements_safe()
    except Exception as e:
        logger.error(f"pre-announce dispatcher in probe_once: {e}")

    # Narrative/story backfill dispatcher — safety net for releases whose
    # fire-and-forget chain dropped (GC, restart, exception escape). Self
    # throttled to ~5min cadence so it doesn't pound Gemini on every tick.
    # 2026-05-13 PPI Apr motivated this: release inserted but narrative
    # task was lost, no Telegram broadcast went out. Strong-ref pattern is
    # the primary fix; this is the safety net.
    try:
        from services.macro_backfill import backfill_missing_narratives_and_stories_safe
        await backfill_missing_narratives_and_stories_safe()
    except Exception as e:
        logger.error(f"backfill dispatcher in probe_once: {e}")

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
