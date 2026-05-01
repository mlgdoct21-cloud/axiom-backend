"""
ETF Flow Multi-Source Scraper — Production-grade fallback chain.

Veri kaynakları (öncelik sırasına göre):
1. CoinGlass (Playwright-rendered) — BTC + ETH, real settlement data
2. btcetffundflow.com (Next.js JSON) — BTC only, real-time
3. bitbo.io (HTML table scrape) — BTC only, last 14 days
4. Supabase cache (last-known-good, max 7 days old)
5. FMP approximation — emergency fallback (current behavior)

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
# 1. COINGLASS PLAYWRIGHT SCRAPER (PRIMARY) — BTC + ETH
# ════════════════════════════════════════════════════════════════════════════

COINGLASS_BTC_URL = "https://www.coinglass.com/etf/bitcoin"
COINGLASS_ETH_URL = "https://www.coinglass.com/etf/ethereum"

# Playwright import lazy (Railway image'da olmalı, lokal'de olmayabilir)
async def _playwright_scrape_coinglass(symbol: str) -> Optional[Dict[str, Any]]:
    """
    CoinGlass /etf/{bitcoin|ethereum} sayfasını render et, "Daily Total Net Inflow"
    değerini DOM'dan oku. Page client-side rendered olduğu için Playwright şart.
    """
    url = COINGLASS_BTC_URL if symbol == "BTC" else COINGLASS_ETH_URL
    coin_label = "BTC" if symbol == "BTC" else "ETH"

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed; skipping CoinGlass scrape")
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/130.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # "Daily Total Net Inflow" kart'ı render olana kadar bekle
                await page.wait_for_selector(
                    "text=Daily Total Net Inflow",
                    timeout=20000,
                )
                # DOM stabilize olsun (network idle yakla)
                await asyncio.sleep(2)

                page_text = await page.content()
            finally:
                await context.close()
                await browser.close()

        # HTML içinden veri parse et
        result = _parse_coinglass_html(page_text, coin_label)
        if result:
            logger.info(
                f"✓ CoinGlass[{symbol}]: net_flow_usd=${result['net_flow_usd']:,.0f}, "
                f"net_flow_coins={result['net_flow_coins']:,.2f}"
            )
        return result

    except Exception as e:
        logger.warning(f"CoinGlass Playwright scrape failed ({symbol}): {e}")
        return None


def _parse_coinglass_html(html: str, coin_label: str) -> Optional[Dict[str, Any]]:
    """
    Rendered HTML'den 'Daily Total Net Inflow' kartını parse et.
    Hedef pattern: "+$23.50M" (USD) + "+310.24 BTC" (coin amount).
    """
    # "Daily Total Net Inflow" başlığını yakala, sonraki rakamları bul
    # CoinGlass DOM yapısında ardışık 2 değer var: USD ve coin
    daily_section = re.search(
        r'Daily\s+Total\s+Net\s+Inflow.*?'
        r'([+-]?\$?[\d,.]+[KMB]?)\s*([+-]?[\d,.]+[KMB]?\s*' + coin_label + r')',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    if not daily_section:
        return None

    usd_str = daily_section.group(1).strip()
    coin_str = daily_section.group(2).strip()

    net_flow_usd = _parse_money_string(usd_str)
    net_flow_coins = _parse_coin_string(coin_str, coin_label)

    if net_flow_usd is None or net_flow_coins is None:
        return None

    # Total Net Assets ve Total Net Inflow ek bilgiler (opsiyonel)
    total_aum = _parse_money_string_after(html, "Total Net Assets")
    total_holdings = _parse_holdings_after(html, "Total Net Inflow", coin_label)

    return {
        "net_flow_usd": net_flow_usd,
        "net_flow_coins": net_flow_coins,
        "total_aum_usd": total_aum,
        "total_holdings_coins": total_holdings,
        "source": "coinglass_scrape",
    }


def _parse_money_string(s: str) -> Optional[float]:
    """'+$23.50M', '-$1.25B', '$101.81B' → float USD değeri"""
    s = s.strip().replace(",", "").replace("$", "")
    sign = 1
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("-"):
        sign = -1
        s = s[1:]
    multiplier = 1
    if s.endswith("K"):
        multiplier = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1_000_000
        s = s[:-1]
    elif s.endswith("B"):
        multiplier = 1_000_000_000
        s = s[:-1]
    try:
        return sign * float(s) * multiplier
    except ValueError:
        return None


def _parse_coin_string(s: str, coin_label: str) -> Optional[float]:
    """'+310.24 BTC', '-211.35 ETH' → 310.24"""
    s = s.upper().replace(coin_label, "").strip().replace(",", "")
    sign = 1
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("-"):
        sign = -1
        s = s[1:]
    multiplier = 1
    if s.endswith("K"):
        multiplier = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1_000_000
        s = s[:-1]
    try:
        return sign * float(s.strip()) * multiplier
    except ValueError:
        return None


def _parse_money_string_after(html: str, label: str) -> Optional[float]:
    """Bir label'dan sonraki ilk para değerini parse et."""
    m = re.search(
        re.escape(label) + r'.*?([+-]?\$[\d,.]+[KMB]?)',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return _parse_money_string(m.group(1))
    return None


def _parse_holdings_after(html: str, label: str, coin_label: str) -> Optional[float]:
    """Total Net Inflow yanındaki '+739.26K BTC' gibi coin holdings'i parse et."""
    m = re.search(
        re.escape(label) + r'.*?([+-]?[\d,.]+[KMB]?)\s*' + coin_label,
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return _parse_coin_string(m.group(1) + " " + coin_label, coin_label)
    return None


# ════════════════════════════════════════════════════════════════════════════
# 2. BTCETFFUNDFLOW.COM JSON (BTC FALLBACK 1)
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
    Production-grade fallback chain.
    Returns: dict or None (caller'a None gelirse Supabase cache veya FMP'a düş)

    Order:
      1. CoinGlass Playwright (BTC + ETH)
      2. btcetffundflow.com (BTC only)
      3. bitbo.io (BTC only)
    """
    # 1. CoinGlass Primary
    result = await _playwright_scrape_coinglass(symbol)
    if result:
        return result

    # 2-3. BTC için extra fallback'ler
    if symbol == "BTC":
        result = await fetch_btcetffundflow_btc(spot_price=spot_price)
        if result:
            return result

        result = await fetch_bitbo_btc(spot_price=spot_price)
        if result:
            return result

    logger.warning(f"All scrape sources failed for {symbol}")
    return None
