"""TCMB USD/TRY rate fetcher with in-memory caching.

We display the live USD→TRY rate next to USD prices in /upgrade and
free-tier broadcast nudges. TCMB publishes today.xml around 15:30 TR
time on weekdays; we cache the parsed rate for 30 minutes and fall
back to the previous business day on weekends or fetch failures.

Public API:
    rate = await get_usd_try_rate()          # float, e.g. 39.85
    text = await format_try_approx(1.99)     # "≈ 79₺"  (or "" on failure)
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger("tcmb_rate")


_CACHE_TTL_SEC = 30 * 60  # 30 dk — TCMB günde 1 kere yayımlar
_TCMB_TODAY_URL = "https://www.tcmb.gov.tr/kurlar/today.xml"
# Geçmiş gün düşmek için (hafta sonu / tatil): /YYYYMM/DDMMYYYY.xml
_TCMB_HISTORICAL_URL = "https://www.tcmb.gov.tr/kurlar/{yyyymm}/{ddmmyyyy}.xml"


@dataclass
class _CacheEntry:
    rate: float
    fetched_at: float


_cache: Optional[_CacheEntry] = None


def _parse_usd_forex_buying(xml_body: str) -> Optional[float]:
    """USD/ForexBuying değerini XML'den çıkarır. ElementTree yerine regex —
    TCMB XML'i bozuk encoding ile gelebiliyor (windows-1254), regex daha
    forgiving."""
    m = re.search(
        r'<Currency[^>]*CurrencyCode="USD"[^>]*>(.*?)</Currency>',
        xml_body,
        re.DOTALL,
    )
    if not m:
        return None
    block = m.group(1)
    fb = re.search(r"<ForexBuying>([\d.]+)</ForexBuying>", block)
    if not fb or not fb.group(1).strip():
        return None
    try:
        return float(fb.group(1))
    except ValueError:
        return None


def _fetch_url(url: str, *, timeout: int = 8) -> Optional[str]:
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return None
        # TCMB ISO-8859-9 / windows-1254 dönebiliyor; latin-1 fallback yeter.
        try:
            return r.content.decode("utf-8")
        except UnicodeDecodeError:
            return r.content.decode("latin-1", errors="replace")
    except Exception as e:
        logger.warning(f"TCMB fetch error {url}: {e}")
        return None


def _fetch_with_fallback() -> Optional[float]:
    """today.xml dene; boşsa geriye doğru 7 güne kadar git."""
    body = _fetch_url(_TCMB_TODAY_URL)
    if body:
        rate = _parse_usd_forex_buying(body)
        if rate:
            return rate

    # Hafta sonu / TCMB henüz yayımlamadı: önceki iş günlerine düş.
    import datetime
    today = datetime.datetime.utcnow().date()
    for delta in range(1, 8):
        d = today - datetime.timedelta(days=delta)
        if d.weekday() >= 5:  # Cmt, Paz
            continue
        url = _TCMB_HISTORICAL_URL.format(
            yyyymm=d.strftime("%Y%m"),
            ddmmyyyy=d.strftime("%d%m%Y"),
        )
        body = _fetch_url(url)
        if body:
            rate = _parse_usd_forex_buying(body)
            if rate:
                return rate
    return None


async def get_usd_try_rate() -> Optional[float]:
    """Returns the current USD/TRY rate (Forex Buying), None on failure.
    Cached for 30 min to keep TCMB load minimal."""
    global _cache
    now = time.time()
    if _cache and now - _cache.fetched_at < _CACHE_TTL_SEC:
        return _cache.rate
    rate = await asyncio.to_thread(_fetch_with_fallback)
    if rate:
        _cache = _CacheEntry(rate=rate, fetched_at=now)
        return rate
    # Stale cache hâlâ varsa onu döndür — TCMB geçici 5xx'de eski kurla
    # yine TL göster, hiç gösterme yerine.
    if _cache:
        logger.info("TCMB fetch failed, using stale cache")
        return _cache.rate
    return None


async def format_try_approx(usd_amount: float) -> str:
    """USD tutarı için '≈ 79₺' formatlı string döner; rate alınamazsa ''.
    /upgrade ve broadcast UI'da inline gösterim için."""
    rate = await get_usd_try_rate()
    if not rate:
        return ""
    try_amount = usd_amount * rate
    return f"≈ {try_amount:.0f}₺"
