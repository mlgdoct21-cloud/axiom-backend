"""Adaptive polling orchestrator — turns the static release calendar into
per-source intervals consumed by reliability_probe.

Default cadence (`SOURCE_INTERVAL` in reliability_probe) is 5 min for fed_rss,
60 min for FRED + Kalshi. When wall-clock falls within ±HOT_WINDOW of any
upcoming event in `data/macro_calendar.yaml`, every source listed under that
event's `sources_to_accelerate` ramps to HOT_INTERVAL (10 s).

Calendar is loaded once at import (via lru_cache) — bounce the process to
pick up edits. Q3 task: replace YAML with FRED `releases/dates` autopopulate.
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


@lru_cache(maxsize=1)
def load_calendar(path: Path | None = None) -> tuple[CalendarEvent, ...]:
    """Read + validate the YAML once. Sorted ascending by scheduled_at."""
    p = path or _YAML_PATH
    try:
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"macro_calendar.yaml not found at {p}; calendar empty")
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


def upcoming_events(now: datetime, *, days: int = 14) -> list[CalendarEvent]:
    """Events scheduled in [now − HOT_WINDOW, now + days). Hot-window inclusion
    on the lower bound lets the admin /calendar endpoint show events that
    just released but are still within the post-release tail.
    """
    horizon = now + timedelta(days=days)
    floor = now - HOT_WINDOW
    return [e for e in load_calendar() if floor <= e.scheduled_at < horizon]


def is_in_hot_window(source: str, now: datetime, window: timedelta = HOT_WINDOW) -> bool:
    """True iff some calendar event accelerating `source` is within ±window of now."""
    for ev in load_calendar():
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
