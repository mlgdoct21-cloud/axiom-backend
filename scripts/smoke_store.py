"""Geçici smoke — Kurumsal Sentez accumulation store (S1c).

İki kademe:
  1. DB'siz (her zaman koşar): fetch+parse → normalize_post → external_id
     determinism; ARK snapshot JSON-serileştirme.
  2. DB roundtrip (yalnız erişilebilir postgres varsa): ensure_schema →
     ingest_posts ×2 (idempotency) → ingest_holdings_snapshot → read_window.
     Postgres yoksa SKIP + mesaj, DURMA.

Çalıştır:  python scripts/smoke_store.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from core.database import engine  # noqa: E402
from services.corporate_sources import mahfi_rss, isyatirim_rss, ark_csv  # noqa: E402
from services.corporate_sources.store import (  # noqa: E402
    ingest_holdings_snapshot,
    ingest_posts,
    normalize_post,
    read_window,
)

TR_TZ = timezone(timedelta(hours=3))


def _week_window_utc(now_utc: datetime) -> tuple[datetime, datetime]:
    now_tr = now_utc.astimezone(TR_TZ)
    this_mon = (now_tr - timedelta(days=now_tr.weekday())).date()
    this_0830 = datetime.combine(this_mon, time(8, 30), TR_TZ)
    prev_0830 = this_0830 - timedelta(days=7)
    return prev_0830.astimezone(timezone.utc), this_0830.astimezone(timezone.utc)


async def _db_reachable() -> bool:
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    (DB erişilemez: {type(e).__name__})")
        return False


async def main() -> None:
    print("=" * 64)
    print("  ACCUMULATION STORE SMOKE — Kurumsal Sentez S1c")
    print("=" * 64)

    # --- Kademe 1: DB'siz ---
    print("\n[1] DB'siz: fetch+parse+normalize")
    mb, _ = await mahfi_rss.fetch_feed()
    ib, _ = await isyatirim_rss.fetch_feed()
    m_posts = mahfi_rss.parse_feed(mb) if mb else []
    i_posts = isyatirim_rss.parse_feed(ib) if ib else []
    print(f"    mahfi={len(m_posts)}  isyatirim={len(i_posts)}")

    norm_m = [normalize_post("mahfi", "article", p) for p in m_posts]
    norm_i = [normalize_post("isyatirim", "report", p) for p in i_posts]

    det_ok = True
    if m_posts:
        a = normalize_post("mahfi", "article", m_posts[0]).external_id
        b = normalize_post("mahfi", "article", m_posts[0]).external_id
        det_ok = a == b
        uniq = len({n.external_id for n in norm_m}) == len(norm_m)
        print(f"    external_id deterministik: {'✓' if det_ok else '✗'} "
              f"(a==b={a==b})  | mahfi id'leri benzersiz: {'✓' if uniq else '✗'}")
        print(f"    örnek id={a}")

    ark_ok = 0
    for fund in ark_csv.FUND_FILES:
        body, _ = await ark_csv.fetch_holdings(fund)
        if not body:
            continue
        snap = ark_csv.parse_holdings(body, fund)
        if snap.holdings:
            from services.corporate_sources.store import _holding_to_jsonable
            import json as _json
            _json.dumps([_holding_to_jsonable(h) for h in snap.holdings])
            ark_ok += 1
    print(f"    ARK snapshot JSON-serileştirme: {ark_ok}/{len(ark_csv.FUND_FILES)} fon OK")

    # --- Kademe 2: DB roundtrip ---
    print("\n[2] DB roundtrip")
    if not await _db_reachable():
        print("    ⏭  DB roundtrip SKIPPED (erişilebilir postgres yok) — "
              "schema_guard runtime garanti, alembic 026 kanonik.")
    else:
        try:
            from core.schema_guard import ensure_schema
            await ensure_schema()
            print("    ✓ ensure_schema çalıştı")
        except Exception as e:  # noqa: BLE001
            print(f"    ! ensure_schema: {e}")

        r1 = await ingest_posts(norm_m + norm_i)
        r2 = await ingest_posts(norm_m + norm_i)  # idempotency
        print(f"    ingest #1 = {r1}")
        print(f"    ingest #2 = {r2}  (idempotent: inserted=0 beklenir → "
              f"{'✓' if r2['inserted'] == 0 else '✗'})")

        hold_ok = 0
        for fund in ark_csv.FUND_FILES:
            body, _ = await ark_csv.fetch_holdings(fund)
            if not body:
                continue
            if await ingest_holdings_snapshot(ark_csv.parse_holdings(body, fund)):
                hold_ok += 1
        print(f"    ingest_holdings_snapshot: {hold_ok}/{len(ark_csv.FUND_FILES)} fon")

        start, end = _week_window_utc(datetime.now(timezone.utc))
        win = await read_window(["mahfi", "isyatirim"], start, end)
        by_src: dict[str, int] = {}
        for w in win:
            by_src[w["source"]] = by_src.get(w["source"], 0) + 1
        print(f"    read_window [{start.date()}→{end.date()}) = {len(win)} post "
              f"{by_src}")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    print(f"  (a) external_id deterministik : {'✓' if det_ok else '✗ / N/A'}")
    print(f"  (b) ARK JSON-serileştirme     : {ark_ok}/{len(ark_csv.FUND_FILES)} fon")
    print(f"  (c) DB roundtrip              : yukarı bak (postgres varsa)")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
