"""Macro Storyteller — tiered 4-7 paragraph storytelling for macro releases.

`macro_narrative.py` üretiyor: tek-paragraf hap yorum (Free tier).
Bu modül üretiyor: Premium + Advance tier için hikayeleştirilmiş yorum.

Output: `macro_stories` tablosuna (event_id, tier) unique key ile yazar.
Tier ayrımı:
- Premium: 300-450 kelime, 4-5 paragraf. Manşet/çekirdek/ağırlık/Fed/mental
  model + portföy 1-cümle.
- Advance: 450-600 kelime, 6-7 paragraf. Premium + 3-ay trend + senaryo
  matematiği + revizyon detayı + sektörel bağlam.

5 katmanlı halüsinasyon koruması:
- L1: sayı whitelist (INPUT'taki rakamlar + sabit ağırlıklar)
- L2: her sayının ardından [KAYNAK] etiketi (60 char penceresinde)
- L3: tarih damgası şart (ay adı / yıl / UTC)
- L4: politik+mutlak ifade YASAK (Cumhuriyetçi, kesin, garanti, ...)
- L5: kelime sayısı [min,max] aralığında

Faz 1: sadece CPI desteği. Faz 2+ NFP/PCE/PPI eklenir.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.validators import (
    build_allowed_numbers,
    extract_numbers,
    validate_numbers,
)

logger = get_logger("macro.storyteller")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=8.0)

Tier = Literal["premium", "advance"]

# BLS CPI relative importance (sabit; yıllık reweight'te elle bump et).
# Kaynak: BLS Table 1, Dec 2024 reference weights.
_CPI_WEIGHTS_PCT = {
    "shelter": 35.0,
    "energy": 6.5,
    "services_ex_shelter": 58.0,  # rough — "tüm hizmetler" çatısı
    "food": 13.5,
}


@dataclass(frozen=True)
class SourceCitation:
    code: str
    name: str
    url: str


_SOURCES: dict[str, SourceCitation] = {
    "FRED:CPIAUCSL": SourceCitation(
        "FRED:CPIAUCSL", "FRED Headline CPI",
        "https://fred.stlouisfed.org/series/CPIAUCSL",
    ),
    "FRED:CPILFESL": SourceCitation(
        "FRED:CPILFESL", "FRED Core CPI",
        "https://fred.stlouisfed.org/series/CPILFESL",
    ),
    "FRED:PAYEMS": SourceCitation(
        "FRED:PAYEMS", "FRED Nonfarm Payrolls",
        "https://fred.stlouisfed.org/series/PAYEMS",
    ),
    "FRED:UNRATE": SourceCitation(
        "FRED:UNRATE", "FRED Unemployment Rate",
        "https://fred.stlouisfed.org/series/UNRATE",
    ),
    "BLS": SourceCitation(
        "BLS", "BLS Employment / CPI release",
        "https://www.bls.gov/news.release/",
    ),
    "CME": SourceCitation(
        "CME", "CME FedWatch Tool",
        "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
    ),
    "FMP": SourceCitation(
        "FMP", "FinancialModelingPrep",
        "https://site.financialmodelingprep.com/",
    ),
}


# ---------- Date helpers ----------

_TR_MONTHS = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)


def _format_tr_date(dt) -> Optional[str]:
    """ISO datetime → '1 Nisan 2026' okunabilir Türkçe format.

    Model paragraf içinde ISO formatı (2026-04-01T00:00:00+00:00) yapıştırdığında
    çirkin duruyordu. Payload'a önceden human-readable formla geçiriyoruz, modele
    sadece bunu yazması söyleniyor.
    """
    if dt is None:
        return None
    try:
        return f"{dt.day} {_TR_MONTHS[dt.month - 1]} {dt.year}"
    except Exception:
        return None


# ---------- Validator ----------

# Sayı sonrası 60 char penceresinde [SRC] etiketi aramak için
_CITATION_RE = re.compile(r"\[([A-Z]+(?::[A-Z0-9_]+)?)\]")
_NUM_TOKEN_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?%?")
_CITATION_WINDOW = 60

_DATE_MARKER_RE = re.compile(
    r"\b(?:20\d\d|Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|"
    r"Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık|UTC)\b",
    re.IGNORECASE,
)

_BANNED_PHRASES = (
    # Politik
    "Cumhuriyetçi", "Demokrat", "AKP", "CHP", "MHP",
    # Mutlaklık — kullanıcı kaybı en büyük risk
    "kesinlikle", "garanti", "asla olmaz", "100% kesin",
    # Yatırım tavsiyesi imajı
    "al, sat", "şimdi al", "şimdi sat",
)


@dataclass
class ValidatorReport:
    ok: bool = False
    unknown_numbers: list[str] = field(default_factory=list)
    numbers_without_citation: list[str] = field(default_factory=list)
    missing_temporal_marker: bool = False
    banned_phrases_found: list[str] = field(default_factory=list)
    word_count: int = 0
    out_of_word_bounds: bool = False
    sources_cited: list[str] = field(default_factory=list)
    unknown_sources: list[str] = field(default_factory=list)


def _validate_story(
    story_md: str,
    allowed_numbers: set[Decimal],
    allowed_source_codes: set[str],
    *,
    min_words: int,
    max_words: int,
) -> ValidatorReport:
    rep = ValidatorReport()

    # Yıl literal'leri (1900-2099) her iki katmandan da exempt — tarih damgası
    # için "Şubat 2026" yazmanın citation chip'i olmamalı.
    def _is_year_literal(d: Decimal, raw: str) -> bool:
        if "%" in raw or "," in raw or "." in raw:
            return False
        try:
            return Decimal("1900") <= d <= Decimal("2099") and d == d.to_integral_value()
        except Exception:
            return False

    # L1 — sayı whitelist. İki tip exempt:
    #   (a) yıl literali 1900-2099 → tarih damgası, citation'sız OK
    #   (b) küçük integer |d| ≤ 10 ve % içermeyen → "son 3 ayda", "6 aylık"
    #       tarzı paragraf-içi anlatım sayıları; whitelist'e koymak imkansız
    def _is_small_int(raw: str) -> bool:
        if "%" in raw:
            return False
        body = raw.replace(",", ".")
        try:
            d = Decimal(body)
        except Exception:
            return False
        return d == d.to_integral_value() and abs(d) <= Decimal("10")

    unk_all = validate_numbers(
        story_md, allowed_numbers, tolerance=Decimal("0.05"),
    )
    rep.unknown_numbers = [
        u for u in unk_all
        if u
        and not _is_year_literal(Decimal(u.replace(",", ".")), u)
        and not _is_small_int(u)
    ]

    # L2 — her sayının 60-char penceresinde [SRC] var mı (yıllar exempt)
    missing: list[str] = []
    for m in _NUM_TOKEN_RE.finditer(story_md):
        body = m.group(0).rstrip("%").replace(",", ".")
        try:
            d = Decimal(body)
        except Exception:
            continue
        # Yıl literal'i (1900-2099) — citation gerektirmez
        if _is_year_literal(d, m.group(0)):
            continue
        # Çok küçük integer'ları (paragraf/madde no gibi) atla
        is_int = (d == d.to_integral_value())
        if is_int and abs(d) <= Decimal("10") and "%" not in m.group(0):
            continue
        # Pencere içinde [...] var mı bak
        window = story_md[m.end(): m.end() + _CITATION_WINDOW]
        if not _CITATION_RE.search(window):
            missing.append(m.group(0))
    rep.numbers_without_citation = missing

    # L3 — tarih damgası
    rep.missing_temporal_marker = _DATE_MARKER_RE.search(story_md) is None

    # L4 — yasak ifadeler
    lower = story_md.lower()
    rep.banned_phrases_found = [p for p in _BANNED_PHRASES if p.lower() in lower]

    # L5 — kelime sayısı
    rep.word_count = len(story_md.split())
    rep.out_of_word_bounds = (
        rep.word_count < min_words or rep.word_count > max_words
    )

    # Kaynak listesi — sadece bilinen kodlardan olmalı
    cited = set(_CITATION_RE.findall(story_md))
    rep.sources_cited = sorted(cited)
    rep.unknown_sources = sorted(s for s in cited if s not in allowed_source_codes)

    rep.ok = (
        not rep.unknown_numbers
        and not rep.numbers_without_citation
        and not rep.missing_temporal_marker
        and not rep.banned_phrases_found
        and not rep.out_of_word_bounds
        and not rep.unknown_sources
    )
    return rep


# ---------- Data fetch ----------

async def _load_event_payload(event_id: str) -> Optional[dict]:
    sql = text("""
        SELECT event_id, event_type, country, source, released_at,
               actual_value, prior_value, narrative_md, source_url,
               expected_mom_pct, expected_yoy_pct
        FROM macro_releases WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
        if not row:
            return None
        payload = {k: row[k] for k in row.keys()}
        # Paired core (CPI ↔ CORE_CPI aynı released_at)
        et = (row["event_type"] or "").upper()
        pair_map = {"CPI": "CORE_CPI", "PCE": "CORE_PCE", "NFP": "UNRATE"}
        pair = pair_map.get(et)
        payload["paired"] = None
        if pair and row["released_at"]:
            psql = text("""
                SELECT event_type, actual_value, prior_value,
                       expected_mom_pct, expected_yoy_pct
                FROM macro_releases
                WHERE event_type = :et AND source = :src
                  AND released_at = :ts
                LIMIT 1
            """)
            prow = (await conn.execute(psql, {
                "et": pair, "src": row["source"], "ts": row["released_at"],
            })).mappings().first()
            if prow:
                payload["paired"] = {k: prow[k] for k in prow.keys()}
        # 12-ay history (trend hesabı için)
        payload["history"] = []
        if row["released_at"] and row["event_type"]:
            hsql = text("""
                SELECT released_at, actual_value, prior_value
                FROM macro_releases
                WHERE event_type = :et AND source = :src
                  AND released_at < :ts AND actual_value IS NOT NULL
                ORDER BY released_at DESC LIMIT 12
            """)
            hrows = (await conn.execute(hsql, {
                "et": row["event_type"], "src": row["source"],
                "ts": row["released_at"],
            })).mappings().all()
            payload["history"] = [
                {
                    "date": r["released_at"].isoformat() if r["released_at"] else None,
                    "actual": float(r["actual_value"]) if r["actual_value"] is not None else None,
                    "prior": float(r["prior_value"]) if r["prior_value"] is not None else None,
                }
                for r in hrows
            ]
    return payload


# ---------- Deterministic helpers ----------

def _pct(actual, prior) -> Optional[float]:
    if actual is None or prior is None or prior == 0:
        return None
    return round((float(actual) - float(prior)) / abs(float(prior)) * 100, 2)


def _avg_n(history: list[dict], n: int, key: str = "actual") -> Optional[float]:
    vals = [h[key] for h in history[:n] if h.get(key) is not None]
    if len(vals) < n:
        return None
    return round(sum(vals) / len(vals), 2)


def _mom_3m_avg(history: list[dict], n: int = 3) -> Optional[float]:
    """3-aylık MoM% ortalaması (history[i] = i ay önceki release)."""
    moms = []
    for h in history[:n]:
        m = _pct(h.get("actual"), h.get("prior"))
        if m is not None:
            moms.append(m)
    if len(moms) < n:
        return None
    return round(sum(moms) / len(moms), 2)


# ---------- CPI prompt builder ----------

def _cpi_payload(payload: dict) -> tuple[dict, set[Decimal], set[str]]:
    """Returns (llm_input, allowed_numbers, allowed_source_codes)."""
    actual = float(payload["actual_value"]) if payload.get("actual_value") is not None else None
    prior = float(payload["prior_value"]) if payload.get("prior_value") is not None else None
    mom = _pct(actual, prior)
    expected_mom = (
        float(payload["expected_mom_pct"])
        if payload.get("expected_mom_pct") is not None else None
    )
    expected_yoy = (
        float(payload["expected_yoy_pct"])
        if payload.get("expected_yoy_pct") is not None else None
    )
    surprise_mom_pp = (
        round(mom - expected_mom, 2)
        if mom is not None and expected_mom is not None else None
    )

    paired = payload.get("paired") or {}
    core_actual = float(paired.get("actual_value")) if paired.get("actual_value") is not None else None
    core_prior = float(paired.get("prior_value")) if paired.get("prior_value") is not None else None
    core_mom = _pct(core_actual, core_prior)
    core_expected_mom = (
        float(paired.get("expected_mom_pct"))
        if paired.get("expected_mom_pct") is not None else None
    )

    history = payload.get("history") or []
    avg_3m_mom = _mom_3m_avg(history, n=3)
    avg_6m_mom = _mom_3m_avg(history, n=6)

    weights = _CPI_WEIGHTS_PCT

    sources_used = ["FRED:CPIAUCSL", "BLS"]
    if paired:
        sources_used.append("FRED:CPILFESL")

    llm_input = {
        "release_date": _format_tr_date(payload.get("released_at")),
        "country": payload.get("country"),
        "headline_cpi": {
            "actual_index": actual,
            "prior_index": prior,
            "mom_pct": mom,
            "expected_mom_pct": expected_mom,
            "expected_yoy_pct": expected_yoy,
            "surprise_mom_pp": surprise_mom_pp,
            "source_code": "FRED:CPIAUCSL",
        },
        "core_cpi": {
            "actual_index": core_actual,
            "prior_index": core_prior,
            "mom_pct": core_mom,
            "expected_mom_pct": core_expected_mom,
            "source_code": "FRED:CPILFESL",
        } if paired else None,
        "weights_pct": weights,
        "trend": {
            "mom_avg_3m_pct": avg_3m_mom,
            "mom_avg_6m_pct": avg_6m_mom,
        },
        "available_sources": [
            {"code": c, "name": _SOURCES[c].name}
            for c in sources_used if c in _SOURCES
        ],
    }

    allowed_inputs = [
        actual, prior, mom, expected_mom, expected_yoy, surprise_mom_pp,
        core_actual, core_prior, core_mom, core_expected_mom,
        avg_3m_mom, avg_6m_mom,
        weights["shelter"], weights["energy"],
        weights["services_ex_shelter"], weights["food"],
    ]
    allowed = build_allowed_numbers([v for v in allowed_inputs if v is not None])
    allowed_codes = {s["code"] for s in llm_input["available_sources"]}
    return llm_input, allowed, allowed_codes


# ---------- NFP prompt builder ----------

def _nfp_payload(payload: dict) -> tuple[dict, set[Decimal], set[str]]:
    """NFP-specific input shape:
    - actual_value/prior_value = total nonfarm payrolls in thousands
    - change_k = actual - prior  (THIS is the "headline number" people see)
    - paired UNRATE actual/prior
    - expected_mom_pct (admin populated, K beklentisi anlamında kullanılır)
    """
    actual = float(payload["actual_value"]) if payload.get("actual_value") is not None else None
    prior = float(payload["prior_value"]) if payload.get("prior_value") is not None else None
    change_k = round(actual - prior, 1) if (actual is not None and prior is not None) else None
    expected_change_k = (
        float(payload["expected_mom_pct"])
        if payload.get("expected_mom_pct") is not None else None
    )
    surprise_k = (
        round(change_k - expected_change_k, 1)
        if change_k is not None and expected_change_k is not None else None
    )

    paired = payload.get("paired") or {}
    unrate_actual = float(paired.get("actual_value")) if paired.get("actual_value") is not None else None
    unrate_prior = float(paired.get("prior_value")) if paired.get("prior_value") is not None else None
    unrate_delta_pp = (
        round(unrate_actual - unrate_prior, 2)
        if unrate_actual is not None and unrate_prior is not None else None
    )

    history = payload.get("history") or []
    # NFP için 3-ay rolling = son 3 ayın change_k ortalaması
    changes = []
    for h in history[:6]:
        a = h.get("actual"); p = h.get("prior")
        if a is not None and p is not None:
            changes.append(round(a - p, 1))
    avg_3m_change_k = round(sum(changes[:3]) / 3, 1) if len(changes) >= 3 else None
    avg_6m_change_k = round(sum(changes[:6]) / 6, 1) if len(changes) >= 6 else None

    sources_used = ["FRED:PAYEMS", "BLS"]
    if paired:
        sources_used.append("FRED:UNRATE")

    llm_input = {
        "release_date": _format_tr_date(payload.get("released_at")),
        "country": payload.get("country"),
        "headline_nfp": {
            # NOT: total_payrolls level (158545) hikayeye girmemeli — sayı çok
            # büyük ve model onu "158 bin" gibi yuvarlayıp hata yapıyor. Anlam
            # taşıyan değişim rakamı `change_k`, asıl manşet o.
            "change_k": change_k,
            "expected_change_k": expected_change_k,
            "surprise_k": surprise_k,
            "source_code": "FRED:PAYEMS",
        },
        "unemployment_rate": {
            "actual_pct": unrate_actual,
            "prior_pct": unrate_prior,
            "delta_pp": unrate_delta_pp,
            "source_code": "FRED:UNRATE",
        } if paired else None,
        "trend": {
            "avg_3m_change_k": avg_3m_change_k,
            "avg_6m_change_k": avg_6m_change_k,
        },
        "available_sources": [
            {"code": c, "name": _SOURCES[c].name}
            for c in sources_used if c in _SOURCES
        ],
    }

    # NOT: actual/prior (158545/158637) whitelist'e girmiyor — hikayede o
    # absolute level rakamı yer almamalı, sadece change_k.
    allowed_inputs = [
        change_k, expected_change_k, surprise_k,
        unrate_actual, unrate_prior, unrate_delta_pp,
        avg_3m_change_k, avg_6m_change_k,
    ]
    allowed = build_allowed_numbers([v for v in allowed_inputs if v is not None])
    allowed_codes = {s["code"] for s in llm_input["available_sources"]}
    return llm_input, allowed, allowed_codes


def _nfp_prompt(llm_input: dict, tier: Tier) -> str:
    if tier == "premium":
        word_min, word_max = 150, 500
        sections = (
            "(1) Manşet rakam (change_k) vs beklenti (expected_change_k) — "
            "sürprizi kelime ile anlat ('beklentinin üzerinde/altında geldi').\n"
            "(2) İşsizlik oranı (unemployment_rate) — yön ve Fed için anlamı.\n"
            "(3) 3-aylık trend — tek ay yanıltıcı, momentum okuma.\n"
            "(4) Fed kararına etki — istihdam soğursa indirim baskısı.\n"
            "(5) Aklında tut + 'Senin için 1-cümle' (BTC veya portföy).\n"
            "NOT: Sektörel kırılım Faz 2'ye saklı — BLS Tablo B-1 detayını "
            "henüz çekmiyoruz. 'Sektörel kırılım için BLS'ye bak' türü atıf "
            "yapma — INPUT'ta yok diye yazma."
        )
    else:  # advance
        word_min, word_max = 340, 680
        sections = (
            "(1) Manşet (change_k) vs beklenti + tarihsel bağlam ('son X ayın "
            "en kötüsü' / 'en iyisi' INPUT history'sine bakarak).\n"
            "(2) İşsizlik oranı detayı + Powell yumuşatma eşiği.\n"
            "(3) 3-ay ve 6-ay rolling — momentum trendi.\n"
            "(4) Fed kararına etki + 'çift kötü/iyi veri' tezi (revizyon "
            "kavramına atıf yapabilirsin ama somut revizyon rakamı yazma — "
            "INPUT'ta yok).\n"
            "(5) Risk dengesi — enerji vs istihdam yönü Fed için çelişki "
            "yaratıyorsa adlandır.\n"
            "(6) Piyasaya etki (DXY/BTC/altın yönü — INPUT'tan sayı yazma, "
            "yön anlat).\n"
            "(7) Aklında tut + 'Senin için 1-cümle'."
        )

    available_codes = [s["code"] for s in llm_input["available_sources"]]

    return (
        "Sen makro analist hikaye anlatıcısısın. Aşağıdaki JSON NFP release "
        "verisini oku ve **sadece geçerli bir JSON** döndür.\n\n"
        f"INPUT:\n{json.dumps(llm_input, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI:\n"
        "{\n"
        '  "story_md": "(string — aşağıdaki bölümleri içermeli)"\n'
        "}\n\n"
        f"BÖLÜMLER (sırayla):\n{sections}\n\n"
        f"KELİME SAYISI: {word_min}-{word_max} aralığında.\n\n"
        "MUTLAK KURALLAR (ihlal = retry):\n"
        "1. Her sayı INPUT JSON'da geçen bir değer olmalı. Toplama/çıkarma "
        "yapma — change_k, surprise_k, avg_3m_change_k, unrate_delta_pp "
        "zaten hazır.\n"
        "2. HER sayının 60 karakter içinde [KAYNAK_KODU] olmalı. "
        f"Geçerli kodlar: {available_codes}. Örnek: '92 bin [FRED:PAYEMS]' "
        "veya 'işsizlik %4.4 [FRED:UNRATE]'.\n"
        "3. Politik yorum YASAK (Cumhuriyetçi/Demokrat/parti/seçim adlandırma).\n"
        "4. Mutlaklık YASAK ('kesin', 'garanti', 'asla').\n"
        "5. Yatırım tavsiyesi YASAK ('şimdi al/sat').\n"
        "6. Tarih damgası ŞART (ay adı veya yıl veya UTC).\n"
        "7. Hikaye anlatır gibi yaz — 'çift kötü/iyi veri' tezi, 'Powell "
        "çelişkide' karakteri, 'aklında tut' mental modeli kullan.\n"
        "8. 'beklenti' SADECE expected_change_k dolu ise. None ise yazma.\n"
        "9. Sayı birimleri NFP için 'bin' (K) — '92 bin istihdam' veya "
        "'92K' yaz, '92.000' yazma (INPUT'ta 92, kullanıcıya bin).\n"
        "10. SIFIR DELTA: change_k, surprise_k veya unrate_delta_pp tam 0 "
        "ise 'sabit kaldı' / 'değişmedi' diye anlat; '0.0 puan değişim' gibi "
        "rakamlı çift-bildirim YAPMA.\n"
        "11. TARİH: paragrafta sadece INPUT'taki `release_date` field'ında "
        "yazıldığı gibi kullan ('1 Nisan 2026'). ISO formatı veya köşeli "
        "parantezli timestamp YAZMA.\n"
        "12. Çıktı sadece JSON; satır sonları için \\n.\n"
    )


def _cpi_prompt(llm_input: dict, tier: Tier) -> str:
    if tier == "premium":
        word_min, word_max = 150, 500
        sections = (
            "(1) Manşet vs beklenti — sürpriz büyüklüğü kelime ile anlatılır "
            "(z-score yazma).\n"
            "(2) Çekirdek + 'süper çekirdek' kavramı — Fed neden buraya bakar?\n"
            "(3) Ağırlıklar (barınma %35 vs enerji %6.5) → manşeti enerji "
            "tetikler, asıl tabloyu barınma söyler.\n"
            "(4) Fed kararına etkisi — INPUT'taki sayılarla, yorumla.\n"
            "(5) Aklında tut + 'Senin için 1-cümle' (BTC veya portföy)."
        )
    else:  # advance
        word_min, word_max = 340, 680
        sections = (
            "(1) Manşet vs beklenti + tarihsel bağlam.\n"
            "(2) Çekirdek + süper çekirdek detayı.\n"
            "(3) Ağırlıklar + sektörel önem.\n"
            "(4) 3-aylık ve 6-aylık MoM trend → momentum okuma.\n"
            "(5) Fed kararına etki + senaryo (örn. 'enerji %20 artarsa "
            "manşete katkısı %20 × %6.5 = %1.3').\n"
            "(6) Risk dengesi (enerji vs istihdam yönü).\n"
            "(7) Aklında tut + 'Senin için 1-cümle'."
        )

    available_codes = [s["code"] for s in llm_input["available_sources"]]

    return (
        "Sen makro analist hikaye anlatıcısısın. Aşağıdaki JSON release verisini "
        "oku ve **sadece geçerli bir JSON** döndür. Hiçbir açıklama, markdown "
        "başlık veya kod bloğu yazma.\n\n"
        f"INPUT:\n{json.dumps(llm_input, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI:\n"
        "{\n"
        '  "story_md": "(string — aşağıdaki bölümleri içermeli)"\n'
        "}\n\n"
        f"BÖLÜMLER (sırayla):\n{sections}\n\n"
        f"KELİME SAYISI: {word_min}-{word_max} aralığında.\n\n"
        "MUTLAK KURALLAR (ihlal = retry; yumuşatma yok):\n"
        "1. Her sayı INPUT JSON'da geçen bir değer olmalı. Yeni rakam üretme, "
        "aritmetik yapma — hesaplanmışı INPUT'ta hazır (surprise_mom_pp, "
        "mom_avg_3m_pct, weights_pct vb).\n"
        "2. HER sayının 60 karakter içinde [KAYNAK_KODU] etiketi olmalı. "
        f"Geçerli kodlar: {available_codes}. Örnek: 'aylık %0.3 [FRED:CPIAUCSL]' "
        "veya 'barınma %35 [BLS]'. Etiketsiz sayı = retry.\n"
        "3. Politik yorum YASAK (Cumhuriyetçi/Demokrat/AKP/CHP/seçim/parti).\n"
        "4. Mutlaklık YASAK ('kesin', 'garanti', 'asla', '100% kesin').\n"
        "5. Yatırım tavsiyesi YASAK ('şimdi al', 'şimdi sat'). 'Senin için' "
        "bölümü olasılık/yön dili kullanır ('pozitif olabilir', 'baskı altında').\n"
        "6. Tarih damgası ŞART: en az bir ay adı (Ocak/Şubat/.../Aralık) veya "
        "yıl (2025/2026) veya 'UTC' geçmeli.\n"
        "7. Hikaye anlatır gibi yaz — soru-cevap (\"Peki neden bu önemli?\"), "
        "metafor (\"Powell çelişkide\"), mental model (\"manşet yön gösterir, "
        "süper çekirdek tabloyu söyler\") kullan. Yorumcu tarzı + akılda "
        "kalıcılık önemli.\n"
        "8. 'beklenti' kelimesini SADECE expected_*_pct dolu ise kullan. "
        "None ise beklenti yazma.\n"
        "9. Emoji bölüm başlıklarında olabilir (📊🔬⚖️🎯💡) — paragraf içinde "
        "abartma.\n"
        "10. SIFIR DELTA: mom_pct veya surprise_mom_pp tam 0 ise 'değişmedi' "
        "diye anlat, rakamı verme.\n"
        "11. TARİH: paragrafta INPUT'taki `release_date` field'ında yazılan "
        "formatı kullan ('1 Mart 2026'). ISO formatı veya köşeli parantezli "
        "timestamp YAZMA.\n"
        "12. Çıktı sadece JSON — story_md tek string, satır sonları için \\n.\n"
    )


# ---------- Gemini ----------

async def _call_gemini(prompt: str, *, max_tokens: int = 8192) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("GEMINI_API_KEY missing for storyteller")
        return None
    url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"storyteller gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"storyteller gemini call failed: {e}")
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        return None
    raw = (
        candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
    )
    if not raw:
        return None
    fence = re.search(r"\{[\s\S]*\}", raw)
    if not fence:
        return None
    try:
        return json.loads(fence.group(0))
    except json.JSONDecodeError:
        return None


# ---------- Persist ----------

async def _persist_story(
    event_id: str, tier: str, story_md: str, meta: dict,
) -> bool:
    sql = text("""
        INSERT INTO macro_stories (event_id, tier, story_md, meta, generated_at)
        VALUES (:eid, :tier, :md, CAST(:meta AS JSONB), NOW())
        ON CONFLICT (event_id, tier) DO UPDATE
        SET story_md = EXCLUDED.story_md,
            meta = EXCLUDED.meta,
            generated_at = NOW()
    """)
    async with engine.begin() as conn:
        result = await conn.execute(sql, {
            "eid": event_id, "tier": tier,
            "md": story_md, "meta": json.dumps(meta, default=str),
        })
    return (result.rowcount or 0) > 0


# ---------- Public entry ----------

@dataclass
class StoryResult:
    event_id: str
    tier: str
    written: bool = False
    rejection_reason: Optional[str] = None
    story_md: Optional[str] = None
    validator: Optional[ValidatorReport] = None
    sources_cited: list[str] = field(default_factory=list)


_SUPPORTED_EVENT_TYPES = frozenset({"CPI", "NFP"})

_DECODER_DISPATCH = {
    "CPI": (_cpi_payload, _cpi_prompt),
    "NFP": (_nfp_payload, _nfp_prompt),
}


async def generate_story(
    event_id: str, tier: Tier, *, force: bool = False,
) -> StoryResult:
    """Generate + persist one tiered story for a release.

    Idempotent unless `force=True`. Returns StoryResult with diagnostics.
    """
    result = StoryResult(event_id=event_id, tier=tier)

    if tier not in ("premium", "advance"):
        result.rejection_reason = f"unknown tier: {tier}"
        return result

    if not force:
        async with engine.begin() as conn:
            existing = (await conn.execute(
                text("SELECT 1 FROM macro_stories WHERE event_id=:eid AND tier=:tier"),
                {"eid": event_id, "tier": tier},
            )).first()
        if existing:
            result.rejection_reason = "already exists (use force=True to regen)"
            return result

    payload = await _load_event_payload(event_id)
    if not payload:
        result.rejection_reason = "release not found"
        return result

    et = (payload.get("event_type") or "").upper()
    if et not in _SUPPORTED_EVENT_TYPES:
        result.rejection_reason = (
            f"event_type {et} not yet supported by storyteller "
            f"(currently CPI, NFP)"
        )
        return result

    payload_fn, prompt_fn = _DECODER_DISPATCH[et]
    llm_input, allowed, allowed_codes = payload_fn(payload)
    prompt = prompt_fn(llm_input, tier)

    word_bounds = (150, 500) if tier == "premium" else (340, 680)

    llm_out = await _call_gemini(prompt)
    if not llm_out or not (llm_out.get("story_md") or "").strip():
        result.rejection_reason = "gemini returned empty"
        return result

    story_md = llm_out["story_md"].strip()
    rep = _validate_story(
        story_md, allowed, allowed_codes,
        min_words=word_bounds[0], max_words=word_bounds[1],
    )
    result.validator = rep

    if not rep.ok:
        logger.warning(
            f"storyteller validator reject (try1) {event_id}/{tier}: "
            f"nums={rep.unknown_numbers[:5]} "
            f"missing_cite={rep.numbers_without_citation[:5]} "
            f"banned={rep.banned_phrases_found} "
            f"temporal={rep.missing_temporal_marker} "
            f"wc={rep.word_count} bounds={word_bounds}"
        )
        retry_prompt = prompt + (
            "\n\n!! ÖNCEKİ DENEME REDDEDİLDİ. Sorunlar:\n"
            f"- unknown_numbers: {rep.unknown_numbers}\n"
            f"- numbers_without_citation: {rep.numbers_without_citation}\n"
            f"- banned_phrases: {rep.banned_phrases_found}\n"
            f"- missing_temporal_marker: {rep.missing_temporal_marker}\n"
            f"- word_count: {rep.word_count} (target {word_bounds[0]}-{word_bounds[1]})\n"
            f"- unknown_sources: {rep.unknown_sources}\n"
            "Sadece bu sorunları düzelt ve JSON döndür."
        )
        llm_out = await _call_gemini(retry_prompt)
        if not llm_out or not (llm_out.get("story_md") or "").strip():
            result.rejection_reason = "validator failed (try1) + retry empty"
            return result
        story_md = llm_out["story_md"].strip()
        rep = _validate_story(
            story_md, allowed, allowed_codes,
            min_words=word_bounds[0], max_words=word_bounds[1],
        )
        result.validator = rep
        if not rep.ok:
            result.rejection_reason = (
                f"validator still failed after retry: "
                f"nums={rep.unknown_numbers[:3]} "
                f"missing_cite={rep.numbers_without_citation[:3]} "
                f"banned={rep.banned_phrases_found}"
            )
            return result

    meta = {
        "tier": tier,
        "event_type": et,
        "model": GEMINI_MODEL,
        "validator": {
            "word_count": rep.word_count,
            "sources_cited": rep.sources_cited,
        },
        "sources_registry": {
            s: {"name": _SOURCES[s].name, "url": _SOURCES[s].url}
            for s in rep.sources_cited if s in _SOURCES
        },
    }

    written = await _persist_story(event_id, tier, story_md, meta)
    result.written = written
    result.story_md = story_md
    result.sources_cited = rep.sources_cited
    return result


async def generate_story_safe(
    event_id: str, tier: Tier, *, force: bool = False,
) -> None:
    """Fire-and-forget wrapper — never raises."""
    try:
        await generate_story(event_id, tier, force=force)
    except Exception as e:
        logger.error(f"generate_story crashed for {event_id}/{tier}: {e}")
