"""CoinGlass ETF flow scraper + scheduler — runs INSIDE the backend container.

Replaces the previous .github/workflows/coinglass-etf-cron.yml which used
GitHub Actions scheduled cron — that turned out unreliable (the May 6 06:00
UTC run was deferred 2.5h, leaving the dashboard 9h stale). Running here
gives us:
  - 24/7 process lifecycle tied to the backend
  - Cron-style daily fire at a fixed UTC hour (default 04:00 UTC = TR 07:00,
    ~7h after NYSE close so the prior trading day's flow is finalized)
  - Direct DB writes via save_etf_flow (no HTTP roundtrip back to ourselves)
  - One source of truth for secrets (no GH secret duplication)

Requires Chromium installed in the container — added to Dockerfile via
`playwright install --with-deps chromium`. If the install is missing the
supervisor logs the failure once and stays quiescent (does not crash startup).

2026-05-14: switched from 6h fixed interval to cron-style fixed UTC hour.
The 6h schedule drifted with Railway restarts (boot at 22:26 → scrapes at
22:26/04:26/10:26/16:26; next deploy at 13:28 → scrapes at 13:28/19:28/...).
Users expected "each morning at the same time" — fixed UTC hour delivers that.
"""
from __future__ import annotations

import asyncio
import os
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

# Daily fire at this UTC hour (default 04:00 UTC = TR 07:00). NYSE closes at
# 21:00 UTC; CoinGlass typically finalizes prior-day flow within 4-6h after
# close, so 04:00 UTC is comfortably past the settle window.
TARGET_HOUR_UTC = int(os.getenv("COINGLASS_SCRAPE_HOUR_UTC", "4"))
TARGET_MINUTE_UTC = int(os.getenv("COINGLASS_SCRAPE_MINUTE_UTC", "0"))
RETRY_INTERVAL = timedelta(minutes=30)
# Startup catch-up: if cache is older than this, scrape immediately on boot
# instead of waiting until the next TARGET_HOUR_UTC tick. 18h means a daily
# scrape that missed once still gets corrected the same day.
STARTUP_REFRESH_THRESHOLD = timedelta(hours=18)


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

# CoinGlass renders 4 tables on /etf/* pages. The flow table has these
# headers; we lock onto it via signature instead of guessing tables[0|1|3].
_FLOW_TABLE_REQUIRED_HEADERS = {"Time(UTC)", "Total"}


async def _find_total_col_index(page) -> Optional[int]:
    """Return the Total column index from whichever table holds the flow header.

    CoinGlass renders the flow table as TWO sibling <table> elements: one
    holds <thead> (data cells empty) and another holds <tbody> (header empty).
    So we can't trust one table to give us both columns + rows. This function
    only locates the *header* table to learn the column layout."""
    tables = await page.locator("table").all()
    for t in tables:
        try:
            header_cells = await t.locator("thead th").all_inner_texts()
        except Exception:
            continue
        headers = [h.strip() for h in header_cells]
        if not _FLOW_TABLE_REQUIRED_HEADERS.issubset(set(headers)):
            continue
        total_idx = next(
            (i for i in range(len(headers) - 1, -1, -1) if headers[i] == "Total"),
            None,
        )
        if total_idx is None:
            continue
        return total_idx
    return None


async def _read_first_complete_row(page) -> Optional[dict]:
    """Return the first flow row with a date != today and a non-zero total.

    Strategy:
      1. Learn the Total column index from the flow header table.
      2. Scan *all* tbody rows on the page and pick the first one whose
         first cell is a YYYY-MM-DD date and which has at least
         total_idx+1 cells. This handles CoinGlass's split-table render
         where header and body live in separate <table> elements.
      3. Skip today's UTC row and zero/placeholder rows.
    """
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_idx = await _find_total_col_index(page)
    if total_idx is None:
        logger.warning("flow header table (Time(UTC)+Total) not found")
        return None
    rows = await page.locator("table tbody tr").all()
    for row in rows[:30]:
        cells = await row.locator("td").all()
        if len(cells) <= total_idx:
            continue
        date_text = (await cells[0].inner_text()).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            continue
        if date_text == today_iso:
            continue
        total_text = (await cells[total_idx].inner_text()).strip()
        val = _parse_compact(total_text)
        if val is None or val == 0.0:
            continue
        return {"date": date_text, "total": val}
    return None


async def _scrape_one(page, symbol: str) -> Optional[dict]:
    """Returns {date, coin_total, usd_total} or None on failure.

    Waits for networkidle so the JS-driven table values settle before
    reading; without this, CoinGlass occasionally exposes a stale snapshot
    that gets revised within a few seconds (root-cause of the ±sign-flip
    we saw on 2026-05-07 where the 00:33 UTC scrape captured -610.91 BTC
    while the same row settled at +571.36 BTC by 06:50 UTC)."""
    url = PAGES[symbol]
    logger.info(f"[{symbol}] navigate → {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_selector("text=/^20\\d{2}-\\d{2}-\\d{2}$/", timeout=30_000)
        # Let CoinGlass XHR-settle so the values aren't a half-rendered snapshot.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await page.wait_for_timeout(2_000)
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
        page.get_by_role("tab", name=re.compile(r"^\s*USD\s*$", re.I)).first,
        page.get_by_role("button", name=re.compile(r"^\s*USD\s*$", re.I)).first,
        page.locator("button:has-text('USD')").first,
    ]
    for loc in toggles:
        try:
            await loc.click(timeout=3_000)
            await page.wait_for_timeout(2_000)
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

    # Sanity guard: USD and coin sign must match (catches the case where a
    # toggle click landed on a different table's row).
    if (usd_total > 0) != (coin_row["total"] > 0):
        logger.warning(
            f"[{symbol}] sign mismatch usd={usd_total:+,.0f} vs coin={coin_row['total']:+,.2f}"
            " — falling back to spot derivation"
        )
        spot = await _fetch_spot_price(symbol)
        usd_total = coin_row["total"] * spot

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


def _seconds_until_next_target(now: datetime) -> tuple[float, datetime]:
    """How many seconds until the next TARGET_HOUR_UTC:TARGET_MINUTE_UTC tick.

    Returns (seconds, target_datetime). If today's tick is already past,
    schedule for tomorrow.
    """
    target = now.replace(
        hour=TARGET_HOUR_UTC,
        minute=TARGET_MINUTE_UTC,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds(), target


async def coinglass_scraper_supervisor():
    """Cron-style supervisor — fires once per UTC day at TARGET_HOUR_UTC.

    Runs in main.py lifespan as a background task. Startup catch-up:
    if cache is older than STARTUP_REFRESH_THRESHOLD, scrape immediately
    instead of waiting for the next tick (covers the case where a deploy
    missed the daily tick window). Failures retry once after RETRY_INTERVAL
    but never crash the supervisor (caught and logged).
    """
    logger.info(
        f"CoinGlass scheduler started — daily fire at "
        f"{TARGET_HOUR_UTC:02d}:{TARGET_MINUTE_UTC:02d} UTC "
        f"(TR {(TARGET_HOUR_UTC + 3) % 24:02d}:{TARGET_MINUTE_UTC:02d})"
    )

    try:
        btc_old = await _needs_refresh("BTC")
        eth_old = await _needs_refresh("ETH")
        if btc_old or eth_old:
            logger.info(
                f"Startup catch-up: stale cache (btc={btc_old} eth={eth_old}) "
                f"— scraping now"
            )
            await scrape_both_symbols()
        else:
            logger.info("Startup: cache fresh, waiting for next daily tick")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"CoinGlass startup scrape error: {e}")

    while True:
        try:
            now = datetime.now(timezone.utc)
            wait_seconds, next_target = _seconds_until_next_target(now)
            logger.info(
                f"Next CoinGlass scrape scheduled for {next_target.isoformat()} "
                f"({wait_seconds/3600:.1f}h from now)"
            )
            await asyncio.sleep(wait_seconds)
            logger.info("CoinGlass daily scheduled scrape triggered")
            results = await scrape_both_symbols()
            if not (results["btc"] and results["eth"]):
                logger.warning(
                    f"Partial scrape ({results}); retry in {RETRY_INTERVAL}"
                )
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
