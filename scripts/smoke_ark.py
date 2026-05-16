"""Geçici smoke — ARK günlük holdings CSV adaptörü (Faz S1b).

Doğrulama: 5 fon CSV erişimi, parse sağlığı (footer atlanıyor mu),
as_of tarihi, top holdings, ağırlık toplamı ~%100 mü, snapshot_event_id.

Çalıştır:  python scripts/smoke_ark.py   (network gerektirir)
ARK prose'unu saklamaz; yalnız olgusal holdings verisini özetler.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.corporate_sources.ark_csv import (  # noqa: E402
    FUND_FILES,
    fetch_holdings,
    parse_holdings,
    snapshot_event_id,
)


async def main() -> None:
    print("=" * 64)
    print("  ARK HOLDINGS CSV SMOKE — Kurumsal Sentez Faz S1b")
    print("=" * 64)

    ok_funds = 0
    for fund in FUND_FILES:
        body, meta = await fetch_holdings(fund)
        if body is None:
            print(f"\n[{fund}] ✗ fetch başarısız → {meta}")
            continue
        snap = parse_holdings(body, fund)
        n = len(snap.holdings)
        if n == 0:
            print(f"\n[{fund}] ✗ parse 0 holding (skipped={snap.skipped_rows}) "
                  f"bytes={len(body):,}")
            continue
        ok_funds += 1
        tot_w = sum(h.weight_pct for h in snap.holdings)
        tot_mv = sum(h.market_value_usd for h in snap.holdings)
        top = sorted(snap.holdings, key=lambda h: h.weight_pct, reverse=True)[:3]
        print(f"\n[{fund}] ✓ http={meta.get('status')} bytes={len(body):,} "
              f"as_of={snap.as_of} holdings={n} skipped={snap.skipped_rows}")
        print(f"   ağırlık toplamı=%{tot_w:.1f} (sağlık: ~%100 beklenir)  "
              f"toplam MV=${tot_mv:,.0f}")
        for h in top:
            print(f"   • {h.company[:34]:<34} {h.ticker or '—':<6} "
                  f"%{h.weight_pct:>5.2f}  {h.shares:>14,.0f} sh")
        empty_tk = sum(1 for h in snap.holdings if not h.ticker)
        if empty_tk:
            print(f"   (boş ticker satırı: {empty_tk} — warrant/yabancı, korunuyor)")
        eid = snapshot_event_id(fund, snap.as_of)
        eid_ok = len(eid) == 16 and all(c in "0123456789abcdef" for c in eid)
        print(f"   snapshot_event_id={eid} (16-hex: {'✓' if eid_ok else '✗'})")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    print(f"  (a) erişilebilen fon          : {ok_funds}/{len(FUND_FILES)}")
    print(f"  (b) kaynak tipi               : structured (prose YOK, "
          f"telif: olgusal holdings)")
    print(f"  (c) footer/disclaimer         : skipped_rows ile atlanıyor mu "
          f"→ yukarıda fon-bazlı")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
