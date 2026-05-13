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
from services.macro_sources.fmp_economic import FMPEvent
from services.macro_sources.fred_api import SERIES as FRED_SERIES
from services.macro_sources.kalshi_fed import KalshiSnapshot
from services.macro_sources.tcmb_evds import SERIES as TCMB_SERIES

# FRED series_id mapping for source_url generation when an FMP-originated row
# is written under the FRED namespace (so when FRED catches up later, the
# value matches and the upsert returns 'unchanged' — no double broadcast).
_EVENT_TYPE_TO_FRED_SERIES = {
    # 2026-05-12 NSA switch — see fred_api.py SERIES dict for rationale.
    "CPI": "CPIAUCNS",
    "CORE_CPI": "CUUR0000SA0L1E",
    "UNRATE": "UNRATE",
    # 2026-05-12 evening — FMP-primary expansion. FMP gives MoM% (or thousands
    # delta for NFP); release_detect composite translates to FRED-level
    # so when FRED catches up hours later the values converge.
    "PPI": "PPIFIS",
    "CORE_PPI": "WPSFD49116",
    "PCE": "PCEPI",
    "CORE_PCE": "PCEPILFE",
    "NFP": "PAYEMS",
    "JOBLESS_INITIAL": "ICSA",
    "JOBLESS_CONTINUING": "CCSA",
    "RETAIL_SALES": "RSAFS",
}

# event_types where FMP returns MoM% (compose level from prior FRED level).
_FMP_MOM_PCT_EVENT_TYPES = frozenset({
    "PPI", "CORE_PPI", "PCE", "CORE_PCE",
    "RETAIL_SALES", "CORE_RETAIL_SALES",
})
# event_type where FMP returns thousands-of-jobs delta (compose level from
# prior PAYEMS level by addition).
_FMP_DELTA_K_EVENT_TYPES = frozenset({"NFP"})
# event_types where FMP returns level directly (no translation needed).
_FMP_DIRECT_LEVEL_EVENT_TYPES = frozenset({
    "CPI", "CORE_CPI", "UNRATE",
    "JOBLESS_INITIAL", "JOBLESS_CONTINUING",
})

# All FMP-primary event_types — used for tolerance-based equality in
# _upsert_release_with_revision so FRED catchup with slight rounding
# difference doesn't fire a spurious revision broadcast.
_FMP_PRIMARY_EVENT_TYPES = (
    _FMP_MOM_PCT_EVENT_TYPES | _FMP_DELTA_K_EVENT_TYPES | _FMP_DIRECT_LEVEL_EVENT_TYPES
)

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
    # CPI sub-kalemleri (Faz D, 2026-05-12) — storyteller'a "kira %X, enerji %Y,
    # sağlık %Z arttı" anlatımı için. CPI_* prefixi _is_data_point_event guard'a
    # giriyor: kendi başlarına narrative/revision broadcast tetiklemez.
    "fred_cpi_shelter":    "CPI_SHELTER",
    "fred_cpi_energy":     "CPI_ENERGY",
    "fred_cpi_food":       "CPI_FOOD",
    "fred_cpi_medical":    "CPI_MEDICAL",
    "fred_cpi_apparel":    "CPI_APPAREL",
    "fred_cpi_transport":  "CPI_TRANSPORT",
    "fred_cpi_recreation": "CPI_RECREATION",
    "fred_cpi_education":  "CPI_EDUCATION",
    # PPI sub-kalemleri (2026-05-12 evening) — CPI iskeletinin PPI muadili.
    # "Üretici hangi maliyetten baskı altında" hikayesi için. Headline +
    # core PPI'dan farklı bir lens: goods vs services split, healthcare lead,
    # margin story. PPI_* prefixi _is_data_point_event guard'a giriyor:
    # kendi başlarına narrative/revision broadcast tetiklemez.
    "fred_ppi_goods":      "PPI_GOODS",
    "fred_ppi_services":   "PPI_SERVICES",
    "fred_ppi_energy":     "PPI_ENERGY",
    "fred_ppi_foods":      "PPI_FOODS",
    "fred_ppi_trade":      "PPI_TRADE",
    "fred_ppi_transport":  "PPI_TRANSPORT",
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

    Includes NFP supersector breakdown (NFP_HEALTH, NFP_GOVT, ...),
    FOMC fed funds target range (FED_FUNDS_UPPER, FED_FUNDS_LOWER),
    SEP projection medians (SEP_FUNDS_END_0, SEP_GDP_0, ...), and
    CPI sub-kalemleri (CPI_SHELTER, CPI_ENERGY, CPI_FOOD, ...).
    """
    if event_type.startswith("NFP_") and event_type != "NFP":
        return True
    if event_type.startswith("FED_FUNDS_"):
        return True
    if event_type.startswith("SEP_"):
        return True
    if event_type.startswith("CPI_") and event_type not in ("CPI", "CORE_CPI"):
        return True
    if event_type.startswith("PPI_") and event_type not in ("PPI", "CORE_PPI"):
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


def _is_within_fmp_tolerance(
    old: Optional[Decimal],
    new: Decimal,
    event_type: str,
) -> bool:
    """For FMP-primary event_types, allow small rounding diff between
    FMP-composite level and FRED official level.

    Composite for PPI: prior_level × (1 + MoM/100), rounded to 3 decimals.
    FRED publishes 3-decimal level. Difference usually <0.05 in 138.xxx scale.

    Tolerance: 0.1% relative (or absolute 0.05 in the unit if level<10).
    """
    if event_type not in _FMP_PRIMARY_EVENT_TYPES:
        return False
    if old is None or new is None:
        return False
    try:
        abs_diff = abs(new - old)
        if abs_diff == 0:
            return True
        if old == 0:
            return abs_diff < Decimal("0.01")
        # Relative diff < 0.1%
        rel_diff = abs_diff / abs(old)
        if rel_diff < Decimal("0.001"):
            return True
        # Absolute diff < 0.05 for small-scale numbers (UNRATE % around 3-4)
        if abs(old) < Decimal("10") and abs_diff < Decimal("0.05"):
            return True
    except (InvalidOperation, ZeroDivisionError):
        return False
    return False


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
    published_at: Optional[datetime] = None,
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
                         prior_value, actual_value, source, source_url,
                         published_at)
                        VALUES
                        (:event_id, :event_type, :country, :released_at,
                         :prior_value, :actual_value, :source, :source_url,
                         :published_at)
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
                        "published_at": published_at,
                    },
                )
                outcome = "inserted"
            else:
                # Opportunistic published_at backfill — historic FRED rows
                # don't have it; FMP later writes fill the gap. Independent
                # of revision detection below.
                if published_at is not None:
                    await conn.execute(
                        text("""
                            UPDATE macro_releases
                            SET published_at = :published_at
                            WHERE event_id = :event_id
                              AND published_at IS NULL
                        """),
                        {"event_id": event_id, "published_at": published_at},
                    )
                old_actual = old_row["actual_value"]
                # Decimal equality. Treat None==None as same; None vs new
                # as a "no-op" since the original could have arrived as
                # missing data and we'd want to back-fill quietly.
                if old_actual == actual:
                    return "unchanged"
                # FMP-composite ↔ FRED official rounding convergence:
                # FMP wrote 138.553 (composite from MoM%), FRED later writes
                # 138.520 (official). Treat as silent backfill if within
                # tolerance — overwrite to FRED's exact value, NO revision
                # broadcast (this is a precision fix, not a real revision).
                if _is_within_fmp_tolerance(old_actual, actual, event_type):
                    await conn.execute(
                        text("""
                            UPDATE macro_releases
                            SET actual_value = :actual_value
                            WHERE event_id = :event_id
                        """),
                        {"event_id": event_id, "actual_value": actual},
                    )
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

                # Revision sanity guard: |delta_pct| > 5% means a spurious
                # FMP variant (e.g. "PPI YoY" matched as "PPI"), a wrong
                # subset, or a stale cache hit. Real BLS/FRED revisions are
                # typically <1% (rounding/methodology). Reject revision +
                # no UPDATE + no audit row + no broadcast. Logged loudly so
                # ops can audit.
                #
                # 2026-05-13 PPI Apr incident: FMP returned multiple PPI
                # variants in one fetch (MoM, YoY, ex-Food&Energy), all
                # mapped to event_type=PPI by the loose ^PPI\b regex. Each
                # variant overwrote the prior row, generating 60+ revision
                # broadcasts to Advance tier in 20 min.
                if delta_pct is not None and abs(delta_pct) > Decimal("5.0"):
                    logger.warning(
                        f"REVISION SANITY REJECT {event_id}: "
                        f"{old_actual} → {actual} delta_pct={delta_pct:.2f}% "
                        f"|>5%| — likely spurious FMP variant or wrong "
                        f"subset. Row unchanged, no broadcast."
                    )
                    return "unchanged"

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


async def _fetch_prior_fred_level(
    event_type: str, before: datetime
) -> Optional[Decimal]:
    """Fetch the most recent FRED-level for `event_type` strictly before
    `before` (the new observation period). Used by composite translation
    for FMP MoM%-only or delta-only events.

    Returns None if no prior level exists in macro_releases.
    """
    sql = text("""
        SELECT actual_value FROM macro_releases
        WHERE event_type = :et
          AND released_at < :before
          AND actual_value IS NOT NULL
        ORDER BY released_at DESC
        LIMIT 1
    """)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {
                "et": event_type, "before": before,
            })).first()
        if row is None:
            return None
        return Decimal(str(row[0]))
    except Exception as e:
        logger.warning(f"prior FRED level fetch failed for {event_type}: {e}")
        return None


async def _translate_fmp_actual(ev: FMPEvent) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """Translate FMP's `actual` and `previous` into FRED-level convention.

    Returns (translated_actual, translated_prior) — both Decimal or None.

    Branching:
      - DIRECT_LEVEL: pass-through (CPI, CORE_CPI, UNRATE, JOBLESS_*).
      - MOM_PCT: new_level = prior_level × (1 + MoM/100).
        prior_level comes from latest FRED row in macro_releases for the
        same event_type. translated_prior = prior_level (the level we
        composed against; useful for storyteller's mom_pct sanity).
      - DELTA_K (NFP): new_level = prior_level + delta. prior_level from
        macro_releases. translated_prior = prior_level.

    If prior_level is missing for a translation-required event, returns
    (None, None) — caller should skip the row (will be retried on next
    probe once FRED has seeded historical data).
    """
    et = ev.event_type
    if et in _FMP_DIRECT_LEVEL_EVENT_TYPES:
        return ev.actual, ev.previous

    prior_level = await _fetch_prior_fred_level(et, before=ev.released_at)
    if prior_level is None:
        logger.warning(
            f"fmp_economic translation skip: no prior FRED level for {et} "
            f"period={ev.released_at.date().isoformat()} — backfill needed"
        )
        return None, None

    if et in _FMP_MOM_PCT_EVENT_TYPES:
        # FMP actual is MoM% (e.g. 0.4 means +0.4%). Compose new level.
        # Sanity guard: |MoM%| > 3.0 rejects. PPI/CORE_PPI/PCE/CORE_PCE/RETAIL
        # monthly moves cluster in -1.5..+1.5; >3% means FMP either reported
        # YoY in the MoM field, mapped a wrong PPI subset, or had a stale row.
        # 2026-05-13 PPI Apr: FMP returned +4.40 (BLS canonical was +1.38) —
        # composite produced 160.782 vs canonical 156.496 (+2.7% inflation).
        # Block the spurious row so storyteller doesn't broadcast a fake
        # "üretici fiyatları patladı" headline.
        try:
            mom_abs = abs(ev.actual)
            if mom_abs > Decimal("3.0"):
                logger.warning(
                    f"FMP composite sanity reject: {et} MoM%={ev.actual} "
                    f"|{mom_abs}| > 3.0 — likely YoY-in-MoM-field or wrong "
                    f"subset, src='{ev.raw_event_name}'. Skipping insert; "
                    f"FRED probe will seed canonical value on next tick."
                )
                return None, None
            mom_factor = Decimal("1") + (ev.actual / Decimal("100"))
            new_level = prior_level * mom_factor
            # Round to 3 decimal places to match FRED's typical index precision.
            new_level = new_level.quantize(Decimal("0.001"))
        except (InvalidOperation, ZeroDivisionError) as e:
            logger.warning(f"MoM%→level translate failed for {et}: {e}")
            return None, None
        return new_level, prior_level

    if et in _FMP_DELTA_K_EVENT_TYPES:
        # NFP: FMP actual is thousands of jobs added (delta).
        # FRED PAYEMS is thousands of jobs (level). new_level = prior + delta.
        try:
            new_level = prior_level + ev.actual
        except (InvalidOperation, TypeError) as e:
            logger.warning(f"delta+level translate failed for {et}: {e}")
            return None, None
        return new_level, prior_level

    # Should not reach — unknown event_type. Pass through.
    return ev.actual, ev.previous


async def record_fmp_events(events: list[FMPEvent]) -> int:
    """FMP economic-calendar release rows → macro_releases (idempotent).

    Strategy: write under the FRED namespace (`fred:<EVENT_TYPE>:<YYYY-MM-DD>`
    event_id, source='fred') so when the FRED probe catches up hours later
    with the same value, `_upsert_release_with_revision` returns 'unchanged'
    — no double broadcast, no spurious revision audit.

    FMP's value semantic must match FRED's for this to work (e.g. FMP
    "CPI s.a" = FRED CPIAUCSL = SA level). The adapter's _EVENT_TYPE_MAP
    only enables event types where this holds.

    Returns count of brand-new inserts (broadcasts that fired this batch).
    """
    inserted = 0
    for ev in events:
        date_str = ev.released_at.date().isoformat()
        event_id = f"fred:{ev.event_type}:{date_str}"
        series_id = _EVENT_TYPE_TO_FRED_SERIES.get(ev.event_type, "")
        source_url = (
            f"https://fred.stlouisfed.org/series/{series_id}"
            if series_id
            else "https://financialmodelingprep.com/economic-calendar"
        )
        # FMP returns MoM%/delta/level depending on event_type. Translate to
        # FRED-level so downstream storyteller (which expects level for
        # MoM% computation) keeps working unchanged.
        translated_actual, translated_prior = await _translate_fmp_actual(ev)
        if translated_actual is None:
            # No prior FRED level available — skip silently; next FRED probe
            # will seed prior and a subsequent FMP probe will translate.
            continue
        outcome = await _upsert_release_with_revision(
            event_id=event_id,
            event_type=ev.event_type,
            country=ev.country,
            released_at=ev.released_at,
            prior=translated_prior,
            actual=translated_actual,
            source="fred",
            source_url=source_url,
            trigger_narrative=True,
            published_at=ev.published_at,
        )
        if outcome == "inserted":
            inserted += 1
            # Backfill consensus into macro_release_expected so storyteller
            # can compute surprise without an admin POST (Faz D regression).
            if ev.estimate is not None:
                try:
                    await _record_consensus_from_fmp(event_id, ev)
                except Exception as e:
                    logger.warning(f"consensus persist failed for {event_id}: {e}")
            logger.info(
                f"fmp_economic new release: {event_id} actual={ev.actual} "
                f"est={ev.estimate} prior={ev.previous} src='{ev.raw_event_name}'"
            )
    return inserted


async def _record_consensus_from_fmp(event_id: str, ev: FMPEvent) -> None:
    """Write FMP's `estimate` as the consensus expected value.

    Branching by event_type:
      - DIRECT_LEVEL (CPI/CORE_CPI/UNRATE/JOBLESS_*): FMP estimate IS a level
        → write to expected_value column.
      - MOM_PCT (PPI/CORE_PPI/PCE/CORE_PCE/RETAIL): FMP estimate IS MoM%
        → write to expected_mom_pct column (storyteller reads from there).
      - DELTA_K (NFP): FMP estimate is thousands-of-jobs delta. Storyteller
        reads NFP's MoM% via paired UNRATE; we store estimate raw on
        expected_value for now (will refine if NFP narrative requires it).
    """
    et = ev.event_type
    if et in _FMP_MOM_PCT_EVENT_TYPES:
        sql = text("""
            UPDATE macro_releases
            SET expected_mom_pct = :estimate
            WHERE event_id = :event_id
              AND (expected_mom_pct IS NULL OR expected_mom_pct <> :estimate)
        """)
    else:
        sql = text("""
            UPDATE macro_releases
            SET expected_value = :estimate
            WHERE event_id = :event_id
              AND (expected_value IS NULL OR expected_value <> :estimate)
        """)
    async with engine.begin() as conn:
        await conn.execute(sql, {"event_id": event_id, "estimate": ev.estimate})


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
                    # FAZ B — FOMC_PROJECTIONS event'i geldiğinde Fed SEP
                    # PDF'ini otomatik parse et + medianları macro_releases'a
                    # SEP_* synthetic event_id'leriyle yaz. Fire-and-forget.
                    if ev.event_type == "FOMC_PROJECTIONS":
                        _trigger_sep_autoparse(ev.event_id, ev.released_at)
    except Exception as e:
        logger.error(f"record_fed_rss_events failed: {e}")
    return inserted


# Strong-ref set so fire-and-forget SEP parse tasks aren't GC'd.
_SEP_AUTOPARSE_INFLIGHT: set = set()


def _trigger_sep_autoparse(event_id: str, released_at: datetime) -> None:
    """Fire-and-forget Fed SEP PDF parse. Lazy import keeps fed_sep ↔
    release_detect import order safe."""
    try:
        task = asyncio.create_task(_autoparse_sep(event_id, released_at))
        _SEP_AUTOPARSE_INFLIGHT.add(task)
        task.add_done_callback(_SEP_AUTOPARSE_INFLIGHT.discard)
    except Exception as e:
        logger.error(f"SEP autoparse trigger failed for {event_id}: {e}")


async def _autoparse_sep(event_id: str, released_at: datetime) -> None:
    """SEP PDF'i indir, parse et, medianları macro_releases'a yaz.

    Sessiz fail eder — admin manuel entry endpoint'i her zaman fallback.
    """
    try:
        from services.macro_sources.fed_sep import fetch_sep_medians
    except Exception as e:
        logger.warning(f"SEP autoparse skip (import): {e}")
        return
    try:
        medians = await fetch_sep_medians(released_at)
    except Exception as e:
        logger.warning(f"SEP autoparse fetch failed for {event_id}: {e}")
        return
    if not medians.success or not medians.has_any():
        logger.warning(
            f"SEP autoparse no data for {event_id}: "
            f"error={medians.error}"
        )
        return
    date_key = released_at.date().isoformat()
    rows = [
        ("FUNDS_END_0", medians.end_year_0),
        ("FUNDS_END_1", medians.end_year_1),
        ("FUNDS_END_2", medians.end_year_2),
        ("FUNDS_LONGER_RUN", medians.longer_run),
    ]
    sql = text("""
        INSERT INTO macro_releases
        (event_id, event_type, country, released_at, actual_value, source, source_url)
        VALUES (:event_id, :event_type, 'US', :released_at, :actual,
                'fed_sep_auto',
                'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm')
        ON CONFLICT (event_id) DO UPDATE
        SET actual_value = EXCLUDED.actual_value,
            released_at = EXCLUDED.released_at
    """)
    inserted = 0
    try:
        async with engine.begin() as conn:
            for suffix, val in rows:
                if val is None:
                    continue
                eid = f"sep:{suffix}:{date_key}"
                await conn.execute(sql, {
                    "event_id": eid,
                    "event_type": f"SEP_{suffix}",
                    "released_at": released_at,
                    "actual": val,
                })
                inserted += 1
        logger.info(
            f"SEP autoparse wrote {inserted} medians for {event_id} "
            f"(end_0={medians.end_year_0}, end_1={medians.end_year_1}, "
            f"end_2={medians.end_year_2}, LR={medians.longer_run})"
        )
    except Exception as e:
        logger.error(f"SEP autoparse persist failed for {event_id}: {e}")


# Strong-ref set so fire-and-forget narrative generation tasks aren't GC'd
# while waiting on Gemini / DB. Mirrors _REVISION_BROADCAST_INFLIGHT below
# and _STORY_BROADCAST_INFLIGHT in macro_storyteller. 2026-05-13 PPI Apr
# incident: narrative task was scheduled but lost to GC before generate_*
# ran, leaving narrative_md=NULL → no Telegram broadcast.
_NARRATIVE_INFLIGHT: set = set()


def _trigger_narrative(event_id: str) -> None:
    """Fire-and-forget narrative generation. Imported lazily to keep
    macro_narrative ↔ release_detect import order safe (narrative reads from
    the same engine import release_detect uses).
    """
    try:
        from services.macro_narrative import generate_narrative_safe
        task = asyncio.create_task(generate_narrative_safe(event_id))
        _NARRATIVE_INFLIGHT.add(task)
        task.add_done_callback(_NARRATIVE_INFLIGHT.discard)
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
