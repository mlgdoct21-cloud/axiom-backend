"""Morgan Stanley "Thoughts on the Market" Art19 podcast RSS adaptörü —
arka-plan sinyali (full-text transkript).

`overshoot_rss.py` forkudur: saf parser (`parse_feed`) network fetch'ten
(`fetch_feed`) ayrı; ETag/If-Modified-Since persist'i
`corporate_source_state` (source='ms').

**Kaynak bulgu (gelecekte koru):** morganstanley.com siteleri WAF/404
(eski memory "Morgan Stanley S3-headless" varsayımı) AMA podcast
KANONİK Art19 RSS'i tamamen açık (iTunes lookup id=1466686717 →
feedUrl=rss.art19.com/thoughts-on-the-market). Yeşilada deseni
(gated site → açık podcast RSS) MS'e uygulandı — HEADLESS GEREKMEZ.
Show-notes blurb DEĞİL: `content:encoded`/`summary` TAM TRANSKRİPT
taşıyor (~5k char, MS ekonomist/stratejist günlük analizi). Bu yüzden
prose kaynağı (overshoot/blackrock/jpm tier'ı), title-radar değil.

Çıktıda İSİM/ATIF YOK: yalnız AXIOM görüşünü besleyen arka-plan
sinyali (data-first atıfsız model; telif güvenliği L_DISPLACE 12-gram
+ özgünlük; MS BlackRock/JPM ile aynı muamele). Yalnız public podcast
RSS; site scrape EDİLMEZ.

Art19 feed dev (1627+ bölüm, ~18MB) → `_MAX_ENTRIES` cap: her poll'da
binlerce upsert anlamsız, haftalık pencere zaten store-tarafı uygulanır
(feed newest-first). Fail policy: sessiz skip + log.
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

logger = get_logger("corporate.ms_totm_rss")

FEED_URL = "https://rss.art19.com/thoughts-on-the-market"

SOURCE_KEY = "ms"

# Günlük (haftaiçi) podcast → 40 bölüm >1 ay; haftalık pencereye fazlasıyla
# yeter. Feed newest-first → [:N] en güncelleri alır.
_MAX_ENTRIES = 40

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SMART_MAP = {
    " ": " ",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "…": "...",
}
# Show-notes yapısı: "<blurb> Read more insights from Morgan Stanley.
# ----- Transcript ----- <TAM TRANSKRİPT>". Transkript işareti varsa
# blurb+CTA boilerplate'i atıp yalnız transkripti al (daha temiz sinyal).
_TRANSCRIPT_SPLIT_RE = re.compile(r"-{2,}\s*Transcript\s*-{2,}", re.IGNORECASE)
# Kapanış boilerplate'i ("Thanks for listening... leave us a review...
# share Thoughts on the Market...") — analiz değil, sinyalden çıkar.
_OUTRO_RE = re.compile(
    r"\bThanks for listening\b.*$", re.IGNORECASE | re.DOTALL
)


@dataclass
class MsPost:
    title: str
    link: str
    published: datetime          # tz-aware UTC
    body_text: str
    truncated: bool
    author: str = ""


def _strip_html(raw: str) -> str:
    """HTML → düz metin. tag strip → entity decode → akıllı tırnak →
    whitespace collapse. BS4 KULLANILMAZ (mahfi/overshoot pattern)."""
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


def parse_feed(xml_bytes: bytes) -> list[MsPost]:
    """Saf parser — feed byte'ları → MsPost listesi. Network YOK.

    Gövde önceliği: content[0].value (full transkript); yoksa summary;
    ikisi de yoksa skip. "----- Transcript -----" işareti varsa yalnız
    transkript kısmı (blurb+CTA boilerplate atılır). En güncel
    `_MAX_ENTRIES` ile sınırlı (feed newest-first).
    """
    if not xml_bytes:
        return []
    feed = feedparser.parse(xml_bytes)
    posts: list[MsPost] = []
    for entry in (feed.entries or [])[:_MAX_ENTRIES]:
        title = _strip_html(entry.get("title") or "").strip()
        # Art19 podcast item'larında <link> yok (feedparser 'None' string
        # döndürür) — kararlı kimlik guid/id'de (gid://art19-episode-...).
        # Atıfsız çıktıda URL gösterilmez; bu yalnız idempotency anahtarı.
        link = (entry.get("link") or "").strip()
        if not link or link.lower() == "none":
            link = (entry.get("id") or entry.get("guid") or "").strip()
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
        # Transkript işaretinden sonrasını al (varsa) — blurb+"Read more"+
        # "leave us a review" boilerplate'ini sinyalden çıkarır.
        parts = _TRANSCRIPT_SPLIT_RE.split(body_text, maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            body_text = parts[1].strip()
        stripped = _OUTRO_RE.sub("", body_text).strip()
        if stripped:  # outro tüm gövde değilse uygula (güvenli)
            body_text = stripped
        if not body_text:
            continue

        posts.append(
            MsPost(
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
        "Accept": "application/rss+xml, application/atom+xml, "
                  "application/xml;q=0.9, */*;q=0.8",
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
        logger.warning(f"ms_totm_rss fetch failed: {e}")
        return None, {"status": 0, "error": str(e)}

    if resp.status_code == 304:
        return None, {"status": 304}
    if resp.status_code >= 400:
        logger.warning(f"ms_totm_rss HTTP {resp.status_code}")
        return None, {"status": resp.status_code,
                       "error": f"HTTP {resp.status_code}"}
    return resp.content, {
        "status": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def posts_in_window(
    posts: list[MsPost], start: datetime, end: datetime
) -> list[MsPost]:
    """[start, end) yarı-açık penceredeki bölümler, yayın tarihi DESC."""
    in_window = [p for p in posts if start <= p.published < end]
    return sorted(in_window, key=lambda p: p.published, reverse=True)


def week_event_id(monday_utc: datetime) -> str:
    """Haftalık idempotency anahtarı — sha1('ms-week|<iso>')[:16].
    Kaynak-özel prefix → event_id çakışmaz."""
    seed = f"ms-week|{monday_utc.date().isoformat()}"
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
