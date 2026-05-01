"""
ETF Flow Multi-Source Scraper — Production-grade fallback chain.

Veri kaynakları (öncelik sırasına göre):
1. btcetffundflow.com (Next.js JSON) — BTC only, gerçek günlük net flow
2. bitbo.io (HTML table scrape) — BTC only, last 14 days
3. Postgres cache (last-known-good, max 7 days old)
4. FMP approximation — emergency fallback (current behavior, ETH için ana kaynak)

Tüm fonksiyonlar başarısız olursa None döner, caller fallback chain'i yönetir.
"""
import os
import re
import json
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from core.logger import get_logger

logger = get_logger("etf_flow_scrapers")

# ════════════════════════════════════════════════════════════════════════════
# 1. BTCETFFUNDFLOW.COM JSON (PRIMARY for BTC)
# ════════════════════════════════════════════════════════════════════════════

BTCETFFUNDFLOW_URL = "https://btcetffundflow.com/us"


async def fetch_btcetffundflow_btc(spot_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    btcetffundflow.com'un Next.js _next/data endpoint'inden BTC ETF flow al.
    BuildID dinamik (her deploy'da değişir) → önce homepage'den scrape.
    """
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Homepage'den buildId çek
            headers = {"User-Agent": "Mozilla/5.0 (compatible; AxiomBot/1.0)"}
            async with session.get(
                BTCETFFUNDFLOW_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            m = re.search(r'"buildId":"([^"]+)"', html)
            if not m:
                logger.warning("btcetffundflow buildId not found")
                return None
            build_id = m.group(1)

            # 2. _next/data JSON endpoint'inden full data al
            data_url = f"https://btcetffundflow.com/_next/data/{build_id}/us.json"
            async with session.get(
                data_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

        return _parse_btcetffundflow_json(data, spot_price)

    except asyncio.TimeoutError:
        logger.warning("btcetffundflow timeout")
        return None
    except Exception as e:
        logger.warning(f"btcetffundflow fetch error: {e}")
        return None


def _parse_btcetffundflow_json(data: Dict, spot_price: Optional[float]) -> Optional[Dict[str, Any]]:
    """
    chart2[] = günlük net flow (USD) per provider
    chart1[] = günlük AUM per provider
    chart3[] = günlük BTC holdings per provider
    chart4[] = günlük BTC delta
    Provider "0" = TOTAL.
    Last entry = en güncel gün.
    """
    try:
        us_data = data["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["data"]
    except (KeyError, IndexError, TypeError):
        return None

    chart2 = us_data.get("chart2") or []  # daily net flow USD
    chart1 = us_data.get("chart1") or []  # AUM USD
    chart4 = us_data.get("chart4") or []  # daily delta BTC count

    if not chart2 or not chart1:
        return None

    # En güncel gün son entry'de
    latest_flow = chart2[-1]
    latest_aum = chart1[-1]
    latest_delta = chart4[-1] if chart4 else {}

    # Provider "0" = TOTAL aggregate
    net_flow_usd = float(latest_flow.get("0", "0"))
    total_aum = float(latest_aum.get("0", "0"))
    delta_btc = float(latest_delta.get("0", "0")) if latest_delta else None

    # Coin amount: delta_btc varsa kullan, yoksa USD/spot fiyatından hesapla
    if delta_btc is not None and delta_btc != 0:
        net_flow_coins = delta_btc
    elif spot_price and spot_price > 0:
        net_flow_coins = net_flow_usd / spot_price
    else:
        net_flow_coins = 0.0

    logger.info(
        f"✓ btcetffundflow[BTC]: net_flow_usd=${net_flow_usd:,.0f}, "
        f"coins={net_flow_coins:,.2f}"
    )

    return {
        "net_flow_usd": round(net_flow_usd, 2),
        "net_flow_coins": round(net_flow_coins, 4),
        "total_aum_usd": round(total_aum, 2),
        "total_holdings_coins": None,
        "source": "btcetffundflow",
    }


# ════════════════════════════════════════════════════════════════════════════
# 3. BITBO.IO HTML TABLE SCRAPE (BTC FALLBACK 2)
# ════════════════════════════════════════════════════════════════════════════

BITBO_BTC_URL = "https://bitbo.io/treasuries/etf-flows/"


async def fetch_bitbo_btc(spot_price: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    bitbo.io HTML table'ını parse et. Son satır = en güncel gün.
    Format: 'Date | IBIT | FBTC | GBTC | ... | Totals' (USD millions)
    """
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; AxiomBot/1.0)"}
            async with session.get(
                BITBO_BTC_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

        # Table extract
        tables = re.findall(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
        if not tables:
            return None

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tables[0], re.DOTALL)
        if len(rows) < 2:
            return None

        # İlk row (en güncel gün)
        first_data_row = rows[1]
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', first_data_row, re.DOTALL)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        if len(cells) < 2:
            return None

        # Son sütun = Totals (USD millions)
        totals_str = cells[-1].replace(",", "")
        totals_millions = float(totals_str)
        net_flow_usd = totals_millions * 1_000_000

        # Coin amount: spot price'den hesapla
        if spot_price and spot_price > 0:
            net_flow_coins = net_flow_usd / spot_price
        else:
            net_flow_coins = 0.0

        logger.info(
            f"✓ bitbo[BTC]: net_flow_usd=${net_flow_usd:,.0f}, "
            f"coins={net_flow_coins:,.2f}"
        )

        return {
            "net_flow_usd": round(net_flow_usd, 2),
            "net_flow_coins": round(net_flow_coins, 4),
            "total_aum_usd": None,
            "total_holdings_coins": None,
            "source": "bitbo",
        }

    except asyncio.TimeoutError:
        logger.warning("bitbo timeout")
        return None
    except Exception as e:
        logger.warning(f"bitbo fetch error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# 4. UNIFIED FALLBACK CHAIN
# ════════════════════════════════════════════════════════════════════════════

async def scrape_etf_flow_with_fallback(
    symbol: str,
    spot_price: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Production-grade HTTP-only fallback chain.
    Returns: dict or None (None gelirse caller cache veya FMP'a düşer).

    Order:
      1. bitbo.io HTML table — BTC only, doğru daily net flow (1 gün gecikme normal)
      2. btcetffundflow.com — DROP edildi: chart2[] AUM delta, net flow DEĞİL

    NOTE: ETH için free public source bulunamadı. ETH FMP approximation'da kalır.
    """
    if symbol != "BTC":
        return None

    result = await fetch_bitbo_btc(spot_price=spot_price)
    if result:
        return result

    logger.warning(f"All scrape sources failed for {symbol}")
    return None
