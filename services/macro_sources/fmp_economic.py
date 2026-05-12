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

Scope (2026-05-12 evening expansion): CPI / Core CPI / Unemployment Rate
(level — already wired), plus **NEW**: PPI / Core PPI / NFP / PCE / Core PCE /
Jobless Claims / Retail Sales (MoM% or thousands — composite translation
in release_detect.py converts to FRED's level convention).

The new event_types use defensive multi-pattern regexes because FMP's exact
event name strings for these aren't fully documented in public docs. Every
unmatched US row is INFO-logged with its raw `event` field so we can refine
patterns post-release if anything slips through.
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
#
# 2026-05-12: switched from "CPI s.a" (SA, FRED CPIAUCSL semantic) to "CPI"
# (NSA, FRED CPIAUCNS semantic). Reason: TR media (Bloomberg HT, Investing.com)
# uses NSA "manşet" rakamı (% 0.6 Nisan); user kıyaslarken farklı seri
# yüzünden kafa karışıyordu. NSA matches headline rakamı 1:1. Fed SA tracking
# yapıyor ama hikaye anlatımı için NSA primary, SA isteğe bağlı ek.
#
# Period token in parens is parsed separately into released_at (observation
# period start, matching FRED convention — e.g. "(Apr)" → 2026-04-01).
_EVENT_TYPE_MAP = (
    # (regex, event_type, country)
    # --- Level series (FMP returns same level as FRED, no translation) ----
    # NSA level — Investing.com'un "manşet CPI" ile aynı. "CPI s.a" (SA)
    # regex'i match etmiyor çünkü "s.a" başında değil — "CPI (" ile başlar.
    (re.compile(r"^CPI\s*\(", re.I),                    "CPI",       "US"),
    (re.compile(r"^Core CPI\s*\(", re.I),               "CORE_CPI",  "US"),
    (re.compile(r"^Unemployment Rate\s*\(", re.I),       "UNRATE",    "US"),

    # --- MoM% events (composite translation to FRED level in release_detect) ----
    # Core PPI — daha spesifik ("Core" kelimesi PPI'dan önce) önce yazılmalı.
    (re.compile(r"^Core PPI\b", re.I),                  "CORE_PPI",  "US"),
    (re.compile(r"^PPI\b", re.I),                       "PPI",       "US"),
    (re.compile(r"^Producer Price Index", re.I),        "PPI",       "US"),
    # Core PCE — daha spesifik önce.
    (re.compile(r"^Core PCE", re.I),                    "CORE_PCE",  "US"),
    (re.compile(r"^PCE Price Index", re.I),             "PCE",       "US"),
    (re.compile(r"^PCE\b", re.I),                       "PCE",       "US"),
    # Retail Sales (Core önce)
    (re.compile(r"^Core Retail Sales", re.I),           "CORE_RETAIL_SALES", "US"),
    (re.compile(r"^Retail Sales", re.I),                "RETAIL_SALES", "US"),

    # --- Thousands-of-jobs delta (composite: prior PAYEMS + delta) ----
    (re.compile(r"^Nonfarm Payrolls", re.I),            "NFP",       "US"),
    (re.compile(r"^Non-Farm Payrolls", re.I),           "NFP",       "US"),
    (re.compile(r"^NFP\b", re.I),                       "NFP",       "US"),

    # --- Thousands level (FMP and FRED ICSA both store thousands) ----
    (re.compile(r"^Initial Jobless Claims", re.I),      "JOBLESS_INITIAL", "US"),
    (re.compile(r"^Continuing Jobless Claims", re.I),   "JOBLESS_CONTINUING", "US"),
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


_MONTHLY_PREV_MONTH_DEFAULT = frozenset({
    "PPI", "CORE_PPI", "PCE", "CORE_PCE",
    "RETAIL_SALES", "CORE_RETAIL_SALES",
    "NFP",
    # CPI/CORE_CPI/UNRATE da prev-month default'a fallback olur ama isimleri
    # "(Apr)" suffix taşıdığı için regex match ediyor. Yine de listede tut.
    "CPI", "CORE_CPI", "UNRATE",
})


def _parse_period(event_name: str, fallback_dt: datetime, event_type: str = "") -> datetime:
    """Extract observation period start from "Event (Mon)" or "Event (MonYY)".

    Primary path: regex `\\([A-Za-z]{3,9}\\)` → e.g. "(Apr)" → April of the
    most recent past-or-current year. Matches CPI/Core CPI/Unemployment Rate.

    Fallback: when event name doesn't carry a parenthesized month token (e.g.
    "PPI MoM" or "Nonfarm Payrolls"), use event_type to infer:
      - Monthly economic releases (PPI/PCE/NFP/Retail): previous-month start.
        Released in May 2026 → April 1, 2026 observation period.
      - JOBLESS_CLAIMS (weekly) and unknown event_types: fallback_dt date
        (month start) as a safe default — caller can refine later.
    """
    m = re.search(r"\(([A-Za-z]{3,9})\)", event_name)
    if m:
        tok = m.group(1).upper()[:3]
        month = _MONTH_ABBR.get(tok)
        if month:
            # Year: pick the most recent past-or-current year where this
            # month lies at or before fallback_dt. Released in May 2026 with
            # token "Apr" → use April 2026. Token "Dec" released in Jan 2027
            # → use Dec 2026 (year-1).
            year = fallback_dt.year
            candidate = date(year, month, 1)
            if candidate > fallback_dt.date():
                year -= 1
            return datetime(year, month, 1, tzinfo=timezone.utc)

    # No explicit period token. Use event_type-based default.
    if event_type in _MONTHLY_PREV_MONTH_DEFAULT:
        # Previous-month start (April CPI released in May → 2026-04-01).
        first_of_this = fallback_dt.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        # One day back lands in previous month; replace day=1 again.
        prev_month_last_day = first_of_this - timedelta(days=1)
        return prev_month_last_day.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

    # Default (weekly or unknown): current-month start.
    return fallback_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


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
    seen_unmatched_us = set()
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
            # Log unmatched US event names once per fetch — actual!=null only.
            # Helps refine regex when FMP uses an unexpected naming for a
            # release we care about. Pre-release (actual=null) rows are
            # noisy so we skip them in the log.
            if ev.get("actual") is not None and event_name not in seen_unmatched_us:
                seen_unmatched_us.add(event_name)
                logger.info(
                    f"fmp_economic UNMATCHED US event: '{event_name}' "
                    f"actual={ev.get('actual')} date={ev.get('date')}"
                )
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

        released_at = _parse_period(event_name, published_at, event_type)

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
