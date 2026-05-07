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

# EVDS3 base URL (2026-04 migration; eski evds2 → evds3.tcmb.gov.tr/igmevdsms-dis)
# URL formatı eskisinden FARKLI:
#   - params query yerine path'in içinde: /series=KOD&startDate=...&type=json
#   - key query param DEĞİL, "key" header olarak gönderiliyor
EVDS_BASE_URL = "https://evds3.tcmb.gov.tr/igmevdsms-dis"

# Canonical TR series codes (2026-05-07 EVDS3 üzerinde live probe ile doğrulandı).
# Aktif olarak çalışan 3 seri: TÜFE manşet + Çekirdek B + Yİ-ÜFE. Diğer 3 kod
# (politika faizi, işsizlik, cari işlemler) EVDS3'te kod değişikliği gördü;
# doğru sürümleri kullanıcıyla birlikte teyit edilecek.
SERIES = {
    "tcmb_tufe":          "TP.FG.J0",            # TÜFE Genel Endeks, aylık ✅
    "tcmb_core_b":        "TP.FE.OKTG02",        # Çekirdek B, aylık ✅
    "tcmb_ufe":           "TP.FE.OKTG01",        # Yİ-ÜFE üretici fiyatları, aylık ✅
    "tcmb_policy_rate":   "TP.BISPOLFAIZ.TUR",   # TR politika faizi (BIS Bankası verisi), aylık ✅
    # TODO Day 28 part 4 — EVDS3'te kod doğrulaması bekleyen seriler:
    # "tcmb_unemployment":  "TP.???",  # İşsizlik oranı
    # "tcmb_current_acct":  "TP.???",  # Cari işlemler dengesi
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
    """EVDS3 aylık format: 'YYYY-M' veya 'YYYY-MM' → 'YYYY-MM-01' ISO.
    Eski format 'MM-YYYY' da destekleniyor (geriye uyumluluk)."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        # EVDS3 yeni format: 'YYYY-M' (örn '2026-1') veya 'YYYY-MM'
        if "-" in raw:
            parts = raw.split("-")
            if len(parts) == 2:
                a, b = parts
                if len(a) == 4 and a.isdigit():
                    # 'YYYY-M' veya 'YYYY-MM'
                    return f"{a}-{b.zfill(2)}-01"
                if len(b) == 4 and b.isdigit():
                    # eski 'MM-YYYY' formatı
                    return f"{b}-{a.zfill(2)}-01"
            if len(raw) == 10 and raw[4] == "-":
                # 'YYYY-MM-DD'
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

    # EVDS3 kuralı: query string DEĞİL, parametreler path'in içinde verbatim.
    # Örn /igmevdsms-dis/series=TP.FG.J0&startDate=01-01-2025&endDate=15-05-2026&type=json
    # ve key query yerine HEADER olarak.
    path_params = (
        f"series={series_code}"
        f"&startDate={fmt_date(start)}"
        f"&endDate={fmt_date(today)}"
        f"&type=json"
    )
    url = f"{EVDS_BASE_URL}/{path_params}"
    headers = {"User-Agent": _USER_AGENT, "key": api_key}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
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
