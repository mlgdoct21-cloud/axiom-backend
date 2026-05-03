"""Adaptive polling orchestrator — turns the static release calendar into
per-source intervals consumed by reliability_probe.

Default cadence (`SOURCE_INTERVAL` in reliability_probe) is 5 min for fed_rss,
60 min for FRED + Kalshi. When wall-clock falls within ±HOT_WINDOW of any
upcoming event in the calendar, every source listed under that event's
`sources_to_accelerate` ramps to HOT_INTERVAL (10 s).

Calendar sources, in dedupe priority:
1. FRED `release/dates` for CPI / NFP / PCE — authoritative announcement
   schedule pulled from api.stlouisfed.org with a 24h in-memory TTL
   (services/macro_sources/fred_calendar.py).
2. `data/macro_calendar.yaml` for FOMC, Kalshi, and any other event types
   FRED doesn't expose. Manual edits still bounce the process.

YAML rows for event_types FRED owns are dropped on overlap to avoid two
hot-window triggers ~1 min apart from a stale manual entry vs the canonical
FRED date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from core.logger import get_logger

logger = get_logger("macro.calendar")

_YAML_PATH = Path(__file__).resolve().parent.parent / "data" / "macro_calendar.yaml"

HOT_WINDOW = timedelta(minutes=30)
HOT_INTERVAL = timedelta(seconds=10)


@dataclass(frozen=True)
class CalendarEvent:
    event_type: str
    label: str
    scheduled_at: datetime           # tz-aware UTC
    sources_to_accelerate: tuple[str, ...]


def _parse_dt(raw) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# Event types FRED owns. YAML rows for these are skipped when they collide
# with a FRED-derived row (same event_type + same date).
_FRED_OWNED_TYPES = frozenset({"CPI", "NFP", "PCE"})


@lru_cache(maxsize=1)
def _load_yaml_calendar(path: Path | None = None) -> tuple[CalendarEvent, ...]:
    """Read + validate `data/macro_calendar.yaml`. Sorted ascending."""
    p = path or _YAML_PATH
    try:
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"macro_calendar.yaml not found at {p}; YAML empty")
        return tuple()

    items = raw.get("events") or []
    out: list[CalendarEvent] = []
    for entry in items:
        sched = _parse_dt(entry.get("scheduled_at"))
        et = entry.get("event_type")
        if not sched or not et:
            continue
        srcs = entry.get("sources_to_accelerate") or []
        out.append(CalendarEvent(
            event_type=str(et),
            label=str(entry.get("label") or et),
            scheduled_at=sched,
            sources_to_accelerate=tuple(str(s) for s in srcs),
        ))
    out.sort(key=lambda e: e.scheduled_at)
    return tuple(out)


def _merge_with_fred(yaml_events: tuple[CalendarEvent, ...], fred_events: list) -> tuple[CalendarEvent, ...]:
    """FRED entries win on (event_type, date) collisions. Keys outside
    _FRED_OWNED_TYPES (FOMC, Kalshi-Fed) come exclusively from YAML."""
    # Build (event_type, YYYY-MM-DD) → CalendarEvent index from FRED side.
    fred_by_key: dict[tuple[str, str], CalendarEvent] = {}
    for ev in fred_events:
        key = (ev.event_type, ev.scheduled_at.date().isoformat())
        # Re-wrap into the local dataclass so downstream isinstance() checks
        # work even though fred_calendar uses its own copy of the dataclass.
        fred_by_key[key] = CalendarEvent(
            event_type=ev.event_type,
            label=ev.label,
            scheduled_at=ev.scheduled_at,
            sources_to_accelerate=tuple(ev.sources_to_accelerate),
        )
    merged: list[CalendarEvent] = list(fred_by_key.values())
    for ev in yaml_events:
        if ev.event_type in _FRED_OWNED_TYPES:
            key = (ev.event_type, ev.scheduled_at.date().isoformat())
            if key in fred_by_key:
                continue  # FRED already covers this slot
        merged.append(ev)
    merged.sort(key=lambda e: e.scheduled_at)
    return tuple(merged)


# Cached merged result. Reset by `invalidate_calendar_cache()` so the admin
# refresh endpoint can force a re-fetch without bouncing the process.
_MERGED_CACHE: dict[str, object] = {"events": None}


async def load_calendar(*, force_refresh: bool = False) -> tuple[CalendarEvent, ...]:
    """Async merge of YAML + FRED. Uses fred_calendar's own 24h TTL cache,
    so most calls just hit memory. `force_refresh=True` bypasses both caches.
    """
    if not force_refresh and _MERGED_CACHE.get("events") is not None:
        return _MERGED_CACHE["events"]  # type: ignore[return-value]
    yaml_events = _load_yaml_calendar()
    try:
        from services.macro_sources.fred_calendar import get_cached_calendar
        fred_events = await get_cached_calendar(force=force_refresh)
    except Exception as e:
        logger.warning(f"FRED calendar fetch failed; falling back to YAML only: {e}")
        fred_events = []
    merged = _merge_with_fred(yaml_events, fred_events)
    _MERGED_CACHE["events"] = merged
    return merged


def invalidate_calendar_cache() -> None:
    """Drop the cached merged calendar. Next load_calendar() call re-merges
    YAML + FRED. Caller should also pass force_refresh=True if they want to
    bypass fred_calendar's 24h TTL on top of this."""
    _MERGED_CACHE["events"] = None


def _sync_calendar_snapshot() -> tuple[CalendarEvent, ...]:
    """Read whatever the merger most recently produced — sync-safe for the
    hot-path `is_in_hot_window` / `effective_interval` callers, which run
    inside reliability_probe's tight loop and can't await. The probe loop
    primes the cache via load_calendar() at startup; this just reads it."""
    cached = _MERGED_CACHE.get("events")
    if cached is not None:
        return cached  # type: ignore[return-value]
    # First-call fallback: YAML-only until the async loader runs.
    return _load_yaml_calendar()


async def upcoming_events(now: datetime, *, days: int = 14) -> list[CalendarEvent]:
    """Events scheduled in [now − HOT_WINDOW, now + days). Hot-window inclusion
    on the lower bound lets the admin /calendar endpoint show events that
    just released but are still within the post-release tail.
    """
    horizon = now + timedelta(days=days)
    floor = now - HOT_WINDOW
    events = await load_calendar()
    return [e for e in events if floor <= e.scheduled_at < horizon]


def is_in_hot_window(source: str, now: datetime, window: timedelta = HOT_WINDOW) -> bool:
    """True iff some calendar event accelerating `source` is within ±window of now.

    Sync read — uses whatever the async loader has most recently cached. The
    probe loop calls `await load_calendar()` at startup and after each TTL
    bump, so this read is always backed by a fresh merge.
    """
    for ev in _sync_calendar_snapshot():
        # Calendar is sorted; once we're past now+window with no acceleration,
        # we can't match further events. Cheap guard for long lists.
        if ev.scheduled_at > now + window:
            break
        if ev.scheduled_at < now - window:
            continue
        if source in ev.sources_to_accelerate:
            return True
    return False


def effective_interval(source: str, now: datetime, default: timedelta) -> timedelta:
    """`HOT_INTERVAL` inside the hot window, otherwise the source's default."""
    if is_in_hot_window(source, now):
        return HOT_INTERVAL
    return default
