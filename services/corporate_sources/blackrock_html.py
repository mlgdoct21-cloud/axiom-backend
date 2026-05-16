"""BlackRock Investment Institute — Weekly Commentary HTML adaptörü —
arka-plan sinyali.

RSS DEĞİL: BlackRock weekly-commentary sayfası server-rendered HTML
döndürüyor (datacenter-IP'den 200, WAF bloğu yok — 2026-05-16 probe).
İçerik `<p>` paragraflarında; yayın tarihi `<meta name="publicationDate">`;
başlık ilk `<h2>`. BS4 KULLANILMAZ — regex strip (fed_statement/mahfi
pattern). Tek sayfa, haftalık döner (sabit URL → tek satır UPSERT;
published güncellenir, read_window pencereye göre alır).

Çıktıda İSİM/ATIF YOK: yalnız AXIOM görüşünü besleyen "arka-plan
sinyali" (data-first atıfsız model; telif L_DISPLACE+özgünlük backstop).
Yalnız public sayfa; paywall/login YOK. Fail policy: sessiz skip + log.

ETag/If-Modified-Since persist'i `corporate_source_state`
(source='blackrock'). Scheduler `_poll_rss` kontratı: read_source_state
/ fetch_feed / parse_feed(list) / write_source_state.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("corporate.blackrock_html")

FEED_URL = (
    "https://www.blackrock.com/us/individual/insights/"
    "blackrock-investment-institute/weekly-commentary"
)

SOURCE_KEY = "blackrock"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPTISH_RE = re.compile(
    r"<(script|style|nav|header|footer|svg)[^>]*>.*?</\1>", re.S | re.I
)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
_PUBDATE_RE = re.compile(
    r'name=["\']publicationDate["\']\s+content=["\']([^"\']+)["\']', re.I
)
_DATEDIV_RE = re.compile(
    r'class\s*=\s*["\']date-format["\']\s*>\s*([A-Za-z]+ \d{1,2},\s*\d{4})', re.I
)
_SMART_MAP = {
    " ": " ",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "…": "...",
}


@dataclass
class BlackRockPost:
    title: str
    link: str
    published: datetime          # tz-aware UTC
    body_text: str
    truncated: bool
    author: str = ""


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text_only = _TAG_RE.sub(" ", raw)
    text_only = html.unescape(text_only)
    for bad, good in _SMART_MAP.items():
        text_only = text_only.replace(bad, good)
    return _WS_RE.sub(" ", text_only).strip()


def _parse_pubdate(doc: str) -> Optional[datetime]:
    m = _PUBDATE_RE.search(doc) or _DATEDIV_RE.search(doc)
    if not m:
        return None
    raw = m.group(1).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            # Date-only kaynak → gün-ortası (12:00 UTC). 00:00 kullanılırsa
            # Pzt-yayın haftalık pencere başına (Pzt 05:30 UTC = 08:30 TR)
            # takılıp bir önceki haftaya düşüyor (boundary bug).
            return datetime.strptime(raw, fmt).replace(
                hour=12, tzinfo=timezone.utc
            )
        except ValueError:
            continue
    return None


def parse_feed(html_bytes: bytes) -> list[BlackRockPost]:
    """Saf parser — sayfa byte'ları → [BlackRockPost] (0 veya 1). Network YOK.

    Tarih `<meta publicationDate>` (yoksa skip — pencere bütünlüğü);
    başlık ilk anlamlı `<h2>`; gövde nav/script/style çıkarılmış uzun
    `<p>`'ler. Boş gövde/tarih → [].
    """
    if not html_bytes:
        return []
    try:
        doc = html_bytes.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []

    published = _parse_pubdate(doc)
    if not published:
        logger.info("blackrock_html: publicationDate bulunamadı — skip")
        return []

    h2s = [
        _strip_html(x) for x in _H2_RE.findall(doc)
    ]
    h2s = [t for t in h2s if t and len(t) > 4]
    head = h2s[0] if h2s else "Weekly market commentary"
    title = f"Weekly Commentary — {head}"[:200]

    cleaned = _SCRIPTISH_RE.sub(" ", doc)
    paras = []
    for p in _P_RE.findall(cleaned):
        t = _strip_html(p)
        if len(t) >= 110:
            paras.append(t)
    body_text = "\n".join(paras).strip()
    if not body_text:
        logger.info("blackrock_html: gövde paragrafı bulunamadı — skip")
        return []

    return [
        BlackRockPost(
            title=title,
            link=FEED_URL,
            published=published,
            body_text=body_text,
            truncated=False,
            author="BlackRock Investment Institute",
        )
    ]


async def fetch_feed(
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
) -> tuple[Optional[bytes], dict]:
    """Sayfayı çek. ETag / If-Modified-Since aware.

    Dönüş:
      - 304          → (None, {'status': 304})
      - 200          → (body, {'status': 200, 'etag': .., 'last_modified': ..})
      - hata / >=400 → (None, {'status': <code|0>, 'error': <msg>})
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
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
        logger.warning(f"blackrock_html fetch failed: {e}")
        return None, {"status": 0, "error": str(e)}

    if resp.status_code == 304:
        return None, {"status": 304}
    if resp.status_code >= 400:
        logger.warning(f"blackrock_html HTTP {resp.status_code}")
        return None, {"status": resp.status_code, "error": f"HTTP {resp.status_code}"}
    return resp.content, {
        "status": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def posts_in_window(
    posts: list[BlackRockPost], start: datetime, end: datetime
) -> list[BlackRockPost]:
    """[start, end) yarı-açık penceredeki yazılar, yayın tarihi DESC."""
    in_window = [p for p in posts if start <= p.published < end]
    return sorted(in_window, key=lambda p: p.published, reverse=True)


def week_event_id(monday_utc: datetime) -> str:
    """sha1('blackrock-week|<iso>')[:16] — kaynak-özel prefix."""
    seed = f"blackrock-week|{monday_utc.date().isoformat()}"
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
