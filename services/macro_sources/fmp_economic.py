"""FMP economic-calendar adapter — instant release detection.

FRED mirrors BLS data but with 2-8h delay (FRED is a research archive, not a
breaking-news feed). Competitors (Bloomberg HT, Investing.com, Trading
Economics) publish CPI/NFP/PCE/PPI within seconds of BLS release because they
have direct feeds.

FMP's `/stable/economic-calendar` endpoint mirrors Trading Economics' release
feed and includes:
  - `actual` value — published at exact release time (e.g. CPI 12:30 UTC)
  - `estimate` — market consensus (free, was Trading Economics $50/mo)
  - `previous` — prior period value

We're already paying for FMP Starter ($29/mo). This adapter is a primary
release-detection source; FRED stays in the loop as a slower confirming
source (catches FMP mistakes and provides long-history backfill).

Scope (initial 2026-05-12): CPI s.a, Core CPI, Unemployment Rate. These are
FMP level series that match FRED's storage semantic (CPIAUCSL / CPILFESL /
UNRATE) so an FMP-reported row converges to FRED's value when FRED syncs —
no spurious revision broadcasts.

NFP/PCE/PPI deferred: FMP gives MoM/YoY only (no level series), requires
semantic translation to FRED's level convention.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.fmp_economic")

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# FMP "event" string → our internal event_type. Matched case-insensitively
# against the start of the event field (after stripping "(Mon)" suffix).
# Only level series that map 1:1 to FRED semantics are enabled here; MoM/YoY
# variants are skipped to avoid double-counting.
#
# Period token in parens is parsed separately into released_at (observation
# period start, matching FRED convention — e.g. "(Apr)" → 2026-04-01).
_EVENT_TYPE_MAP = (
    # (regex, event_type, country)
    (re.compile(r"^CPI s\.a\s*\(", re.I),              "CPI",       "US"),
    (re.compile(r"^Core CPI\s*\(", re.I),               "CORE_CPI",  "US"),
    (re.compile(r"^Unemployment Rate\s*\(", re.I),       "UNRATE",    "US"),
)

# Months for period parsing — both English (FMP locale) and short forms.
_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class FMPEvent:
    """A single FMP economic-calendar row we care about.

    `released_at` is the observation period start (Apr CPI → 2026-04-01),
    matching FRED's convention. `published_at` is when FMP listed actual
    (12:30 UTC on the release day) — used for ordering, not stored as
    canonical released_at.
    """
    event_type: str               # CPI / CORE_CPI / UNRATE
    country: str
    released_at: datetime         # observation period start, UTC midnight
    published_at: datetime        # FMP listed timestamp (release wall-clock)
    actual: Decimal
    estimate: Optional[Decimal] = None
    previous: Optional[Decimal] = None
    raw_event_name: str = ""      # for logging only


@dataclass
class FMPFetchResult:
    success: bool = False
    events: list[FMPEvent] = field(default_factory=list)
    http_status: Optional[int] = None
    payload_bytes: int = 0
    error: Optional[str] = None


def _api_key() -> str:
    return os.getenv("FMP_API_KEY", "").strip()


def _safe_decimal(raw) -> Optional[Decimal]:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_period(event_name: str, fallback_dt: datetime) -> datetime:
    """Extract observation period start from "Event (Mon)" or "Event (MonYY)".

    FMP usually writes "(Apr)" or "(Q1)" without year — assume "near"
    fallback_dt (the release wall-clock). For monthly CPI April release on
    2026-05-12, "(Apr)" maps to 2026-04-01.

    On unparseable input returns fallback_dt at month start.
    """
    m = re.search(r"\(([A-Za-z]{3,9})\)", event_name)
    if not m:
        return fallback_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    tok = m.group(1).upper()[:3]
    month = _MONTH_ABBR.get(tok)
    if not month:
        return fallback_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Year: pick the most recent past-or-current year where this month lies
    # at or before fallback_dt. Released in May 2026 with token "Apr" →
    # use April 2026 (same year, month -1). Token "Dec" released in Jan
    # 2027 → use Dec 2026 (year-1).
    year = fallback_dt.year
    candidate = date(year, month, 1)
    if candidate > fallback_dt.date():
        year -= 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _classify(event_name: str) -> Optional[tuple[str, str]]:
    for pattern, event_type, country in _EVENT_TYPE_MAP:
        if pattern.match(event_name):
            return event_type, country
    return None


async def fetch_fmp_calendar(
    *, from_date: date, to_date: date
) -> FMPFetchResult:
    """One HTTP call to FMP /stable/economic-calendar.

    Returns only mappable, released (actual non-null) events. Pre-release
    events with `actual=null` are skipped (we trigger only on actual).
    """
    api_key = _api_key()
    if not api_key:
        return FMPFetchResult(error="FMP_API_KEY missing")

    params = {
        "from": from_date.strftime("%Y-%m-%d"),
        "to": to_date.strftime("%Y-%m-%d"),
        "apikey": api_key,
    }
    headers = {"User-Agent": _USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{FMP_BASE_URL}/economic-calendar",
                params=params,
                headers=headers,
            )
    except Exception as e:
        err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.error(f"fmp_economic fetch failed: {err}")
        return FMPFetchResult(error=err)

    out = FMPFetchResult(http_status=resp.status_code, payload_bytes=len(resp.content))
    if resp.status_code != 200:
        out.error = f"HTTP {resp.status_code}"
        return out

    try:
        body = resp.json()
    except Exception as e:
        out.error = f"json decode: {e}"
        return out

    if isinstance(body, dict) and ("Error Message" in body or "error" in body):
        out.error = str(body.get("Error Message") or body.get("error"))
        return out
    if not isinstance(body, list):
        out.error = f"unexpected response type: {type(body).__name__}"
        return out

    events: list[FMPEvent] = []
    for ev in body:
        if not isinstance(ev, dict):
            continue
        country = ev.get("country") or ""
        # US-only initial scope.
        if country not in ("US", "United States"):
            continue
        event_name = ev.get("event") or ""
        cls = _classify(event_name)
        if not cls:
            continue
        event_type, normalized_country = cls

        actual = _safe_decimal(ev.get("actual"))
        if actual is None:
            # Pre-release row — wait for actual to land.
            continue

        # FMP date format: "2026-05-12 12:30:00" (UTC by inspection).
        raw_dt = ev.get("date") or ""
        try:
            published_at = datetime.strptime(raw_dt[:16], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            # Skip rows with malformed date — can't anchor period inference.
            continue

        released_at = _parse_period(event_name, published_at)

        events.append(
            FMPEvent(
                event_type=event_type,
                country=normalized_country,
                released_at=released_at,
                published_at=published_at,
                actual=actual,
                estimate=_safe_decimal(ev.get("estimate")),
                previous=_safe_decimal(ev.get("previous")),
                raw_event_name=event_name,
            )
        )

    out.success = True
    out.events = events
    return out


# ── Probe helper ──────────────────────────────────────────────────────────

# Default look-back window for the probe — 7 days of past releases plus the
# next 1 day catches yesterday's hold-over and intraday's fresh release.
# Cheap to fetch (a few hundred rows of JSON, ~50-100 KB).
_DEFAULT_WINDOW_PAST = timedelta(days=7)
_DEFAULT_WINDOW_FUTURE = timedelta(days=1)


async def fetch_fmp_recent_releases() -> FMPFetchResult:
    """Convenience wrapper for the periodic probe — past 7d + next 1d window."""
    now = datetime.now(timezone.utc)
    return await fetch_fmp_calendar(
        from_date=(now - _DEFAULT_WINDOW_PAST).date(),
        to_date=(now + _DEFAULT_WINDOW_FUTURE).date(),
    )
