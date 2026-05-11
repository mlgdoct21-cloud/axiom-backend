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
    # NFP supersector alt-serileri (Faz 3 sektörel kırılım)
    "FRED:USEHS": SourceCitation(
        "FRED:USEHS", "FRED Private Education & Health Services",
        "https://fred.stlouisfed.org/series/USEHS",
    ),
    "FRED:USGOVT": SourceCitation(
        "FRED:USGOVT", "FRED Government Employment",
        "https://fred.stlouisfed.org/series/USGOVT",
    ),
    "FRED:USPBS": SourceCitation(
        "FRED:USPBS", "FRED Professional & Business Services",
        "https://fred.stlouisfed.org/series/USPBS",
    ),
    "FRED:USLAH": SourceCitation(
        "FRED:USLAH", "FRED Leisure & Hospitality",
        "https://fred.stlouisfed.org/series/USLAH",
    ),
    "FRED:MANEMP": SourceCitation(
        "FRED:MANEMP", "FRED Manufacturing Employment",
        "https://fred.stlouisfed.org/series/MANEMP",
    ),
    "FRED:USCONS": SourceCitation(
        "FRED:USCONS", "FRED Construction Employment",
        "https://fred.stlouisfed.org/series/USCONS",
    ),
    "FRED:USTPU": SourceCitation(
        "FRED:USTPU", "FRED Trade, Transportation & Utilities",
        "https://fred.stlouisfed.org/series/USTPU",
    ),
    "FRED:USINFO": SourceCitation(
        "FRED:USINFO", "FRED Information Sector",
        "https://fred.stlouisfed.org/series/USINFO",
    ),
    "FRED:PCEPI": SourceCitation(
        "FRED:PCEPI", "FRED Headline PCE Price Index",
        "https://fred.stlouisfed.org/series/PCEPI",
    ),
    "FRED:PCEPILFE": SourceCitation(
        "FRED:PCEPILFE", "FRED Core PCE Price Index",
        "https://fred.stlouisfed.org/series/PCEPILFE",
    ),
    "FRED:PPIFIS": SourceCitation(
        "FRED:PPIFIS", "FRED PPI Final Demand",
        "https://fred.stlouisfed.org/series/PPIFIS",
    ),
    "FRED:WPSFD49116": SourceCitation(
        "FRED:WPSFD49116", "FRED Core PPI Final Demand (Less Foods & Energy)",
        "https://fred.stlouisfed.org/series/WPSFD49116",
    ),
    # Legacy basket — eski PPIACO citation'larının resolve olabilmesi için tutuldu.
    # Yeni stories PPIFIS + WPSFD49116 emit eder (basket switch 2026-05-11).
    "FRED:PPIACO": SourceCitation(
        "FRED:PPIACO", "FRED Producer Price Index (All Commodities, legacy)",
        "https://fred.stlouisfed.org/series/PPIACO",
    ),
    # FOMC decoder (Faz 3) — fed funds target range + statement
    "FRED:DFEDTARU": SourceCitation(
        "FRED:DFEDTARU", "FRED Fed Funds Target Range Upper",
        "https://fred.stlouisfed.org/series/DFEDTARU",
    ),
    "FRED:DFEDTARL": SourceCitation(
        "FRED:DFEDTARL", "FRED Fed Funds Target Range Lower",
        "https://fred.stlouisfed.org/series/DFEDTARL",
    ),
    "FED:STATEMENT": SourceCitation(
        "FED:STATEMENT", "Federal Reserve FOMC Statement",
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    ),
    "FED:SEP": SourceCitation(
        "FED:SEP", "Fed Summary of Economic Projections (Dot Plot)",
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
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
_BRACKET_RANGE_RE = re.compile(r"\[[^\]]*\]")  # citation chip aralığı (içindeki rakamlar exempt)
_NUM_TOKEN_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?%?")
_CITATION_WINDOW = 60

_DATE_MARKER_RE = re.compile(
    r"\b(?:20\d\d|Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|"
    r"Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık|UTC)\b",
    re.IGNORECASE,
)

# "29 Nisan", "18 Mart 2026" tarzı tarih ifadelerinde gün numarası — citation
# chip'i istemez (calendar token, data değil). Sayıdan sonra boşluk + TR ay
# adı geliyorsa, exempt.
_DAY_DATE_RE = re.compile(
    r"^\s+(?:Ocak|Şubat|Mart|Nisan|Mayıs|Haziran|"
    r"Temmuz|Ağustos|Eylül|Ekim|Kasım|Aralık)\b",
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

    # Citation chip aralıkları — `[FRED:WPSFD49116]` gibi kodların İÇİNDEKİ
    # rakamlar number-validation'dan exempt (chip içerik bağlam değil, etiket).
    bracket_ranges = [(m.start(), m.end()) for m in _BRACKET_RANGE_RE.finditer(story_md)]

    def _in_citation(pos: int) -> bool:
        return any(s <= pos < e for s, e in bracket_ranges)

    # L1 için stripped versiyon — sayı arama sırasında bracket içeriği skip
    story_md_for_nums = _BRACKET_RANGE_RE.sub("", story_md)

    unk_all = validate_numbers(
        story_md_for_nums, allowed_numbers, tolerance=Decimal("0.05"),
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
        if _in_citation(m.start()):
            continue
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
        # Tarih günü (1-31, "29 Nisan 2026" veya "26'sındaki") — citation
        # chip istemez. Gün numarası proximate'inde (±25 char) TR ay adı
        # veya yıl literali varsa exempt.
        if is_int and "%" not in m.group(0) and Decimal("1") <= d <= Decimal("31"):
            ctx = story_md[max(0, m.start() - 25): m.end() + 25]
            if _DATE_MARKER_RE.search(ctx):
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
        pair_map = {"CPI": "CORE_CPI", "PCE": "CORE_PCE", "PPI": "CORE_PPI", "NFP": "UNRATE"}
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
        # NFP sektör alt-serileri (Faz 3 BLS B-1 eşdeğeri) — aynı released_at'te
        # 8 supersector change_k hesaplanır, prompt'a inject edilir.
        payload["sectors"] = []
        if et == "NFP" and row["released_at"]:
            ssql = text("""
                SELECT event_type, actual_value, prior_value
                FROM macro_releases
                WHERE event_type IN (
                    'NFP_HEALTH', 'NFP_GOVT', 'NFP_PROF', 'NFP_LEISURE',
                    'NFP_MFG', 'NFP_CONST', 'NFP_TPU', 'NFP_INFO'
                )
                AND source = :src AND released_at = :ts
            """)
            srows = (await conn.execute(ssql, {
                "src": row["source"], "ts": row["released_at"],
            })).mappings().all()
            payload["sectors"] = [
                {k: r[k] for k in r.keys()} for r in srows
            ]
        # FOMC_STATEMENT — fed funds target range latest + prior decision.
        # DFEDTARU/L günlük seri; çoğu gün değer aynı. "Prior decision" =
        # actual_value değiştiği son tarih (FOMC decision day).
        payload["fed_funds"] = None
        payload["sep"] = None
        if et == "FOMC_STATEMENT" and row["released_at"]:
            ts = row["released_at"]
            ff_sql = text("""
                SELECT actual_value, released_at FROM macro_releases
                WHERE event_type = :et AND released_at <= :ts
                ORDER BY released_at DESC LIMIT 1
            """)
            up_row = (await conn.execute(ff_sql, {
                "et": "FED_FUNDS_UPPER", "ts": ts,
            })).mappings().first()
            lo_row = (await conn.execute(ff_sql, {
                "et": "FED_FUNDS_LOWER", "ts": ts,
            })).mappings().first()
            prior_up_row = None
            prior_lo_row = None
            if up_row:
                prior_sql = text("""
                    SELECT actual_value, released_at FROM macro_releases
                    WHERE event_type = :et AND actual_value != :cur
                      AND released_at < :ts
                    ORDER BY released_at DESC LIMIT 1
                """)
                prior_up_row = (await conn.execute(prior_sql, {
                    "et": "FED_FUNDS_UPPER",
                    "cur": up_row["actual_value"], "ts": ts,
                })).mappings().first()
            if lo_row:
                prior_sql = text("""
                    SELECT actual_value, released_at FROM macro_releases
                    WHERE event_type = :et AND actual_value != :cur
                      AND released_at < :ts
                    ORDER BY released_at DESC LIMIT 1
                """)
                prior_lo_row = (await conn.execute(prior_sql, {
                    "et": "FED_FUNDS_LOWER",
                    "cur": lo_row["actual_value"], "ts": ts,
                })).mappings().first()
            # SEP (FAZ B) — same FOMC day veya ±2 gün içinde Fed projection
            # medians varsa current + prior SEP'i çek (dot plot şift için).
            # event_id format: 'sep:FUNDS_END_0:<YYYY-MM-DD>' (current year),
            # 'sep:FUNDS_END_1:<...>' (year+1), 'sep:FUNDS_END_2:<...>'
            # (year+2), 'sep:FUNDS_LONGER_RUN:<...>'.
            sep_keys = ("SEP_FUNDS_END_0", "SEP_FUNDS_END_1",
                        "SEP_FUNDS_END_2", "SEP_FUNDS_LONGER_RUN")
            sep_current = {}
            sep_sql = text("""
                SELECT event_type, actual_value FROM macro_releases
                WHERE event_type = :et
                  AND released_at BETWEEN :ts - interval '2 days'
                                      AND :ts + interval '2 days'
                ORDER BY released_at DESC LIMIT 1
            """)
            for k in sep_keys:
                r = (await conn.execute(sep_sql, {
                    "et": k, "ts": ts,
                })).mappings().first()
                if r and r["actual_value"] is not None:
                    sep_current[k] = float(r["actual_value"])
            sep_prior = {}
            sep_prior_date = None
            if sep_current:
                # Önceki SEP (en az 60 gün önce — SEP 3-aylık)
                prior_sep_sql = text("""
                    SELECT released_at FROM macro_releases
                    WHERE event_type = 'SEP_FUNDS_END_0'
                      AND released_at < :ts - interval '60 days'
                    ORDER BY released_at DESC LIMIT 1
                """)
                p = (await conn.execute(prior_sep_sql, {"ts": ts})).mappings().first()
                if p and p.get("released_at"):
                    prior_ts = p["released_at"]
                    sep_prior_date = prior_ts.isoformat()
                    prior_sep_get_sql = text("""
                        SELECT event_type, actual_value FROM macro_releases
                        WHERE event_type = :et
                          AND released_at BETWEEN :ts - interval '2 days'
                                              AND :ts + interval '2 days'
                        ORDER BY released_at DESC LIMIT 1
                    """)
                    for k in sep_keys:
                        r = (await conn.execute(prior_sep_get_sql, {
                            "et": k, "ts": prior_ts,
                        })).mappings().first()
                        if r and r["actual_value"] is not None:
                            sep_prior[k] = float(r["actual_value"])
            payload["sep"] = {
                "current": sep_current or None,
                "prior": sep_prior or None,
                "prior_date_iso": sep_prior_date,
            } if sep_current else None
            payload["fed_funds"] = {
                "current_upper": float(up_row["actual_value"]) if up_row else None,
                "current_lower": float(lo_row["actual_value"]) if lo_row else None,
                "prior_upper": float(prior_up_row["actual_value"]) if prior_up_row else None,
                "prior_lower": float(prior_lo_row["actual_value"]) if prior_lo_row else None,
                "prior_decision_date": (
                    prior_up_row["released_at"].isoformat()
                    if prior_up_row and prior_up_row.get("released_at") else None
                ),
            }
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
    unrate_delta_pp_raw = (
        round(unrate_actual - unrate_prior, 2)
        if unrate_actual is not None and unrate_prior is not None else None
    )
    # Delta sıfırsa rakamı tamamen payload'dan çıkar; LLM "0.0 puan değişim"
    # diye çift-bildirim yapmasın diye fiziksel olarak yazamayacak.
    unrate_delta_pp = unrate_delta_pp_raw if (unrate_delta_pp_raw not in (None, 0, 0.0)) else None
    unrate_change_label = "sabit" if unrate_delta_pp_raw == 0 else None

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

    # Sektörel kırılım (Faz 3 BLS B-1 eşdeğeri) — 8 supersector change_k
    _SECTOR_META = {
        "NFP_HEALTH":  ("Sağlık ve Eğitim", "FRED:USEHS"),
        "NFP_GOVT":    ("Hükümet",          "FRED:USGOVT"),
        "NFP_PROF":    ("Profesyonel ve İş Hizmetleri", "FRED:USPBS"),
        "NFP_LEISURE": ("Eğlence ve Konaklama", "FRED:USLAH"),
        "NFP_MFG":     ("İmalat",           "FRED:MANEMP"),
        "NFP_CONST":   ("İnşaat",           "FRED:USCONS"),
        "NFP_TPU":     ("Ticaret-Ulaşım-Hizmet", "FRED:USTPU"),
        "NFP_INFO":    ("Bilgi/Tek",        "FRED:USINFO"),
    }
    sector_rows = payload.get("sectors") or []
    sectors_input = []
    sector_change_ks = []
    for row in sector_rows:
        et = row.get("event_type")
        meta = _SECTOR_META.get(et)
        if not meta:
            continue
        s_actual = float(row["actual_value"]) if row.get("actual_value") is not None else None
        s_prior = float(row["prior_value"]) if row.get("prior_value") is not None else None
        s_change_k = round(s_actual - s_prior, 1) if (s_actual is not None and s_prior is not None) else None
        if s_change_k is None:
            continue
        sectors_input.append({
            "label_tr": meta[0],
            "change_k": s_change_k,
            "source_code": meta[1],
        })
        sector_change_ks.append(s_change_k)
        if meta[1] not in sources_used:
            sources_used.append(meta[1])
    # En etkileyici sektörleri öne al — abs(change_k) DESC. LLM en önemli
    # 4-6 sektörü doğal olarak kullanır.
    sectors_input.sort(key=lambda s: abs(s["change_k"]), reverse=True)

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
            "change_label": unrate_change_label,  # delta=0 ise "sabit"
            "source_code": "FRED:UNRATE",
        } if paired else None,
        "trend": {
            "avg_3m_change_k": avg_3m_change_k,
            "avg_6m_change_k": avg_6m_change_k,
        },
        "sectors": sectors_input if sectors_input else None,
        "available_sources": [
            {"code": c, "name": _SOURCES[c].name}
            for c in sources_used if c in _SOURCES
        ],
    }

    # NOT: actual/prior (158545/158637) whitelist'e girmiyor — hikayede o
    # absolute level rakamı yer almamalı, sadece change_k.
    # unrate_delta_pp=0 da whitelist'e girmiyor (0 zaten sentinel olarak
    # otomatik ekleniyor + change_label='sabit' anlatım yapacak).
    allowed_inputs = [
        change_k, expected_change_k, surprise_k,
        unrate_actual, unrate_prior, unrate_delta_pp,
        avg_3m_change_k, avg_6m_change_k,
        *sector_change_ks,
    ]
    # NFP'de negative change_k (örn -13K) LLM tarafından "13 bin daralma" diye
    # yazılıyor (pozitif rakam + yön kelimesi). Validator için iki tarafı da
    # whitelist'e ekle — sign-flip exempt.
    abs_changes = [
        abs(v) for v in [change_k, surprise_k, avg_3m_change_k, avg_6m_change_k, *sector_change_ks]
        if v is not None and v < 0
    ]
    allowed_inputs.extend(abs_changes)
    allowed = build_allowed_numbers([v for v in allowed_inputs if v is not None])
    allowed_codes = {s["code"] for s in llm_input["available_sources"]}
    return llm_input, allowed, allowed_codes


def _nfp_prompt(llm_input: dict, tier: Tier) -> str:
    has_sectors = bool(llm_input.get("sectors"))
    if tier == "premium":
        word_min, word_max = 150, 500
        sections = (
            "(1) Manşet rakam (change_k) vs beklenti (expected_change_k) — "
            "sürprizi kelime ile anlat ('beklentinin üzerinde/altında geldi').\n"
            "(2) İşsizlik oranı (unemployment_rate) — yön ve Fed için anlamı.\n"
            "(3) 3-aylık trend — tek ay yanıltıcı, momentum okuma.\n"
            "(4) Fed kararına etki — istihdam soğursa indirim baskısı.\n"
            "(5) Aklında tut + 'Senin için 1-cümle' (BTC veya portföy)."
        )
    else:  # advance
        word_min, word_max = 340, 680
        if has_sectors:
            sections = (
                "(1) Manşet (change_k) vs beklenti + tarihsel bağlam ('son X "
                "ayın en kötüsü' / 'en iyisi' INPUT history'sine bakarak).\n"
                "(2) İşsizlik oranı detayı + Powell yumuşatma eşiği.\n"
                "(3) 3-ay ve 6-ay rolling — momentum trendi.\n"
                "(4) **Sektörel kırılım** — INPUT `sectors` listesinden EN AZ "
                "3 sektörü adlandır + change_k rakamlarını [KAYNAK] etiketiyle "
                "yaz. En etkileyici (pozitif veya negatif) sektörler listede "
                "ÖNCE geliyor. Bu manşet rakamın altındaki gerçek hikaye: "
                "hangi sektör çekti, hangi sektör fren oldu, kompozisyon "
                "kalitesi nasıl. Örnek anlatım: 'Sağlık ve Eğitim 50 bin "
                "[FRED:USEHS] istihdamla en güçlü itici güç olurken, İmalat "
                "12 bin [FRED:MANEMP] daralma ile bir kez daha fren rolünü "
                "üstlendi.' Kompozisyon kalitesi (defensif vs cyclical) "
                "hakkında 1-2 cümle değerlendirme ekle.\n"
                "(5) Fed kararına etki + 'çift kötü/iyi veri' tezi (revizyon "
                "kavramına atıf yapabilirsin ama somut revizyon rakamı yazma "
                "— INPUT'ta yok).\n"
                "(6) Risk dengesi — enerji vs istihdam yönü Fed için çelişki "
                "yaratıyorsa adlandır.\n"
                "(7) Piyasaya etki (DXY/BTC/altın yönü — INPUT'tan sayı "
                "yazma, yön anlat).\n"
                "(8) Aklında tut + 'Senin için 1-cümle'."
            )
        else:
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
        f"Geçerli kodlar: {available_codes}. Örnekler: '92 bin [FRED:PAYEMS]', "
        "'işsizlik %4.4 [FRED:UNRATE]', 'Sağlık ve Eğitim 50 bin [FRED:USEHS]', "
        "'İmalat 12 bin [FRED:MANEMP] daralma'. Her sektör için INPUT'ta "
        "belirtilen `source_code` zorunlu — başka kaynak yazma.\n"
        "3. Politik yorum YASAK (Cumhuriyetçi/Demokrat/parti/seçim adlandırma).\n"
        "4. Mutlaklık YASAK ('kesin', 'garanti', 'asla').\n"
        "5. Yatırım tavsiyesi YASAK ('şimdi al/sat').\n"
        "6. Tarih damgası ŞART (ay adı veya yıl veya UTC).\n"
        "7. Hikaye anlatır gibi yaz — 'çift kötü/iyi veri' tezi, 'Powell "
        "çelişkide' karakteri, 'aklında tut' mental modeli kullan.\n"
        "8. 'beklenti' SADECE expected_change_k dolu ise. None ise yazma.\n"
        "9. Sayı birimleri NFP için 'bin' (K) — '92 bin istihdam' veya "
        "'92K' yaz, '92.000' yazma (INPUT'ta 92, kullanıcıya bin).\n"
        "10. SIFIR DELTA: INPUT'ta `change_label: \"sabit\"` veya delta_pp "
        "null ise paragrafta '0' rakamını veya 'puan' birimini KESİNLİKLE "
        "YAZMA. Sadece 'sabit kaldı' / 'değişmedi' diye anlat. Örnek: "
        "'işsizlik %4.3 [FRED:UNRATE] seviyesinde sabit kaldı.' (devam yok).\n"
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


# ---------- PCE prompt builder ----------

def _pce_payload(payload: dict) -> tuple[dict, set[Decimal], set[str]]:
    """PCE = Fed'in tercih ettiği enflasyon. Yapısı CPI ile aynı (index level,
    paired CORE_PCE, MoM hesaplanır). Farkı: chain-weighted (substitution etkisi)
    + işveren-sağlanan sağlık dahil. Bu yüzden ayrı prompt sürümü var.
    Hesaplama tarafında CPI ağırlıklarına benzer şey yok — PCE ağırlıkları
    farklı, fake number göndermemek için weight payload'a koymuyoruz.
    """
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

    sources_used = ["FRED:PCEPI", "BLS"]
    if paired:
        sources_used.append("FRED:PCEPILFE")

    llm_input = {
        "release_date": _format_tr_date(payload.get("released_at")),
        "country": payload.get("country"),
        "headline_pce": {
            "actual_index": actual,
            "prior_index": prior,
            "mom_pct": mom,
            "expected_mom_pct": expected_mom,
            "expected_yoy_pct": expected_yoy,
            "surprise_mom_pp": surprise_mom_pp,
            "source_code": "FRED:PCEPI",
        },
        "core_pce": {
            "actual_index": core_actual,
            "prior_index": core_prior,
            "mom_pct": core_mom,
            "expected_mom_pct": core_expected_mom,
            "source_code": "FRED:PCEPILFE",
        } if paired else None,
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
    ]
    allowed = build_allowed_numbers([v for v in allowed_inputs if v is not None])
    allowed_codes = {s["code"] for s in llm_input["available_sources"]}
    return llm_input, allowed, allowed_codes


def _pce_prompt(llm_input: dict, tier: Tier) -> str:
    if tier == "premium":
        word_min, word_max = 150, 500
        sections = (
            "(1) Manşet PCE vs beklenti — sürpriz büyüklüğü kelime ile anlat.\n"
            "(2) Çekirdek PCE — Fed bunu CPI'dan neden daha çok takip eder "
            "(chain-weighted yani sepet değişikliğine adapte, işveren sağlık "
            "harcamaları dahil). Yalnızca KAVRAM olarak anlat, yeni rakam "
            "üretme.\n"
            "(3) 3-aylık MoM trend — tek ay yanıltıcı, momentum okuma.\n"
            "(4) Fed kararına etki — PCE 'Fed'in resmi hedefi' (%2 yıllık) "
            "olduğundan tepki CPI'dan daha net olur.\n"
            "(5) Aklında tut + 'Senin için 1-cümle' (BTC veya portföy)."
        )
    else:  # advance
        word_min, word_max = 340, 680
        sections = (
            "(1) Manşet vs beklenti + tarihsel bağlam ('son X ayın en yüksek/"
            "düşük MoM'u' INPUT history'ye bakarak).\n"
            "(2) Çekirdek PCE detayı + 'süper çekirdek hizmet' kavramı "
            "(barınma ex hariç hizmet enflasyonu — INPUT'ta yok ama kavram "
            "kullanılabilir, somut rakam yazma).\n"
            "(3) PCE vs CPI farkı — chain-weighting + sağlık kapsamı + "
            "sektörel ağırlık farkı (CPI barınma-ağır, PCE hizmet-ağır). "
            "Bu kısım kavramsal, sayı yazma.\n"
            "(4) 3-ay ve 6-ay rolling MoM — Fed'in 'durable disinflation' "
            "okuma çerçevesi (tek ay değil seri).\n"
            "(5) Fed kararına etki + CME FedWatch implikasyonu (yön anlat, "
            "INPUT'ta CME oranı yok diye sayı YAZMA).\n"
            "(6) Risk dengesi — enerji vs hizmet enflasyonu Fed için çelişki "
            "yaratıyorsa adlandır.\n"
            "(7) Aklında tut + 'Senin için 1-cümle' (DXY/BTC/altın yönü)."
        )

    available_codes = [s["code"] for s in llm_input["available_sources"]]

    return (
        "Sen makro analist hikaye anlatıcısısın. Aşağıdaki JSON PCE release "
        "verisini oku ve **sadece geçerli bir JSON** döndür.\n\n"
        f"INPUT:\n{json.dumps(llm_input, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI:\n"
        "{\n"
        '  "story_md": "(string — aşağıdaki bölümleri içermeli)"\n'
        "}\n\n"
        f"BÖLÜMLER (sırayla):\n{sections}\n\n"
        f"KELİME SAYISI: {word_min}-{word_max} aralığında.\n\n"
        "MUTLAK KURALLAR (ihlal = retry):\n"
        "1. Her sayı INPUT JSON'da geçen bir değer olmalı. Aritmetik yapma — "
        "mom_pct, surprise_mom_pp, avg_3m_mom_pct INPUT'ta hazır.\n"
        "2. HER sayının 60 karakter içinde [KAYNAK_KODU] olmalı — manşet, "
        "çekirdek, beklenti, sürpriz, TREND ortalamaları (3-ay, 6-ay) DAHİL. "
        f"Geçerli kodlar: {available_codes}. Örnekler: '%0.18 [FRED:PCEPI]', "
        "'çekirdek %0.32 [FRED:PCEPILFE]', 'son 3 ay ortalaması %0.35 "
        "[FRED:PCEPI]', '6-aylık ortalama %0.28 [FRED:PCEPI]'.\n"
        "3. Politik yorum YASAK (parti/seçim/Demokrat/Cumhuriyetçi).\n"
        "4. Mutlaklık YASAK ('kesin', 'garanti', 'asla').\n"
        "5. Yatırım tavsiyesi YASAK ('şimdi al/sat').\n"
        "6. Tarih damgası ŞART: ay adı veya yıl veya UTC geçmeli.\n"
        "7. Hikaye anlatır gibi yaz — 'Fed'in tercihi neden PCE' karakteri, "
        "'durable disinflation' mental modeli kullan.\n"
        "8. 'beklenti' SADECE expected_*_pct dolu ise yazılır. None ise yazma.\n"
        "9. SIFIR DELTA: mom_pct veya surprise_mom_pp tam 0 ise 'değişmedi' "
        "diye anlat, '0' rakamını yazma.\n"
        "10. TARİH: INPUT `release_date` formatını ('1 Mart 2026') aynen "
        "kullan; ISO formatı YAZMA.\n"
        "11. Çıktı sadece JSON; satır sonları için \\n.\n"
    )


# ---------- PPI prompt builder ----------

def _ppi_payload(payload: dict) -> tuple[dict, set[Decimal], set[str]]:
    """PPI = üretici fiyat endeksi. CPI'dan 1-3 ay önce hareket eder (pipeline
    basıncı). 2026-05-11 basket switch: headline FRED:PPIFIS (Final Demand SA,
    market-standard), core FRED:WPSFD49116 (Final Demand Less Foods & Energy
    SA). Paired core CPI/PCE pattern'iyle aynı.
    """
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

    sources_used = ["FRED:PPIFIS", "BLS"]
    if paired:
        sources_used.append("FRED:WPSFD49116")

    llm_input = {
        "release_date": _format_tr_date(payload.get("released_at")),
        "country": payload.get("country"),
        "headline_ppi": {
            "actual_index": actual,
            "prior_index": prior,
            "mom_pct": mom,
            "expected_mom_pct": expected_mom,
            "expected_yoy_pct": expected_yoy,
            "surprise_mom_pp": surprise_mom_pp,
            "source_code": "FRED:PPIFIS",
        },
        "core_ppi": {
            "actual_index": core_actual,
            "prior_index": core_prior,
            "mom_pct": core_mom,
            "expected_mom_pct": core_expected_mom,
            "source_code": "FRED:WPSFD49116",
        } if paired else None,
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
    ]
    allowed = build_allowed_numbers([v for v in allowed_inputs if v is not None])
    allowed_codes = {s["code"] for s in llm_input["available_sources"]}
    return llm_input, allowed, allowed_codes


def _ppi_prompt(llm_input: dict, tier: Tier) -> str:
    if tier == "premium":
        word_min, word_max = 150, 500
        sections = (
            "(1) Manşet PPI Final Demand vs beklenti — sürpriz büyüklüğü kelime "
            "ile anlat.\n"
            "(2) Çekirdek PPI (gıda + enerji hariç) — Fed bunu manşet PPI'dan "
            "neden daha çok takip eder (volatil emtia gürültüsü çıkarılmış, "
            "altta yatan üretici fiyat baskısını gösterir). KAVRAM olarak "
            "anlat, yeni rakam üretme.\n"
            "(3) PPI ne demek — üretici maliyeti, CPI'ya 1-3 ay önden hareket "
            "eden 'pipeline basıncı'. Kavram olarak anlat, fake lead-lag "
            "rakamı yazma.\n"
            "(4) Tüketiciye geçiş — PPI yukarı çıkıyorsa şirket marjı sıkışır "
            "veya fiyatı tüketiciye yansır. Hangi senaryo daha olası, INPUT "
            "verisine bakarak kelime ile anlat.\n"
            "(5) Aklında tut + 'Senin için 1-cümle'."
        )
    else:  # advance
        word_min, word_max = 340, 680
        sections = (
            "(1) Manşet vs beklenti + tarihsel bağlam (history'ye bakarak "
            "'son X ayın en yüksek/düşük MoM'u').\n"
            "(2) Çekirdek PPI detayı — gıda ve enerji çıkarılmış 'temiz' "
            "üretici fiyat sinyali; manşet ile divergence varsa adlandır "
            "(emtia-driven vs altta yatan demand-pull).\n"
            "(3) PPI lead-CPI ilişkisi detayı — 'pipeline basıncı' kavramı + "
            "marj sıkışması tezi (somut lead-lag rakamı YAZMA, INPUT'ta yok).\n"
            "(4) 3-ay ve 6-ay rolling — momentum trendi (yön + ivme).\n"
            "(5) Supply-side vs demand-side enflasyon ayrımı — manşet hızlı "
            "yukarı + çekirdek yumuşaksa supply-driven, ikisi birlikte "
            "yukarı ise demand-driven. Kavramsal anlat.\n"
            "(6) Fed kararına etki — Fed PPI'a direkt bakmaz ama 'core PCE'ye "
            "geçiş kanalı' okuyabilir. Çekirdek PPI'ın yönü kritik.\n"
            "(7) Risk dengesi — emtia + enerji PPI'ı tetikliyorsa hangi sektör "
            "marjı en çok sıkışır (kavramsal, sayı yok).\n"
            "(8) Aklında tut + 'Senin için 1-cümle' (DXY/BTC/altın yönü)."
        )

    available_codes = [s["code"] for s in llm_input["available_sources"]]

    return (
        "Sen makro analist hikaye anlatıcısısın. Aşağıdaki JSON PPI release "
        "verisini oku ve **sadece geçerli bir JSON** döndür.\n\n"
        f"INPUT:\n{json.dumps(llm_input, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI:\n"
        "{\n"
        '  "story_md": "(string — aşağıdaki bölümleri içermeli)"\n'
        "}\n\n"
        f"BÖLÜMLER (sırayla):\n{sections}\n\n"
        f"KELİME SAYISI: {word_min}-{word_max} aralığında.\n\n"
        "MUTLAK KURALLAR (ihlal = retry):\n"
        "1. Her sayı INPUT JSON'da geçen bir değer olmalı. Aritmetik yapma — "
        "mom_pct, surprise_mom_pp, avg_3m_mom_pct INPUT'ta hazır.\n"
        "2. HER sayının 60 karakter içinde [KAYNAK_KODU] olmalı — manşet, "
        "çekirdek, beklenti, sürpriz, TREND ortalamaları (3-ay, 6-ay) DAHİL. "
        f"Geçerli kodlar: {available_codes}. Örnekler: '%0.32 [FRED:PPIFIS]', "
        "'çekirdek %0.18 [FRED:WPSFD49116]', 'son 3 ay ortalaması %0.28 "
        "[FRED:PPIFIS]', '6-aylık ortalama %0.21 [FRED:PPIFIS]'.\n"
        "3. Politik yorum YASAK.\n"
        "4. Mutlaklık YASAK ('kesin', 'garanti', 'asla').\n"
        "5. Yatırım tavsiyesi YASAK.\n"
        "6. Tarih damgası ŞART (ay adı veya yıl veya UTC).\n"
        "7. 'pipeline basıncı', 'marj sıkışması', 'supply vs demand' mental "
        "modellerini kullan. CPI'ya tahmin/lead-lag rakamı YAZMA (INPUT'ta yok).\n"
        "8. 'beklenti' SADECE expected_*_pct dolu ise yazılır.\n"
        "9. SIFIR DELTA: mom_pct veya surprise_mom_pp 0 ise 'değişmedi' diye "
        "anlat, '0' rakamını yazma.\n"
        "10. TARİH: INPUT `release_date` formatını ('1 Mart 2026') aynen "
        "kullan; ISO formatı YAZMA.\n"
        "11. Çıktı sadece JSON; satır sonları için \\n.\n"
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


_SUPPORTED_EVENT_TYPES = frozenset({"CPI", "NFP", "PCE", "PPI", "FOMC_STATEMENT"})

# ---------- FOMC prompt builder ----------

def _fomc_payload(payload: dict) -> tuple[dict, set[Decimal], set[str]]:
    """FOMC karar yorumu. Diğer decoder'lardan yapısal farklı: scalar
    surprise yok, karar binary (hold/cut/hike) + bp magnitude. Veri kaynağı
    fed_rss event'i (FOMC_STATEMENT) + FRED DFEDTARU/L target range.

    FAZ A (2026-05-11): statement text scrape yok, sadece rate decision +
    önceki kararla karşılaştırma. Dot plot manuel (admin endpoint ile sonra
    enjekte edilebilir). FAZ B'de SEP PDF parse eklenir.
    """
    fed_funds = payload.get("fed_funds") or {}
    cur_up = fed_funds.get("current_upper")
    cur_lo = fed_funds.get("current_lower")
    prior_up = fed_funds.get("prior_upper")
    prior_lo = fed_funds.get("prior_lower")

    # Midpoint of target range (market convention for "the rate")
    mid_cur = round((cur_up + cur_lo) / 2, 2) if cur_up is not None and cur_lo is not None else None
    mid_prior = round((prior_up + prior_lo) / 2, 2) if prior_up is not None and prior_lo is not None else None

    # Basis points change: positive = hike, negative = cut, 0 = hold.
    bp_change = None
    if mid_cur is not None and mid_prior is not None:
        bp_change = int(round((mid_cur - mid_prior) * 100))

    if bp_change is None:
        decision_label = "bilinmiyor"
    elif bp_change == 0:
        decision_label = "değişiklik yok (hold)"
    elif bp_change > 0:
        decision_label = f"{bp_change} baz puan artış (hike)"
    else:
        decision_label = f"{abs(bp_change)} baz puan indirim (cut)"

    sources_used = ["FED:STATEMENT", "FRED:DFEDTARU", "FRED:DFEDTARL"]

    # SEP (FAZ B) — dot plot + projection medians varsa enrichment.
    # sep_current/prior keys: SEP_FUNDS_END_0 (current year), SEP_FUNDS_END_1
    # (year+1), SEP_FUNDS_END_2 (year+2), SEP_FUNDS_LONGER_RUN.
    sep = payload.get("sep") or None
    sep_block = None
    if sep and sep.get("current"):
        cur = sep["current"]
        prior = sep.get("prior") or {}
        def _delta(k):
            cv = cur.get(k)
            pv = prior.get(k)
            if cv is None or pv is None:
                return None
            return round(cv - pv, 2)
        sep_block = {
            "current": {
                "end_year_0_pct": cur.get("SEP_FUNDS_END_0"),
                "end_year_1_pct": cur.get("SEP_FUNDS_END_1"),
                "end_year_2_pct": cur.get("SEP_FUNDS_END_2"),
                "longer_run_pct": cur.get("SEP_FUNDS_LONGER_RUN"),
            },
            "prior": {
                "end_year_0_pct": prior.get("SEP_FUNDS_END_0"),
                "end_year_1_pct": prior.get("SEP_FUNDS_END_1"),
                "end_year_2_pct": prior.get("SEP_FUNDS_END_2"),
                "longer_run_pct": prior.get("SEP_FUNDS_LONGER_RUN"),
            } if prior else None,
            "delta_pp": {
                "end_year_0": _delta("SEP_FUNDS_END_0"),
                "end_year_1": _delta("SEP_FUNDS_END_1"),
                "end_year_2": _delta("SEP_FUNDS_END_2"),
                "longer_run": _delta("SEP_FUNDS_LONGER_RUN"),
            } if prior else None,
            "prior_sep_date": _format_tr_date_iso(sep.get("prior_date_iso")) if sep.get("prior_date_iso") else None,
            "source_code": "FED:SEP",
        }
        sources_used.append("FED:SEP")

    llm_input = {
        "release_date": _format_tr_date(payload.get("released_at")),
        "country": payload.get("country") or "US",
        "title": payload.get("narrative_md") or "FOMC Statement",
        "source_url": payload.get("source_url"),
        "fed_funds": {
            "current_upper_pct": cur_up,
            "current_lower_pct": cur_lo,
            "current_midpoint_pct": mid_cur,
            "prior_upper_pct": prior_up,
            "prior_lower_pct": prior_lo,
            "prior_midpoint_pct": mid_prior,
            "bp_change": bp_change,
            "decision_label": decision_label,
            "prior_decision_date": _format_tr_date_iso(fed_funds.get("prior_decision_date")),
        },
        "sep": sep_block,
        "available_sources": [
            {"code": c, "name": _SOURCES[c].name}
            for c in sources_used if c in _SOURCES
        ],
    }

    # Allowed numbers: current/prior range values + midpoints + bp magnitude.
    # bp_change negatif olabilir → LLM "25 baz puan indirim" yazar (pozitif),
    # NFP sign-flip exempt pattern'iyle abs() eklenir.
    allowed_inputs = [
        cur_up, cur_lo, prior_up, prior_lo, mid_cur, mid_prior, bp_change,
    ]
    if bp_change is not None and bp_change < 0:
        allowed_inputs.append(abs(bp_change))
    # FOMC tarihleri ay ortasında/sonunda (örn. 29 Nisan, 18 Mart) — release
    # date'in günü kaçınılmaz olarak hikayeye yazılır. CPI/NFP gibi ay başı
    # değil (day≤10 küçük-int skip'i çalışmaz). Day + prior decision day
    # exempt et.
    rel_dt = payload.get("released_at")
    if rel_dt is not None:
        try:
            allowed_inputs.append(rel_dt.day)
        except Exception:
            pass
    prior_iso = fed_funds.get("prior_decision_date")
    if prior_iso:
        try:
            prior_dt = datetime.fromisoformat(prior_iso.replace("Z", "+00:00"))
            allowed_inputs.append(prior_dt.day)
        except Exception:
            pass
    # SEP medians + deltas (FAZ B). Delta'lar pozitif/negatif olabilir →
    # NFP sign-flip exempt pattern'iyle abs() de eklenir.
    if sep_block:
        for v in (sep_block["current"] or {}).values():
            if v is not None:
                allowed_inputs.append(v)
        if sep_block.get("prior"):
            for v in (sep_block["prior"] or {}).values():
                if v is not None:
                    allowed_inputs.append(v)
        if sep_block.get("delta_pp"):
            for v in (sep_block["delta_pp"] or {}).values():
                if v is not None:
                    allowed_inputs.append(v)
                    if v < 0:
                        allowed_inputs.append(abs(v))
    allowed = build_allowed_numbers([v for v in allowed_inputs if v is not None])
    allowed_codes = {s["code"] for s in llm_input["available_sources"]}
    return llm_input, allowed, allowed_codes


def _fomc_prompt(llm_input: dict, tier: Tier) -> str:
    has_sep = llm_input.get("sep") is not None
    if tier == "premium":
        word_min, word_max = 130, 400
        if has_sep:
            word_min, word_max = 170, 480
            sections = (
                "(1) Karar manşeti — Fed funds target range mevcut seviye + "
                "önceki karara göre değişim (hold/cut/hike + baz puan). "
                "INPUT'taki rakamları aynen kullan.\n"
                "(2) **Dot Plot şifti** — INPUT.sep.current ve sep.delta_pp'ye "
                "bak. Median yıl-sonu projection değişimi (örn. '2026 sonu "
                "%3.4 → %3.6, +0.2 puan hawkish kayma'). Sadece INPUT'taki "
                "delta'ları kullan, yeni rakam üretme. Citation: [FED:SEP].\n"
                "(3) Bağlam — Hold ise 'Fed bekle-gör modunda', cut ise "
                "'gevşeme döngüsü', hike ise 'sıkılaştırma'. SEP yönü "
                "(hawkish/dovish kayma) ile karar yönü tutarlı mı?\n"
                "(4) Piyasaya etki — politika faizi + dot plot kombinasyonu "
                "(USD, tahvil getirisi, hisse, BTC kavramsal yön).\n"
                "(5) Aklında tut + 'Senin için 1-cümle' (genel yön; tavsiye değil)."
            )
        else:
            sections = (
                "(1) Karar manşeti — Fed funds target range mevcut seviye + "
                "önceki karara göre değişim (hold/cut/hike + baz puan). "
                "INPUT'taki rakamları aynen kullan.\n"
                "(2) Bağlam — bu kararın ne anlama geldiği. Hold ise 'Fed bekle-gör "
                "modunda', cut ise 'gevşeme döngüsü', hike ise 'sıkılaştırma'. "
                "Mental model olarak anlat, yeni rakam yazma.\n"
                "(3) Piyasaya etki — politika faizi düştüğünde/yükseldiğinde tipik "
                "olarak ne olur (USD, tahvil getirisi, hisse, BTC kavramsal yön). "
                "INPUT verisine bağlı kal.\n"
                "(4) Aklında tut + 'Senin için 1-cümle' (genel yön; tavsiye değil)."
            )
    else:  # advance
        word_min, word_max = 270, 600
        if has_sep:
            word_min, word_max = 350, 720
            sections = (
                "(1) Karar manşeti + önceki karar karşılaştırması (target range "
                "mevcut, önceki, midpoint, bp delta) — INPUT rakamlarını aynen.\n"
                "(2) **Dot Plot şifti** — INPUT.sep'e bak. Median projection "
                "değerleri: cari yıl, gelecek yıl, +2 yıl, longer-run; "
                "ÖNCEKİ SEP ile delta_pp. 'Hawkish kayma' (delta pozitif) "
                "= Fed daha yüksek faiz öngörüyor; 'dovish' (delta negatif) "
                "= daha düşük. SADECE INPUT delta'larını kullan. Citation: "
                "[FED:SEP]. Önceki SEP tarihi prior_sep_date'ten.\n"
                "(3) Karar yönü vs SEP yönü tutarlılığı — hold karar + "
                "hawkish dot plot şifti = 'şahin pause', cut + hawkish şift = "
                "çelişki, vb. Mental model olarak anlat.\n"
                "(4) Karar tarihi vs önceki karar tarihi arası — Fed bu "
                "süreçte ne dedi/yaptı (kavramsal, INPUT'ta yoksa rakam "
                "yazma).\n"
                "(5) Kanal analizi — kararın + dot plot'un geçeceği "
                "makanizmalar: kredi maliyeti, USD likiditesi, tahvil "
                "eğrisi, risk varlıklar. Kavramsal anlat.\n"
                "(6) İleriye dönük — Fed'in 'data-dependent' duruşu + dot "
                "plot patikası; bir sonraki toplantıya kadar izlenmesi "
                "gerekenler (CPI, NFP, PCE veri akışı). Spekülatif rakam "
                "YASAK.\n"
                "(7) Risk dengesi — yumuşak iniş vs durgunluk vs yeniden "
                "ısınma gibi senaryolar (kavramsal, olasılık dili).\n"
                "(8) Aklında tut + 'Senin için 1-cümle' (DXY/BTC/altın yönü, "
                "tavsiye değil)."
            )
        else:
            sections = (
                "(1) Karar manşeti + önceki karar karşılaştırması (target range "
                "mevcut, önceki, midpoint, bp delta) — INPUT rakamlarını aynen.\n"
                "(2) Karar tarihi vs önceki karar tarihi arası — Fed bu süreçte "
                "ne dedi/yaptı (kavramsal, INPUT'ta yoksa rakam yazma).\n"
                "(3) Piyasa pricing'inden sapma — INPUT'ta market consensus yok, "
                "o yüzden 'beklenti' kelimesi YAZMA. Sadece 'piyasa şu yönde "
                "fiyatlamıştı, gerçekleşen şu' gibi kavramsal kıyas YAPMA — "
                "veri yoksa atla.\n"
                "(4) Kanal analizi — kararın geçeceği makanizmalar: kredi maliyeti, "
                "USD likiditesi, tahvil eğrisi, risk varlıklar. Kavramsal anlat.\n"
                "(5) İleriye dönük — Fed'in 'data-dependent' duruşunu vurgula, "
                "bir sonraki toplantıya kadar izlenmesi gerekenler (CPI, NFP, "
                "PCE veri akışı). Spekülatif rakam YASAK.\n"
                "(6) Risk dengesi — yumuşak iniş vs durgunluk vs yeniden ısınma "
                "gibi senaryolar (kavramsal, olasılık dili).\n"
                "(7) Aklında tut + 'Senin için 1-cümle' (DXY/BTC/altın yönü, "
                "tavsiye değil)."
            )

    available_codes = [s["code"] for s in llm_input["available_sources"]]

    return (
        "Sen makro analist hikaye anlatıcısısın. Aşağıdaki JSON FOMC karar "
        "verisini oku ve **sadece geçerli bir JSON** döndür.\n\n"
        f"INPUT:\n{json.dumps(llm_input, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI:\n"
        "{\n"
        '  "story_md": "(string — aşağıdaki bölümleri içermeli)"\n'
        "}\n\n"
        f"BÖLÜMLER (sırayla):\n{sections}\n\n"
        f"KELİME SAYISI: {word_min}-{word_max} aralığında.\n\n"
        "MUTLAK KURALLAR (ihlal = retry):\n"
        "1. Her sayı INPUT JSON'da geçen bir değer olmalı. Aritmetik yapma — "
        "bp_change INPUT'ta hazır.\n"
        "2. HER sayının 60 karakter içinde [KAYNAK_KODU] olmalı. "
        f"Geçerli kodlar: {available_codes}. Örnekler: '%5.50 üst sınır "
        "[FRED:DFEDTARU]', '%5.25 alt sınır [FRED:DFEDTARL]', '25 baz puan "
        "indirim [FED:STATEMENT]'.\n"
        "3. Politik yorum YASAK (Powell hariç — kurumsal isim olarak "
        "geçebilir; ama 'Cumhuriyetçi/Demokrat/AKP/CHP/seçim/parti' yasak).\n"
        "4. Mutlaklık YASAK ('kesin', 'garanti', 'asla').\n"
        "5. Yatırım tavsiyesi YASAK.\n"
        "6. Tarih damgası ŞART (ay adı veya yıl veya UTC).\n"
        "7. 'beklenti' SADECE INPUT'ta piyasa pricing varsa kullan — FAZ A'da "
        "yok, o yüzden 'beklenti'/'consensus' kelimelerini YAZMA.\n"
        "8. SIFIR DELTA (hold): bp_change=0 ise 'değişmedi/sabit tutuldu' "
        "de, '0 baz puan' yazma. Yine de mevcut range'i (upper/lower) yaz.\n"
        "9. TARİH: INPUT `release_date` formatını ('1 Mart 2026') aynen "
        "kullan; ISO formatı YAZMA.\n"
        "10. 'data-dependent', 'gevşeme döngüsü', 'sıkılaştırma', 'yumuşak "
        "iniş' gibi mental modelleri kullan — somut yeni rakam üretme.\n"
        "11. Çıktı sadece JSON; satır sonları için \\n.\n"
    )


def _format_tr_date_iso(iso_str: Optional[str]) -> Optional[str]:
    """ISO timestamp string → '1 Mart 2026' formatı."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return _format_tr_date(dt)
    except Exception:
        return None


_DECODER_DISPATCH = {
    "CPI": (_cpi_payload, _cpi_prompt),
    "NFP": (_nfp_payload, _nfp_prompt),
    "PCE": (_pce_payload, _pce_prompt),
    "PPI": (_ppi_payload, _ppi_prompt),
    "FOMC_STATEMENT": (_fomc_payload, _fomc_prompt),
}

# Per-decoder word count bounds. PPI paired core eklendi (2026-05-11 basket
# switch: PPIFIS + WPSFD49116) → Advance default 340'a revert.
# NFP Advance 8-bölüm format (sektörel kırılım dahil) ile gemini-2.5-flash
# tipik 320-340w yazıyor; min 340 sık takılıyor → 300'e indirildi.
_DEFAULT_BOUNDS = {"premium": (150, 500), "advance": (340, 680)}
_DECODER_WORD_BOUNDS = {
    "NFP": {"premium": (150, 500), "advance": (300, 680)},
    # FOMC FAZ A — scalar surprise yok, daha kısa hikaye yeterli. Smoke'da
    # premium 147w / advance 293w'de takıldı (gemini-2.5-flash kompakt yazıyor:
    # market consensus + dot plot input verisi olmadığından bölümler kısa) →
    # premium min 130, advance min 270'e indirildi.
    "FOMC_STATEMENT": {"premium": (130, 400), "advance": (270, 600)},
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
            f"(currently CPI, NFP, PCE, PPI)"
        )
        return result

    payload_fn, prompt_fn = _DECODER_DISPATCH[et]
    llm_input, allowed, allowed_codes = payload_fn(payload)
    prompt = prompt_fn(llm_input, tier)

    word_bounds = _DECODER_WORD_BOUNDS.get(et, _DEFAULT_BOUNDS)[tier]

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

    # Fire-and-forget Telegram broadcast to Premium/Advance recipients. Stamp
    # column (`broadcasted_<tier>_at`) ensures regen with force=True only
    # re-broadcasts if the previous push was wiped (admin path uses force).
    # Idempotent at the row level so concurrent generate_story calls for the
    # same (event_id, tier) won't double-push.
    if written:
        try:
            from services.macro_broadcaster import broadcast_story_safe
            task = asyncio.create_task(
                broadcast_story_safe(event_id, tier, force=False)
            )
            _STORY_BROADCAST_INFLIGHT.add(task)
            task.add_done_callback(_STORY_BROADCAST_INFLIGHT.discard)
        except Exception as e:
            logger.warning(f"failed to schedule story broadcast {event_id}/{tier}: {e}")

    return result


# Strong-ref set so fire-and-forget broadcast tasks aren't GC'd while waiting
# on Telegram API. Matches the _DELAYED_INFLIGHT pattern in macro_broadcaster.
_STORY_BROADCAST_INFLIGHT: set = set()


async def generate_story_safe(
    event_id: str, tier: Tier, *, force: bool = False,
) -> None:
    """Fire-and-forget wrapper — never raises."""
    try:
        await generate_story(event_id, tier, force=force)
    except Exception as e:
        logger.error(f"generate_story crashed for {event_id}/{tier}: {e}")
