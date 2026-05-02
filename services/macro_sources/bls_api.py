"""BLS Public Data API v2 — multi-series fetch.

Used by reliability_probe to monitor CPI / NFP source availability and to
back release-event detection (Day 5+) by tracking the latest reported period
per series.

Rate limits (v2):
  - Unregistered: 25 daily queries, 10 series per query, 10 years per query.
  - Registered (free key, BLS_API_KEY env var): 500 daily, 50 series, 20 years.

We default to combining series in a single POST so probe cost stays small.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.bls_api")

BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# Canonical series we care about. Add more here without changing call sites.
SERIES = {
    "bls_cpi": "CUUR0000SA0",        # CPI-U All Items (NSA), monthly
    "bls_nfp": "CES0000000001",      # Total Nonfarm Payrolls, monthly
}

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
# Generous connect budget — Railway egress to api.bls.gov has been observed
# at ~5s+ during TLS handshake while local + `railway run` finish in <1s.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


@dataclass
class BLSSeriesResult:
    series_id: str
    success: bool = False
    data_points: int = 0
    latest_period: Optional[str] = None    # e.g. "2026-M03"
    latest_value: Optional[str] = None     # raw string; "-" means unavailable
    error: Optional[str] = None


@dataclass
class BLSFetchResult:
    http_status: Optional[int] = None
    payload_bytes: int = 0
    api_status: Optional[str] = None       # "REQUEST_SUCCEEDED" / failure
    series: dict[str, BLSSeriesResult] = field(default_factory=dict)
    error: Optional[str] = None            # network/global error


async def fetch_bls_multi(
    series_ids: list[str],
    *,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
) -> BLSFetchResult:
    """POST a single multi-series request. Returns one BLSFetchResult.

    Each requested series gets its own BLSSeriesResult with success/latest.
    A single network error fails all series uniformly.
    """
    payload: dict = {"seriesid": series_ids}
    if start_year is not None:
        payload["startyear"] = str(start_year)
    if end_year is not None:
        payload["endyear"] = str(end_year)
    api_key = os.getenv("BLS_API_KEY", "").strip()
    if api_key:
        payload["registrationkey"] = api_key

    headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(BLS_API_URL, content=json.dumps(payload), headers=headers)
    except Exception as e:
        # Many httpx exception types stringify to "" — capture the type name.
        err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.error(f"bls_api fetch failed: {err}")
        result = BLSFetchResult(error=err)
        for sid in series_ids:
            result.series[sid] = BLSSeriesResult(series_id=sid, error=err)
        return result

    raw = resp.content
    out = BLSFetchResult(http_status=resp.status_code, payload_bytes=len(raw))

    if resp.status_code != 200:
        out.error = f"HTTP {resp.status_code}"
        for sid in series_ids:
            out.series[sid] = BLSSeriesResult(series_id=sid, error=out.error)
        return out

    try:
        body = resp.json()
    except json.JSONDecodeError as e:
        out.error = f"json decode: {e}"
        for sid in series_ids:
            out.series[sid] = BLSSeriesResult(series_id=sid, error=out.error)
        return out

    out.api_status = body.get("status")
    if out.api_status != "REQUEST_SUCCEEDED":
        msgs = body.get("message") or []
        out.error = f"BLS {out.api_status}: {'; '.join(msgs) if msgs else 'no detail'}"
        for sid in series_ids:
            out.series[sid] = BLSSeriesResult(series_id=sid, error=out.error)
        return out

    by_id = {s["seriesID"]: s for s in body.get("Results", {}).get("series", [])}
    for sid in series_ids:
        s = by_id.get(sid)
        if not s:
            out.series[sid] = BLSSeriesResult(series_id=sid, error="series missing in response")
            continue
        data = s.get("data") or []
        latest = data[0] if data else None  # BLS returns most-recent-first
        out.series[sid] = BLSSeriesResult(
            series_id=sid,
            success=True,
            data_points=len(data),
            latest_period=f"{latest['year']}-{latest['period']}" if latest else None,
            latest_value=latest.get("value") if latest else None,
        )
    return out
