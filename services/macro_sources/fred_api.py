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
    "fred_cpi": "CPIAUCSL",                   # CPI-U All Items SA, monthly index
    "fred_core_cpi": "CPILFESL",              # CPI-U Less Food & Energy (Core), SA, monthly index
    "fred_nfp": "PAYEMS",                     # Total Nonfarm Payrolls SA, thousands
    "fred_unrate": "UNRATE",                  # Civilian Unemployment Rate SA, % (4.2 = 4.2%)
    "fred_pce": "PCEPI",                      # Headline PCE Price Index SA, monthly index
    "fred_core_pce": "PCEPILFE",              # Core PCE Price Index SA, monthly index
    # Day 28 part 3 (2026-05-07) eklendi — kullanıcı "günlük açıklanan veriler" dedi.
    "fred_jobless_initial": "ICSA",           # Initial Jobless Claims SA, weekly Thursday 08:30 ET
    "fred_jobless_continuing": "CCSA",        # Continuing Claims SA, weekly Thursday 08:30 ET
    "fred_retail_sales": "RSAFS",             # Advance Monthly Retail Sales (ex food services) SA, ~mid-month
    "fred_ppi": "PPIACO",                     # Producer Price Index All Commodities NSA, ~13th
    "fred_housing_starts": "HOUST",           # Housing Starts: Total New Privately Owned SA, ~17th
    "fred_gdp": "GDPC1",                      # Real GDP, Quarterly Annualized SA, ~25th of quarter+1
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
