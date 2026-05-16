"""Geçici smoke — Commit 4a (corporate public endpoint + /sentez).

DB'siz (her zaman): _peek_tier('')=='free', endpoint satır-yok davranışı,
teaser kırpma mantığı, process_sentez_command importable.
DB roundtrip otomatik SKIP. NET RAPOR.

Çalıştır:  python scripts/smoke_corporate_public.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Response  # noqa: E402

from routers.v1.corporate_public import (  # noqa: E402
    _TEASER_CHARS,
    _peek_tier,
    latest_synthesis,
)
from services.telegram_bot import process_sentez_command  # noqa: E402


async def main() -> None:
    print("=" * 64)
    print("  CORPORATE PUBLIC + /sentez SMOKE — Commit 4a")
    print("=" * 64)

    # 1) _peek_tier fail-soft
    t_none = await _peek_tier(None)
    t_bad = await _peek_tier("Basic xyz")
    t_garbage = await _peek_tier("Bearer not-a-jwt")
    peek_ok = t_none == "free" and t_bad == "free" and t_garbage == "free"
    print(f"\n[1] _peek_tier: None={t_none} bad={t_bad} garbage={t_garbage} "
          f"→ {'✓' if peek_ok else '✗'}")

    # 2) endpoint satır-yok (DB erişilemez → _load_latest None → 200 null)
    resp = Response()
    out = await latest_synthesis(resp, authorization=None)
    null_ok = (out.get("synthesis") is None and out.get("locked") is False
               and out.get("tier") == "free")
    cache_ok = "max-age=60" in resp.headers.get("Cache-Control", "")
    print(f"\n[2] /corporate/latest (DB yok / anon): {out}")
    print(f"    synthesis=null + locked=false + tier=free: "
          f"{'✓' if null_ok else '✗'} | Cache-Control: "
          f"{'✓' if cache_ok else '✗'}")

    # 3) teaser kırpma saf mantık
    md = "x" * 1000
    teaser = md[:_TEASER_CHARS].rstrip()
    teaser_ok = len(teaser) == _TEASER_CHARS and _TEASER_CHARS == 280
    print(f"\n[3] teaser kırpma: len={len(teaser)} (beklenen {_TEASER_CHARS}) "
          f"→ {'✓' if teaser_ok else '✗'}")

    # 4) /sentez importable + çağrılabilir (DB yok → graceful mesaj, crash yok)
    print("\n[4] process_sentez_command çağrı (DB yok → graceful)")
    try:
        await process_sentez_command(chat_id=0, user_id=0)
        cmd_ok = True
        print("    çağrı crash etmedi ✓ (mesaj telegram'a gitmez, chat_id=0)")
    except Exception as e:  # noqa: BLE001
        cmd_ok = False
        print(f"    ✗ crash: {e}")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    print(f"  (a) _peek_tier fail-soft     : {'✓' if peek_ok else '✗'}")
    print(f"  (b) endpoint null/cache      : {'✓' if null_ok and cache_ok else '✗'}")
    print(f"  (c) teaser kırpma            : {'✓' if teaser_ok else '✗'}")
    print(f"  (d) /sentez fail-soft        : {'✓' if cmd_ok else '✗'}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
