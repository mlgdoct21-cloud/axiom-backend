"""Watchlist diff engine — Plan B akıllı (structured ön-eleme + Gemini sentez).

Akış:
  1) compute_structured_diff(prev, curr) — yapılandırılmış alanları karşılaştır:
       - verdict değişimi (AL/TUT/SAT)
       - conviction kaymaları (>= ±2)
       - target_price ortalama değişimi (>= ±%5)
       - trigger metin değişikliği
       - risk sayısı + yeni risk anahtar kelimeleri
       - stop_level değişimi
     Eğer hiçbir yapısal değişiklik yoksa: [] döner → Gemini call SKİP (maliyet).

  2) summarize_with_gemini(prev, curr, structured) — sadece (1) bir şey
     bulduğunda. Gemini Flash'a iki snapshot + tespit edilen değişiklikler
     verilir, "uzun vadeli yatırımcı veya swing trader için anlamlı bir
     değişiklik var mı, kategorisi ve severity'si nedir, 1-2 cümle Türkçe
     özet" döndürür.

  3) classify_trigger_proximity(payload, last_close) — fiyat trigger/stop/
     target seviyelerine ±%2 yakınsa otomatik diff oluştur (Gemini call yok).

Env: AXIOM_WATCHLIST_DIFF_SMART_ENABLED — default true. Kapalıysa sadece (1) +
(3) çalışır; (2) atlanır ve summary metinleri deterministik şablondan üretilir.
"""
from __future__ import annotations

import os
import re
import json
import asyncio
import logging
from typing import Optional, Any

import requests

from core.logger import get_logger

logger = get_logger("watchlist_diff")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TIMEOUT = 30
SMART_ENABLED = (os.getenv("AXIOM_WATCHLIST_DIFF_SMART_ENABLED", "true").lower() == "true")

# Trigger yakınlık eşiği: fiyat referans seviyesine ±%2 ise mid severity üret.
TRIGGER_NEAR_PCT = 2.0
# Conviction kayması eşik
CONVICTION_DELTA = 2
# Target ortalama değişim eşiği (%)
TARGET_CHANGE_PCT = 5.0


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _avg_targets(payload: dict) -> Optional[float]:
    tl = (payload or {}).get("target_levels") or []
    vals: list[float] = []
    for t in tl:
        try:
            v = t.get("level") if isinstance(t, dict) else t
            if v is None:
                continue
            f = float(re.sub(r"[^0-9.\-]", "", str(v)))
            if f > 0:
                vals.append(f)
        except Exception:
            continue
    if not vals:
        return None
    return sum(vals) / len(vals)


def _parse_price(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(re.sub(r"[^0-9.\-]", "", str(val)))
        return f if f > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1) Structured ön-eleme
# ---------------------------------------------------------------------------
def compute_structured_diff(prev: Optional[dict], curr: dict) -> list[dict]:
    """İki payload arasında yapısal değişiklikleri döner. Her madde:
        {"type": "decision_shift", "severity": "high", "details": {...}}
    `prev` None ise (ilk dossier) — [] döner (diff yok, baseline).
    """
    if not prev or not curr:
        return []

    diffs: list[dict] = []

    # 1.1 Verdict değişimi
    pv = (prev.get("verdict") or "").upper().strip()
    cv = (curr.get("verdict") or "").upper().strip()
    if pv and cv and pv != cv:
        sev = "high" if {pv, cv} & {"AL", "SAT"} else "mid"
        diffs.append({
            "type": "decision_shift",
            "severity": sev,
            "details": {"prev": pv, "curr": cv},
        })

    # 1.2 Conviction büyük kayma
    try:
        pconv = int(prev.get("conviction") or 0)
        cconv = int(curr.get("conviction") or 0)
        if pconv and cconv and abs(cconv - pconv) >= CONVICTION_DELTA:
            diffs.append({
                "type": "conviction_shift",
                "severity": "mid",
                "details": {"prev": pconv, "curr": cconv, "delta": cconv - pconv},
            })
    except Exception:
        pass

    # 1.3 Target ortalama değişimi
    pa = _avg_targets(prev)
    ca = _avg_targets(curr)
    if pa and ca and pa > 0:
        pct = (ca - pa) / pa * 100.0
        if abs(pct) >= TARGET_CHANGE_PCT:
            diffs.append({
                "type": "target_change",
                "severity": "mid" if abs(pct) < 10 else "high",
                "details": {"prev_avg": round(pa, 4), "curr_avg": round(ca, 4), "pct": round(pct, 2)},
            })

    # 1.4 Trigger metin değişimi
    pt = (prev.get("trigger") or "").strip()
    ct = (curr.get("trigger") or "").strip()
    if pt and ct and pt != ct and len(ct) > 10:
        diffs.append({
            "type": "thesis_change",
            "severity": "low",
            "details": {"field": "trigger", "prev": pt[:200], "curr": ct[:200]},
        })

    # 1.5 Stop seviyesi değişimi
    ps = _parse_price(prev.get("stop_level"))
    cs = _parse_price(curr.get("stop_level"))
    if ps and cs and abs(cs - ps) / ps > 0.05:
        diffs.append({
            "type": "stop_change",
            "severity": "mid",
            "details": {"prev": ps, "curr": cs, "pct": round((cs - ps) / ps * 100, 2)},
        })

    # 1.6 Risk sayısı / yeni risk
    prisks = [str(r).lower() for r in (prev.get("risks") or [])]
    crisks = [str(r).lower() for r in (curr.get("risks") or [])]
    new_risks = [r for r in crisks if not any(r[:40] in pr for pr in prisks)]
    if new_risks and len(crisks) > len(prisks):
        diffs.append({
            "type": "risk_escalation",
            "severity": "mid",
            "details": {"new_count": len(new_risks), "new_first": new_risks[0][:200]},
        })

    return diffs


# ---------------------------------------------------------------------------
# 2) Gemini ile sentez (Plan B)
# ---------------------------------------------------------------------------
_DIFF_PROMPT = """Sen Mehmet'in kişisel watchlist analistisin. Aşağıda aynı sembol için iki dossier snapshot var ve aralarında yapısal değişiklikler tespit edildi. Görev: bu değişiklik {category} stratejisi için anlamlı mı, kategori ve severity'si ne, 1-2 cümle Türkçe özet yaz.

Sembol: {symbol}  |  Strateji: {category}  |  Önceki: {prev_ts}  |  Şimdi: {curr_ts}

=== STRUCTURED DEĞIŞIKLIKLER ===
{structured_json}

=== ÖNCEKİ DOSSIER ÖZET ===
verdict: {p_verdict} | conviction: {p_conv} | trigger: {p_trigger}
thesis: {p_thesis}

=== YENİ DOSSIER ÖZET ===
verdict: {c_verdict} | conviction: {c_conv} | trigger: {c_trigger}
thesis: {c_thesis}

KURAL: Sadece JSON döndür. Strateji 'long_term' ise küçük target oynamaları LOW; verdict shift veya yeni şirkete-özel risk MID/HIGH. 'swing' ise trigger/stop yakını HIGH.

ÇIKTI:
{{
  "category": "decision_shift" | "target_change" | "trigger_near" | "risk_escalation" | "thesis_change" | "noise",
  "severity": "low" | "mid" | "high",
  "summary": "1-2 cümle Türkçe — neden önemli, ne yapmalı"
}}
"""


def _call_gemini_sync(prompt: str) -> tuple[Optional[dict], Optional[str]]:
    if not GEMINI_API_KEY or "buraya" in GEMINI_API_KEY:
        return None, "GEMINI_API_KEY yok"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 400,
            "responseMimeType": "application/json",
        },
    }
    try:
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=GEMINI_TIMEOUT)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        cands = data.get("candidates", [])
        if not cands:
            return None, "no candidates"
        txt = cands[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        if not txt:
            return None, "empty"
        try:
            return json.loads(txt), None
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", txt, re.DOTALL)
            if m:
                return json.loads(m.group(0)), None
            return None, f"json parse failed: {txt[:200]}"
    except Exception as e:
        return None, f"call error: {e}"


async def summarize_with_gemini(
    *,
    symbol: str,
    category: str,
    structured: list[dict],
    prev_payload: dict,
    curr_payload: dict,
    prev_ts: str,
    curr_ts: str,
) -> dict:
    """Plan B: 2. Gemini call ile akıllı kategori+severity+Türkçe özet.

    Dönen: {"category": str, "severity": str, "summary": str, "model_error": Optional[str]}
    SMART_ENABLED=false ise deterministik şablon döner.
    """
    # Deterministik şablon (Gemini yoksa veya disabled)
    primary = max(structured, key=lambda d: {"high": 3, "mid": 2, "low": 1}.get(d.get("severity"), 1))
    fallback = {
        "category": primary.get("type", "thesis_change"),
        "severity": primary.get("severity", "low"),
        "summary": _fallback_summary(symbol, primary, prev_payload, curr_payload),
        "model_error": None,
    }

    if not SMART_ENABLED:
        return fallback

    prompt = _DIFF_PROMPT.format(
        symbol=symbol,
        category=category,
        prev_ts=prev_ts,
        curr_ts=curr_ts,
        structured_json=json.dumps(structured, ensure_ascii=False, indent=2),
        p_verdict=prev_payload.get("verdict", "?"),
        p_conv=prev_payload.get("conviction", "?"),
        p_trigger=(prev_payload.get("trigger") or "")[:200],
        p_thesis=(prev_payload.get("thesis") or "")[:300],
        c_verdict=curr_payload.get("verdict", "?"),
        c_conv=curr_payload.get("conviction", "?"),
        c_trigger=(curr_payload.get("trigger") or "")[:200],
        c_thesis=(curr_payload.get("thesis") or "")[:300],
    )

    parsed, err = await asyncio.to_thread(_call_gemini_sync, prompt)
    if not parsed:
        logger.warning(f"watchlist_diff Gemini fail {symbol}: {err}")
        return {**fallback, "model_error": err}

    cat = parsed.get("category", fallback["category"])
    sev = parsed.get("severity", fallback["severity"])
    summary = (parsed.get("summary") or fallback["summary"]).strip()
    if cat == "noise":
        # Gemini "gürültü" derse severity'yi low'a çek
        sev = "low"
    return {"category": cat, "severity": sev, "summary": summary, "model_error": None}


def _fallback_summary(symbol: str, primary: dict, prev: dict, curr: dict) -> str:
    t = primary.get("type")
    d = primary.get("details", {})
    if t == "decision_shift":
        return f"{symbol}: karar {d.get('prev')} → {d.get('curr')} oldu. Tezi yeniden değerlendir."
    if t == "target_change":
        pct = d.get("pct", 0)
        yon = "yükseldi" if pct > 0 else "düştü"
        return f"{symbol}: hedef ortalaması %{abs(pct):.1f} {yon}. Risk/ödül oranını gözden geçir."
    if t == "conviction_shift":
        return f"{symbol}: ikna seviyesi {d.get('prev')}→{d.get('curr')}. Tez güç kaybediyor/kazanıyor olabilir."
    if t == "risk_escalation":
        return f"{symbol}: yeni risk maddesi eklendi — '{(d.get('new_first') or '')[:120]}'."
    if t == "stop_change":
        pct = d.get("pct", 0)
        return f"{symbol}: stop seviyesi %{abs(pct):.1f} kaydı. Pozisyon koruma hattını güncelle."
    if t == "thesis_change":
        return f"{symbol}: tetik koşulu değişti. Yeni tetik dossier'da."
    return f"{symbol}: dossier'da değişiklik var."


# ---------------------------------------------------------------------------
# 3) Trigger/stop/target fiyat yakınlığı (Gemini call yok, ucuz)
# ---------------------------------------------------------------------------
def classify_trigger_proximity(payload: dict, last_close: Optional[float]) -> Optional[dict]:
    """Fiyat target/stop seviyelerine ±TRIGGER_NEAR_PCT% yakınsa diff üret.

    Dönen: None veya {"type": "trigger_near", "severity": "mid"|"high", "details": {...}, "summary": str}
    """
    if not payload or last_close is None or last_close <= 0:
        return None

    levels: list[tuple[str, float]] = []
    sl = _parse_price(payload.get("stop_level"))
    if sl:
        levels.append(("stop", sl))
    for t in payload.get("target_levels") or []:
        if isinstance(t, dict):
            v = _parse_price(t.get("level"))
            if v:
                levels.append((f"hedef ({(t.get('rationale') or '')[:60]})", v))

    closest = None
    closest_pct = 1e9
    for label, lvl in levels:
        pct = abs((last_close - lvl) / lvl * 100)
        if pct < closest_pct:
            closest_pct = pct
            closest = (label, lvl, pct)

    if not closest or closest_pct > TRIGGER_NEAR_PCT:
        return None

    label, lvl, pct = closest
    sev = "high" if closest_pct <= 0.7 else "mid"
    yon = "üstünde" if last_close > lvl else "altında"
    return {
        "type": "trigger_near",
        "severity": sev,
        "details": {"label": label, "level": lvl, "last_close": last_close, "pct": round(pct, 2)},
        "summary": f"Fiyat {lvl:g} seviyesindeki {label}'in %{pct:.2f} {yon}. Aksiyon değerlendir.",
    }
