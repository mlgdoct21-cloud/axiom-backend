"""J.P. Morgan — "Eye on the Market" (Michael Cembalest) HTML adaptörü —
arka-plan sinyali.

RSS/PDF DEĞİL: privatebank.jpmorgan.com EotM index'i datacenter-IP'den
200 (WAF yok — 2026-05-16 probe); makaleler server-rendered HTML
(am.jpmorgan.com 404/403, kullanılmaz). İki-hop: index → en güncel
slug → makale. BS4 KULLANILMAZ — regex (fed_statement/mahfi pattern).
Tarih byline'da ("Economy & Markets <Ay G, YYYY>"); date-only →
gün-ortası 12:00 UTC (haftalık pencere boundary, BlackRock ile aynı).

Çıktıda İSİM/ATIF YOK: yalnız AXIOM görüşünü besleyen "arka-plan
sinyali" (data-first atıfsız; telif L_DISPLACE+özgünlük backstop).
Yalnız public sayfa; PDF indirilmez (HTML gövde yeterli). Fail policy:
sessiz skip + log. ETag/IMS persist'i corporate_source_state
(source='jpm') — INDEX üstünde izlenir (yeni EotM → index değişir).
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

logger = get_logger("corporate.jpm_eotm_html")

BASE = "https://privatebank.jpmorgan.com"
INDEX_URL = (
    BASE + "/nam/en/insights/markets-and-investing/eye-on-the-market"
)

SOURCE_KEY = "jpm"

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
_SLUG_RE = re.compile(
    r"/nam/en/insights/latest-and-featured/eotm/[a-z0-9-]+", re.I
)
_OGTITLE_RE = re.compile(
    r'og:title["\']\s+content=["\']([^"\']{1,140})["\']', re.I
)
_TITLE_RE = re.compile(r"<title>([^<]{1,140})</title>", re.I)
_DATE_RE = re.compile(
    r"Economy\s*&(?:amp;)?\s*Markets\s+([A-Z][a-z]+ \d{1,2},?\s*\d{4})", re.I
)
_DATE_FALLBACK_RE = re.compile(r"\b([A-Z][a-z]+ \d{1,2},?\s*\d{4})\b")
_SMART_MAP = {
    " ": " ",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "…": "...",
}


@dataclass
class JpmPost:
    title: str
    link: str
    published: datetime          # tz-aware UTC
    body_text: str
    truncated: bool
    author: str = ""


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    t = _TAG_RE.sub(" ", raw)
    t = html.unescape(t)
    for bad, good in _SMART_MAP.items():
        t = t.replace(bad, good)
    return _WS_RE.sub(" ", t).strip()


def _parse_date(doc: str) -> Optional[datetime]:
    m = _DATE_RE.search(doc) or _DATE_FALLBACK_RE.search(doc)
    if not m:
        return None
    raw = m.group(1).replace(",", "").strip()
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            # date-only → 12:00 UTC (haftalık pencere boundary fix)
            return datetime.strptime(raw, fmt).replace(
                hour=12, tzinfo=timezone.utc
            )
        except ValueError:
            continue
    return None


def parse_feed(article_bytes: bytes) -> list[JpmPost]:
    """Saf parser — makale byte'ları → [JpmPost] (0/1). Network YOK.

    fetch_feed zaten index→en güncel makaleyi çözüp makale HTML'ini
    verir; burada yalnız o makale parse edilir.
    """
    if not article_bytes:
        return []
    try:
        doc = article_bytes.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []

    link = getattr(parse_feed, "_link", "") or INDEX_URL

    published = _parse_date(doc)
    if not published:
        logger.info("jpm_eotm: tarih bulunamadı — skip")
        return []

    mt = _OGTITLE_RE.search(doc) or _TITLE_RE.search(doc)
    title = _strip_html(mt.group(1)) if mt else "Eye on the Market"
    title = re.split(r"\s*\|\s*", title)[0].strip()[:200] or "Eye on the Market"

    cleaned = _SCRIPTISH_RE.sub(" ", doc)
    paras = []
    for p in _P_RE.findall(cleaned):
        t = _strip_html(p)
        if len(t) < 110:
            continue
        if "Download the pdf" in t or "Chairman of Market" in t:
            # baş-chrome paragrafı: tarih/başlık/indir linki — atla
            t = re.sub(r"^.*?Download the pdf\s*", "", t).strip()
            if len(t) < 110:
                continue
        paras.append(t)
    body_text = "\n".join(paras).strip()
    if not body_text:
        logger.info("jpm_eotm: gövde bulunamadı — skip")
        return []

    return [
        JpmPost(
            title=title,
            link=link,
            published=published,
            body_text=body_text,
            truncated=False,
            author="J.P. Morgan / Michael Cembalest",
        )
    ]


async def fetch_feed(
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
) -> tuple[Optional[bytes], dict]:
    """İki-hop: INDEX (conditional) → en güncel EotM slug → makale (full).

    Dönüş:
      - index 304          → (None, {'status': 304})
      - başarı             → (article_bytes, {'status': 200, etag, last_modified})
      - slug/makale yoksa  → (None, {'status': <code|0>, 'error': ..})
    source_state INDEX üstünde izlenir (yeni EotM → index etag değişir).
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ch = dict(headers)
    if etag:
        ch["If-None-Match"] = etag
    if last_modified:
        ch["If-Modified-Since"] = last_modified

    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, follow_redirects=True
        ) as client:
            idx = await client.get(INDEX_URL, headers=ch)
            if idx.status_code == 304:
                return None, {"status": 304}
            if idx.status_code >= 400:
                logger.warning(f"jpm_eotm index HTTP {idx.status_code}")
                return None, {"status": idx.status_code,
                              "error": f"index HTTP {idx.status_code}"}
            idx_meta = {
                "status": 200,
                "etag": idx.headers.get("etag"),
                "last_modified": idx.headers.get("last-modified"),
            }
            m = _SLUG_RE.search(idx.text)
            if not m:
                logger.info("jpm_eotm: index'te EotM slug yok — skip")
                return None, {"status": 0, "error": "no slug"}
            art_url = BASE + m.group(0)
            art = await client.get(art_url, headers=headers)
    except httpx.HTTPError as e:
        logger.warning(f"jpm_eotm fetch failed: {e}")
        return None, {"status": 0, "error": str(e)}

    if art.status_code >= 400:
        logger.warning(f"jpm_eotm article HTTP {art.status_code} ({art_url})")
        return None, {"status": art.status_code,
                      "error": f"article HTTP {art.status_code}"}
    parse_feed._link = art_url  # parse_feed link'i bilsin (idempotency)
    return art.content, idx_meta


def posts_in_window(
    posts: list[JpmPost], start: datetime, end: datetime
) -> list[JpmPost]:
    in_window = [p for p in posts if start <= p.published < end]
    return sorted(in_window, key=lambda p: p.published, reverse=True)


def week_event_id(monday_utc: datetime) -> str:
    seed = f"jpm-week|{monday_utc.date().isoformat()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


async def read_source_state(
    source: str = SOURCE_KEY,
) -> tuple[Optional[str], Optional[str]]:
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
