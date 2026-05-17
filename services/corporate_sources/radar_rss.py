"""Tema-Radar RSS adaptörü — Kurumsal Sentez Faz S4.

`isyatirim_rss.py` pattern forkudur ama YALNIZ BAŞLIK-düzeyi tema sinyali
döndürür: `body_text` ASLA doldurulmaz (sabit ""), `truncated=True` sabit.

**Telif gerekçesi (gelecekte koru):** Investing.com ToS gövde yeniden-
yayınını yasaklıyor (RSS'te zaten gövde yok); Project Syndicate paywall;
Mesele Ekonomi audio-only (show-notes jenerik). Bu yüzden tema-radar
kaynakları prose DEĞİL — yalnız gündem teması başlığı; `_build_live_block`
"TEMA RADARI" bloğunda AXIOM'un düşüncesini besler, ASLA aktarılmaz
(prompt Kural 2/3 dönüştürme + atıfsızlık zaten kapsar).

Kaynaklar (hepsi kind='radar', source='radar_<key>'):
- mesele     : Mesele Ekonomi podcast (Yeşilada çevresi TR makro), tüm entry
- investing  : Investing.com "Economy News" (news_14), yüksek-hacim
- ps         : Project Syndicate site-geneli + makro-kw başlık filtresi
               (kullanıcı kararı: "El-Erian değil, PS makro geneli")

ETag/Last-Modified persist'i `corporate_source_state` (source='radar_<key>').
Sentez ÜRETMEZ (week_event_id yok) — yalnız sinyal beslemesi. Fail policy:
sessiz skip + log. NOT: yüksek-hacim feed'ler artımlı poll + accumulation
store'a güvenir (corporate_posts idempotent UPSERT, kind='radar').
"""
from __future__ import annotations

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

logger = get_logger("corporate.radar_rss")


@dataclass(frozen=True)
class _Feed:
    url: str
    macro_filter: bool = False  # True → başlık makro-kw içermeli (PS firehose)


# source-key → feed konfigürasyonu. source-key DB'de 'radar_<key>' olur.
RADAR_FEEDS: dict[str, _Feed] = {
    "mesele": _Feed("https://media.rss.com/mesele-ekonomi/feed.xml"),
    "investing": _Feed("https://www.investing.com/rss/news_14.rss"),
    "ps": _Feed("https://www.project-syndicate.org/rss", macro_filter=True),
}

# Mesele 1056-entry'lik dev feed → her poll'da 1056 upsert anlamsız.
# Pencere zaten live_block'ta uygulanıyor; en güncel N başlık fazlasıyla
# >1 hafta kapsar (feed'ler newest-first).
_MAX_ENTRIES = 60

# PS site-geneli RSS makro/ekonomi-dışı (felsefe/kültür/iç-siyaset) da
# taşır → başlık makro-kw eşleşmesi şart ("makro geneli" yorumu).
_MACRO_KW = re.compile(
    r"econom|inflation|deflation|recession|growth|gdp|fiscal|monetary|"
    r"central bank|\bfed\b|ecb|\bboj\b|\bpboc\b|interest rate|\brates?\b|"
    r"\byield|bond|currenc|\bfx\b|dollar|\beuro\b|\byen\b|trade war|"
    r"tariff|supply chain|\boil\b|energy|commodit|geopolit|sanction|"
    r"debt|deficit|stimulus|labor market|unemploy|capital|market|"
    r"financ|bank|crisis|stagflat|hormuz|opec|emerging market",
    re.IGNORECASE,
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SMART_MAP = {
    " ": " ",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "…": "...",
}


@dataclass
class RadarPost:
    title: str
    link: str
    published: datetime          # tz-aware UTC
    body_text: str = ""          # SABIT "" — tema-radar gövde TAŞIMAZ (telif)
    truncated: bool = True       # SABIT True — yalnız başlık
    author: str = ""             # normalize_post duck-type uyumu


def _clean(raw: str) -> str:
    """HTML → düz metin (tag strip → entity decode → akıllı tırnak →
    whitespace collapse). BS4 KULLANILMAZ (proje pattern'i)."""
    if not raw:
        return ""
    txt = _TAG_RE.sub(" ", raw)
    txt = html.unescape(txt)
    for bad, good in _SMART_MAP.items():
        txt = txt.replace(bad, good)
    return _WS_RE.sub(" ", txt).strip()


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


def parse_feed(xml_bytes: bytes, source: str) -> list[RadarPost]:
    """Saf parser — feed byte'ları → RadarPost listesi (Network YOK).

    Yalnız title/link/published; gövde ASLA. PS gibi macro_filter=True
    kaynakta başlık makro-kw eşleşmeli (firehose'u eler). En güncel
    `_MAX_ENTRIES` ile sınırlı (feed'ler newest-first)."""
    if not xml_bytes:
        return []
    cfg = RADAR_FEEDS.get(source)
    if cfg is None:
        return []
    feed = feedparser.parse(xml_bytes)
    posts: list[RadarPost] = []
    for entry in (feed.entries or [])[:_MAX_ENTRIES]:
        title = _clean(entry.get("title") or "")
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        if cfg.macro_filter and not _MACRO_KW.search(title):
            continue
        published = _parse_published(entry)
        if not published:
            continue
        posts.append(RadarPost(title=title, link=link, published=published))
    return posts


async def fetch_feed(
    source: str,
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
) -> tuple[Optional[bytes], dict]:
    """Tek feed'i çek. ETag / If-Modified-Since aware.

    Dönüş:
      - 304          → (None, {'status': 304})
      - 200          → (body, {'status': 200, 'etag': .., 'last_modified': ..})
      - hata / >=400 → (None, {'status': <code|0>, 'error': <msg>})
    """
    cfg = RADAR_FEEDS.get(source)
    if cfg is None:
        return None, {"status": 0, "error": f"unknown radar source {source}"}
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
            resp = await client.get(cfg.url, headers=headers)
    except httpx.HTTPError as e:
        logger.warning(f"radar_rss fetch failed ({source}): {e}")
        return None, {"status": 0, "error": str(e)}

    if resp.status_code == 304:
        return None, {"status": 304}
    if resp.status_code >= 400:
        logger.warning(f"radar_rss HTTP {resp.status_code} ({source})")
        return None, {"status": resp.status_code,
                       "error": f"HTTP {resp.status_code}"}
    return resp.content, {
        "status": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def posts_in_window(
    posts: list[RadarPost], start: datetime, end: datetime
) -> list[RadarPost]:
    """[start, end) yarı-açık penceredeki başlıklar, yayın tarihi DESC."""
    return sorted(
        (p for p in posts if start <= p.published < end),
        key=lambda p: p.published,
        reverse=True,
    )


def _state_key(source: str) -> str:
    return f"radar_{source}"


async def read_source_state(
    source: str,
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
                    {"s": _state_key(source)},
                )
            ).first()
        if row:
            return row[0], row[1]
    except Exception as e:  # noqa: BLE001
        logger.info(f"radar read_source_state skip ({source}): {e}")
    return None, None


async def write_source_state(
    etag: Optional[str],
    last_modified: Optional[str],
    source: str,
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
                {"s": _state_key(source), "e": etag, "lm": last_modified},
            )
    except Exception as e:  # noqa: BLE001
        logger.info(f"radar write_source_state skip ({source}): {e}")
