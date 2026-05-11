"""Release detection — write new macro events into `macro_releases`.

Idempotent: every event has a deterministic `event_id`; INSERT ON CONFLICT
DO NOTHING ensures repeated probes don't re-insert the same period.

Triggered from `reliability_probe` after each successful source fetch:
- FRED: per-series, observation-based event_id (`fred:CPI:2025-04-01`)
- fed_rss: per-event, parser already produced `fed_rss:<sha1>` ids

For FRED, `released_at` stores the observation period start date — not the
true publication date, which FRED's series/observations endpoint doesn't
expose. The macro_narrative phase will fill in publication dates from the
`fred/releases/dates` endpoint when we need them.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import json

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.fed_rss import ReleaseEvent
from services.macro_sources.fred_api import SERIES as FRED_SERIES
from services.macro_sources.kalshi_fed import KalshiSnapshot
from services.macro_sources.tcmb_evds import SERIES as TCMB_SERIES

logger = get_logger("macro.release_detect")


# FRED source name → macro_releases.event_type canonical label.
_FRED_EVENT_TYPE = {
    "fred_cpi": "CPI",
    "fred_core_cpi": "CORE_CPI",
    "fred_nfp": "NFP",
    "fred_unrate": "UNRATE",
    "fred_pce": "PCE",
    "fred_core_pce": "CORE_PCE",
    # Day 28 part 3
    "fred_jobless_initial": "JOBLESS_INITIAL",
    "fred_jobless_continuing": "JOBLESS_CONTINUING",
    "fred_retail_sales": "RETAIL_SALES",
    "fred_ppi": "PPI",
    "fred_core_ppi": "CORE_PPI",
    # NFP sektör alt-serileri (Faz 3 sektörel kırılım). Bu event_type'lar
    # storyteller payload'ında join edilir; kendi başlarına narrative/story
    # üretilmez (narrative trigger aşağıda override edilir).
    "fred_nfp_health":  "NFP_HEALTH",
    "fred_nfp_govt":    "NFP_GOVT",
    "fred_nfp_prof":    "NFP_PROF",
    "fred_nfp_leisure": "NFP_LEISURE",
    "fred_nfp_mfg":     "NFP_MFG",
    "fred_nfp_const":   "NFP_CONST",
    "fred_nfp_tpu":     "NFP_TPU",
    "fred_nfp_info":    "NFP_INFO",
    "fred_housing_starts": "HOUSING_STARTS",
    "fred_gdp": "GDP",
    # FOMC decoder (Faz 3, 2026-05-11) — fed funds target range. Daily series;
    # storyteller payload'ında join edilen DATA, kendi başına narrative
    # üretmiyor (FOMC_STATEMENT event fed_rss'ten gelir, esas tetik o).
    "fred_fed_funds_upper": "FED_FUNDS_UPPER",
    "fred_fed_funds_lower": "FED_FUNDS_LOWER",
}


def _is_data_point_event(event_type: str) -> bool:
    """True for sub-series event_types that are payload data only —
    they don't trigger their own narrative or revision broadcast.

    Includes NFP supersector breakdown (NFP_HEALTH, NFP_GOVT, ...) and
    FOMC fed funds target range (FED_FUNDS_UPPER, FED_FUNDS_LOWER).
    """
    if event_type.startswith("NFP_") and event_type != "NFP":
        return True
    if event_type.startswith("FED_FUNDS_"):
        return True
    return False

# Day 28 part 3 — Türkiye TCMB EVDS source name → event_type label mapping.
# 3 seri aktif (kod doğrulandı); diğer 3 (policy_rate/unemployment/current_acct)
# EVDS3'te kod değişti, Day 28 part 4'te eklenecek.
_TCMB_EVENT_TYPE = {
    "tcmb_tufe":         "TR_TUFE",
    "tcmb_core_b":       "TR_CORE_TUFE",
    "tcmb_ufe":          "TR_UFE",
    "tcmb_policy_rate":  "TR_POLICY_RATE",
    "tcmb_unemployment": "TR_UNEMPLOYMENT",
    "tcmb_current_acct": "TR_CURRENT_ACCT",
}


def _decimal_or_none(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None or raw == "" or raw == ".":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _parse_obs_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _upsert_release_with_revision(
    *,
    event_id: str,
    event_type: str,
    country: str,
    released_at: datetime,
    prior: Optional[Decimal],
    actual: Decimal,
    source: str,
    source_url: str,
    trigger_narrative: bool,
) -> str:
    """Idempotent upsert for a macro_releases row with revision detection.

    Returns one of:
        'inserted'   — brand-new event_id (fires narrative)
        'revised'    — event_id existed with a different actual_value
                       (writes audit row + fires revision broadcast)
        'unchanged'  — event_id existed with the same actual_value (no-op)
        'error'      — DB error logged, no side-effects

    Detection is done in a single transaction: SELECT old value, INSERT or
    UPDATE, then conditionally append to `macro_release_revisions`. The
    SELECT-then-write race is acceptable — FRED poller is a single async
    task per process; a duplicate revision audit row would be a harmless
    cosmetic duplicate, not a correctness bug.
    """
    try:
        async with engine.begin() as conn:
            old_row = (await conn.execute(
                text("SELECT actual_value FROM macro_releases WHERE event_id = :eid"),
                {"eid": event_id},
            )).mappings().first()

            if old_row is None:
                # Brand-new release. Standard INSERT path.
                await conn.execute(
                    text("""
                        INSERT INTO macro_releases
                        (event_id, event_type, country, released_at,
                         prior_value, actual_value, source, source_url)
                        VALUES
                        (:event_id, :event_type, :country, :released_at,
                         :prior_value, :actual_value, :source, :source_url)
                        ON CONFLICT (event_id) DO NOTHING
                    """),
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "country": country,
                        "released_at": released_at,
                        "prior_value": prior,
                        "actual_value": actual,
                        "source": source,
                        "source_url": source_url,
                    },
                )
                outcome = "inserted"
            else:
                old_actual = old_row["actual_value"]
                # Decimal equality. Treat None==None as same; None vs new
                # as a "no-op" since the original could have arrived as
                # missing data and we'd want to back-fill quietly.
                if old_actual == actual:
                    return "unchanged"
                if old_actual is None:
                    # Edge: existing row has NULL actual; treat as silent
                    # backfill (no revision broadcast — that case is a
                    # data-quality fix, not a real revision).
                    await conn.execute(
                        text("""
                            UPDATE macro_releases
                            SET actual_value = :actual_value
                            WHERE event_id = :event_id
                        """),
                        {"event_id": event_id, "actual_value": actual},
                    )
                    return "unchanged"

                # Real revision — actual changed.
                delta_abs = actual - old_actual
                try:
                    delta_pct = (delta_abs / abs(old_actual)) * Decimal("100") \
                        if old_actual != 0 else None
                except (InvalidOperation, ZeroDivisionError):
                    delta_pct = None

                await conn.execute(
                    text("""
                        UPDATE macro_releases
                        SET actual_value = :actual_value,
                            revision_count = revision_count + 1
                        WHERE event_id = :event_id
                    """),
                    {"event_id": event_id, "actual_value": actual},
                )
                await conn.execute(
                    text("""
                        INSERT INTO macro_release_revisions
                        (event_id, event_type, source,
                         old_actual_value, new_actual_value,
                         delta_abs, delta_pct)
                        VALUES
                        (:event_id, :event_type, :source,
                         :old, :new, :delta_abs, :delta_pct)
                    """),
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "source": source,
                        "old": old_actual,
                        "new": actual,
                        "delta_abs": delta_abs,
                        "delta_pct": delta_pct,
                    },
                )
                outcome = "revised"
    except Exception as e:
        logger.error(f"upsert {event_id} failed: {e}")
        return "error"

    if outcome == "inserted":
        logger.info(f"new release: {event_id} actual={actual} prior={prior}")
        if trigger_narrative:
            _trigger_narrative(event_id)
    elif outcome == "revised":
        logger.info(
            f"REVISION detected: {event_id} {old_row['actual_value']} → {actual}"
        )
        # NFP sektör alt-serileri ve FED_FUNDS_* payload data; revision
        # broadcast (Advance push) tetiklemez — kullanıcı her sub-series
        # revizyonu için ayrı mesaj almasın.
        if not _is_data_point_event(event_type):
            _trigger_revision_broadcast(event_id)
    return outcome


async def record_fred_observation(
    source: str,
    latest_date: Optional[str],
    latest_value: Optional[str],
    prior_value: Optional[str],
    *,
    trigger_narrative: bool = True,
) -> bool:
    """Insert one FRED observation as a `macro_releases` row.

    Returns True only when a new row was inserted. Revisions (same event_id,
    different actual_value) trigger a separate Advance-tier broadcast and
    write a `macro_release_revisions` audit row, but still return False to
    keep the legacy "new release count" semantics intact for callers like
    `backfill_fred_series`.
    """
    event_type = _FRED_EVENT_TYPE.get(source)
    if not event_type or not latest_date:
        return False
    series_id = FRED_SERIES.get(source, "")
    event_id = f"fred:{event_type}:{latest_date}"

    released_at = _parse_obs_date(latest_date)
    if released_at is None:
        return False

    actual = _decimal_or_none(latest_value)
    prior = _decimal_or_none(prior_value)
    if actual is None:
        return False

    # NFP sektör alt-serileri ve FED_FUNDS_* storyteller payload'ında join
    # edilen DATA, kendi başlarına Free hap / Premium hikaye üretmiyor.
    effective_trigger = trigger_narrative and not _is_data_point_event(event_type)

    outcome = await _upsert_release_with_revision(
        event_id=event_id,
        event_type=event_type,
        country="US",
        released_at=released_at,
        prior=prior,
        actual=actual,
        source="fred",
        source_url=f"https://fred.stlouisfed.org/series/{series_id}",
        trigger_narrative=effective_trigger,
    )
    return outcome == "inserted"


async def record_tcmb_observation(
    source: str,
    latest_date: Optional[str],
    latest_value: Optional[str],
    prior_value: Optional[str],
    *,
    trigger_narrative: bool = True,
) -> bool:
    """Day 28 part 3 — TCMB EVDS observation kayıt. macro_releases.country='TR'.
    event_id format `tcmb:TR_TUFE:2026-04-01` deterministik."""
    event_type = _TCMB_EVENT_TYPE.get(source)
    if not event_type or not latest_date:
        return False
    series_code = TCMB_SERIES.get(source, "")
    event_id = f"tcmb:{event_type}:{latest_date}"
    released_at = _parse_obs_date(latest_date)
    if released_at is None:
        return False
    actual = _decimal_or_none(latest_value)
    prior = _decimal_or_none(prior_value)
    if actual is None:
        return False

    source_url = (
        f"https://evds2.tcmb.gov.tr/index.php?/evds/serieMarket/collapse_2/5949/"
        f"DataGroup/turkish/bie_{series_code.replace('.', '')}/"
    )
    outcome = await _upsert_release_with_revision(
        event_id=event_id,
        event_type=event_type,
        country="TR",
        released_at=released_at,
        prior=prior,
        actual=actual,
        source="tcmb_evds",
        source_url=source_url,
        trigger_narrative=trigger_narrative,
    )
    return outcome == "inserted"


async def backfill_fred_series(
    source: str,
    observations: list[dict],
) -> int:
    """Bulk insert N historical FRED observations for one series, no narrative
    fire — used to seed YoY comparisons. `observations` are FRED's raw dict
    items {date, value} ordered newest-first. The newest one is treated as
    'fresh' (narrative will fire) and the rest are pure backfill.

    Returns count of newly inserted rows.
    """
    inserted = 0
    for i, obs in enumerate(observations):
        if not obs:
            continue
        # Each insert needs its own prior_value (next item in the desc list)
        prior_raw = observations[i + 1]["value"] if (i + 1) < len(observations) else None
        is_fresh = (i == 0)
        ok = await record_fred_observation(
            source=source,
            latest_date=obs.get("date"),
            latest_value=obs.get("value"),
            prior_value=prior_raw,
            trigger_narrative=is_fresh,
        )
        if ok:
            inserted += 1
    return inserted


async def record_kalshi_snapshot(snap: KalshiSnapshot) -> bool:
    """Append one Kalshi rate-distribution snapshot. Always inserts (no dedup).

    `before/after` Narrative Change reads come from this append-only log, so we
    intentionally keep every probe even when the distribution didn't move.
    """
    if not snap.success or not snap.meeting_ticker:
        return False
    sql = text("""
        INSERT INTO macro_market_pricing
        (source, meeting_ticker, snapshot_ts, modal_rate_pct, modal_prob, distribution, payload_bytes)
        VALUES
        ('kalshi_fed', :meeting_ticker, :snapshot_ts, :modal_rate_pct, :modal_prob,
         CAST(:distribution AS JSONB), :payload_bytes)
    """)
    params = {
        "meeting_ticker": snap.meeting_ticker,
        "snapshot_ts": snap.snapshot_ts,
        "modal_rate_pct": snap.modal_rate_pct,
        "modal_prob": snap.modal_prob,
        "distribution": json.dumps(snap.distribution),
        "payload_bytes": snap.payload_bytes or None,
    }
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, params)
        return True
    except Exception as e:
        logger.error(f"record_kalshi_snapshot failed for {snap.meeting_ticker}: {e}")
        return False


async def record_fed_rss_events(events: list[ReleaseEvent]) -> int:
    """Bulk INSERT new fed_rss events. Returns count of newly inserted rows."""
    if not events:
        return 0
    sql = text("""
        INSERT INTO macro_releases
        (event_id, event_type, country, released_at, source, source_url, narrative_md)
        VALUES
        (:event_id, :event_type, 'US', :released_at, 'fed_rss', :source_url, :narrative_md)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
    """)
    inserted = 0
    try:
        async with engine.begin() as conn:
            for ev in events:
                params = {
                    "event_id": ev.event_id,
                    "event_type": ev.event_type,
                    "released_at": ev.released_at,
                    "source_url": ev.url,
                    "narrative_md": (ev.title or "").strip()[:500] or None,
                }
                row = (await conn.execute(sql, params)).first()
                if row is not None:
                    inserted += 1
                    logger.info(f"new release: {ev.event_id} type={ev.event_type}")
                    _trigger_narrative(ev.event_id)
    except Exception as e:
        logger.error(f"record_fed_rss_events failed: {e}")
    return inserted


def _trigger_narrative(event_id: str) -> None:
    """Fire-and-forget narrative generation. Imported lazily to keep
    macro_narrative ↔ release_detect import order safe (narrative reads from
    the same engine import release_detect uses).
    """
    try:
        from services.macro_narrative import generate_narrative_safe
        asyncio.create_task(generate_narrative_safe(event_id))
    except Exception as e:
        logger.error(f"narrative trigger failed for {event_id}: {e}")


# Strong-ref set so fire-and-forget revision broadcast tasks aren't GC'd
# while waiting on Telegram. Matches the _DELAYED_INFLIGHT pattern from
# macro_broadcaster and _STORY_BROADCAST_INFLIGHT from macro_storyteller.
_REVISION_BROADCAST_INFLIGHT: set = set()


def _trigger_revision_broadcast(event_id: str) -> None:
    """Fire-and-forget Advance-tier revision broadcast. Lazy import keeps
    macro_revisions ↔ release_detect import order safe.
    """
    try:
        from services.macro_revisions import broadcast_revision_safe
        task = asyncio.create_task(broadcast_revision_safe(event_id))
        _REVISION_BROADCAST_INFLIGHT.add(task)
        task.add_done_callback(_REVISION_BROADCAST_INFLIGHT.discard)
    except Exception as e:
        logger.error(f"revision broadcast trigger failed for {event_id}: {e}")
