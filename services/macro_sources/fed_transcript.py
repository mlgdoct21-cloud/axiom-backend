"""Fed Chair (Powell) press conference transcript fetch + hawkish/dovish
sentiment — FAZ D.

Powell every FOMC günü statement'tan ~30 dakika sonra basın toplantısı
yapar; Fed bunun transcript PDF'ini tipik olarak aynı gün veya 1-2 saat
gecikmeli yayımlar. URL pattern deterministik:

    https://www.federalreserve.gov/mediacenter/files/FOMCpresconf<YYYYMMDD>.pdf

Bu modül:
  1. PDF'i indir (pdfplumber ile metne dönüştür)
  2. Hawkish/dovish kelime/ifade taraması (deterministic lexicon scan)
  3. Skoru ve birkaç anahtar cümleyi döndür

Sentiment çıkmadığında veya PDF henüz yayımlanmadığında sessizce None
döner — storyteller bu yokluğa karşı dayanıklıdır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.fed_transcript")

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# Hawkish/dovish lexicon — Powell'ın kullandığı kalıp ifadeler. Tek kelime
# yerine (örn "patient") cümle-içi kalıplar kullanıyoruz; "patient" tek
# başına bağlamından çıkarılırsa yanıltıcı (hem hawkish "remain patient
# about cutting" hem dovish "patient with data" anlamı taşıyabilir).
_HAWKISH_PATTERNS = [
    r"\binflation\s+(?:remains|is|stays)\s+(?:elevated|high|too\s+high|sticky)\b",
    r"\bremains?\s+(?:restrictive|sufficiently\s+restrictive|too\s+tight)\b",
    r"\b(?:longer|extended)\s+(?:period|time)\s+(?:of\s+)?(?:restrictive|tight)",
    r"\b(?:premature|too\s+early)\s+to\s+(?:cut|ease|loosen)",
    r"\bfurther\s+(?:tighten|hike|hikes|increases?)",
    r"\bnot\s+ready\s+to\s+(?:cut|ease)",
    r"\bmore\s+(?:work|confidence|evidence)\s+(?:to\s+do|needed|required)",
    r"\bdata\s+has\s+(?:not|n't)\s+(?:given|provided)",
    r"\bstronger\s+than\s+(?:expected|anticipated)\s+(?:labor|jobs|inflation|growth)",
    r"\b(?:upside|increased)\s+risks?\s+to\s+inflation\b",
    r"\bstill\s+(?:too\s+)?high\s+for\s+(?:our\s+)?(?:target|comfort)",
    r"\binflation\s+(?:remains?\s+)?stuck\b",
    r"\breaccelerat(?:e|ion|ing)\b",
    r"\boverheating\b",
]

_DOVISH_PATTERNS = [
    r"\binflation\s+(?:has\s+)?(?:moderated|eased|cooled|come\s+down|declined)",
    r"\bprogress\s+(?:on|toward)\s+(?:our\s+)?(?:target|two\s+percent|2%|2\s+percent)",
    r"\b(?:labor\s+market|hiring|job\s+gains?)\s+(?:has\s+)?(?:cooled|softened|moderated|weakened|easing)",
    r"\b(?:closer|nearing|approaching)\s+(?:to\s+)?(?:our\s+)?(?:target|2\s+percent|two\s+percent)",
    r"\bconfident\s+(?:that\s+)?inflation\s+(?:is\s+)?(?:moving|coming\s+down|declining)",
    r"\b(?:appropriate|time)\s+to\s+(?:begin|start)\s+(?:easing|cutting|reducing|dialing\s+back)",
    r"\b(?:risks?\s+have\s+become\s+more\s+balanced|risks?\s+are\s+more\s+balanced)",
    r"\b(?:disinflation|disinflationary)\s+(?:process|trend|continues?)",
    r"\bsofter?\s+than\s+(?:expected|anticipated)\s+(?:labor|jobs|inflation)",
    r"\bgrowth\s+(?:has\s+)?(?:slowed|moderated|cooled)",
    r"\b(?:downside|increased)\s+risks?\s+to\s+(?:employment|labor\s+market|growth)",
    r"\bclose\s+to\s+(?:neutral|the\s+neutral\s+rate)",
    r"\bsupply[\s-]chain\s+(?:improvements?|easing)",
    r"\brate\s+cuts?\s+(?:this\s+year|in\s+\d{4}|coming|appropriate)",
]


@dataclass
class TranscriptSentiment:
    """Powell press conference sentiment skor + örnek ifadeler.

    `score` ∈ [-1, 1]: pozitif = dovish; negatif = hawkish; 0 = denge.
    Skor formülü: tanh(2 * (dovish_hits - hawkish_hits) / max(1, total_hits))
    """
    score: float = 0.0
    hawkish_count: int = 0
    dovish_count: int = 0
    hawkish_phrases: list[str] = field(default_factory=list)
    dovish_phrases: list[str] = field(default_factory=list)
    word_count: int = 0
    success: bool = False
    error: Optional[str] = None


def _press_conf_url(fomc_date: datetime) -> str:
    """FOMC tarihinden Powell PDF URL'ini üret. Format: YYYYMMDD (no sep)."""
    return (
        "https://www.federalreserve.gov/mediacenter/files/"
        f"FOMCpresconf{fomc_date.strftime('%Y%m%d')}.pdf"
    )


async def _fetch_pdf_bytes(url: str) -> Optional[bytes]:
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/pdf"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:
        logger.warning(f"powell pdf fetch failed for {url}: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"powell pdf HTTP {resp.status_code} for {url}")
        return None
    return resp.content


def _pdf_to_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        import pdfplumber
    except ImportError:
        logger.error("pdfplumber not installed — required for fed_transcript")
        return None
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n\n".join(pages)
    except Exception as e:
        logger.warning(f"powell pdf parse failed: {e}")
        return None


def _scan_sentiment(text: str, *, max_phrases: int = 3) -> TranscriptSentiment:
    """Hawkish/dovish kalıpları say + her kategoriden birkaç örnek cümle çek."""
    if not text or len(text) < 200:
        return TranscriptSentiment(error="text too short")
    norm = text.lower()
    word_count = len(text.split())

    def _hits(patterns: list[str], target: str) -> tuple[int, list[str]]:
        count = 0
        phrases: list[str] = []
        for pat in patterns:
            for m in re.finditer(pat, target, re.IGNORECASE):
                count += 1
                if len(phrases) < max_phrases:
                    # ±80 char bağlam pencere (cümle parçası)
                    start = max(0, m.start() - 40)
                    end = min(len(target), m.end() + 40)
                    snippet = target[start:end].strip()
                    snippet = re.sub(r"\s+", " ", snippet)
                    phrases.append(snippet)
        return count, phrases

    haw_n, haw_p = _hits(_HAWKISH_PATTERNS, text)  # original case
    dov_n, dov_p = _hits(_DOVISH_PATTERNS, text)
    total = max(1, haw_n + dov_n)
    raw = (dov_n - haw_n) / total  # in [-1, 1]
    # Doygunluk için tanh-benzeri sıkıştırma; az hit varken merkeze yakın.
    import math
    score = math.tanh(2 * raw)
    return TranscriptSentiment(
        score=round(score, 3),
        hawkish_count=haw_n,
        dovish_count=dov_n,
        hawkish_phrases=haw_p,
        dovish_phrases=dov_p,
        word_count=word_count,
        success=True,
    )


async def fetch_powell_sentiment(fomc_date: datetime) -> TranscriptSentiment:
    """Tek shot: FOMC tarihi → Powell transcript → sentiment.

    PDF henüz yayımlanmadığında veya parse hata verdiğinde sessizce
    `success=False` döner; storyteller bunu None gibi davranır.
    """
    url = _press_conf_url(fomc_date)
    pdf_bytes = await _fetch_pdf_bytes(url)
    if not pdf_bytes:
        return TranscriptSentiment(error=f"pdf fetch failed for {url}")
    text = _pdf_to_text(pdf_bytes)
    if not text:
        return TranscriptSentiment(error="pdf text extraction failed")
    return _scan_sentiment(text)
