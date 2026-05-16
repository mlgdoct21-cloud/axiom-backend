"""ARK Invest günlük ETF holdings CSV adaptörü — Kurumsal Sentez Faz S1b.

RSS adaptörlerinden (mahfi/isyatirim) FARKLI: kaynak prose değil,
**structured günlük holdings snapshot**. 5 ARK ETF'i için kamuya açık,
login'siz CSV (`assets.ark-funds.com/.../<FUND>_HOLDINGS.csv`).

S1b kapsamı: çek + parse + idempotent snapshot event_id. Gemini /
broadcast / scheduler / gün-gün delta YOK. Fail policy: sessiz skip + log.

TELİF (KRİTİK): ARK CSV footer'ı "no part ... reproduced ... or referred
to ... without written permission" diyor. Kullanım: yalnız **olgusal
holdings verisi** (pay adedi/ağırlık/değer = telife tabi olmayan faktlar);
ARK'ın prose/yorum metni ASLA çoğaltılmaz/alıntılanmaz. Atıf: "Kaynak: ARK
kamuya açık ETF fon bildirimi". Bu, Commit 2 attribution guard'ına girer.

Gün-gün delta ("ARK bu hafta ne aldı/sattı") accumulation store ister
(snapshot'lar biriktirilip diff'lenir) — bu Commit 3 işi, S1b'de YOK.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("corporate.ark_csv")

_BASE = "https://assets.ark-funds.com/fund-documents/funds-etf-csv"

# Fon → kanonik CSV dosya adı (probe ile doğrulandı 2026-05-16; ARKQ
# 'TECH.' kısaltması + URL-encoded %26 — tahmin değil, verify edildi).
FUND_FILES: dict[str, str] = {
    "ARKK": "ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "ARKW": "ARK_NEXT_GENERATION_INTERNET_ETF_ARKW_HOLDINGS.csv",
    "ARKG": "ARK_GENOMIC_REVOLUTION_ETF_ARKG_HOLDINGS.csv",
    "ARKQ": "ARK_AUTONOMOUS_TECH._%26_ROBOTICS_ETF_ARKQ_HOLDINGS.csv",
    "ARKF": "ARK_FINTECH_INNOVATION_ETF_ARKF_HOLDINGS.csv",
}

SOURCE_KEY = "ark"  # state anahtarı fon başına: f"{SOURCE_KEY}_{fund.lower()}"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

_EXPECTED_COLS = 8  # date,fund,company,ticker,cusip,shares,market value ($),weight (%)


@dataclass
class ArkHolding:
    fund: str
    as_of: date
    company: str
    ticker: str          # bazı satırlarda boş (warrant/yabancı) — boş kalabilir
    cusip: str
    shares: float
    market_value_usd: float
    weight_pct: float


@dataclass
class ArkSnapshot:
    fund: str
    as_of: Optional[date] = None
    holdings: list[ArkHolding] = field(default_factory=list)
    skipped_rows: int = 0           # footer/disclaimer/bozuk satır sayısı


def _num(raw: str) -> Optional[float]:
    """'"3,090,538"' / '$1,370,035,495.40' / '11.16%' → float. Parse
    edilemezse None (fail-soft)."""
    if raw is None:
        return None
    s = raw.strip().strip('"').replace("$", "").replace(",", "").replace("%", "").strip()
    if not s or s in ("-", "N/A", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_holdings(csv_bytes: bytes, fund: str) -> ArkSnapshot:
    """Saf parser — CSV byte'ları → ArkSnapshot. Network YOK.

    Footer/disclaimer ve bozuk satırlar sessizce atlanır (8 kolon değilse,
    tarih MM/DD/YYYY parse olmuyorsa, fund eşleşmiyorsa). Boş ticker kabul
    (warrant/yabancı hisse); company boşsa veya shares+value ikisi de
    parse olmuyorsa skip.
    """
    snap = ArkSnapshot(fund=fund)
    if not csv_bytes:
        return snap
    try:
        text_data = csv_bytes.decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ark_csv decode failed ({fund}): {e}")
        return snap

    reader = csv.reader(io.StringIO(text_data))
    rows = list(reader)
    for i, row in enumerate(rows):
        if i == 0 and row and row[0].strip().lower() == "date":
            continue  # header
        if len(row) != _EXPECTED_COLS:
            snap.skipped_rows += 1
            continue
        d_raw, f_raw, company, ticker, cusip, shares_raw, mv_raw, w_raw = (
            c.strip() for c in row
        )
        try:
            as_of = datetime.strptime(d_raw, "%m/%d/%Y").date()
        except ValueError:
            snap.skipped_rows += 1
            continue
        if f_raw.upper() != fund.upper():
            snap.skipped_rows += 1
            continue
        if not company:
            snap.skipped_rows += 1
            continue
        shares = _num(shares_raw)
        mv = _num(mv_raw)
        if shares is None and mv is None:
            snap.skipped_rows += 1
            continue
        weight = _num(w_raw)
        snap.holdings.append(
            ArkHolding(
                fund=fund.upper(),
                as_of=as_of,
                company=company,
                ticker=ticker,
                cusip=cusip,
                shares=shares if shares is not None else 0.0,
                market_value_usd=mv if mv is not None else 0.0,
                weight_pct=weight if weight is not None else 0.0,
            )
        )
    if snap.holdings:
        snap.as_of = snap.holdings[0].as_of
    return snap


async def fetch_holdings(
    fund: str,
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
) -> tuple[Optional[bytes], dict]:
    """Tek fonun holdings CSV'ini çek. ETag / If-Modified-Since aware.

    Dönüş:
      - bilinmeyen fund → (None, {'status': 0, 'error': 'unknown fund'})
      - 304             → (None, {'status': 304})
      - 200             → (body, {'status': 200, 'etag', 'last_modified'})
      - hata / >=400    → (None, {'status': <code|0>, 'error'})
    """
    fname = FUND_FILES.get(fund.upper())
    if not fname:
        return None, {"status": 0, "error": f"unknown fund {fund}"}
    url = f"{_BASE}/{fname}"
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/csv, */*;q=0.8"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.warning(f"ark_csv fetch failed ({fund}): {e}")
        return None, {"status": 0, "error": str(e)}

    if resp.status_code == 304:
        return None, {"status": 304}
    if resp.status_code >= 400:
        logger.warning(f"ark_csv HTTP {resp.status_code} ({fund})")
        return None, {"status": resp.status_code, "error": f"HTTP {resp.status_code}"}
    return resp.content, {
        "status": resp.status_code,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def snapshot_event_id(fund: str, as_of: date) -> str:
    """Günlük snapshot idempotency anahtarı —
    sha1('ark|<FUND>|<iso date>')[:16]. RSS week_event_id muadili ama
    ARK günlük (haftalık değil)."""
    seed = f"ark|{fund.upper()}|{as_of.isoformat()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


async def read_source_state(
    fund: str,
) -> tuple[Optional[str], Optional[str]]:
    """corporate_source_state'ten fon-başına (etag, last_modified) oku.
    Tablo/DB yoksa sessiz (None, None)."""
    src = f"{SOURCE_KEY}_{fund.lower()}"
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT etag, last_modified FROM corporate_source_state "
                        "WHERE source = :s"
                    ),
                    {"s": src},
                )
            ).first()
        if row:
            return row[0], row[1]
    except Exception as e:  # noqa: BLE001
        logger.info(f"read_source_state skip ({src}): {e}")
    return None, None


async def write_source_state(
    fund: str,
    etag: Optional[str],
    last_modified: Optional[str],
) -> None:
    """corporate_source_state'e fon-başına ETag/Last-Modified UPSERT.
    Hata = sessiz skip + log."""
    if not etag and not last_modified:
        return
    src = f"{SOURCE_KEY}_{fund.lower()}"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO corporate_source_state "
                    "(source, etag, last_modified, updated_at) "
                    "VALUES (:s, :e, :lm, NOW()) "
                    "ON CONFLICT (source) DO UPDATE SET "
                    "etag = EXCLUDED.etag, "
                    "last_modified = EXCLUDED.last_modified, "
                    "updated_at = NOW()"
                ),
                {"s": src, "e": etag, "lm": last_modified},
            )
    except Exception as e:  # noqa: BLE001
        logger.info(f"write_source_state skip ({src}): {e}")
