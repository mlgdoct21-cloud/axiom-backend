"""Fed SEP (Summary of Economic Projections) PDF parser — Faz B otomasyon.

Fed her 3 ayda bir (Mart/Haziran/Eylül/Aralık FOMC toplantılarında) SEP
yayımlar. Public PDF URL deterministic:
    https://www.federalreserve.gov/monetarypolicy/files/fomcprojtabl<YYYYMMDD>.pdf

"Implied path of the federal funds rate" tablosundan **median** satırı çekip
yıl-sonu projection değerlerini extract ederiz. Bu sayılar storyteller'ın
dot plot şift bölümünü besler.

Pragmatik scope (FAZ B):
- SADECE "Federal funds rate" tablosunun median satırı
- GDP/işsizlik/PCE projection medianları FAZ C'ye ertelenmiş

Eğer parse fail olursa (PDF formatı değişirse, indirilemezse, vb), session
silently skip eder ve admin manuel entry endpoint'i kalır.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.fed_sep")

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class SEPMedians:
    """Fed SEP federal funds rate median projections (yüzde, yıl-sonu)."""
    end_year_0: Optional[float] = None   # cari yıl sonu
    end_year_1: Optional[float] = None   # +1 yıl sonu
    end_year_2: Optional[float] = None   # +2 yıl sonu
    longer_run: Optional[float] = None
    success: bool = False
    error: Optional[str] = None
    raw_text: Optional[str] = None       # debug için ham metin parçası

    def has_any(self) -> bool:
        return any(v is not None for v in (
            self.end_year_0, self.end_year_1, self.end_year_2, self.longer_run,
        ))


def _build_pdf_url(release_date: datetime) -> str:
    """fomcprojtabl<YYYYMMDD>.pdf URL'si üret."""
    yyyymmdd = release_date.strftime("%Y%m%d")
    return (
        f"https://www.federalreserve.gov/monetarypolicy/files/"
        f"fomcprojtabl{yyyymmdd}.pdf"
    )


async def _fetch_pdf_bytes(url: str) -> Optional[bytes]:
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:
        logger.warning(f"fed_sep PDF fetch failed: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"fed_sep HTTP {resp.status_code} for {url}")
        return None
    if not resp.content.startswith(b"%PDF"):
        logger.warning(f"fed_sep response not a PDF (first 4 bytes: {resp.content[:4]!r})")
        return None
    return resp.content


def parse_sep_pdf(pdf_bytes: bytes) -> SEPMedians:
    """PDF bytes → SEP federal funds rate medians.

    Strateji: pdfplumber ile tüm sayfaları text'e çevir, "Federal funds rate"
    bölümünü bul, ardından "Median" satırını yakala. Median satırı tipik
    olarak 4-5 sayı içerir (cari + 2 yıl + longer-run + bazı tablolarda
    +3 yıl).

    Format örneği (2024 SEP):
        Variable    2024 2025 2026 Longer run
        Federal funds rate
          Median    4.6  3.9  3.1  2.9
    """
    try:
        import pdfplumber  # lazy import — release_detect'in PDF-bağımsız import sırasını korur
    except ImportError as e:
        return SEPMedians(error=f"pdfplumber not installed: {e}")

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text_parts = []
            for page in pdf.pages:
                t = page.extract_text() or ""
                text_parts.append(t)
            full_text = "\n".join(text_parts)
    except Exception as e:
        return SEPMedians(error=f"pdfplumber open failed: {e}")

    # "Federal funds rate" → median satırı pencereye al
    ff_idx = full_text.lower().find("federal funds rate")
    if ff_idx < 0:
        return SEPMedians(error="'Federal funds rate' header not found", raw_text=full_text[:500])
    # Median satırı ff_idx'ten sonra ilk 'Median' kelimesi (case insensitive)
    window = full_text[ff_idx: ff_idx + 1500]
    median_match = re.search(r"Median\s+([\d\s.\-]+)", window, re.IGNORECASE)
    if not median_match:
        return SEPMedians(error="'Median' row not found in window", raw_text=window[:500])

    nums_raw = median_match.group(1).strip()
    nums = re.findall(r"-?\d+\.\d+", nums_raw)
    if len(nums) < 3:
        return SEPMedians(
            error=f"insufficient numbers in Median row: {nums}",
            raw_text=window[:500],
        )

    # Tipik format: [cur_year, +1, +2, longer_run] veya
    # [cur_year, +1, +2, +3, longer_run]. Son sayı longer_run.
    result = SEPMedians(success=True, raw_text=window[:500])
    try:
        if len(nums) == 4:
            result.end_year_0 = float(nums[0])
            result.end_year_1 = float(nums[1])
            result.end_year_2 = float(nums[2])
            result.longer_run = float(nums[3])
        elif len(nums) == 5:
            result.end_year_0 = float(nums[0])
            result.end_year_1 = float(nums[1])
            result.end_year_2 = float(nums[2])
            # nums[3] = +3 year — şimdilik kullanılmıyor (FAZ B medians 4 alan)
            result.longer_run = float(nums[4])
        elif len(nums) == 3:
            result.end_year_0 = float(nums[0])
            result.end_year_1 = float(nums[1])
            result.longer_run = float(nums[2])
        else:
            # 6+ → ilk 3'ü + sonuncuyu kullan
            result.end_year_0 = float(nums[0])
            result.end_year_1 = float(nums[1])
            result.end_year_2 = float(nums[2])
            result.longer_run = float(nums[-1])
    except ValueError as e:
        return SEPMedians(error=f"number parse failed: {e}", raw_text=window[:500])

    return result


async def fetch_sep_medians(release_date: datetime) -> SEPMedians:
    """Tek shot — Fed PDF URL üret, indir, parse et."""
    url = _build_pdf_url(release_date)
    logger.info(f"fed_sep fetch: {url}")
    pdf = await _fetch_pdf_bytes(url)
    if not pdf:
        return SEPMedians(error="PDF download failed")
    return parse_sep_pdf(pdf)
