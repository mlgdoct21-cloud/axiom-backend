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
from decimal import Decimal
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
_HTTP_TIMEOUT = httpx.Timeout(40.0, connect=8.0)

# Türkiye kalıcı UTC+3 (DST yok, 2016+) — sabit offset, tzdata yok.
_TR_TZ = timezone(timedelta(hours=3))
_PUBLISH_TIME = time(8, 30)

_PROSE_SOURCES = ["mahfi", "isyatirim"]
_ARK_FUNDS = ["ARKK", "ARKW", "ARKG", "ARKQ", "ARKF"]

# Prompt Kural 9 kelime sınırını "rehber" ilan ediyor; bu yüzden guard
# sert hedef değil runaway-çıktıyı yakalayan sanity-tavan olmalı (gözlem:
# premium ~440-480, advance ~750-990 — eski (400/700) tavan normal çıktıyı
# reddediyordu).
_WORD_BOUNDS: dict[str, tuple[int, int]] = {
    "premium": (120, 700),
    "advance": (300, 1150),
}

# Kaynak kayıt defteri → sondaki Kaynaklar listesi (ad+URL; kod ekler,
# model URL yazmaz). Atıf metinde doğal AD ile yapılır (chip değil).
_SOURCE_REGISTRY: dict[str, dict] = {
    "MAHFI": {"name": "Mahfi Eğilmez", "url": "https://www.mahfiegilmez.com/"},
    "ISYATIRIM": {"name": "İş Yatırım Araştırma",
                  "url": "https://arastirma.isyatirim.com.tr/"},
    "ARK": {"name": "ARK Invest (kamuya açık fon bildirimi)",
            "url": "https://ark-funds.com/"},
    "GUNDEM": {"name": "Piyasa gündemi (tema-radar)", "url": ""},
}
_LABEL_FOR_SOURCE = {"mahfi": "MAHFI", "isyatirim": "ISYATIRIM"}

# Doğal (cümle-içi) atıf tespiti — köşeli-parantez chip yerine kaynak
# adının metinde geçmesi yeterli (telif: atıf zorunlu, formu serbest).
_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "MAHFI": ("Mahfi",),
    "ISYATIRIM": ("İş Yatırım", "Is Yatirim"),
    "ARK": ("ARK",),
    "GUNDEM": ("piyasa gündemi", "gündem"),
}


def _names_present(md: str, labels: set[str]) -> set[str]:
    """labels içinden adı md'de (case-insensitive) geçenler."""
    low = md.casefold()
    out = set()
    for lb in labels:
        for a in _NAME_ALIASES.get(lb, ()):
            if a in md or a.casefold() in low:
                out.add(lb)
                break
    return out

_FOOTER = (
    "Bu içerik AXIOM'un bağımsız makro değerlendirmesidir; adı geçen kişi "
    "ve kurumlar yatırım tavsiyesi vermez. Kaynaklar: özgün içeriğe "
    "bağlantılar yukarıdadır."
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
        live_block="",  # Commit 3+ zengin grounding; şimdilik boş (prompt tolere eder)
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
        tgt = "analiz ~250-550 kelime hedef"
    else:
        out_schema = (
            '{"analiz":"...(TEK harmanlanmış, akıcı, hikâyeleştirilmiş '
            'AXIOM makro ana anlatısı; alt-başlık YOK)",'
            '"ark_pozisyon_ozeti":"yalnız olgusal: en son snapshot '
            'ağırlıkları, gün-gün delta DEĞİL",'
            '"senaryolar_ve_takip":"ileriye dönük izlenecekler",'
            '"footer":"...(SABIT METIN)"}'
        )
        tgt = "analiz ~500-1000 kelime + kısa ark/senaryo bölümleri hedef"
    return f"""# ROL VE MİSYON
Sen AXIOM'un bağımsız makro/piyasa sentez editörüsün. Görevin: sana sağlanan
TÜM kaynakları oku ve TEK, harmanlanmış, hikâyeleştirilmiş bir "AXIOM makro
analizi" üret. Bu kaynak-kaynak özet DEĞİL; hepsini tek bir akıcı anlatıda
eritip o haftaya dair bağımsız AXIOM görüşü veren bir sentez katmanıdır.
Trading sinyali değil, makro bağlam sağlar.

# GİRDİ
[HAFTA] {pl.prev_iso} - {pl.this_iso}
[KAYNAKLAR] (her biri: etiket | tarih | tip | başlık | gövde/veri)
{_source_blocks(pl)}
[CANLI VERİ BAĞLAMI]
{pl.live_block or "(bu sürümde canlı veri bloğu boş — yoksa çapraz okumada bunu belirt, veri uydurma)"}

# KESİN VE DEĞİŞMEZ KURALLAR
1. YALNIZCA GİRDİDEKİ SAYILARI KULLAN. Context'te yoksa sayı yazma; geçmiş
   bilgini/faiz/fiyat ekleme. İhlal → o cümleyi düşür.
2. ARDIŞIK ALINTI YASAĞI (TELİF): hiçbir kaynaktan 12+ kelimelik ardışık
   alıntı/örtüşme YAPMA. Harmanlı anlatıda kaynağın cümle yapısını/sözcük
   dizilişini TAKİP ETME; her fikri tamamen KENDİ kelimelerinle, farklı
   cümle kurgusuyla yeniden ifade et. Uzun sembol/hisse/veri listelerini
   (ör. 4+ ardışık ticker veya rakam dizisi) BİREBİR DÖKME; bunları
   niteliksel özetle ("çok sayıda banka ve sanayi hissesi", "geniş bir
   hisse grubu" gibi), en çok 2-3 örnek ver.
3. DOĞAL ATIF ZORUNLULUĞU (TELİF): her iddiayı kaynağına AD ile, cümle
   içinde doğal şekilde atfet — "Mahfi Eğilmez'e göre…", "İş Yatırım
   raporları işaret ediyor ki…", "ARK fonlarının açıklanan pozisyonlarına
   göre…". KÖŞELİ PARANTEZ / [ETİKET] / kod-işareti KULLANMA; akıcı düz
   metin yaz. ARK = yalnız olgusal pozisyon (pay/ağırlık), niyet/yorum
   atfetme. URL/bağlantı YAZMA — kod ekleyecek. Atıfsız iddia YASAK.
4. HARMANLAMA: kaynakları tek anlatıda ört; nerede hemfikir/ayrışıyorlarsa
   akış içinde göster. "çelişki" yalnız birden çok kaynak varken yazılır;
   tek kaynak varsa çelişki UYDURMA, kaynak azlığını doğal cümleyle belirt.
5. YÖN ETİKETİ ZORLAMA YOK: bir kaynak boğa/ayı/temkinli yön belirtmiyorsa
   ona yön etiketi ATAMA; görüşünü olduğu gibi aktar.
6. BAYAT VERİ DAMGASI: bir kaynağın tarihi eskiyse "{{tarih}} tarihli veriye
   göre" damgası koy ya da hariç tut.
7. YATIRIM TAVSİYESİ YASAĞI: "al/sat" deme, yönlendirme yapma. Makro
   değerlendirme + rasyonel olasılık dili.
8. AXIOM GÖRÜŞÜ TÜREMELİ: analiz kaynakların kesişim/ayrışımı + canlı veri
   çapraz okumasından türemeli; dışarıdan yeni iddia ekleme. Anlatının
   sonunda AXIOM'un bağımsız değerlendirmesi + risk perspektifi olsun.
9. YALNIZ ham geçerli JSON döndür. Markdown başlık/madde, kod fence,
   açıklama, ön-söz YOK. "analiz" akıcı paragraf(lar) olsun (alt-başlık
   yazma). Kelime sınırı rehberdir; uyamasan bile geçerli JSON ver.
10. JSON'un "footer" alanına AYNEN şu metni koy: "{_FOOTER}"

# SENTEZ METODOLOJİSİ
Tüm kaynakları tek bir hikâyede birleştir: haftanın resmi → kaynakların ne
dediği (ada doğal atıfla, harmanlı) → canlı veriyle çapraz okuma (veri yoksa
veri yok de, uydurma) → AXIOM'un bağımsız görüşü ve riskler. Bunları AYRI
bölümler değil, tek akış olarak yaz.

# ÇIKTI (JSON; tier={tier}; {tgt})
{out_schema}

TON: profesyonel, temkinli, bağımsız AXIOM sesi; tek anlatıcı. Abartı/
sansasyon yok, tamamen veri odaklı."""


# ---------- Gemini (macro_storyteller forku) ----------

async def _call_gemini(prompt: str, *, max_tokens: int = 8192) -> Optional[dict]:
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
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"corp gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"corp gemini call failed: {e}")
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
    missing_attribution: bool = False
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


def _run_guards(
    tier: Tier, obj: dict, allowed: set[Decimal],
    prose_bodies: list[str], allowed_labels: set[str],
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

    # L1 numbers
    unk = validate_numbers(md, allowed)
    if unk:
        rep.unknown_numbers = unk[:10]
        rep.reasons.append(f"unknown_numbers {unk[:5]}")

    # L4 attribution — kaynak adı doğal cümlede geçmeli (chip değil)
    if not _names_present(md, allowed_labels):
        rep.missing_attribution = True
        rep.reasons.append("kaynak adı atfı yok")

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
        or rep.missing_attribution or rep.displaced_ngram or rep.banned
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
    allowed_labels = {
        _LABEL_FOR_SOURCE.get(r.get("source", ""), "")
        for r in pl.prose
    }
    allowed_labels.discard("")
    if pl.ark:
        allowed_labels.add("ARK")

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

        md, rep = _run_guards(tier, out, allowed, prose_bodies, allowed_labels)
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
            if rep.missing_attribution:
                hint += (
                    "\nİddiaları kaynağına AD ile doğal cümlede atfet "
                    "(ör. 'Mahfi Eğilmez'e göre…', 'İş Yatırım raporları…'); "
                    "köşeli parantez/etiket KULLANMA."
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
            md, rep = _run_guards(tier, out, allowed, prose_bodies,
                                  allowed_labels)
            attempt += 1
        if not rep.ok:
            if not res.reason:
                res.reason = f"guard still failing: {rep.reasons[:3]}"
                res.word_count = rep.word_count
            results.append(res)
            continue

        cited = sorted(
            _names_present(md, set(_SOURCE_REGISTRY)) or allowed_labels
        )
        kaynaklar = "\n".join(
            f"- {_SOURCE_REGISTRY[c]['name']}"
            + (f" — {_SOURCE_REGISTRY[c]['url']}" if _SOURCE_REGISTRY[c]['url'] else "")
            for c in cited
        )
        final_md = md + "\n\n## Kaynaklar\n" + kaynaklar + "\n\n---\n" + _FOOTER
        meta = {
            "tier": tier,
            "model": GEMINI_MODEL,
            "week_start": pl.week_start.isoformat(),
            "source_count": pl.source_count,
            "sources_cited": cited,
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
        res.sources = cited
        results.append(res)
        logger.info(
            f"corporate_synthesis OK {eid}/{tier} wc={rep.word_count} "
            f"sources={cited} (broadcast Commit 3'te)"
        )

    return results


async def synthesize_week_safe(**kw) -> list[SynthResult]:
    try:
        return await synthesize_week(**kw)
    except Exception as e:  # noqa: BLE001 — pipeline kırma
        logger.error(f"synthesize_week_safe swallow: {e}")
        return []
