"""Geçici smoke — Mahfi RSS adaptörü (Kurumsal Sentez Commit 1).

KRİTİK BULGU: feed full-text mi truncated mi? → Commit 2 prompt modunu
(özet-genişletme vs. tam-metin-sentez) belirler.

Çalıştır:  python scripts/smoke_mahfi.py
Network gerektirir. Mahfi metnini DB'ye yazmaz / saklamaz.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.corporate_sources.mahfi_rss import (  # noqa: E402
    fetch_feed,
    parse_feed,
    posts_in_window,
    week_event_id,
)

# Türkiye 2016'dan beri kalıcı UTC+3 (DST yok) — sabit offset, tzdata
# bağımlılığı yok (Railway/Docker'da güvenli).
TR_TZ = timezone(timedelta(hours=3))
PUBLISH_TIME = time(8, 30)  # her Pazartesi 08:30 TR


def _week_window_utc(now_utc: datetime) -> tuple[datetime, datetime, datetime]:
    """(prev_monday_0830_utc, this_monday_0830_utc, week_key_monday_utc).

    Pencere: önceki Pzt 08:30 TR → bu Pzt 08:30 TR (hafta sonu dahil).
    week_key = sentezlenecek haftanın başlangıç Pazartesi'si (prev).
    """
    now_tr = now_utc.astimezone(TR_TZ)
    this_monday_date = (now_tr - timedelta(days=now_tr.weekday())).date()
    this_monday_0830_tr = datetime.combine(this_monday_date, PUBLISH_TIME, TR_TZ)
    prev_monday_0830_tr = this_monday_0830_tr - timedelta(days=7)
    return (
        prev_monday_0830_tr.astimezone(timezone.utc),
        this_monday_0830_tr.astimezone(timezone.utc),
        prev_monday_0830_tr.astimezone(timezone.utc),
    )


async def main() -> None:
    print("=" * 64)
    print("  MAHFI RSS SMOKE — Kurumsal Sentez Commit 1")
    print("=" * 64)

    # 1) fetch_feed — status + byte
    body, meta = await fetch_feed()
    print(f"\n[1] fetch_feed → meta = {meta}")
    if body is None:
        print("    ✗ body None — fetch başarısız (304 ya da hata).")
        print("    RAPOR: feed çekilemedi; aşağıdaki bulgular boş.")
        print("=" * 64)
        return
    print(f"    ✓ body = {len(body):,} byte")
    if meta.get("fallback") == "atom":
        print("    ↪ ATOM fallback kullanıldı (alt=rss 0 entry döndü).")

    # 2) parse_feed — toplam + ilk 3
    posts = parse_feed(body)
    print(f"\n[2] parse_feed → toplam {len(posts)} yazı")
    for i, p in enumerate(posts[:3], 1):
        print(
            f"    {i}. {p.title[:70]!r}\n"
            f"       published={p.published.isoformat()}  "
            f"truncated={p.truncated}  body={len(p.body_text):,} char"
        )

    # 3) truncated oranı — KRİTİK
    print("\n[3] TRUNCATED ANALİZİ (Commit 2 prompt modu)")
    if posts:
        n_trunc = sum(1 for p in posts if p.truncated)
        ratio = n_trunc / len(posts)
        avg_len = sum(len(p.body_text) for p in posts) / len(posts)
        print(
            f"    truncated {n_trunc}/{len(posts)} = %{ratio*100:.0f}  |  "
            f"ort. gövde {avg_len:,.0f} char"
        )
        if ratio == 0:
            verdict = "FULL-TEXT (content[0].value dolu) → tam-metin sentez"
        elif ratio == 1:
            verdict = "TRUNCATED (yalnız summary) → özet-bağlam sentez"
        else:
            verdict = "KARIŞIK → Commit 2'de per-post truncated flag'ine göre"
        print(f"    ⇒ BULGU: {verdict}")
    else:
        print("    (yazı yok — truncated oranı hesaplanamadı)")

    # 4) pencere
    now_utc = datetime.now(timezone.utc)
    start, end, week_monday = _week_window_utc(now_utc)
    print("\n[4] PENCERE (önceki Pzt 08:30 → bu Pzt 08:30, TR)")
    print(f"    start (UTC) = {start.isoformat()}")
    print(f"    end   (UTC) = {end.isoformat()}")

    # 5) posts_in_window
    win = posts_in_window(posts, start, end)
    print(f"\n[5] posts_in_window → bu hafta {len(win)} yazı")
    for i, p in enumerate(win, 1):
        print(f"    {i}. [{p.published.date()}] {p.title[:70]!r} (trunc={p.truncated})")
    if not win:
        print("    (pencerede yazı yok — 0 yazı = sentez YOK, sessiz skip)")

    # 6) week_event_id
    eid = week_event_id(week_monday)
    ok = len(eid) == 16 and all(c in "0123456789abcdef" for c in eid)
    print(f"\n[6] week_event_id({week_monday.date()}) = {eid}  "
          f"(16-hex: {'✓' if ok else '✗'})")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    print(f"  (a) full-text mi truncated mi : "
          f"{'FULL-TEXT' if posts and not any(p.truncated for p in posts) else ('TRUNCATED' if posts and all(p.truncated for p in posts) else ('KARIŞIK' if posts else 'N/A'))}")
    print(f"  (b) bu hafta yazı sayısı      : {len(win)}")
    print(f"  (c) feed HTTP status          : {meta.get('status')}"
          f"{' (ATOM fallback)' if meta.get('fallback') == 'atom' else ''}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
