"""Fed FOMC statement HTML fetch + language diff — FAZ C.

Fed her FOMC sonrası bir "monetary policy statement" yayımlar (URL
pattern `monetary<YYYYMMDD>a.htm`). Statement metni Fed-watcher'lar için
kritik: önceki ile karşılaştırınca eklenmiş/çıkarılmış cümleler **language
shift** sinyali verir (örn. "patient" → "data-dependent" geçişi piyasanın
en hızlı tepki verdiği şeydir).

Bu modül:
1. Statement HTML'ini indir → düz metne dönüştür (regex strip, BS4 yok)
2. Cümlelere böl (difflib SequenceMatcher tokenize)
3. Önceki statement ile karşılaştır → added/removed cümle listesi

Translation: statement orijinali İngilizce, story Türkçe — storyteller
prompt'unda Gemini ifadeleri paraphrase eder. Bu modül sadece raw
İngilizce cümleler döndürür.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.fed_statement")

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


@dataclass
class StatementDiff:
    """İki FOMC statement metni arasındaki cümle bazında diff.

    `added`: cari statement'a eklenmiş (önceki'de yok) cümleler.
    `removed`: önceki statement'tan çıkarılmış cümleler.
    `success`: True ise içerik kullanılabilir; False ise error doludur.
    """
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    curr_word_count: int = 0
    prior_word_count: int = 0
    success: bool = False
    error: Optional[str] = None


# Statement gövdesi tipik olarak <div id="article">...<p>...</p>...</div>
# içinde. Kenar tag'leri (<head>, <nav>, <footer>) hariç tutup gövde
# paragraflarını çekiyoruz.
_BODY_RE = re.compile(r'<div\s+id=["\']article["\']\s*>(.*?)</div>', re.DOTALL | re.IGNORECASE)
_P_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _strip_html(html: str) -> str:
    """HTML'den FOMC statement gövdesi → düz metin.

    1. <div id="article">...</div> içeriğini al (Fed yayın şablonu)
    2. <p> bloklarını sırayla çek
    3. İç tag'leri strip et, whitespace normalize et
    """
    body_match = _BODY_RE.search(html)
    body = body_match.group(1) if body_match else html  # fallback: full HTML
    paragraphs = []
    for m in _P_RE.finditer(body):
        raw = m.group(1)
        clean = _TAG_RE.sub("", raw)
        clean = _WS_RE.sub(" ", clean).strip()
        # HTML entity decode için minimal değişiklik (en sık karşılaşılanlar)
        clean = clean.replace("&nbsp;", " ").replace("&amp;", "&")
        clean = clean.replace("&ldquo;", '"').replace("&rdquo;", '"')
        clean = clean.replace("&lsquo;", "'").replace("&rsquo;", "'")
        if clean and len(clean) > 20:  # çok kısa paragraf = footer/footer link
            paragraphs.append(clean)
    return "\n\n".join(paragraphs)


def _sentences(text: str) -> list[str]:
    """Cümle ayırma — basit punctuation-based. FOMC statement formal
    İngilizce yazılıyor, edge case'ler nadir."""
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    return [s.strip() for s in parts if s.strip() and len(s.strip()) > 15]


async def fetch_statement_text(url: str) -> Optional[str]:
    """FOMC statement URL → düz metin. Failure'da None döner."""
    if not url:
        return None
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:
        logger.warning(f"fed_statement fetch failed for {url}: {e}")
        return None
    if resp.status_code != 200:
        logger.warning(f"fed_statement HTTP {resp.status_code} for {url}")
        return None
    return _strip_html(resp.text)


def compute_diff(curr_text: str, prev_text: str, *, max_each: int = 5) -> StatementDiff:
    """İki metin → cümle bazında added/removed listesi.

    `max_each`: her listede en fazla N cümle (önemli olanları üst sıraya
    almak için pure-equality; "en önemli" filtresi LLM'e bırakılıyor).
    """
    if not curr_text or not prev_text:
        return StatementDiff(error="empty text")
    curr_sents = _sentences(curr_text)
    prev_sents = _sentences(prev_text)
    matcher = difflib.SequenceMatcher(None, prev_sents, curr_sents)
    added: list[str] = []
    removed: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            added.extend(curr_sents[j1:j2])
        if tag in ("replace", "delete"):
            removed.extend(prev_sents[i1:i2])
    return StatementDiff(
        added=added[:max_each],
        removed=removed[:max_each],
        curr_word_count=sum(len(s.split()) for s in curr_sents),
        prior_word_count=sum(len(s.split()) for s in prev_sents),
        success=True,
    )


async def fetch_diff(curr_url: str, prev_url: str) -> StatementDiff:
    """Tek shot: iki URL → diff. Failure graceful."""
    curr = await fetch_statement_text(curr_url)
    prev = await fetch_statement_text(prev_url)
    if curr is None or prev is None:
        return StatementDiff(
            error=f"fetch failed (curr={curr is not None}, prev={prev is not None})"
        )
    return compute_diff(curr, prev)
