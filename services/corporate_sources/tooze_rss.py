"""Adam Tooze (adamtooze.com) WordPress RSS adaptörü — arka-plan sinyali.

`isyatirim_rss.py` forkudur: saf parser (`parse_feed`) network fetch'ten
(`fetch_feed`) ayrı; ETag/If-Modified-Since persist'i
`corporate_source_state` tablosunda (source='tooze').

Tooze adamtooze.com WordPress feed'i `content:encoded` ile TAM METİN
taşır (probe: medyan ~8.5k karakter/yazı, truncated=False beklenir).
Tek feed — kategori/fallback yok. Çıktıda İSİM/ATIF YOK: bu kaynak
yalnız AXIOM'un kendi görüşünü besleyen "arka-plan sinyali"dir
(data-first atıfsız model; telif güvenliği L_DISPLACE + özgünlük).

Fail policy: sessiz skip + log; exception ile pipeline kırılmaz.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import feedparser
import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("corporate.tooze_rss")

FEED_URL = "https://adamtooze.com/feed/"

SOURCE_KEY = "tooze"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SMART_MAP = {
    " ": " ",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "…": "...",
}


@dataclass
class ToozePost:
    title: str
    link: str
    published: datetime          # tz-aware UTC
    body_text: str
    truncated: bool              # True = sadece summary geldi (content yok)
    author: str = ""


def _strip_html(raw: str) -> str:
    """HTML → düz metin. tag strip → entity decode → akıllı tırnak →
    whitespace collapse. BS4 KULLANILMAZ (mahfi/isyatirim pattern)."""
    if not raw:
        return ""
    text_only = _TAG_RE.sub(" ", raw)
    text_only = html.unescape(text_only)
    for bad, good in _SMART_MAP.items():
        text_only = text_only.replace(bad, good)
    return _WS_RE.sub(" ", text_only).strip()


def _parse_published(entry) -> Optional[datetime]:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if parsed:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_feed(xml_bytes: bytes) -> list[ToozePost]:
    """Saf parser — feed byte'ları → ToozePost listesi. Network YOK.

    Gövde önceliği: content[0].value (full-text, truncated=False); yoksa
    summary (truncated=True); ikisi de yoksa skip. Boş gövde skip.
    """
    if not xml_bytes:
        return []
    feed = feedparser.parse(xml_bytes)
    posts: list[ToozePost] = []
    for entry in feed.entries or []:
        title = _strip_html(entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        published = _parse_published(entry)
        if not published:
            continue

        content_list = entry.get("content") or []
        raw_body = ""
        truncated = True
        if content_list and (content_list[0].get("value") or "").strip():
            raw_body = content_list[0]["value"]
            truncated = False
        elif (entry.get("summary") or "").strip():
            raw_body = entry["summary"]
            truncated = True
        else:
            continue

        body_text = _strip_html(raw_body)
        if not body_text:
            continue

        posts.append(
            ToozePost(
                title=title,
                link=link,
                published=published,
                body_text=body_text,
                truncated=truncated,
                author=_strip_html(entry.get("author") or "").strip(),
            )
        )
    return posts


async def fetch_feed(
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
) -> tuple[Optional[bytes], dict]:
    """Feed'i çek. ETag / If-Modified-Since aware.

    Dönüş:
      - 304          → (None, {'status': 304})
      - 200          → (body, {'status': 200, 'etag': .., 'last_modified': ..})
      - hata / >=400 → (None, {'status': <code|0>, 'error': <msg>})
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(FEED_URL, headers=headers)
    except httpx.HTTPError as e:
        logger.warning(f"tooze_rss fetch failed: {e}")
        return None, {"status": 0, "error": str(e)}

    if resp.status_code == 304:
        return None, {"status": 304}
    if resp.status_code >= 400:
        logger.warning(f"tooze_rss HTTP {resp.status_code}")
        return None, {"status": resp.status_code, "error": f"HTTP {resp.status_code}"}
    return resp.content, {
        "status": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def posts_in_window(
    posts: list[ToozePost], start: datetime, end: datetime
) -> list[ToozePost]:
    """[start, end) yarı-açık penceredeki yazılar, yayın tarihi DESC."""
    in_window = [p for p in posts if start <= p.published < end]
    return sorted(in_window, key=lambda p: p.published, reverse=True)


def week_event_id(monday_utc: datetime) -> str:
    """Haftalık idempotency anahtarı — sha1('tooze-week|<iso>')[:16].
    Kaynak-özel prefix → event_id çakışmaz."""
    seed = f"tooze-week|{monday_utc.date().isoformat()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


async def read_source_state(
    source: str = SOURCE_KEY,
) -> tuple[Optional[str], Optional[str]]:
    """corporate_source_state'ten (etag, last_modified) oku. Tablo/DB
    yoksa sessiz (None, None) — fail policy."""
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT etag, last_modified FROM corporate_source_state "
                        "WHERE source = :s"
                    ),
                    {"s": source},
                )
            ).first()
        if row:
            return row[0], row[1]
    except Exception as e:  # noqa: BLE001
        logger.info(f"read_source_state skip ({source}): {e}")
    return None, None


async def write_source_state(
    etag: Optional[str],
    last_modified: Optional[str],
    source: str = SOURCE_KEY,
) -> None:
    """corporate_source_state'e ETag/Last-Modified UPSERT. Hata = sessiz
    skip + log (pipeline kırılmaz)."""
    if not etag and not last_modified:
        return
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO corporate_source_state "
                    "(source, etag, last_modified, updated_at) "
                    "VALUES (:s, :e, :lm, NOW()) "
                    "ON CONFLICT (source) DO UPDATE SET "
                    "etag = EXCLUDED.etag, "
                    "last_modified = EXCLUDED.last_modified, "
                    "updated_at = NOW()"
                ),
                {"s": source, "e": etag, "lm": last_modified},
            )
    except Exception as e:  # noqa: BLE001
        logger.info(f"write_source_state skip ({source}): {e}")
