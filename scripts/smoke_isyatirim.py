"""Geçici smoke — İş Yatırım RSS adaptörü (Kurumsal Sentez Faz S1a).

Doğrulama: feed full-text mi (content:encoded), bu hafta kaç rapor,
rapor tipi dağılımı (Commit 2 tip-filtresi girdisi).

Çalıştır:  python scripts/smoke_isyatirim.py   (network gerektirir)
İş Yatırım metnini DB'ye yazmaz / saklamaz.
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.corporate_sources.isyatirim_rss import (  # noqa: E402
    fetch_feed,
    parse_feed,
    posts_in_window,
    week_event_id,
)

# Türkiye 2016'dan beri kalıcı UTC+3 (DST yok) — sabit offset, tzdata yok.
TR_TZ = timezone(timedelta(hours=3))
PUBLISH_TIME = time(8, 30)


def _week_window_utc(now_utc: datetime) -> tuple[datetime, datetime, datetime]:
    now_tr = now_utc.astimezone(TR_TZ)
    this_monday_date = (now_tr - timedelta(days=now_tr.weekday())).date()
    this_monday_0830_tr = datetime.combine(this_monday_date, PUBLISH_TIME, TR_TZ)
    prev_monday_0830_tr = this_monday_0830_tr - timedelta(days=7)
    return (
        prev_monday_0830_tr.astimezone(timezone.utc),
        this_monday_0830_tr.astimezone(timezone.utc),
        prev_monday_0830_tr.astimezone(timezone.utc),
    )


def _doc_type(title: str) -> str:
    """Başlıktan kaba rapor-tipi (Commit 2 filtre girdisi)."""
    t = title.lower()
    if "piyasalarda bugün" in t:
        return "Piyasalarda Bugün"
    if "elüs" in t or "elus" in t:
        return "ELÜS Bülteni"
    if "pay geri al" in t:
        return "Pay Geri Alımları"
    if "yabancı oran" in t:
        return "Yabancı Oranları"
    if "şirket" in t or "sirket" in t:
        return "Şirket Haberi"
    return "Diğer"


async def main() -> None:
    print("=" * 64)
    print("  İŞ YATIRIM RSS SMOKE — Kurumsal Sentez Faz S1a")
    print("=" * 64)

    body, meta = await fetch_feed()
    print(f"\n[1] fetch_feed → meta = {meta}")
    if body is None:
        print("    ✗ body None — fetch başarısız. RAPOR boş.")
        print("=" * 64)
        return
    print(f"    ✓ body = {len(body):,} byte")
    if meta.get("fallback"):
        print(f"    ↪ fallback kullanıldı: {meta['fallback']}")

    posts = parse_feed(body)
    print(f"\n[2] parse_feed → toplam {len(posts)} rapor")
    for i, p in enumerate(posts[:4], 1):
        print(
            f"    {i}. {p.title[:60]!r}  [{p.author or '—'}]\n"
            f"       published={p.published.isoformat()}  "
            f"truncated={p.truncated}  body={len(p.body_text):,} ch"
        )

    print("\n[3] FULL-TEXT ANALİZİ")
    if posts:
        n_trunc = sum(1 for p in posts if p.truncated)
        ratio = n_trunc / len(posts)
        avg_len = sum(len(p.body_text) for p in posts) / len(posts)
        print(f"    truncated {n_trunc}/{len(posts)} = %{ratio*100:.0f}  |  "
              f"ort. gövde {avg_len:,.0f} ch")
        verdict = ("FULL-TEXT (content:encoded)" if ratio == 0
                   else "TRUNCATED" if ratio == 1 else "KARIŞIK")
        print(f"    ⇒ BULGU: {verdict}")
    else:
        print("    (rapor yok)")

    now_utc = datetime.now(timezone.utc)
    start, end, week_monday = _week_window_utc(now_utc)
    print("\n[4] PENCERE (önceki Pzt 08:30 → bu Pzt 08:30, TR)")
    print(f"    start (UTC) = {start.isoformat()}")
    print(f"    end   (UTC) = {end.isoformat()}")

    win = posts_in_window(posts, start, end)
    print(f"\n[5] posts_in_window → bu hafta {len(win)} rapor")
    types = Counter(_doc_type(p.title) for p in win)
    for i, p in enumerate(win[:8], 1):
        print(f"    {i}. [{p.published.date()}] {p.title[:55]!r} "
              f"({_doc_type(p.title)})")
    if len(win) > 8:
        print(f"    … +{len(win)-8} rapor daha")
    print(f"    Tip dağılımı (Commit 2 filtresi): {dict(types)}")
    if not win:
        print("    (pencerede rapor yok — 0 = sentez YOK, sessiz skip)")

    eid = week_event_id(week_monday)
    ok = len(eid) == 16 and all(c in "0123456789abcdef" for c in eid)
    print(f"\n[6] week_event_id({week_monday.date()}) = {eid}  "
          f"(16-hex: {'✓' if ok else '✗'})")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    ftv = ("FULL-TEXT" if posts and not any(p.truncated for p in posts)
           else "TRUNCATED" if posts and all(p.truncated for p in posts)
           else "KARIŞIK" if posts else "N/A")
    print(f"  (a) full-text mi truncated mi : {ftv}")
    print(f"  (b) bu hafta rapor sayısı     : {len(win)}  {dict(types)}")
    print(f"  (c) feed HTTP status          : {meta.get('status')}"
          f"{' (fallback)' if meta.get('fallback') else ''}")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
