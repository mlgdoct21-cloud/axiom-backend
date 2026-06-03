"""Watchlist AI Daily Brief — Gemini sentez (3-4 satır Türkçe).

Kullanıcının watchlist'ini tarar; her sembolün son dossier'ını (verdict,
trigger, target, last_close) toplar; tek bir Gemini 2.5 Flash call ile
"Bugün dikkat" özet liste üretir:

  ["NVDA earnings öncesi son gün, hedef $185 yakın",
   "AAPL Çin riski 3. günü artıyor, $220 desteği test",
   "BTC squeeze daralıyor, $72k üstü tetik"]

6 saat in-memory cache (kullanıcı-bazlı). Watchlist boşsa [] döner.
"""
import os
import re
import json
import asyncio
import logging
from typing import Optional, Any

import requests
from sqlalchemy import text

from core.database import AsyncSessionLocal
from core.logger import get_logger

logger = get_logger("watchlist_brief")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TIMEOUT = 30
CACHE_TTL_SECONDS = 6 * 3600  # 6 saat

# {user_id: (timestamp, brief_list)}
_CACHE: dict[int, tuple[float, list[str]]] = {}


def _now() -> float:
    import time
    return time.time()


_BRIEF_PROMPT = """Sen Mehmet'in kişisel watchlist analistisin. Aşağıda watchlist sembollerinin son dossier özetleri var. Görev: trader gözüyle BUGÜN dikkat edilmesi gereken TOP 3-5 maddeyi Türkçe, kısa, somut tetik/seviye/yargıyla yaz.

KURAL:
- Her madde TEK CÜMLE, max 90 karakter.
- SOMUT seviye/tarih/rakam ZORUNLU (ezbere "izle"/"dikkat" yok).
- Sıra: aciliyet/önem azalan. Tetiği yakın olanlar üstte.
- "Veri eksik" sembolleri ATLA.
- En fazla 5 madde, en az 3 madde.

WATCHLIST DOSSIER ÖZETLERİ:
{snapshots_json}

ÇIKTI (saf JSON dizi, başka metin yazma):
{{"items": ["NVDA earnings 26 Tem, $185 hedef yakın", "AAPL Çin riski sürüyor, $220 desteği test", "..."]}}
"""


def _call_gemini(prompt: str) -> tuple[Optional[list[str]], Optional[str]]:
    if not GEMINI_API_KEY or "buraya" in GEMINI_API_KEY:
        return None, "GEMINI_API_KEY yok"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 800,
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
        parsed = json.loads(txt)
        items = parsed.get("items", [])
        if isinstance(items, list):
            return [str(i)[:120] for i in items if i], None
        return None, "items not list"
    except json.JSONDecodeError:
        # markdown wrap fallback
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if m:
            try:
                items = json.loads(m.group(0)).get("items", [])
                return [str(i)[:120] for i in items if i], None
            except Exception:
                pass
        return None, "json parse"
    except Exception as e:
        return None, f"call error: {e}"


async def get_daily_brief(user_id: int, force_refresh: bool = False) -> dict:
    """Kullanıcının watchlist'i için AI Daily Brief. 6 saat cache.

    Dönen: {"items": [...], "from_cache": bool, "generated_at": iso, "error": Optional[str]}
    """
    from datetime import datetime, timezone

    # Cache kontrol
    if not force_refresh:
        cached = _CACHE.get(user_id)
        if cached and (_now() - cached[0]) < CACHE_TTL_SECONDS:
            return {
                "items": cached[1],
                "from_cache": True,
                "generated_at": datetime.fromtimestamp(cached[0], tz=timezone.utc).isoformat(),
                "error": None,
            }

    # Watchlist + son dossier snapshot topla
    async with AsyncSessionLocal() as db:
        sql = text("""
            SELECT w.symbol, w.category, w.avg_cost, w.qty,
                   d.payload, d.created_at
            FROM watchlist_items w
            LEFT JOIN LATERAL (
                SELECT payload, created_at FROM trade_dossiers
                WHERE symbol = w.symbol ORDER BY id DESC LIMIT 1
            ) d ON true
            WHERE w.user_id = :uid
            ORDER BY w.category, w.symbol
        """)
        res = await db.execute(sql, {"uid": user_id})
        rows = res.fetchall()

    if not rows:
        return {"items": [], "from_cache": False, "generated_at": None, "error": "watchlist boş"}

    # Snapshot'ları kompaktla
    snapshots = []
    for r in rows:
        symbol, category, avg_cost, qty, payload, created_at = r
        p = payload or {}
        ta = (p.get("_data") or {}).get("ta") or {}
        snapshots.append({
            "symbol": symbol,
            "category": category,
            "avg_cost": float(avg_cost) if avg_cost else None,
            "qty": float(qty) if qty else None,
            "verdict": p.get("verdict"),
            "conviction": p.get("conviction"),
            "trigger": (p.get("trigger") or "")[:140],
            "stop_level": p.get("stop_level"),
            "targets": p.get("target_levels"),
            "last_close": ta.get("last_close"),
            "perf_7d_pct": ta.get("perf_7d_pct"),
            "rsi14": ta.get("rsi14"),
            "above_ema50": ta.get("above_ema50"),
            "dossier_age_h": (
                (_now() - created_at.timestamp()) / 3600.0 if created_at else None
            ),
        })

    prompt = _BRIEF_PROMPT.format(
        snapshots_json=json.dumps(snapshots, ensure_ascii=False, indent=2),
    )
    items, err = await asyncio.to_thread(_call_gemini, prompt)
    items = items or []

    # Cache yaz (boş olsa bile, tekrar Gemini patlatmayalım)
    _CACHE[user_id] = (_now(), items)

    return {
        "items": items,
        "from_cache": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error": err,
    }
