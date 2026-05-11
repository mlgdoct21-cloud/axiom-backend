"""FRED `release/dates` → CalendarEvent fetcher.

The hand-maintained `data/macro_calendar.yaml` was the original source for
adaptive polling, but it goes stale and silently misses releases when no one
remembers to bump the file. FRED itself publishes the announcement schedule
for every series via `release/dates`, so we let it own CPI/NFP/PCE dates and
keep YAML for everything FRED doesn't track (FOMC decisions, Kalshi).

Returned CalendarEvents are fed alongside YAML entries into macro_calendar
so reliability_probe's hot-window logic doesn't care which source filled
the schedule.

In-memory TTL cache (24h) — FRED's calendar is stable; we don't need the
1-hour `lru_cache` semantics that `load_calendar` started with. Admin
endpoint exposes a manual refresh in case of mid-day BLS reschedules.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date as date_t, datetime, timedelta, timezone
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.fred_calendar")

FRED_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"

# US/Eastern is the canonical release tz for these BLS / BEA / Fed datasets.
# Use zoneinfo so DST flips automatically rather than hard-coding 12:30 UTC.
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — Python <3.9 wouldn't reach prod
    _ET = None

# release_id → (event_type, release time HH:MM in US/Eastern, accelerated sources).
# Times are the standard announcement clock — 08:30 ET for BLS (CPI, NFP, PPI),
# 08:30 ET for BEA (PCE, GDP), 08:30 ET DOL (ICSA), 08:30 ET Census (RSAFS, HOUST).
# FOMC decisions are 14:00 ET but FRED doesn't have a clean release_id for those,
# so YAML still owns them.
#
# IDs canonical FRED ID'leridir; 2026-05-07'de FRED `/releases` endpoint'inden
# doğrulandı. Önceki kodda PCE için release_id=21 yazılmıştı, ama 21 aslında
# "H.6 Money Stock Measures" — PCE'nin doğru ID'si 54 (Personal Income and
# Outlays). Bu fix ile PCE adaptive polling doğru release date'e bakacak.
_RELEASES = {
    10:  ("CPI",  "08:30", ("fred_cpi", "fred_core_cpi")),
    50:  ("NFP",  "08:30", ("fred_nfp", "fred_unrate")),
    54:  ("PCE",  "08:30", ("fred_pce", "fred_core_pce")),  # was incorrectly 21
    # Day 28 part 3 — haftalık jobless + aylık retail/PPI/housing + çeyreklik GDP
    180: ("JOBLESS_CLAIMS", "08:30", ("fred_jobless_initial", "fred_jobless_continuing")),
    9:   ("RETAIL_SALES",   "08:30", ("fred_retail_sales",)),
    46:  ("PPI",            "08:30", ("fred_ppi", "fred_core_ppi")),
    27:  ("HOUSING_STARTS", "08:30", ("fred_housing_starts",)),
    53:  ("GDP",            "08:30", ("fred_gdp",)),
}

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_CACHE_TTL_SECONDS = 24 * 3600

# Module-level cache: list[CalendarEvent] last fetched + timestamp.
_CACHE: dict[str, object] = {"events": None, "fetched_at": 0.0}


@dataclass(frozen=True)
class CalendarEvent:
    """Lightweight mirror of macro_calendar.CalendarEvent. Re-exported so
    callers don't import macro_calendar just for the type."""
    event_type: str
    label: str
    scheduled_at: datetime
    sources_to_accelerate: tuple[str, ...]


def _et_to_utc(d: date_t, hhmm: str) -> Optional[datetime]:
    """8:30 ET on `d` → tz-aware UTC datetime. None on parse failure."""
    if _ET is None:
        return None
    try:
        h, m = (int(x) for x in hhmm.split(":"))
        local = datetime(d.year, d.month, d.day, h, m, 0, tzinfo=_ET)
        return local.astimezone(timezone.utc)
    except Exception:
        return None


async def _fetch_release_dates(release_id: int, *, days_ahead: int) -> list[date_t]:
    """Hit /fred/release/dates for one release_id, return upcoming `date`s
    (today through today+days_ahead). Empty list on auth/HTTP failure —
    callers degrade gracefully to YAML-only schedule.
    """
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        logger.warning("FRED_API_KEY missing; calendar fetch skipped")
        return []
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=days_ahead)
    params = {
        "release_id": str(release_id),
        "api_key": api_key,
        "file_type": "json",
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        # Wide window — cheap (one row per date) and we filter client-side.
        "realtime_start": today.isoformat(),
        "realtime_end": horizon.isoformat(),
    }
    headers = {"User-Agent": _USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(FRED_DATES_URL, params=params, headers=headers)
    except Exception as e:
        logger.warning(f"fred_calendar fetch failed (release {release_id}): {e}")
        return []
    if resp.status_code != 200:
        logger.warning(f"fred_calendar HTTP {resp.status_code} for release {release_id}: {resp.text[:200]}")
        return []
    try:
        body = resp.json()
    except Exception as e:
        logger.warning(f"fred_calendar JSON parse failed: {e}")
        return []
    out: list[date_t] = []
    for entry in body.get("release_dates", []):
        raw = entry.get("date")
        if not raw:
            continue
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= horizon:
            out.append(d)
    return out


def _label_for(event_type: str, d: date_t) -> str:
    # Period being measured = month before announcement (CPI for March is
    # released in mid-April). Generates "Mar 2026 CPI-U" style labels.
    period = (d.replace(day=1) - timedelta(days=1)).strftime("%b %Y")
    if event_type == "NFP":
        return f"{period} Employment Situation"
    if event_type == "CPI":
        return f"{period} CPI-U"
    if event_type == "PCE":
        return f"{period} Personal Income & Outlays"
    # Day 28 part 3
    if event_type == "JOBLESS_CLAIMS":
        # Haftalık — periyot ay başı yerine ilgili haftanın bittiği gün etrafında
        return f"Initial & Continuing Jobless Claims (week ending {d.strftime('%d %b')})"
    if event_type == "RETAIL_SALES":
        return f"{period} Advance Retail Sales"
    if event_type == "PPI":
        return f"{period} Producer Price Index"
    if event_type == "HOUSING_STARTS":
        return f"{period} Housing Starts"
    if event_type == "GDP":
        # Çeyrek bazlı — yayın ayı kullanılacak (advance/preliminary/final ayrımı yapılmıyor)
        return f"GDP Release ({d.strftime('%b %Y')})"
    return f"{period} {event_type}"


async def fetch_fred_calendar_events(*, days_ahead: int = 90) -> list[CalendarEvent]:
    """All FRED-derived events in the next `days_ahead` days, no caching.

    Caller (macro_calendar) handles dedup against YAML and overall caching;
    this just returns the raw FRED slice.
    """
    out: list[CalendarEvent] = []
    for rid, (event_type, hhmm, sources) in _RELEASES.items():
        dates = await _fetch_release_dates(rid, days_ahead=days_ahead)
        for d in dates:
            sched = _et_to_utc(d, hhmm)
            if sched is None:
                continue
            out.append(CalendarEvent(
                event_type=event_type,
                label=_label_for(event_type, d),
                scheduled_at=sched,
                sources_to_accelerate=sources,
            ))
    out.sort(key=lambda e: e.scheduled_at)
    return out


async def get_cached_calendar(*, force: bool = False) -> list[CalendarEvent]:
    """Cached read for hot-path consumers (probe loop). 24h TTL — `force=True`
    bypasses to support the admin refresh endpoint.
    """
    now = time.monotonic()
    cached = _CACHE.get("events")
    fetched_at = float(_CACHE.get("fetched_at") or 0.0)
    if not force and cached is not None and (now - fetched_at) < _CACHE_TTL_SECONDS:
        return list(cached)  # type: ignore[arg-type]
    fresh = await fetch_fred_calendar_events()
    _CACHE["events"] = fresh
    _CACHE["fetched_at"] = now
    return list(fresh)


def cache_status() -> dict:
    """Diagnostic snapshot for the admin endpoint."""
    cached = _CACHE.get("events")
    fetched_at = float(_CACHE.get("fetched_at") or 0.0)
    age_s = (time.monotonic() - fetched_at) if fetched_at else None
    return {
        "cached_event_count": len(cached) if cached else 0,
        "cache_age_seconds": int(age_s) if age_s is not None else None,
        "ttl_seconds": _CACHE_TTL_SECONDS,
    }
