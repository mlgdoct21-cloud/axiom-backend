"""Federal Reserve press release RSS poller — ETag/If-Modified-Since aware.

Feeds the Macro Intelligence Layer with FOMC statement / rate decision /
minutes events. The pure parser (`parse_rss`) is decoupled from the network
fetch so it can be unit-tested with fixture XML.

ETag persistence is in-memory in this v0; promoted to Postgres in Hafta 2.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx

from core.logger import get_logger

logger = get_logger("macro.fed_rss")

FED_PRESS_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"

# Title classifier: ordered — first match wins.
_EVENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("FOMC_STATEMENT", re.compile(r"\bFOMC\s+statement\b", re.I)),
    ("FOMC_MINUTES", re.compile(r"\bFOMC\b.*\bminutes\b|\bminutes\s+of\s+the\s+Federal\s+Open\s+Market", re.I)),
    ("RATE_DECISION", re.compile(r"\bfederal\s+funds\s+rate\b|\btarget\s+range\b", re.I)),
    ("FOMC_PROJECTIONS", re.compile(r"\bSummary\s+of\s+Economic\s+Projections\b|\bSEP\b", re.I)),
]

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@dataclass
class ReleaseEvent:
    event_id: str            # source+sha1(title|published) — idempotency key
    event_type: str          # FOMC_STATEMENT / FOMC_MINUTES / RATE_DECISION / FOMC_PROJECTIONS
    title: str
    url: str
    released_at: datetime    # tz-aware UTC
    source: str = "fed_rss"
    raw_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "title": self.title,
            "url": self.url,
            "released_at": self.released_at.isoformat(),
            "source": self.source,
            "raw_summary": self.raw_summary,
        }


@dataclass
class FetchResult:
    events: list[ReleaseEvent] = field(default_factory=list)
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False     # 304 short-circuit
    error: Optional[str] = None


def _classify(title: str) -> Optional[str]:
    for label, pattern in _EVENT_PATTERNS:
        if pattern.search(title):
            return label
    return None


def _make_event_id(title: str, published_iso: str) -> str:
    digest = hashlib.sha1(f"{title}|{published_iso}".encode("utf-8")).hexdigest()
    return f"fed_rss:{digest[:16]}"


def _parse_published(entry) -> Optional[datetime]:
    if getattr(entry, "published_parsed", None):
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        # feedparser falls back to RFC822; isoformat-ish fallback
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_rss(xml_bytes: bytes) -> list[ReleaseEvent]:
    """Pure parser — takes feed bytes, returns filtered FOMC-relevant events.

    Unit-testable: pass fixture bytes, no network involved.
    """
    feed = feedparser.parse(xml_bytes)
    events: list[ReleaseEvent] = []
    for entry in feed.entries or []:
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        event_type = _classify(title)
        if not event_type:
            continue
        published = _parse_published(entry)
        if not published:
            continue
        url = entry.get("link") or ""
        summary_html = entry.get("summary") or entry.get("description") or ""
        events.append(
            ReleaseEvent(
                event_id=_make_event_id(title, published.isoformat()),
                event_type=event_type,
                title=title,
                url=url,
                released_at=published,
                raw_summary=summary_html[:1000],
            )
        )
    return events


async def fetch_fed_rss(
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    url: str = FED_PRESS_RSS_URL,
) -> FetchResult:
    """Async fetch with ETag/If-Modified-Since. Returns FetchResult.

    On 304: events=[], not_modified=True. On 200: parsed events + new etag.
    Network errors logged; FetchResult.error populated, events=[].
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.error(f"fed_rss fetch failed: {e}")
        return FetchResult(error=str(e))

    if resp.status_code == 304:
        return FetchResult(not_modified=True, etag=etag, last_modified=last_modified)

    if resp.status_code != 200:
        msg = f"unexpected status {resp.status_code}"
        logger.error(f"fed_rss {msg}")
        return FetchResult(error=msg, etag=etag, last_modified=last_modified)

    events = parse_rss(resp.content)
    return FetchResult(
        events=events,
        etag=resp.headers.get("etag"),
        last_modified=resp.headers.get("last-modified"),
    )
