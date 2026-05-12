"""FRED (St. Louis Fed) API — series observations fetch.

Replaces BLS as the CPI/NFP source after Railway egress was observed to be
blackholed by the BLS WAF (silent SYN drop, 10s+ ConnectTimeout). FRED hosts
on api.stlouisfed.org and is reachable from the same egress that already
talks to federalreserve.gov for fed_rss.

Free tier: 50,000 requests/day with a registered key (FRED_API_KEY env var).

Each call fetches one series; reliability_probe issues N calls per cycle.
At hourly cadence with 2 series, daily load is ~48 calls — far under quota.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.fred_api")

FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

# Canonical series. Add more without changing call sites.
SERIES = {
    # 2026-05-12: CPI / Core CPI switched from SA (CPIAUCSL / CPILFESL) to
    # NSA (CPIAUCNS / CUUR0000SA0L1E). TR medya (Bloomberg HT, Investing.com)
    # NSA "manşet" rakamı kullanır; kullanıcı kıyaslarken SA değerlerimiz
    # farklı çıkıyordu. FMP economic-calendar de "CPI (Apr)" → NSA level
    # gönderiyor. Sistem genel NSA olarak hizalandı.
    "fred_cpi": "CPIAUCNS",                   # CPI-U All Items NSA, monthly index
    "fred_core_cpi": "CUUR0000SA0L1E",        # CPI-U Less Food & Energy (Core), NSA, monthly index
    "fred_nfp": "PAYEMS",                     # Total Nonfarm Payrolls SA, thousands
    "fred_unrate": "UNRATE",                  # Civilian Unemployment Rate SA, % (4.2 = 4.2%)
    # NFP supersector breakdown (BLS Tablo B-1 eşdeğeri, FRED üzerinden) — Faz 3 sektörel kırılım
    "fred_nfp_health":  "USEHS",              # Private Education & Health Services, thousands SA
    "fred_nfp_govt":    "USGOVT",             # Government, thousands SA
    "fred_nfp_prof":    "USPBS",              # Professional & Business Services, thousands SA
    "fred_nfp_leisure": "USLAH",              # Leisure & Hospitality, thousands SA
    "fred_nfp_mfg":     "MANEMP",             # Manufacturing, thousands SA
    "fred_nfp_const":   "USCONS",             # Construction, thousands SA
    "fred_nfp_tpu":     "USTPU",              # Trade, Transportation & Utilities, thousands SA
    "fred_nfp_info":    "USINFO",             # Information, thousands SA
    "fred_pce": "PCEPI",                      # Headline PCE Price Index SA, monthly index
    "fred_core_pce": "PCEPILFE",              # Core PCE Price Index SA, monthly index
    # Day 28 part 3 (2026-05-07) eklendi — kullanıcı "günlük açıklanan veriler" dedi.
    "fred_jobless_initial": "ICSA",           # Initial Jobless Claims SA, weekly Thursday 08:30 ET
    "fred_jobless_continuing": "CCSA",        # Continuing Claims SA, weekly Thursday 08:30 ET
    "fred_retail_sales": "RSAFS",             # Advance Monthly Retail Sales (ex food services) SA, ~mid-month
    "fred_ppi": "PPIFIS",                     # PPI Final Demand SA, monthly index (market-standard headline, was PPIACO basket switch 2026-05-11)
    "fred_core_ppi": "WPSFD49116",            # PPI Final Demand Less Foods & Energy SA = Core PPI
    "fred_housing_starts": "HOUST",           # Housing Starts: Total New Privately Owned SA, ~17th
    "fred_gdp": "GDPC1",                      # Real GDP, Quarterly Annualized SA, ~25th of quarter+1
    # FOMC decoder (Faz 3, 2026-05-11): Fed Funds target range upper/lower bounds.
    # Daily series — values change on FOMC decision days. Used as data points;
    # FOMC_STATEMENT event (fed_rss) is the actual narrative trigger.
    "fred_fed_funds_upper": "DFEDTARU",       # Federal Funds Target Range Upper, daily
    "fred_fed_funds_lower": "DFEDTARL",       # Federal Funds Target Range Lower, daily
}

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


@dataclass
class FREDSeriesResult:
    series_id: str
    success: bool = False
    data_points: int = 0
    latest_date: Optional[str] = None      # ISO yyyy-mm-dd
    latest_value: Optional[str] = None     # raw string ("." means missing)
    prior_date: Optional[str] = None       # filled when fetch limit >= 2
    prior_value: Optional[str] = None
    observations: list = field(default_factory=list)  # full descending list
    payload_bytes: int = 0
    http_status: Optional[int] = None
    error: Optional[str] = None


async def fetch_fred_series(series_id: str, *, limit: int = 1) -> FREDSeriesResult:
    """One series, descending sort, default limit=1 (just the latest point).

    Missing FRED_API_KEY is treated as a hard error so probes fail loud rather
    than silently degrade.
    """
    api_key = os.getenv("FRED_API_KEY", "").strip()
    if not api_key:
        return FREDSeriesResult(series_id=series_id, error="FRED_API_KEY missing")

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": str(limit),
    }
    headers = {"User-Agent": _USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(FRED_BASE_URL, params=params, headers=headers)
    except Exception as e:
        err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.error(f"fred_api fetch failed ({series_id}): {err}")
        return FREDSeriesResult(series_id=series_id, error=err)

    out = FREDSeriesResult(
        series_id=series_id,
        http_status=resp.status_code,
        payload_bytes=len(resp.content),
    )
    if resp.status_code != 200:
        # FRED puts JSON error_message on 400; surface it.
        try:
            body = resp.json()
            out.error = f"HTTP {resp.status_code}: {body.get('error_message', '')}"
        except Exception:
            out.error = f"HTTP {resp.status_code}"
        return out

    try:
        body = resp.json()
    except json.JSONDecodeError as e:
        out.error = f"json decode: {e}"
        return out

    obs = body.get("observations") or []
    out.data_points = body.get("count", len(obs))
    out.observations = obs
    if obs:
        latest = obs[0]
        out.latest_date = latest.get("date")
        out.latest_value = latest.get("value")
        if len(obs) >= 2:
            prior = obs[1]
            out.prior_date = prior.get("date")
            out.prior_value = prior.get("value")
        out.success = True
    else:
        out.error = "no observations"
    return out


@dataclass
class FREDFetchResult:
    """Aggregate of multiple per-series fetches (reliability_probe convenience)."""
    series: dict[str, FREDSeriesResult] = field(default_factory=dict)


async def fetch_fred_multi(series_ids: list[str], *, limit: int = 1) -> FREDFetchResult:
    """Sequential fetch of N series. Returns aggregate; per-series errors isolated.

    `limit` propagates to each per-series call; pass 2 when callers need a
    prior observation alongside the latest one (e.g. release detection).
    """
    out = FREDFetchResult()
    for sid in series_ids:
        out.series[sid] = await fetch_fred_series(sid, limit=limit)
    return out
