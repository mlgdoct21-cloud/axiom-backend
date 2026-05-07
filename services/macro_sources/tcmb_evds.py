"""TCMB EVDS (Elektronik Veri Dağıtım Sistemi) — Türkiye macro series fetcher.

Mirrors fred_api.py shape so reliability_probe + release_detect can plug it in
identically. EVDS is the canonical Turkish source for:
  - TCMB politika faizi
  - TÜFE (Manşet) ve Çekirdek-B
  - ÜFE
  - İşsizlik oranı
  - GSYİH büyüme
  - Cari açık

API: https://evds2.tcmb.gov.tr/service/evds/series=KOD&startDate=...&type=json&key=...
Free key: evds2.tcmb.gov.tr → "Bilgi/Hesap" → API anahtarı (registration required).

Without `TCMB_EVDS_API_KEY` env, all probes return graceful "key_missing"
errors and reliability_probe degrades to skip — no crashes.

Series notation: TCMB EVDS uses dotted codes like TP.AB.A1.GERCEK.
The JSON response wraps each row as {"Tarih": "01-2026", "TP_AB_A1_GERCEK": "47.5"}
(dots replaced with underscores in field names). Our parser normalises this.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.tcmb_evds")

EVDS_BASE_URL = "https://evds2.tcmb.gov.tr/service/evds"

# Canonical TR series. Frequencies vary — TCMB returns monthly for most TÜFE
# series, weekly for some, daily for FX. `frequency` query param is optional;
# we default to monthly (5) which matches CPI/UNRATE/UNEMPLOYMENT cadence.
#
# Source codes verified manually against EVDS UI as of 2026-05; if a code
# returns 0 rows the probe will log + degrade gracefully (no narrative fired).
SERIES = {
    "tcmb_policy_rate":   "TP.AB.A1.GERCEK",   # TCMB 1-week repo (Politika Faizi), aylık ortalama
    "tcmb_tufe":          "TP.FG.J0",          # TÜFE Genel Endeks (manşet TR enflasyon), aylık
    "tcmb_core_b":        "TP.FE.OKTG02",      # Çekirdek B (gıda+enerji+alkol+tütün hariç), aylık
    "tcmb_ufe":           "TP.FE.OKTG01",      # Yİ-ÜFE (üretici fiyatları), aylık
    "tcmb_unemployment":  "TP.HKFE01",         # İşsizlik oranı, aylık
    "tcmb_current_acct":  "TP.ODEMGZS.ABFE",   # Cari işlemler dengesi (USD milyon), aylık
}

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


@dataclass
class TCMBSeriesResult:
    series_code: str
    success: bool = False
    data_points: int = 0
    latest_date: Optional[str] = None     # "YYYY-MM-01" normalised
    latest_value: Optional[str] = None
    prior_date: Optional[str] = None
    prior_value: Optional[str] = None
    observations: list = field(default_factory=list)
    payload_bytes: int = 0
    http_status: Optional[int] = None
    error: Optional[str] = None


def _api_key() -> str:
    return os.getenv("TCMB_EVDS_API_KEY", "").strip()


def _is_configured() -> bool:
    return bool(_api_key())


def _norm_date(raw: str) -> Optional[str]:
    """EVDS aylık format: 'MM-YYYY' → 'YYYY-MM-01' ISO. Veri yoksa None."""
    if not raw:
        return None
    raw = raw.strip()
    # Format genelde MM-YYYY, bazen YYYY-MM-DD
    try:
        if len(raw) == 7 and raw[2] == "-":
            mm, yyyy = raw.split("-")
            return f"{yyyy}-{mm.zfill(2)}-01"
        if len(raw) == 10 and raw[4] == "-":
            return raw
    except Exception:
        return None
    return None


async def fetch_tcmb_series(series_code: str, *, lookback_months: int = 18) -> TCMBSeriesResult:
    """Fetch one EVDS series, returning newest first. lookback_months=18 gives
    YoY + MoM comparisons with margin."""
    api_key = _api_key()
    if not api_key:
        return TCMBSeriesResult(
            series_code=series_code,
            error="TCMB_EVDS_API_KEY missing — set Railway env via evds2.tcmb.gov.tr",
        )

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_months * 31)
    # EVDS bekliyor: 'DD-MM-YYYY'
    fmt_date = lambda d: f"{d.day:02d}-{d.month:02d}-{d.year}"

    params = {
        "series": series_code,
        "startDate": fmt_date(start),
        "endDate": fmt_date(today),
        "type": "json",
        "key": api_key,
    }
    headers = {"User-Agent": _USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(EVDS_BASE_URL, params=params, headers=headers)
    except Exception as e:
        return TCMBSeriesResult(
            series_code=series_code,
            error=f"http: {type(e).__name__}: {str(e)[:120]}",
        )

    if resp.status_code != 200:
        return TCMBSeriesResult(
            series_code=series_code,
            http_status=resp.status_code,
            error=f"non-200: {resp.text[:200]}",
        )

    try:
        body = resp.json()
    except Exception as e:
        return TCMBSeriesResult(
            series_code=series_code,
            http_status=resp.status_code,
            error=f"json parse: {e}",
        )

    rows = body.get("items", []) if isinstance(body, dict) else []
    # Field key for the value: dots in series_code become underscores
    value_key = series_code.replace(".", "_")

    parsed_obs: list[dict] = []
    for row in rows:
        d = _norm_date(row.get("Tarih", ""))
        v = row.get(value_key)
        if v is None or v == "":
            continue
        parsed_obs.append({"date": d, "value": str(v)})

    # EVDS sorts oldest-first; reverse for fred-compatible newest-first
    parsed_obs.reverse()

    latest = parsed_obs[0] if parsed_obs else {}
    prior = parsed_obs[1] if len(parsed_obs) > 1 else {}

    return TCMBSeriesResult(
        series_code=series_code,
        success=bool(parsed_obs),
        data_points=len(parsed_obs),
        latest_date=latest.get("date"),
        latest_value=latest.get("value"),
        prior_date=prior.get("date"),
        prior_value=prior.get("value"),
        observations=parsed_obs,
        payload_bytes=len(resp.content),
        http_status=resp.status_code,
    )


@dataclass
class TCMBFetchResult:
    series: dict[str, TCMBSeriesResult] = field(default_factory=dict)


async def fetch_tcmb_multi(source_keys: list[str]) -> TCMBFetchResult:
    """Sequential fetch of multiple EVDS series. Per-series errors isolated."""
    out = TCMBFetchResult()
    for source_key in source_keys:
        series_code = SERIES.get(source_key)
        if not series_code:
            continue
        out.series[source_key] = await fetch_tcmb_series(series_code)
    return out
