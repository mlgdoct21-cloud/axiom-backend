"""Kurumsal Sentez — Commit 2 sentez servisi (Gemini; broadcast KAPALI).

macro_storyteller.py forku. KORUNAN pattern: _call_gemini (gemini-2.5-flash
temp 0.3, JSON mime, thinkingBudget 0, 1 retry, fence regex), L1 sayı
whitelist (services.macro_sources.validators), idempotent UPSERT
(corporate_syntheses, alembic 025). DEĞİŞEN: N-doküman karşılaştırmalı
sentez, çok-kaynak prompt, L4 attribution + L5 length + L_DISPLACE telif
guard'ı, footer zorunlu.

Girdi = store.read_window (mahfi/isyatirim prose) + ARK en son snapshot
(olgusal). Broadcast/scheduler YOK (Commit 3). 0 kaynak → sentez YOK
(sessiz skip + log). Fail policy: sessiz skip + log; exception ile
pipeline kırma (PPI 333-spam dersi).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.corporate_sources import ark_csv
from services.corporate_sources.store import read_window
from services.macro_sources.validators import (
    build_allowed_numbers,
    extract_numbers,
    validate_numbers,
)

logger = get_logger("corporate.synthesis")

Tier = Literal["premium", "advance"]

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
# Gemini çağrı timeout'u. Kapsamlı çok-tema prompt (~20K+ token girdi)
# yanıt gecikmesini artırır; 40s sınırı ReadTimeout→None→"gemini empty"
# veriyordu (önceki aralıklı premium-empty'nin de kök nedeni). Tanılama:
# 20.4K-token prompt STOP+geçerli JSON ~<120s. 8s connect korunur.
_HTTP_TIMEOUT = httpx.Timeout(120.0, connect=8.0)

# Türkiye kalıcı UTC+3 (DST yok, 2016+) — sabit offset, tzdata yok.
_TR_TZ = timezone(timedelta(hours=3))
_PUBLISH_TIME = time(8, 30)

_PROSE_SOURCES = ["mahfi", "isyatirim", "overshoot", "blackrock", "jpm", "ms"]
# Arka-plan sinyali gövde kırpma — prompt şişmesini/maliyeti/L_DISPLACE
# yüzeyini sınırla (tez/sinyal yeterli; tam-metin gerekmez).
# Prose kaynak gövde tavanı. 2800 MS transkriptini (~4.5K) kesip
# özgün tezini (ör. "AI-funding fiyat-duyarsız; bakır+%40, bellek
# +%150-300") yutuyordu → 3800 (cluster digest kısıldığı için toplam
# prompt yine kontrollü; RECITATION riski digest tarafında çözüldü).
_SIGNAL_BODY_CAP = 3800
_ARK_FUNDS = ["ARKK", "ARKW", "ARKG", "ARKQ", "ARKF"]

# Prompt Kural 9 kelime sınırını "rehber" ilan ediyor; bu yüzden guard
# sert hedef değil runaway-çıktıyı yakalayan sanity-tavan olmalı (gözlem:
# premium ~440-480, advance ~750-990 — eski (400/700) tavan normal çıktıyı
# reddediyordu).
# Tavan = uzunluk sanity (telif backstop'u L_DISPLACE 12-gram'dır, O'NA
# DOKUNULMADI). Kapsamlı çok-tema kapsam kararı (kullanıcı onaylı) daha
# uzun anlatı gerektirir → tavan yükseltildi (premium 700→1100,
# advance 1150→2000); taban korundu.
_WORD_BOUNDS: dict[str, tuple[int, int]] = {
    "premium": (120, 1100),
    "advance": (300, 2000),
}

# Arka-plan sinyali bloklarında kullanılan iç etiket (prompt girdisi;
# çıktıda kaynak adı/atıf YOK — data-first atıfsız AXIOM sesi).
_LABEL_FOR_SOURCE = {
    "mahfi": "MAHFI", "isyatirim": "ISYATIRIM", "overshoot": "OVERSHOOT",
    "blackrock": "BLACKROCK", "jpm": "JPM", "ms": "MS",
}

_FOOTER = (
    "Bu içerik AXIOM'un bağımsız makro değerlendirmesidir; yatırım "
    "tavsiyesi değildir. Kamuya açık veri ve piyasa gelişmeleri "
    "AXIOM tarafından sentezlenmiştir."
)

_BANNED = ("kesin al", "kesin sat", "garanti", "tavsiye ederim",
           "almalısınız", "satmalısınız")


# ---------- Week window ----------

def _week_bounds(ref: Optional[date] = None) -> tuple[datetime, datetime, date]:
    """(start_utc, end_utc, week_start_date). Pencere: önceki Pzt 08:30 TR
    → bu Pzt 08:30 TR. week_start = sentezlenen haftanın başlangıç Pzt'si."""
    now_tr = datetime.now(_TR_TZ) if ref is None else datetime.combine(
        ref, time(12, 0), _TR_TZ
    )
    this_mon = (now_tr - timedelta(days=now_tr.weekday())).date()
    this_0830 = datetime.combine(this_mon, _PUBLISH_TIME, _TR_TZ)
    prev_0830 = this_0830 - timedelta(days=7)
    return (
        prev_0830.astimezone(timezone.utc),
        this_0830.astimezone(timezone.utc),
        prev_0830.date(),
    )


def week_event_id(week_start: date) -> str:
    seed = f"corp-week|{week_start.isoformat()}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


# ---------- Payload ----------

@dataclass
class SynthPayload:
    week_start: date
    prev_iso: str
    this_iso: str
    prose: list[dict] = field(default_factory=list)     # read_window rows
    ark: list[dict] = field(default_factory=list)        # per-fund fact dict
    live_block: str = ""
    source_count: int = 0


async def _fetch_ark_facts() -> list[dict]:
    """ARK en son snapshot — olgusal özet (top ağırlık). Gün-gün delta YOK
    (accumulation Commit 3). Fail-soft: erişilemeyen fon atlanır."""
    out: list[dict] = []
    for fund in _ARK_FUNDS:
        try:
            body, _ = await ark_csv.fetch_holdings(fund)
            if not body:
                continue
            snap = ark_csv.parse_holdings(body, fund)
            if not snap.holdings or snap.as_of is None:
                continue
            top = sorted(
                snap.holdings, key=lambda h: h.weight_pct, reverse=True
            )[:6]
            out.append({
                "fund": fund,
                "as_of": snap.as_of.isoformat(),
                "top": [
                    {"company": h.company, "ticker": h.ticker,
                     "weight_pct": round(h.weight_pct, 2)}
                    for h in top
                ],
            })
        except Exception as e:  # noqa: BLE001
            logger.info(f"ark fact skip {fund}: {e}")
    return out


_DELTA_MIN_PP = 0.5  # ağırlık değişimi eşiği (puan) — gürültü filtresi


async def _ark_delta(fund: str, start: datetime, end: datetime) -> Optional[dict]:
    """corporate_holdings_snapshots'tan pencere içi en eski + en yeni
    snapshot → 'hafta içi hareket' (artan/azalan/yeni/çıkan). <2 snapshot
    veya DB yok → None (fail-soft; Commit 2 tek-snapshot davranışı korunur)."""
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(
                text(
                    "SELECT as_of, payload FROM corporate_holdings_snapshots "
                    "WHERE fund = :f AND as_of >= :s AND as_of <= :e "
                    "ORDER BY as_of ASC"
                ),
                {"f": fund.upper(), "s": start.date(), "e": end.date()},
            )).mappings().all()
    except Exception as e:  # noqa: BLE001
        logger.info(f"ark_delta skip {fund}: {e}")
        return None
    if len(rows) < 2:
        return None

    def _wmap(payload) -> dict:
        items = payload if isinstance(payload, list) else json.loads(payload or "[]")
        out: dict[str, tuple[str, float]] = {}
        for h in items:
            tk = (h.get("ticker") or h.get("company") or "").strip()
            if tk:
                out[tk] = (h.get("company") or tk, float(h.get("weight_pct") or 0))
        return out

    first = _wmap(rows[0]["payload"])
    last = _wmap(rows[-1]["payload"])
    increased, decreased, added, removed = [], [], [], []
    for tk, (name, w_new) in last.items():
        if tk not in first:
            added.append({"ticker": tk, "company": name,
                          "weight_pct": round(w_new, 2)})
        else:
            dp = w_new - first[tk][1]
            if dp >= _DELTA_MIN_PP:
                increased.append({"ticker": tk, "company": name,
                                   "delta_pp": round(dp, 2)})
            elif dp <= -_DELTA_MIN_PP:
                decreased.append({"ticker": tk, "company": name,
                                  "delta_pp": round(dp, 2)})
    for tk, (name, _w) in first.items():
        if tk not in last:
            removed.append({"ticker": tk, "company": name})
    if not (increased or decreased or added or removed):
        return None
    return {
        "from": str(rows[0]["as_of"]), "to": str(rows[-1]["as_of"]),
        "increased": increased[:6], "decreased": decreased[:6],
        "added": added[:6], "removed": removed[:6],
    }


def _fmt_num(v):
    """numeric kolon kozmetiği: Decimal('3.500000')→'3.5', '199000'→'199000'
    (bilimsel-notasyon değil), '5.0'→'5'. None/parse-edilemez aynen döner."""
    if v is None:
        return v
    try:
        d = Decimal(str(v)).normalize()
    except (InvalidOperation, ValueError, TypeError):
        return v
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))  # 1E+5 → 100000
    return format(d, "f")


async def _build_live_block() -> str:
    """AXIOM'un kendi verisinden kompakt 'canlı veri bağlamı': son makro
    açıklamalar (US+TR), piyasa anlık görünüm, global gündem başlıkları.
    Her blok bağımsız fail-soft (biri patlarsa diğerleri kalır)."""
    lines: list[str] = []

    # 1) Makro açıklamalar — gösterge başına en güncel (US+TR, son 14 gün)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT DISTINCT ON (event_type) event_type, country, "
                "released_at, actual_value, expected_value, prior_value, "
                "surprise_pct FROM macro_releases "
                "WHERE country IN ('US','TR') AND actual_value IS NOT NULL "
                "AND released_at >= NOW() - INTERVAL '14 days' "
                "ORDER BY event_type, released_at DESC"
            ))).mappings().all()
        if rows:
            lines.append("MAKRO (son 14g, gösterge başına en güncel):")
            for r in rows[:16]:
                exp = r["expected_value"]
                sp = r["surprise_pct"]
                seg = (f"  {r['country']} {r['event_type']}: "
                       f"actual={_fmt_num(r['actual_value'])}")
                if exp is not None:
                    seg += f" beklenti={_fmt_num(exp)}"
                if r["prior_value"] is not None:
                    seg += f" önceki={_fmt_num(r['prior_value'])}"
                if sp is not None:
                    seg += f" sürpriz%={_fmt_num(sp)}"
                seg += f" ({str(r['released_at'])[:10]})"
                lines.append(seg)
    except Exception as e:  # noqa: BLE001
        logger.info(f"live_block macro skip: {e}")

    # 2) GÜNCEL PİYASA SEVİYELERİ — otoriter fiyat çapası (hallüsinasyon
    #    kökü: eski kod market_summary FMP batch-quote 402'leşiyordu →
    #    canlı seviye yok → model bir HABERDEKİ "10Y<4.40" sayısını
    #    güncelmiş gibi yazıyordu). FMP /stable/quote per-sembol ÇALIŞIR
    #    (probe). UST faizi/DXY/WTI plan-dışı (402) → KASTEN dahil edilmez;
    #    prompt onları niteliksel konuşur (sayı UYDURMAZ). Her sembol
    #    bağımsız fail-soft.
    try:
        fmp_key = os.getenv("FMP_API_KEY", "").strip()
        if fmp_key:
            # (FMP sembol, görünen ad, ondalık)
            _MKT = [
                ("^GSPC", "S&P500", 0), ("^IXIC", "Nasdaq", 0),
                ("^VIX", "VIX", 1), ("XAUUSD", "Altın(ons$)", 0),
                ("XAGUSD", "Gümüş(ons$)", 2), ("BZUSD", "Brent($)", 2),
                ("BTCUSD", "Bitcoin($)", 0), ("EURUSD", "EUR/USD", 4),
            ]

            async def _q(sym: str) -> Optional[dict]:
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(8.0, connect=4.0)
                    ) as c:
                        r = await c.get(
                            "https://financialmodelingprep.com/stable/"
                            f"quote?symbol={sym}&apikey={fmp_key}"
                        )
                    if r.status_code != 200:
                        return None
                    d = r.json()
                    return d[0] if isinstance(d, list) and d else None
                except Exception:  # noqa: BLE001
                    return None

            quotes = await asyncio.gather(*[_q(s) for s, _, _ in _MKT])
            seg: list[str] = []
            for (sym, name, dp), q in zip(_MKT, quotes):
                if not q or q.get("price") is None:
                    continue
                px = q["price"]
                cp = q.get("changePercentage")
                pxs = f"{px:,.{dp}f}"
                seg.append(
                    f"{name} {pxs}"
                    + (f" ({cp:+.2f}%)" if cp is not None else "")
                )
            if seg:
                today = datetime.now(timezone.utc).date().isoformat()
                lines.append(
                    f"GÜNCEL PİYASA SEVİYELERİ ({today} — otoriter fiyat "
                    f"çapası; güncel seviye iddiası YALNIZ buradan): "
                    + " | ".join(seg)
                )
    except Exception as e:  # noqa: BLE001
        logger.info(f"live_block market skip: {e}")

    # 3) HAFTANIN ANA GELİŞMELERİ — tema-kümeli ana-olay özeti.
    #    Eskiden: son 5g'den rastgele 25 başlık + ai_summary[:200] (10K+
    #    ilgili haber varken). Şimdi: ~11 makro tema kümesi, küme başına
    #    en güncel ayrık haberlerin `axiom_analysis`'i (kısa+yön-odaklı
    #    AXIOM ön-analizi; ai_summary'den daha sinyal-yoğun). Global
    #    dedup + per-küme/global cap → kapsamlı ama token-kontrollü.
    #    Retail hisse-pick spam'i elenir. Telif: prompt Kural 2/3
    #    dönüştürme+atıfsızlık kapsar.
    try:
        _NOISE = (
            r"stocks? to buy|better buy| vs\.|overlooked|before they soar|"
            r"should you buy|motley|best stock|top \d+ stock|price target|"
            r"buy now|to own|dividend stock"
        )
        # (etiket, başlık|özet eşleşme regex'i) — sıra = sunum sırası
        _CLUSTERS: list[tuple[str, str]] = [
            ("ABD-Çin & Ticaret",
             r"trump.*chin|chin.*trump|xi jinping|trump.*xi|u\.?s\.?-china|"
             r"sino-?american|beijing summit|tarife|tariff|trade war|"
             r"ticaret savaş|ticaret görüşme"),
            ("İran / Orta Doğu / Barış Görüşmeleri",
             r"iran|hormuz|hürmüz|strait of hormuz|orta do[ğg]u|middle east|"
             r"israel|israil|gaza|gazze|hizbullah|peace term|barış görüşme|"
             r"barış şart|ateşkes|cease.?fire|truce|müzakere|negotiat"),
            ("Fed & Faiz Politikası",
             r"\bfed\b|federal reserve|\bfomc\b|powell|warsh|fed chair|"
             r"fed başkan|faiz karar|interest rate|rate (hike|cut|decision|"
             r"path)|faiz artır|faiz indir|şahin|dovish|hawkish"),
            ("Diğer Merkez Bankaları (ECB/BoJ/TCMB)",
             r"\becb\b|european central|\bboj\b|bank of japan|bank of england|"
             r"\bpboc\b|tcmb|türkiye cumhuriyet merkez|merkez bankası|"
             r"central bank(?!.*\bfed\b)"),
            ("Enflasyon & Fiyatlar",
             r"enflasyon|inflation|\bcpi\b|\bppi\b|\bpce\b|deflation|"
             r"fiyat artış|cost of living|price pressure|disinflation"),
            ("İşgücü & İstihdam",
             r"istihdam|jobless|unemploy|nonfarm|payroll|\bnfp\b|"
             r"labor market|işsizlik|jobs report|işgücü|layoff|işten çıkar"),
            ("Enerji & Petrol",
             r"petrol|oil price|\bopec\b|crude|brent|\bwti\b|natural gas|"
             r"doğalgaz|enerji fiyat|energy price|refinery|lng"),
            ("Büyüme & Resesyon",
             r"resesyon|recession|\bgdp\b|gayri safi|büyüme|growth forecast|"
             r"economic outlook|slowdown|daralma|contraction|durgunluk"),
            ("Tahvil / Kur / Likidite",
             r"tahvil|treasury yield|bond yield|\byield|dolar|\bdollar\b|"
             r"\bfx\b|exchange rate|döviz kur|liquidity|para arzı|"
             r"money supply|\bm2\b|borçlanma|debt issuance"),
            ("Kripto & Dijital Varlık",
             r"bitcoin|\bbtc\b|ethereum|\beth\b|kripto|crypto|stablecoin|"
             r"\bxrp\b|solana|spot etf|kripto etf|digital asset"),
            ("Yapay Zeka & Teknoloji Yatırımı",
             r"yapay zeka|artificial intelligence|\bai\b|agentic|çip|chip|"
             r"semiconductor|data center|veri merkezi|nvidia|capex|"
             r"sermaye harcama|tech invest|hyperscaler"),
        ]
        # Kapsamı korurken prompt'u küçült: büyük near-verbatim haber
        # yığını Gemini RECITATION/empty riskini artırıyordu (~22K
        # live_block). ~10 tema × 4 madde hâlâ kapsamlı ama daha güvenli.
        _PER = 4            # küme başına en fazla madde
        _GLOBAL = 28        # toplam madde tavanı (token + RECITATION)
        _AA_CAP = 320       # axiom_analysis kırpma (medyan≈362)
        seen: set[str] = set()
        emitted = 0
        block: list[str] = []
        async with engine.connect() as conn:
            for label, rgx in _CLUSTERS:
                if emitted >= _GLOBAL:
                    break
                rows = (await conn.execute(text(
                    "SELECT original_title, axiom_analysis, ai_summary, "
                    "created_at FROM news_items "
                    "WHERE created_at >= NOW() - INTERVAL '8 days' "
                    "AND (lower(original_title) ~ :rx "
                    "     OR lower(coalesce(ai_summary,'')) ~ :rx) "
                    "AND lower(coalesce(source,'')) !~ "
                    "    'fool\\.com|247wallst|seekingalpha' "
                    "AND lower(original_title) !~ :noise "
                    "ORDER BY created_at DESC LIMIT 14"
                ), {"rx": rgx, "noise": _NOISE})).mappings().all()
                picked: list[str] = []
                for r in rows:
                    if len(picked) >= _PER or emitted >= _GLOBAL:
                        break
                    title = (r["original_title"] or "").strip()
                    key = title.lower()[:45]
                    if not title or key in seen:
                        continue
                    seen.add(key)
                    body = (r["axiom_analysis"] or "").strip()
                    if not body:
                        body = (r["ai_summary"] or "").strip()[:300]
                    body = body.replace("\n", " ")[:_AA_CAP]
                    if not body:
                        continue
                    picked.append(
                        f"  - {title[:110]} → {body} "
                        f"({str(r['created_at'])[:10]})"
                    )
                    emitted += 1
                if picked:
                    block.append(f"▸ {label}:")
                    block.extend(picked)
        if block:
            lines.append(
                "HAFTANIN ANA GELİŞMELERİ (son 8g, tema bazlı; her madde "
                "AXIOM ön-analizidir — yeniden ÜRETME/ALINTILAMA, kendi "
                "muhakemene DÖNÜŞTÜR, isim/kaynak verme):"
            )
            lines.extend(block)
    except Exception as e:  # noqa: BLE001
        logger.info(f"live_block news skip: {e}")

    # 4) Tema radarı — analist/yorumcu BAŞLIK-düzeyi sinyal (gövde YOK;
    #    telif-güvenli; düşünceyi besler, ASLA aktarılmaz — prompt Kural
    #    2/3 dönüştürme+atıfsızlık zaten kapsar).
    try:
        async with engine.connect() as conn:
            rrows = (await conn.execute(text(
                "SELECT title, published FROM corporate_posts "
                "WHERE kind = 'radar' "
                "AND published >= NOW() - INTERVAL '8 days' "
                "ORDER BY published DESC LIMIT 30"
            ))).mappings().all()
        if rrows:
            lines.append("TEMA RADARI (yorumcu/gündem başlık-düzeyi sinyal — "
                         "gövde/atıf YOK, yalnız tema; dönüştür):")
            for r in rrows:
                lines.append(
                    f"  {(r['title'] or '')[:120]} "
                    f"({str(r['published'])[:10]})"
                )
    except Exception as e:  # noqa: BLE001
        logger.info(f"live_block radar skip: {e}")

    # 5) YAKLAŞAN KATALİZÖRLER — gelecek ~10g makro takvim + mega-cap
    #    bilanço (FMP economic/earnings-calendar; ikisi de 200/probe).
    #    İLERİYE DÖNÜK bölümün somut beslemesi (ör. NVDA bilanço Çrş,
    #    FOMC tutanakları). Fail-soft.
    try:
        fmp_key = os.getenv("FMP_API_KEY", "").strip()
        if fmp_key:
            _t = datetime.now(timezone.utc).date()
            _to = (_t + timedelta(days=10)).isoformat()
            _ev_kw = re.compile(
                r"fomc|fed|powell|rate decision|cpi|inflation|ppi|"
                r"nonfarm|payroll|unemployment|jobless|\bgdp\b|\bpce\b|"
                r"retail sales|consumer confidence|ism |pmi|housing|"
                r"durable|enflasyon|faiz|tcmb|işsizlik", re.IGNORECASE)
            _BIG = {
                "NVDA", "MSFT", "AAPL", "GOOGL", "GOOG", "AMZN", "META",
                "TSLA", "AVGO", "AMD", "JPM", "NFLX", "CRM", "ORCL",
                "ADBE", "QCOM", "MU", "PLTR", "COIN", "SMCI", "LLY",
                "WMT", "TSM", "ASML", "BABA", "HD", "MA", "V",
            }

            async def _fmp_cal(path: str) -> list:
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(10.0, connect=4.0)
                    ) as c:
                        r = await c.get(
                            f"https://financialmodelingprep.com/stable/"
                            f"{path}?from={_t}&to={_to}&apikey={fmp_key}"
                        )
                    if r.status_code != 200:
                        return []
                    d = r.json()
                    return d if isinstance(d, list) else []
                except Exception:  # noqa: BLE001
                    return []

            econ, earn = await asyncio.gather(
                _fmp_cal("economic-calendar"), _fmp_cal("earnings-calendar")
            )
            cats: list[str] = []
            seen_ev: set[str] = set()
            for e in econ:
                if e.get("country") not in ("US", "TR", "EU"):
                    continue
                ev = (e.get("event") or "").strip()
                if not ev or not _ev_kw.search(ev):
                    continue
                k = ev.lower()[:30]
                if k in seen_ev:
                    continue
                seen_ev.add(k)
                d = str(e.get("date") or "")[:10]
                extra = ""
                if e.get("previous") is not None:
                    extra = f" (önc {e['previous']}"
                    if e.get("estimate") is not None:
                        extra += f" bek {e['estimate']}"
                    extra += ")"
                cats.append(f"  {d} {e.get('country')} {ev}{extra}")
                if len(cats) >= 12:
                    break
            ecount = 0
            for e in earn:
                sym = (e.get("symbol") or "").upper()
                if sym not in _BIG:
                    continue
                d = str(e.get("date") or "")[:10]
                eps = e.get("epsEstimated")
                cats.append(
                    f"  {d} {sym} bilanço"
                    + (f" (epsBek {eps})" if eps is not None else "")
                )
                ecount += 1
                if ecount >= 8:
                    break
            if cats:
                lines.append(
                    "YAKLAŞAN KATALİZÖRLER (gelecek ~10g — İLERİYE DÖNÜK "
                    "bölümün SOMUT beslemesi; tarih+olay):"
                )
                lines.extend(cats)
    except Exception as e:  # noqa: BLE001
        logger.info(f"live_block catalysts skip: {e}")

    return "\n".join(lines)


async def build_payload(ref: Optional[date] = None) -> SynthPayload:
    start, end, week_start = _week_bounds(ref)
    prose: list[dict] = []
    try:
        prose = await read_window(_PROSE_SOURCES, start, end)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"build_payload read_window error (yok sayılıyor): {e}")
    ark = await _fetch_ark_facts()
    for a in ark:  # accumulation birikince hafta-içi hareket (fail-soft)
        d = await _ark_delta(a["fund"], start, end)
        if d:
            a["delta"] = d
    pl = SynthPayload(
        week_start=week_start,
        prev_iso=start.date().isoformat(),
        this_iso=end.date().isoformat(),
        prose=prose,
        ark=ark,
        live_block=await _build_live_block(),
        source_count=len(prose) + (1 if ark else 0),
    )
    return pl


# ---------- Prompt ----------

def _source_blocks(pl: SynthPayload) -> str:
    lines: list[str] = []
    for r in pl.prose:
        label = _LABEL_FOR_SOURCE.get(r.get("source", ""), r.get("source", "?").upper())
        pub = r.get("published")
        pub_s = pub.isoformat() if hasattr(pub, "isoformat") else str(pub)
        body = (r.get("body_text") or "").strip()
        if len(body) > _SIGNAL_BODY_CAP:
            body = body[:_SIGNAL_BODY_CAP] + " …(kısaltıldı)"
        lines.append(
            f"[{label}] | {pub_s} | {r.get('kind','')} | "
            f"{(r.get('title') or '').strip()}\n{body}"
        )
    for a in pl.ark:
        tops = "; ".join(
            f"{t['company']} ({t['ticker'] or '-'}) %{t['weight_pct']}"
            for t in a["top"]
        )
        block = (
            f"[ARK] | {a['as_of']} | structured | {a['fund']} en yüksek "
            f"ağırlıklar\n{tops}"
        )
        d = a.get("delta")
        if d:
            mv = []
            for x in d.get("increased", []):
                mv.append(f"{x['ticker']} +{x['delta_pp']}pp")
            for x in d.get("decreased", []):
                mv.append(f"{x['ticker']} {x['delta_pp']}pp")
            for x in d.get("added", []):
                mv.append(f"{x['ticker']} yeni")
            for x in d.get("removed", []):
                mv.append(f"{x['ticker']} çıktı")
            if mv:
                block += (
                    f"\nHafta içi hareket ({d['from']}→{d['to']}): "
                    + "; ".join(mv)
                )
        lines.append(block)
    return "\n\n".join(lines) if lines else "(kaynak yok)"


def _build_prompt(tier: Tier, pl: SynthPayload) -> str:
    if tier == "premium":
        out_schema = (
            '{"analiz":"...(TEK harmanlanmış, akıcı, hikâyeleştirilmiş '
            'AXIOM makro anlatısı; alt-başlık YOK)","footer":"...(SABIT METIN)"}'
        )
        tgt = "analiz ~500-850 kelime hedef (DERİNLİK > kapsam)"
        depth_directive = (
            "# PREMIUM ODAĞI: DERİNLİK > KAPSAM\n"
            "Tüm temaları yüzeysel saymak YOK. Bu haftanın EN piyasa-"
            "hareket-ettiren ~5 temasını SEÇ (girdiye göre, ör. ABD-Çin "
            "zirvesi, Orta Doğu/enerji, Fed/işgücü, AI-yatırım/kredi, "
            "kıymetli maden/kripto). Her seçilen tema için ZORUNLU: "
            "(a) SPESİFİK ne oldu (olayı/sayıyı adıyla), (b) GÜNCEL "
            "seviye/veri (CANLI VERİ'den), (c) AXIOM neden-zinciri "
            "çıkarımı, (d) yön/etki. İkincil temalar en çok tek cümle. "
            "Az tema, çok derinlik — okuyan yön tayin edebilmeli.\n"
        )
    else:
        out_schema = (
            '{"analiz":"...(TEK harmanlanmış, akıcı, hikâyeleştirilmiş '
            'AXIOM makro ana anlatısı; alt-başlık YOK)",'
            '"ark_pozisyon_ozeti":"yalnız olgusal: en son snapshot '
            'ağırlıkları, gün-gün delta DEĞİL",'
            '"senaryolar_ve_takip":"ileriye dönük izlenecekler",'
            '"footer":"...(SABIT METIN)"}'
        )
        tgt = ("analiz ~900-1700 kelime (kapsamlı çok-tema + DERİNLİK) "
               "+ kısa ark/senaryo bölümleri hedef")
        depth_directive = (
            "# ADVANCE ODAĞI: KAPSAM + DERİNLİK\n"
            "Haftanın TÜM büyük temalarını işle AMA her birini somut "
            "olay + güncel sayı + AXIOM neden-zinciri + yön ile derinleştir; "
            "tema sayma değil, her tema gerçek analiz.\n"
        )
    return f"""# ROL VE MİSYON
Sen AXIOM'sun: bağımsız bir makro/piyasa analiz sesi. Görevin, CANLI VERİ
ve global gelişmeleri temel alıp, sağlanan arka-plan sinyallerini de
düşünceni beslemek için kullanarak, o haftaya dair TEK, harmanlanmış,
hikâyeleştirilmiş ve TAMAMEN KENDİNE AİT bir makro değerlendirme üretmek.
Bu bir kaynak özeti/aktarımı DEĞİL; verilerden ve kendi muhakemenden
türeyen, hiçbir kişi/kurum adı geçmeyen özgün AXIOM görüşüdür. Trading
sinyali değil, makro bağlam. AMACIN: okuyucuyu haftanın TÜM büyük makro
gelişmelerinden (jeopolitik — ör. ABD-Çin zirvesi/ticaret, İran-Orta
Doğu/barış görüşmeleri; merkez bankaları — Fed faiz patikası/olasılığı
ve başkan değişimi; enerji; kilit veri) hikâye akışı içinde haberdar
etmek VE piyasanın yönü hakkında net bir okuma vermek — yüzeysel değil,
eksiksiz ve bağlantılı.

# GİRDİ
[HAFTA] {pl.prev_iso} - {pl.this_iso}
[CANLI VERİ — BİRİNCİL TEMEL] (makro açıklamalar, piyasa, global gündem)
{pl.live_block or "(canlı veri bu sürümde sınırlı — eldeki veriyle yetin, veri UYDURMA, eksikse bunu doğal cümleyle belirt)"}
[ARKA PLAN SİNYALİ] (yalnız düşünceni besleyen ham girdi — yeniden ÜRETME,
ALINTILAMA, İSİM/kaynak VERME; fikri dönüştürüp kendi muhakemene kat)
{_source_blocks(pl)}

# KESİN VE DEĞİŞMEZ KURALLAR
1. YALNIZCA GİRDİDEKİ SAYILARI KULLAN (öncelik CANLI VERİ). Context'te
   yoksa sayı yazma; ezbere faiz/fiyat/oran ekleme. İhlal → o cümleyi düşür.
   SAYI BİÇİMİ ZORUNLU: her sayıyı GİRDİDE GEÇTİĞİ BİÇİMDE, BİREBİR yaz —
   binlik ayraç (nokta/virgül) veya "bin/milyon" sözcüğü EKLEME. Örn.
   girdi "211000" ise "211000" yaz; "211.000", "211,000", "211 bin"
   YAZMA. Ondalık da girdideki gibi kalsın (3.50 → 3.50). Bu, doğrulama
   için kritiktir; biçim değiştirmek sayıyı geçersiz kılar.
   GÜNCEL SEVİYE KAYNAĞI (KRİTİK — hallüsinasyon önleme): bir varlığın
   GÜNCEL fiyat/seviye/oranını YALNIZ [CANLI VERİ]'deki "GÜNCEL PİYASA
   SEVİYELERİ" ve "MAKRO" bloklarından al. [ARKA PLAN SİNYALİ] ve
   gündem/haber içindeki sayılar BAĞLAMDIR (o kaynağın o günkü yorumu)
   — bunları GÜNCEL gerçek seviye gibi SUNMA. Ör. bir haber metninde
   "10 yıllık faiz 4.40 altında" geçse bile, CANLI VERİ'de tahvil
   faizi YOKSA güncel faiz oranı sayısı YAZMA; "tahvil faizleri
   yüksek/baskı altında" gibi NİTELİKSEL konuş. Elinde güncel sayı
   yoksa sayı UYDURMA — yön/eğilim cümlesi kur.
2. KOPYA/TÜREV YASAĞI (TELİF — KRİTİK): arka-plan sinyalinden 12+ kelime
   ardışık örtüşme YAPMA; cümle yapısını/sözcük dizilişini/argüman
   kurgusunu TAKİP ETME. Her fikri tamamen kendi muhakemenle, sıfırdan,
   farklı çerçeveyle yeniden üret — bir kaynağın özgün analizini "kendi
   görüşün" gibi yakın-parafrazla aktarmak YASAK. Uzun sembol/veri
   listelerini birebir dökme; niteliksel özetle, en çok 2-3 örnek.
3. ATIFSIZ TEK SES (TELİF): metinde HİÇBİR kişi/kurum/kaynak ADI geçmesin
   ("Mahfi", "Eğilmez", "İş Yatırım", "rapora göre", "analist", "uzmanlara
   göre" vb. YASAK). Köşeli parantez/etiket/URL YOK. Her şeyi AXIOM'un
   kendi bağımsız değerlendirmesi olarak, birinci-el ses ile yaz. Arka-plan
   sinyali yalnız fikir kaynağıdır; aktarılmaz, AXIOM'un muhakemesine erir.
4. TEK AXIOM SESİ: çıktı verilerden + global gelişmelerden + dönüştürülmüş
   sinyalden türeyen tek, harmanlı, akıcı bir AXIOM anlatısıdır. "Kaynaklar
   ne diyor" bölümü/ayrımı YOK; doğrudan AXIOM'un okuması.
5. YÖN ETİKETİ ZORLAMA YOK: veri/gelişme net bir yön vermiyorsa zorla yön
   atama; belirsizliği dürüst belirt.
6. BAYAT VERİ DAMGASI: bir veri tarihi eskiyse "{{tarih}} tarihli veriye
   göre" damgası koy ya da hariç tut.
7. YATIRIM TAVSİYESİ YASAĞI: "al/sat" deme, yönlendirme yapma. Makro
   değerlendirme + rasyonel olasılık dili.
8. AXIOM GÖRÜŞÜ + İLERİYE DÖNÜK BEKLENTİ: analiz canlı veri + global
   gelişmeler + sinyalin dönüştürülmüş sentezinden türemeli; dışarıdan
   ezbere iddia ekleme. Anlatının sonunda (a) AXIOM'un net bağımsız
   değerlendirmesi + risk, ve (b) İLERİYE DÖNÜK bir bölüm olsun:
   yaklaşan/beklenen veri ve gelişmelerin olası etkileri, AXIOM'un baz
   senaryosu + alternatif senaryo, hangi tetikleyicide hangi yöne
   gideceği. ŞART: olasılık/senaryo dili ("olabilir, riski artar,
   izlenmeli"); UYDURMA gelecek-rakam/hedef-fiyat YOK (Kural 1), al/sat
   YOK (Kural 7). Öngörü = mekanizma temelli akıl yürütme, kehanet değil.
9. NEDEN-ZİNCİRİ ZORUNLU: olguları sıralama; aktarım mekanizmasıyla
   BAĞLA. Her önemli gelişme için "A olduğu için B → C; çünkü <mekanizma>"
   kur (ör. Hürmüz kapalı → navlun + gemi sigortası primi ↑ ve gübre/
   girdi maliyeti ↑ → gıda enflasyonu baskısı; çünkü …). En az 2-3 somut
   ikincil-etki zinciri kur; yüzeysel "belirsizlik arttı" demekle yetinme.
10. YALNIZ ham geçerli JSON döndür. Markdown başlık/madde, kod fence,
   açıklama, ön-söz YOK. "analiz" akıcı paragraf(lar) olsun (alt-başlık
   yazma). Kelime sınırı rehberdir; uyamasan bile geçerli JSON ver.
11. JSON'un "footer" alanına AYNEN şu metni koy: "{_FOOTER}"
12. SOMUT OLAY ZORUNLU — JENERİK DOLGU YASAK (KRİTİK kalite kuralı):
   Olaylar, veriler ve gelişmeler OLGUDUR, telifli DEĞİLDİR — onları
   SOMUT ve SPESİFİK yaz (örn. "Trump-Xi Pekin zirvesi ve gümrük
   tarifelerini düşürmeye yönelik geçici mutabakat", "NVDA bilançosu",
   "altın ons fiyatındaki sert düşüş" gibi GERÇEK olayı/sayıyı adıyla).
   Telif kuralı (Kural 2/3) yalnız bir kaynağın ÖZGÜN ANALİZ-İFADESİNE
   uygulanır — OLGUYU bulanıklaştırmak için DEĞİL. Şu kalıplar YASAK
   (içi boş): "gerilimler devam etti / belirsizlik sürdü / yakından
   izlenmeli / dengeli seyir / karmaşık bir görünüm" — bunları somut
   olay + güncel sayı + AXIOM çıkarımıyla DOLDUR. Her büyük tema en az
   bir SPESİFİK gelişme (ne oldu, hangi sayı) içermeli; girdide o tema
   için somut bir şey yoksa o temayı kısa geç, uydurma. AYRICA: arka-
   plan sinyalindeki ÖZGÜN, AYIRT EDİCİ tezleri (jenerik makro-klişe
   değil; ör. "AI yatırımı fiyat-duyarsız: bakır +%40, bellek +%150-300
   ama harcama hız kesmiyor" gibi spesifik, sayısal, sıra-dışı argüman)
   kendi muhakemenle DÖNÜŞTÜREREK (Kural 2/3 — atıfsız, parafraz değil)
   anlatıya KAT; bu tezler raporun derinliğidir, jenerik cümleyle
   geçiştirme.
13. YÖNLÜ KONVİKSİYON ZORUNLU (ürün amacı): okuyucu bunu okuyunca
   piyasa hakkında NET bir fikir edinip yön tayin edebilmeli. Anlatı
   ve özellikle yön bölümü; (a) net bir AXIOM duruşu (risk-iştahı
   risk-on mu risk-off mu, NEDEN), (b) hangi varlık sınıfı/temanın
   LEHTE, hangisinin BASKI altında olduğu, (c) izlenecek somut
   seviye/tetikleyici ve "hangi gelişmede yön nasıl değişir"
   vermeli. Gerekçeli ve iddialı ol; "her iki senaryo da mümkün,
   izlenmeli" tarzı kaçamak hedge YASAK. Bu yatırım tavsiyesi DEĞİL
   (Kural 7 geçerli: al/sat deme) ama NET, gerekçeli makro duruş —
   yuvarlak/nötr cümle değil.

# SENTEZ METODOLOJİSİ
Tek bir AXIOM hikâyesi yaz: (1) KAPSAM ZORUNLU — "HAFTANIN ANA
GELİŞMELERİ" bloğundaki TÜM büyük temaları işle: ABD-Çin/ticaret,
İran-Orta Doğu/barış görüşmeleri, Fed (faiz patikası + piyasanın
fiyatladığı artış/indirim olasılığı + başkan değişimi varsa), diğer
merkez bankaları, enflasyon, işgücü, enerji, büyüme/resesyon, tahvil/
kur/likidite, kripto, yapay zeka/teknoloji yatırımı. Girdide belirgin
olan hiçbir BÜYÜK gelişmeyi atlama; her birini bir-iki cümleyle de
olsa anlatıya ÖR (yoksa "veri yok" de, UYDURMA); (2) NEDEN-ZİNCİRLERİ
— gelişmeleri aktarım mekanizmasıyla bağla, ikincil/üçüncül etkileri
aç (ör. boğaz kapanışı → navlun+sigorta primi+girdi maliyeti → gıda/
çekirdek enflasyon kanalı); (3) verilerin birbiriyle çapraz okunması;
(4) AXIOM'un bağımsız görüşü + riskler; (5) PİYASA YÖN-GÖRÜŞÜ — net
bir "piyasa nereye" okuması ver: genel risk iştahı (risk-on/off
eğilimi), hangi varlık sınıfı/temanın öne çıkıp hangisinin baskı
göreceği; olasılık dili, tavsiye/al-sat DEĞİL (Kural 5/7); (6)
İLERİYE DÖNÜK: yaklaşan veri/gelişmeler için baz + alternatif senaryo,
tetikleyiciler (olasılık dili; uydurma rakam/tavsiye YOK). Ayrı
bölümler/atıflar değil tek akış; hiçbir yerde kaynak adı/"rapora
göre" ifadesi olmasın. Kapsamı genişletmek özgünlüğü/atıfsızlığı
(Kural 2/3) ve sayı kuralını (Kural 1) ASLA gevşetmez.

{depth_directive}
# ÇIKTI (JSON; tier={tier}; {tgt})
{out_schema}

TON: profesyonel, temkinli, bağımsız AXIOM sesi; tek anlatıcı. Abartı/
sansasyon yok, tamamen veri odaklı."""


# ---------- Gemini (macro_storyteller forku) ----------

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _parse_json_lenient(s: str) -> Optional[dict]:
    """Gemini bazen STOP (tam çıktı) ama parse-edilemez JSON dönüyor —
    en sık neden: string DEĞERİ İÇİNDE literal \\n/\\t (çok-paragraflı
    'analiz'). strict=False bunları kabul eder → boşa retry'ı keser.
    Sıra: strict → strict=False → trailing-comma temizle + strict=False.
    Başarısızsa None (çağıran retry/empty sayar)."""
    for attempt in (0, 1, 2):
        txt = s if attempt < 2 else _TRAILING_COMMA_RE.sub(r"\1", s)
        try:
            obj = json.loads(txt, strict=(attempt == 0))
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            continue
    return None


async def _call_gemini(prompt: str, *, max_tokens: int = 24000) -> Optional[dict]:
    try:
        from services.gemini_budget import check_budget
        allowed, _u, _c = await check_budget(caller="corporate_synthesis")
        if not allowed:
            logger.warning("corporate_synthesis: gemini budget reddetti")
            return None
    except Exception as e:  # noqa: BLE001 — budget modülü yoksa engelleme
        logger.info(f"check_budget skip: {e}")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("GEMINI_API_KEY missing for corporate_synthesis")
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
    # Büyük haber-yoğun prompt'ta Gemini ARA SIRA boş/RECITATION/OTHER
    # candidate dönüyor (non-determinist). Tek deneme + sessiz None
    # "gemini empty" veriyordu → finishReason logla + üstel backoff'lu
    # 4 deneme. Çoğu transient empty 2. denemede düzeliyor; kalıcı
    # RECITATION ise log ile teşhis edilir (çözüm: prompt'u daha da kıs).
    last_reason = "?"
    for attempt in range(1, 5):
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(url, json=body)
            if resp.status_code != 200:
                last_reason = f"http{resp.status_code}"
                logger.warning(
                    f"corp gemini {resp.status_code} (try{attempt}): "
                    f"{resp.text[:160]}"
                )
            else:
                data = resp.json()
                cands = data.get("candidates") or []
                if not cands:
                    pf = data.get("promptFeedback", {})
                    last_reason = f"no_candidates pf={str(pf)[:120]}"
                else:
                    c0 = cands[0]
                    fin = c0.get("finishReason", "?")
                    parts = c0.get("content", {}).get("parts", [{}])
                    raw = (parts[0].get("text", "") if parts else "").strip()
                    if raw:
                        fence = re.search(r"\{[\s\S]*\}", raw)
                        if fence:
                            parsed = _parse_json_lenient(fence.group(0))
                            if parsed is not None:
                                return parsed
                            last_reason = f"json_decode (fin={fin})"
                        else:
                            last_reason = f"no_json_brace (fin={fin})"
                    else:
                        last_reason = f"empty_text fin={fin}"
        except Exception as e:  # noqa: BLE001
            last_reason = f"exc {type(e).__name__}: {str(e)[:120]}"
        logger.warning(
            f"corp gemini empty (try{attempt}/4) reason={last_reason}"
        )
        if attempt < 4:
            await asyncio.sleep(2 ** attempt)  # 2,4,8s backoff
    logger.error(f"corp gemini GAVE UP after 4 tries: {last_reason}")
    return None


# ---------- Guards ----------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _words(s: str) -> list[str]:
    return _WORD_RE.findall((s or "").lower())


def _ngram_overlap(src_body: str, out_text: str, n: int = 12) -> Optional[str]:
    """src_body ile out_text arasında n+ kelimelik ortak ardışık dizi.
    İlk ihlali döndürür (telif L_DISPLACE), yoksa None."""
    sw = _words(src_body)
    ow = _words(out_text)
    if len(sw) < n or len(ow) < n:
        return None
    src_grams = {
        " ".join(sw[i:i + n]) for i in range(len(sw) - n + 1)
    }
    for i in range(len(ow) - n + 1):
        g = " ".join(ow[i:i + n])
        if g in src_grams:
            return g
    return None


@dataclass
class GuardReport:
    ok: bool = False
    word_count: int = 0
    unknown_numbers: list[str] = field(default_factory=list)
    displaced_ngram: Optional[str] = None
    missing_footer: bool = False
    out_of_bounds: bool = False
    banned: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def _as_text(v) -> str:
    """Gemini bazen bölüm/footer'ı string yerine dict/list döndürüyor;
    string yapraklarını düz metne indir (chip'ler korunur)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return "\n".join(_as_text(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return "\n".join(_as_text(x) for x in v)
    return str(v)


def _assemble_md(tier: Tier, obj: dict) -> str:
    order = (
        ["analiz"]
        if tier == "premium" else
        ["analiz", "ark_pozisyon_ozeti", "senaryolar_ve_takip"]
    )
    parts = []
    titles = {
        "analiz": "AXIOM Makro Analiz",
        "ark_pozisyon_ozeti": "ARK Pozisyon Özeti",
        "senaryolar_ve_takip": "Senaryolar ve Takip",
    }
    for k in order:
        v = _as_text(obj.get(k)).strip()
        if v:
            parts.append(f"## {titles[k]}\n{v}")
    return "\n\n".join(parts)


_SCALE_FACTORS = (
    Decimal(1), Decimal(1000), Decimal("0.001"),
    Decimal(100), Decimal("0.01"),
    Decimal(1_000_000), Decimal("0.000001"),
)


def _reconcile_scale(
    unk: list[str], allowed: set[Decimal],
    tol: Decimal = Decimal("0.02"),
) -> list[str]:
    """validate_numbers'ın 'bilinmeyen' listesini ölçek/biçim farkından
    doğan yanlış-pozitiflerden arındır. Bir token, izinli bir değerin
    ×10^k katıysa (binlik ayraç/ "78K"/"bin" → 78000/78/0.078 vb.)
    gerçek-bilinmeyen DEĞİLDİR. Sadece ELER; asla yeni unknown EKLEMEZ.
    shared validators.py'ye dokunulmaz (macro etkilenmez); telif
    backstop L_DISPLACE 12-gram ayrı ve değişmedi."""
    if not unk or not allowed:
        return unk
    nz = [a for a in allowed if a != 0]
    out: list[str] = []
    for tok in unk:
        try:
            d = Decimal(str(tok))
        except (InvalidOperation, ValueError):
            out.append(tok)
            continue
        matched = False
        for f in _SCALE_FACTORS:
            v = abs(d * f)
            for a in nz:
                aa = abs(a)
                if abs(v - aa) <= aa * tol + Decimal("0.5"):
                    matched = True
                    break
            if matched:
                break
        if not matched:
            out.append(tok)
    return out


def _run_guards(
    tier: Tier, obj: dict, allowed: set[Decimal],
    prose_bodies: list[str],
) -> tuple[str, GuardReport]:
    rep = GuardReport()
    md = _assemble_md(tier, obj)
    footer = _as_text(obj.get("footer")).strip()

    # Footer (tam metin zorunlu)
    if _FOOTER[:40] not in footer:
        rep.missing_footer = True
        rep.reasons.append("footer eksik/yanlış")

    # L5 length
    rep.word_count = len(_words(md))
    lo, hi = _WORD_BOUNDS[tier]
    if rep.word_count < lo or rep.word_count > hi:
        rep.out_of_bounds = True
        rep.reasons.append(f"word_count {rep.word_count} ∉ [{lo},{hi}]")

    # L1 numbers — shared validate_numbers + corporate-local ölçek/ayraç
    # mutabakatı (shared validators.py DEĞİŞMEZ; yalnız yanlış-pozitif
    # eler: binlik ayraç/ölçek "78000≡78≡78.000", "211000≡211.000",
    # "1.16≡1.16000" gibi biçim farkları gerçek-bilinmeyen DEĞİL).
    unk = _reconcile_scale(validate_numbers(md, allowed), allowed)
    if unk:
        rep.unknown_numbers = unk[:10]
        rep.reasons.append(f"unknown_numbers {unk[:5]}")

    # L4 KALDIRILDI: atıfsız tek AXIOM sesi (data-first). Telif güvenliği
    # artık L_DISPLACE 12-gram + Kural 2/3 (özgünlük) + L1 (sayı) üstünde.

    # banned phrases
    low = md.lower()
    rep.banned = [b for b in _BANNED if b in low]
    if rep.banned:
        rep.reasons.append(f"banned {rep.banned}")

    # L_DISPLACE — telif 12-gram
    for body in prose_bodies:
        hit = _ngram_overlap(body, md, n=12)
        if hit:
            rep.displaced_ngram = hit
            rep.reasons.append(f"displaced 12-gram: '{hit[:60]}...'")
            break

    rep.ok = not (
        rep.missing_footer or rep.out_of_bounds or rep.unknown_numbers
        or rep.displaced_ngram or rep.banned
    )
    return md, rep


# ---------- Persist ----------

async def _persist(
    event_id: str, tier: str, week_start: date,
    synthesis_md: str, source_count: int, meta: dict,
) -> bool:
    sql = text("""
        INSERT INTO corporate_syntheses
          (event_id, tier, week_start, synthesis_md, source_count, meta,
           generated_at)
        VALUES (:eid, :tier, :ws, :md, :sc, CAST(:meta AS JSONB), NOW())
        ON CONFLICT (event_id, tier) DO UPDATE SET
          synthesis_md = EXCLUDED.synthesis_md,
          source_count = EXCLUDED.source_count,
          meta         = EXCLUDED.meta,
          generated_at = NOW()
    """)
    async with engine.begin() as conn:
        res = await conn.execute(sql, {
            "eid": event_id, "tier": tier, "ws": week_start,
            "md": synthesis_md, "sc": source_count,
            "meta": json.dumps(meta, default=str),
        })
    return (res.rowcount or 0) > 0


# ---------- Public entry ----------

@dataclass
class SynthResult:
    event_id: str
    tier: str
    week_start: Optional[date] = None
    written: bool = False
    skipped: bool = False
    reason: Optional[str] = None
    word_count: int = 0
    sources: list[str] = field(default_factory=list)


def _allowed_numbers(pl: SynthPayload) -> set[Decimal]:
    vals: list = []
    for r in pl.prose:
        vals.extend(extract_numbers(r.get("body_text") or ""))
        vals.extend(extract_numbers(r.get("title") or ""))
    for a in pl.ark:
        for t in a["top"]:
            vals.append(t["weight_pct"])
        d = a.get("delta")
        if d:
            for x in d.get("increased", []) + d.get("decreased", []):
                vals.append(x["delta_pp"])
            for x in d.get("added", []):
                vals.append(x.get("weight_pct", 0))
    vals.extend(extract_numbers(pl.live_block))
    # Yapısal/anlatı tam sayıları: takvim günü, "son N yıl", madde sayısı,
    # tarih aralığı ("11-15"), küçük delta. Bunlar uydurma finansal istatistik
    # DEĞİL (kanıt: reddedilen sayıların tümü [-15..-1] aralığındaydı) →
    # whitelist'e ekle. Finansal büyüklük/yüzde hâlâ katı kaynak-only.
    vals.extend(range(-31, 32))
    vals.extend(range(1900, 2101))
    return build_allowed_numbers([str(v) for v in vals])


async def synthesize_week(
    *, ref: Optional[date] = None,
    tiers: tuple[Tier, ...] = ("premium", "advance"),
    force: bool = False,
) -> list[SynthResult]:
    """Bir haftanın sentezini üret + corporate_syntheses'e yaz. Broadcast
    YOK. 0 kaynak → skip. Her tier idempotent (event_id,tier)."""
    pl = await build_payload(ref)
    eid = week_event_id(pl.week_start)
    results: list[SynthResult] = []

    if pl.source_count == 0:
        logger.info(f"corporate_synthesis skip: 0 kaynak (week={pl.week_start})")
        for t in tiers:
            results.append(SynthResult(eid, t, pl.week_start, skipped=True,
                                       reason="no sources"))
        return results

    allowed = _allowed_numbers(pl)
    prose_bodies = [r.get("body_text") or "" for r in pl.prose]

    for tier in tiers:
        res = SynthResult(eid, tier, pl.week_start)
        if tier not in ("premium", "advance"):
            res.reason = f"unknown tier {tier}"
            res.skipped = True
            results.append(res)
            continue

        if not force:
            try:
                async with engine.begin() as conn:
                    exists = (await conn.execute(
                        text("SELECT 1 FROM corporate_syntheses "
                             "WHERE event_id=:e AND tier=:t"),
                        {"e": eid, "t": tier},
                    )).first()
                if exists:
                    res.reason = "already exists (force=True ile yenile)"
                    res.skipped = True
                    results.append(res)
                    continue
            except Exception as e:  # noqa: BLE001 — DB yoksa üretime devam
                logger.info(f"exists-check skip: {e}")

        prompt = _build_prompt(tier, pl)
        out = await _call_gemini(prompt)
        if not out:
            res.reason = "gemini empty"
            results.append(res)
            continue

        md, rep = _run_guards(tier, out, allowed, prose_bodies)
        # Gemini non-determinist (özellikle doğal atıf) → 2 retry,
        # attribution eksikse hedefli ipucu ekle.
        attempt = 1
        while not rep.ok and attempt <= 2:
            logger.warning(
                f"corp guard reject (try{attempt}) {eid}/{tier}: {rep.reasons}"
            )
            hint = (
                "\n\n!! ÖNCEKİ DENEME REDDEDİLDİ. Sorunlar: "
                + "; ".join(rep.reasons)
                + "\nSadece bunları düzelt, geçerli JSON döndür."
            )
            if rep.displaced_ngram:
                hint += (
                    "\nTELİF: şu ardışık ifade kaynağa fazla yakın — "
                    f"\"{rep.displaced_ngram[:80]}\" — bu cümleyi tamamen "
                    "farklı kelime ve kurguyla SIFIRDAN yeniden yaz."
                )
            out = await _call_gemini(prompt + hint)
            if not out:
                res.reason = f"guard fail + retry empty ({rep.reasons[:2]})"
                break
            md, rep = _run_guards(tier, out, allowed, prose_bodies)
            attempt += 1
        if not rep.ok:
            if not res.reason:
                res.reason = f"guard still failing: {rep.reasons[:3]}"
                res.word_count = rep.word_count
            results.append(res)
            continue

        # Atıfsız tek AXIOM sesi → Kaynaklar bölümü YOK, yalnız footer.
        final_md = md + "\n\n---\n" + _FOOTER
        meta = {
            "tier": tier,
            "model": GEMINI_MODEL,
            "week_start": pl.week_start.isoformat(),
            "source_count": pl.source_count,
            "sources_cited": [],
            "word_count": rep.word_count,
            "structured": out,
        }
        try:
            written = await _persist(eid, tier, pl.week_start, final_md,
                                     pl.source_count, meta)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"corp persist error {eid}/{tier}: {e}")
            res.reason = f"persist error: {e}"
            results.append(res)
            continue
        res.written = written
        res.word_count = rep.word_count
        res.sources = []
        results.append(res)
        logger.info(
            f"corporate_synthesis OK {eid}/{tier} wc={rep.word_count} "
            f"(atıfsız data-first; broadcast Commit 3'te)"
        )

    return results


async def synthesize_week_safe(**kw) -> list[SynthResult]:
    try:
        return await synthesize_week(**kw)
    except Exception as e:  # noqa: BLE001 — pipeline kırma
        logger.error(f"synthesize_week_safe swallow: {e}")
        return []
