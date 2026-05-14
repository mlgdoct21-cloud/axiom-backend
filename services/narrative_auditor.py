"""
Layer 2 — Narrative Auditor Agent

Storyteller'ın ürettiği metni yayınlamadan önce 2. bir Gemini çağrısıyla
SEMANTİK denetim yapar. Tek tek metrik contract'larına ('CAN_claim' /
'CANNOT_claim' kelime hazinesi) karşı metni denetler.

Yakaladığı hata sınıfları:
  - OVERCLAIM: contract.CANNOT_claim'de bulunan iddia (örn coinbase_premium
    için "kurumsal" — sadece spot fiyat farkıdır)
  - UNSUPPORTED: contract'ta yer almayan vehicle/window iddiası
  - WRONG_WINDOW: anlık metriği trend gibi sunma (ya da tersi)
  - VEHICLE_CONFUSION: ETF (kurumsal) iddiasını Coinbase Premium'a (spot)
    bağlamak

Çıktı: { ok: bool, issues: [{claim, source_metric, verdict, reason}] }

Auditor'ın bulduğu issue varsa caller (storyteller) Gemini'yi audit
notlarıyla 1 kez yeniden çağırır; hâlâ varsa fallback deterministic
template kullanır.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx

from core.logger import get_logger
from data.metric_contracts import CONTRACTS

logger = get_logger("crypto.auditor")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


# Audit'a giren metriklerin contract bilgisini sıkıştır.
def _build_relevant_contracts(present_metrics: list[str]) -> dict:
    out = {}
    for m in present_metrics:
        c = CONTRACTS.get(m)
        if not c:
            continue
        out[m] = {
            "display_tr": c.get("display_tr"),
            "measures": c.get("measures"),
            "window": c.get("window"),
            "vehicle": c.get("vehicle"),
            "CAN_claim": c.get("CAN_claim", []),
            "CANNOT_claim": c.get("CANNOT_claim", []),
            "reconcile_with": c.get("reconcile_with", []),
        }
    return out


def _build_audit_prompt(narrative_text: str, ctx: dict) -> str:
    present_metrics = [s.get("metric") for s in (ctx.get("signals") or []) if s.get("metric")]
    if ctx.get("etf_flow"):
        present_metrics.append("etf_flow")
    contracts = _build_relevant_contracts(present_metrics)

    return (
        "Sen 'Axiom Veri Denetçisi' agent'ısın. Görevin: storyteller'ın "
        "ürettiği metni metric contract'lara karşı SEMANTİK doğrulamak. "
        "Bir iddia metric'in CAN_claim listesinde olmayan veya CANNOT_claim "
        "listesinde olan kelime/kavram içeriyorsa OVERCLAIM/UNSUPPORTED.\n\n"
        "### METNİ KONTROL ET:\n"
        f"```\n{narrative_text}\n```\n\n"
        "### METRIK CONTRACT'LARI (referans gerçeklik):\n"
        f"{json.dumps(contracts, ensure_ascii=False, indent=2)}\n\n"
        "### KESİN KURALLAR:\n"
        "1. Coinbase Premium 'kurumsal' iddia EDEMEZ — sadece spot fiyat "
        "farkıdır (kim alıyor/satıyor söyleyemez). 'Kurumsal' iddia için "
        "TEK izinli kaynak etf_flow.\n"
        "2. MPI'yı 'satış baskısı endeksi' diye SUNMA — eşik metriğidir, "
        "yön sinyali değil. 'Madenci Pozisyon Endeksi' doğru çeviri.\n"
        "3. Stablecoin için 'alım gücü' iddiası NETFLOW pozitifse mantıklı; "
        "metin gross inflow'a dayanıp 'alım gücü' diyorsa UNSUPPORTED.\n"
        "4. miner_outflow ANLIK günlük spike; miner_reserve TREND. Aynı "
        "paragrafta yan yana çıkıyorsa açıklayıcı uzlaştırma cümlesi şart.\n\n"
        "### ÇIKTI ŞEMASI (sadece JSON):\n"
        "{\n"
        '  "ok": boolean,\n'
        '  "issues": [\n'
        '    {\n'
        '      "claim": string,        // metindeki problemli ifadenin alıntısı\n'
        '      "source_metric": string,// hangi metrik bağlamında\n'
        '      "verdict": "OVERCLAIM"|"UNSUPPORTED"|"WRONG_WINDOW"|"VEHICLE_CONFUSION"|"OK",\n'
        '      "reason": string        // contracta referansla 1-2 cumle aciklama\n'
        '    }\n'
        "  ]\n"
        "}\n\n"
        "ok=true SADECE issues listesi BOŞSA. En ufak şüphede issue ekle. "
        "issues[].verdict='OK' kabul ETMEM — gerçek issue ise OVERCLAIM/"
        "UNSUPPORTED/WRONG_WINDOW/VEHICLE_CONFUSION yaz."
    )


async def _call_gemini_audit(prompt: str) -> Optional[dict]:
    from services.gemini_budget import check_budget
    allowed, _used, _cap = await check_budget(caller="narrative_auditor")
    if not allowed:
        return None
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("auditor: GEMINI_API_KEY missing")
        return None
    url = GEMINI_URL.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,  # düşük; deterministik denetim
            "maxOutputTokens": 3000,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"auditor gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"auditor gemini call failed: {e}")
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
    payload = fence.group(0) if fence else raw
    try:
        return json.loads(payload)
    except Exception as e:
        logger.warning(f"auditor json parse failed: {e}; raw={raw[:200]}")
        return None


async def audit_narrative(
    headline: str,
    paragraphs: list[str],
    footer: str,
    ctx: dict,
) -> dict:
    """Storyteller output'unu audit et. Returns {ok, issues}.

    Caller akışı:
      result = await audit_narrative(...)
      if not result["ok"] and len(result["issues"]) > 0:
          # storyteller'a issues feed back, retry once
    """
    narrative_text = "\n\n".join(
        [headline, *paragraphs, footer]
    )
    prompt = _build_audit_prompt(narrative_text, ctx)
    audit = await _call_gemini_audit(prompt)
    if not audit:
        # Auditor down → caller fail-open (audit yok, eski validator devam etsin)
        logger.warning("auditor: response unavailable, fail-open")
        return {"ok": True, "issues": [], "_meta": "auditor_unavailable"}

    # Şema doğrulama
    if not isinstance(audit, dict):
        return {"ok": True, "issues": [], "_meta": "auditor_invalid_response"}
    issues_raw = audit.get("issues") or []
    if not isinstance(issues_raw, list):
        return {"ok": True, "issues": [], "_meta": "auditor_invalid_issues"}

    real_issues = [
        i for i in issues_raw
        if isinstance(i, dict) and i.get("verdict") in (
            "OVERCLAIM", "UNSUPPORTED", "WRONG_WINDOW", "VEHICLE_CONFUSION"
        )
    ]

    return {
        "ok": len(real_issues) == 0,
        "issues": real_issues,
        "_meta": f"auditor_v1 · {len(real_issues)} issues from {len(issues_raw)} raw",
    }
