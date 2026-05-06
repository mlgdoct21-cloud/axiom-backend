"""CoinGlass ETF flow scraper + scheduler — runs INSIDE the backend container.

Replaces the previous .github/workflows/coinglass-etf-cron.yml which used
GitHub Actions scheduled cron — that turned out unreliable (the May 6 06:00
UTC run was deferred 2.5h, leaving the dashboard 9h stale). Running here
gives us:
  - 24/7 process lifecycle tied to the backend
  - 6h cadence (4 runs/day) so users see post-market data within the window
  - Direct DB writes via save_etf_flow (no HTTP roundtrip back to ourselves)
  - One source of truth for secrets (no GH secret duplication)

Requires Chromium installed in the container — added to Dockerfile via
`playwright install --with-deps chromium`. If the install is missing the
supervisor logs the failure once and stays quiescent (does not crash startup).
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.logger import get_logger
from services.etf_flow_cache_service import save_etf_flow, get_latest_etf_flow

logger = get_logger("coinglass_scheduler")

PAGES = {
    "BTC": "https://www.coinglass.com/etf/bitcoin",
    "ETH": "https://www.coinglass.com/etf/ethereum",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum"}
SPOT_PRICE_FALLBACK = {"BTC": 80_000.0, "ETH": 2_300.0}

SCRAPE_INTERVAL = timedelta(hours=6)
RETRY_INTERVAL = timedelta(minutes=30)
STARTUP_REFRESH_THRESHOLD = timedelta(hours=6)


# ─── Parsing helpers ───────────────────────────────────────────────────────

def _parse_compact(text: str) -> Optional[float]:
    if not text or text in {"-", "—", ""}:
        return None
    t = text.strip().replace(",", "").replace("$", "").replace(" ", "")
    sign = 1.0
    if t.startswith("+"):
        t = t[1:]
    elif t.startswith("-"):
        sign = -1.0
        t = t[1:]
    mult = 1.0
    if t.endswith("K"):
        mult, t = 1e3, t[:-1]
    elif t.endswith("M"):
        mult, t = 1e6, t[:-1]
    elif t.endswith("B"):
        mult, t = 1e9, t[:-1]
    try:
        return sign * float(t) * mult
    except ValueError:
        return None


async def _fetch_spot_price(symbol: str) -> float:
    """CoinGecko spot price, with a hardcoded fallback so usd_total derivation
    never returns None even if CoinGecko rate-limits us."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": COINGECKO_IDS[symbol], "vs_currencies": "usd"},
            )
            if r.status_code == 200:
                price = r.json().get(COINGECKO_IDS[symbol], {}).get("usd")
                if isinstance(price, (int, float)) and price > 0:
                    return float(price)
    except Exception as e:
        logger.debug(f"[{symbol}] CoinGecko spot fetch failed: {e}")
    return SPOT_PRICE_FALLBACK[symbol]


# ─── Playwright scrape ─────────────────────────────────────────────────────

async def _read_first_complete_row(page) -> Optional[dict]:
    """Return the first table row with a date != today and a non-zero total.
    CoinGlass renders today's incomplete row as "0" so we explicitly skip it."""
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = await page.locator("table tbody tr").all()
    for row in rows[:10]:
        cells = await row.locator("td").all()
        if len(cells) < 2:
            continue
        date_text = (await cells[0].inner_text()).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            continue
        if date_text == today_iso:
            continue
        total_text = (await cells[-1].inner_text()).strip()
        val = _parse_compact(total_text)
        if val is None or val == 0.0:
            continue
        return {"date": date_text, "total": val}
    return None


async def _scrape_one(page, symbol: str) -> Optional[dict]:
    """Returns {date, coin_total, usd_total} or None on failure."""
    url = PAGES[symbol]
    logger.info(f"[{symbol}] navigate → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("text=/^20\\d{2}-\\d{2}-\\d{2}$/", timeout=30_000)
    except Exception as e:
        logger.warning(f"[{symbol}] page render failed: {e}")
        return None

    coin_row = await _read_first_complete_row(page)
    if coin_row is None:
        logger.warning(f"[{symbol}] no complete coin row found")
        return None

    # Try USD toggle for accurate dollar total; fall back to coin × spot.
    usd_total: Optional[float] = None
    toggles = [
        page.get_by_role("button", name=re.compile(r"^\s*USD\s*$", re.I)).first,
        page.get_by_role("tab", name=re.compile(r"^\s*USD\s*$", re.I)).first,
        page.locator("button:has-text('USD')").first,
    ]
    for loc in toggles:
        try:
            await loc.click(timeout=3_000)
            await page.wait_for_timeout(1_500)
            usd_row = await _read_first_complete_row(page)
            if usd_row and usd_row["date"] == coin_row["date"]:
                usd_total = usd_row["total"]
                break
        except Exception:
            continue

    if usd_total is None:
        spot = await _fetch_spot_price(symbol)
        usd_total = coin_row["total"] * spot
        logger.info(
            f"[{symbol}] USD toggle unavailable — derived usd={usd_total:+,.0f} "
            f"from coin={coin_row['total']:+,.2f} × spot={spot:,.2f}"
        )

    return {
        "date": coin_row["date"],
        "coin_total": coin_row["total"],
        "usd_total": usd_total,
    }


async def scrape_both_symbols() -> dict:
    """Scrape BTC + ETH in one Playwright session. Writes to etf_flow_cache.

    Returns {'btc': bool, 'eth': bool} indicating which symbols were saved."""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.error(f"playwright import failed (Chromium not installed?): {e}")
        return {"btc": False, "eth": False}

    results = {"btc": False, "eth": False}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        try:
            for sym in ("BTC", "ETH"):
                scraped = await _scrape_one(page, sym)
                if scraped is None:
                    continue
                spot = await _fetch_spot_price(sym)
                new_id = await save_etf_flow(
                    symbol=sym,
                    net_flow_usd=scraped["usd_total"],
                    net_flow_coins=scraped["coin_total"],
                    source="coinglass_playwright",
                    spot_price=spot,
                    raw_data={
                        "scraped_date": scraped["date"],
                        "auto_scrape_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                if new_id is not None:
                    results[sym.lower()] = True
                    logger.info(
                        f"[{sym}] saved id={new_id} date={scraped['date']} "
                        f"coin={scraped['coin_total']:+,.2f} usd={scraped['usd_total']:+,.0f}"
                    )
                # Brief pause between symbols
                await asyncio.sleep(2)
        finally:
            await browser.close()

    return results


# ─── Supervisor ────────────────────────────────────────────────────────────

async def _needs_refresh(symbol: str) -> bool:
    cached = await get_latest_etf_flow(symbol)
    if not cached:
        return True
    age = timedelta(hours=cached["age_hours"])
    return age >= STARTUP_REFRESH_THRESHOLD


async def coinglass_scraper_supervisor():
    """6h cadence supervisor. Runs in main.py lifespan as a background task.

    On startup: if cache is older than STARTUP_REFRESH_THRESHOLD, scrape now;
    otherwise wait one full interval. Failures back off by RETRY_INTERVAL but
    never crash the supervisor (caught and logged)."""
    logger.info("CoinGlass scheduler supervisor started (interval=6h)")

    try:
        btc_old = await _needs_refresh("BTC")
        eth_old = await _needs_refresh("ETH")
        if btc_old or eth_old:
            logger.info(f"Startup: stale cache (btc={btc_old} eth={eth_old}) — scraping now")
            await scrape_both_symbols()
        else:
            logger.info("Startup: cache fresh, deferring first scrape")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"CoinGlass startup scrape error: {e}")

    while True:
        try:
            await asyncio.sleep(SCRAPE_INTERVAL.total_seconds())
            logger.info("CoinGlass scheduled scrape triggered")
            results = await scrape_both_symbols()
            if not (results["btc"] and results["eth"]):
                logger.warning(f"Partial scrape ({results}); retry in {RETRY_INTERVAL}")
                await asyncio.sleep(RETRY_INTERVAL.total_seconds())
                await scrape_both_symbols()
        except asyncio.CancelledError:
            logger.info("CoinGlass scheduler cancelled")
            break
        except Exception as e:
            logger.error(f"CoinGlass loop error: {e}; retry in {RETRY_INTERVAL}")
            try:
                await asyncio.sleep(RETRY_INTERVAL.total_seconds())
            except asyncio.CancelledError:
                break
