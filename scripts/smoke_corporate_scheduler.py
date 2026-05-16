"""Geçici smoke — Kurumsal Sentez Commit 3 (scheduler + broadcast).

DB'siz/Gemini'siz (her zaman): weekly tick hesabı, broadcast kill-switch
default OFF kanıtı, _poll_once fail-soft, _ark_delta fail-soft.
DB/Gemini roundtrip otomatik SKIP (önceki commit deseni).

Çalıştır:  python scripts/smoke_corporate_scheduler.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.corporate_scheduler import (  # noqa: E402
    _next_weekly_tick,
    _poll_once,
    _week_start_for,
)
from services.corporate_synthesis import _ark_delta  # noqa: E402
from services.corporate_broadcaster import _enabled, broadcast_synthesis  # noqa: E402


async def main() -> None:
    print("=" * 64)
    print("  KURUMSAL SENTEZ SCHEDULER SMOKE — Commit 3")
    print("=" * 64)

    # 1) Weekly tick — bilinen Cumartesi 2026-05-16 12:00 UTC
    ref = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
    secs, tick = _next_weekly_tick(ref)
    tick_tr_wd = tick.astimezone(timezone.utc)
    # 08:30 TR = 05:30 UTC; sonraki Pazartesi 2026-05-18
    wk_start, this0830 = _week_start_for(ref)
    print("\n[1] Weekly tick (ref=Cmt 2026-05-16 12:00Z)")
    print(f"    next tick UTC = {tick.isoformat()}  (+{secs/3600:.1f}h)")
    tick_ok = (tick.weekday() == 0 and tick.hour == 5 and tick.minute == 30
               and tick.date().isoformat() == "2026-05-18")
    print(f"    Pazartesi 05:30 UTC (=08:30 TR) 2026-05-18: "
          f"{'✓' if tick_ok else '✗'}")
    print(f"    _week_start_for → week_start={wk_start} "
          f"this0830UTC={this0830.isoformat()}")
    ws_ok = wk_start.isoformat() == "2026-05-04" and this0830.hour == 5

    # 2) Broadcast kill-switch default OFF
    print("\n[2] Broadcast kill-switch (default OFF)")
    os.environ.pop("CORPORATE_SYNTH_BROADCAST_ENABLED", None)
    en = _enabled()
    res = await broadcast_synthesis("deadbeef", "premium")
    off_ok = (en is False) and res.get("skipped_disabled") is True
    print(f"    _enabled()={en}  broadcast→{res}")
    print(f"    default OFF kanıt: {'✓' if off_ok else '✗'}")
    os.environ["CORPORATE_SYNTH_BROADCAST_ENABLED"] = "true"
    en_on = _enabled()
    os.environ.pop("CORPORATE_SYNTH_BROADCAST_ENABLED", None)
    print(f"    env=true → _enabled()={en_on} (toggle çalışıyor: "
          f"{'✓' if en_on else '✗'})")

    # 3) _poll_once fail-soft (network var; DB yok → ingest 0, crash yok)
    print("\n[3] _poll_once fail-soft")
    try:
        out = await _poll_once()
        poll_ok = isinstance(out, list) and len(out) == 3
        print(f"    {out}")
        print(f"    3 kaynak sonucu, crash yok: {'✓' if poll_ok else '✗'}")
    except Exception as e:  # noqa: BLE001
        poll_ok = False
        print(f"    ✗ crash: {e}")

    # 4) _ark_delta DB yok → None
    print("\n[4] _ark_delta fail-soft (DB yok)")
    d = await _ark_delta("ARKK", ref, ref)
    delta_ok = d is None
    print(f"    _ark_delta=None: {'✓' if delta_ok else '✗'}")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    print(f"  (a) weekly tick hesabı       : {'✓' if tick_ok and ws_ok else '✗'}")
    print(f"  (b) broadcast default OFF    : {'✓' if off_ok else '✗'}")
    print(f"  (c) _poll_once fail-soft     : {'✓' if poll_ok else '✗'}")
    print(f"  (d) _ark_delta fail-soft     : {'✓' if delta_ok else '✗'}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
