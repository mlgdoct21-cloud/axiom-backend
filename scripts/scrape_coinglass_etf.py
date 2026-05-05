"""
Scrape CoinGlass spot ETF flow pages and push to backend admin cache endpoint.

Designed to run in GitHub Actions (or any environment with Playwright + chromium).
Renders the JS-driven CoinGlass tables, extracts the last *complete* daily row
(skipping today if it's still '-'), and POSTs the values to
/api/v1/admin/etf/cache for both BTC and ETH.

Required env vars:
  AXIOM_BACKEND_URL       e.g. https://vivacious-growth-production-4875.up.railway.app
  BOT_INTERNAL_SECRET     auth header for the admin endpoint

Exit codes:
  0  both BTC + ETH pushed
  1  scrape failed for at least one symbol
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Optional

import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

PAGES = {
    "BTC": "https://www.coinglass.com/etf/bitcoin",
    "ETH": "https://www.coinglass.com/etf/ethereum",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

SPOT_PRICE_FALLBACK = {"BTC": 80_000.0, "ETH": 2_300.0}


def _parse_compact(text: str) -> Optional[float]:
    """Parse CoinGlass cell text into a float. Handles +6.78K, -$24.21M, etc."""
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
        mult = 1e3
        t = t[:-1]
    elif t.endswith("M"):
        mult = 1e6
        t = t[:-1]
    elif t.endswith("B"):
        mult = 1e9
        t = t[:-1]
    try:
        return sign * float(t) * mult
    except ValueError:
        return None


def _scrape_symbol(page, symbol: str) -> Optional[dict]:
    """Returns {date, coin_total, usd_total} for the last complete daily row."""
    url = PAGES[symbol]
    print(f"[{symbol}] navigate → {url}", flush=True)
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    # Wait for the table that shows YYYY-MM-DD rows.
    try:
        page.wait_for_selector("text=/^20\\d{2}-\\d{2}-\\d{2}$/", timeout=30_000)
    except PWTimeoutError:
        print(f"[{symbol}] table never rendered", flush=True)
        return None

    # First read the table in the default unit shown (BTC for /bitcoin, ETH for /ethereum).
    coin_row = _read_first_complete_row(page)
    if coin_row is None:
        print(f"[{symbol}] no complete coin row found", flush=True)
        return None

    # Switch the unit toggle to USD and re-read.
    try:
        usd_btn = page.get_by_role("button", name=re.compile(r"^USD$"))
        usd_btn.first.click(timeout=5_000)
        page.wait_for_timeout(1_500)
    except Exception as e:
        print(f"[{symbol}] could not click USD toggle: {e}", flush=True)
        return None

    usd_row = _read_first_complete_row(page)
    if usd_row is None or usd_row["date"] != coin_row["date"]:
        print(f"[{symbol}] usd row missing or date mismatch ({coin_row['date']} vs "
              f"{usd_row['date'] if usd_row else None})", flush=True)
        return None

    return {
        "date": coin_row["date"],
        "coin_total": coin_row["total"],
        "usd_total": usd_row["total"],
    }


def _read_first_complete_row(page) -> Optional[dict]:
    """Find the first row whose 'Total' cell isn't '-'. Returns {date, total}."""
    # CoinGlass renders rows as <tr> with first cell = date, last cell = total.
    rows = page.locator("table tbody tr").all()
    for row in rows[:10]:
        cells = row.locator("td").all()
        if len(cells) < 2:
            continue
        date_text = cells[0].inner_text().strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_text):
            continue
        total_text = cells[-1].inner_text().strip()
        val = _parse_compact(total_text)
        if val is None:
            continue
        return {"date": date_text, "total": val}
    return None


def _push(symbol: str, payload: dict, backend_url: str, secret: str) -> bool:
    url = backend_url.rstrip("/") + "/api/v1/admin/etf/cache"
    body = {
        "symbol": symbol,
        "net_flow_usd": payload["usd_total"],
        "net_flow_coins": payload["coin_total"],
        "source": "coinglass_playwright",
        "spot_price": SPOT_PRICE_FALLBACK[symbol],
    }
    print(f"[{symbol}] POST {url} body={body}", flush=True)
    try:
        r = httpx.post(url, json=body, headers={"x-internal-secret": secret}, timeout=30.0)
        print(f"[{symbol}] HTTP {r.status_code} {r.text[:200]}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[{symbol}] POST failed: {e}", flush=True)
        return False


def main() -> int:
    backend_url = os.environ.get("AXIOM_BACKEND_URL", "").strip()
    secret = os.environ.get("BOT_INTERNAL_SECRET", "").strip()
    if not backend_url or not secret:
        print("AXIOM_BACKEND_URL and BOT_INTERNAL_SECRET are required", file=sys.stderr)
        return 1

    results: dict[str, bool] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        for sym in ("BTC", "ETH"):
            scraped = _scrape_symbol(page, sym)
            if scraped is None:
                results[sym] = False
                continue
            print(f"[{sym}] scraped {scraped['date']} coin={scraped['coin_total']:+,.2f} "
                  f"usd={scraped['usd_total']:+,.2f}", flush=True)
            results[sym] = _push(sym, scraped, backend_url, secret)
            # Brief pause before next nav.
            time.sleep(2)

        browser.close()

    ok_count = sum(1 for v in results.values() if v)
    print(f"\n=== Result: {ok_count}/2 symbols pushed ({results}) ===", flush=True)
    return 0 if ok_count == 2 else 1


if __name__ == "__main__":
    sys.exit(main())
