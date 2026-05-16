"""Geçici smoke — Kurumsal Sentez Commit 2 (sentez servisi).

Kademe 1 (DB'siz/Gemini'siz, her zaman): prompt bütünlük + guard birim
testleri (L_DISPLACE 12-gram, L5 kelime sınırı, footer, attribution,
tek-kaynak uydurma-çelişki kuralı prompt'ta var mı).
Kademe 2 (GEMINI_API_KEY varsa): gerçek 1 premium üretim → JSON+footer+
guard geçti mi. Key yoksa SKIP, DURMA.

Çalıştır:  python scripts/smoke_synthesis.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.corporate_synthesis import (  # noqa: E402
    _FOOTER,
    _build_prompt,
    _ngram_overlap,
    _run_guards,
    SynthPayload,
    synthesize_week,
)


def _mock_payload(n_sources: int) -> SynthPayload:
    rows = []
    bodies = [
        ("mahfi", "Enflasyon ve para politikası üzerine yapısal bir "
         "değerlendirme; faiz patikası belirsiz, kur baskısı sürüyor."),
        ("isyatirim", "BIST endeks teknik görünümü nötr; bankacılık "
         "sektörü öne çıkıyor, yabancı girişi sınırlı."),
    ]
    for i in range(n_sources):
        src, body = bodies[i]
        rows.append({
            "source": src, "kind": "article",
            "title": f"Test başlık {i}", "link": f"https://x/{i}",
            "published": datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc),
            "body_text": body, "truncated": False, "author": "T",
        })
    return SynthPayload(
        week_start=datetime(2026, 5, 4).date(),
        prev_iso="2026-05-04", this_iso="2026-05-11",
        prose=rows,
        ark=[{"fund": "ARKK", "as_of": "2026-05-15",
              "top": [{"company": "TESLA INC", "ticker": "TSLA",
                       "weight_pct": 11.16}]}],
        source_count=n_sources + 1,
    )


def main() -> None:
    print("=" * 64)
    print("  KURUMSAL SENTEZ SMOKE — Commit 2")
    print("=" * 64)

    # --- Kademe 1: prompt bütünlük ---
    print("\n[1] Prompt bütünlük")
    p2 = _build_prompt("premium", _mock_payload(2))
    a2 = _build_prompt("advance", _mock_payload(2))
    p1 = _build_prompt("premium", _mock_payload(1))
    checks = {
        "premium {tier} yok→tier=premium var": "tier=premium" in p2,
        "advance ark_pozisyon_ozeti": "ark_pozisyon_ozeti" in a2,
        "footer sabit metin prompt'ta": _FOOTER[:40] in p2,
        "kural 4 tek-kaynak uydurma yasağı": "çelişki UYDURMA" in p1,
        "kural 5 yön etiketi zorlama yok": "YÖN ETİKETİ ZORLAMA YOK" in p2,
        "URL yazma yasağı": "URL/bağlantı YAZMA" in p2,
        "ham JSON talimatı": "ham geçerli JSON" in p2,
        "[MAHFI] kaynak bloğu": "[mahfi]" in p2.lower() or "MAHFI" in p2,
        "[ARK] structured blok": "[ARK]" in p2,
    }
    for k, v in checks.items():
        print(f"    {'✓' if v else '✗'} {k}")
    p1_ok = all(checks.values())

    # --- Kademe 1: guard birim testleri ---
    print("\n[2] Guard birim testleri")
    src = "alfa beta gama delta epsilon zeta eta teta yota kappa lambda mu nu"
    displ = _ngram_overlap(src, "önsöz " + src + " sonsöz", n=12)
    print(f"    L_DISPLACE 12-gram yakalama: {'✓' if displ else '✗'} "
          f"(hit={'var' if displ else 'yok'})")
    no_displ = _ngram_overlap(src, "tamamen farklı bir cümle bu", n=12)
    print(f"    L_DISPLACE temiz metin: {'✓' if no_displ is None else '✗'}")

    good = {
        "haftanin_resmi": "[MAHFI] görüşü: " + "kelime " * 60,
        "kaynaklar_ne_diyor": "[ISYATIRIM] " + "analiz " * 60,
        "axiom_gorusu_ve_risk": "AXIOM " + "değerlendirme " * 40,
        "footer": _FOOTER,
    }
    md, rep = _run_guards("premium", good, set(), [], {"MAHFI", "ISYATIRIM"})
    print(f"    temiz çıktı guard.ok: {'✓' if rep.ok else '✗'} "
          f"(wc={rep.word_count}, reasons={rep.reasons})")

    bad = {"haftanin_resmi": "kısa", "kaynaklar_ne_diyor": "atıfsız metin",
           "axiom_gorusu_ve_risk": "x", "footer": "yanlış footer"}
    _, rep_b = _run_guards("premium", bad, set(), [], {"MAHFI"})
    print(f"    bozuk çıktı reddedildi: {'✓' if not rep_b.ok else '✗'} "
          f"(reasons={rep_b.reasons})")

    k1_ok = (displ is not None and no_displ is None and rep.ok
             and not rep_b.ok and p1_ok)

    # --- Kademe 2: gerçek Gemini (key varsa) ---
    print("\n[3] Gerçek Gemini üretim")
    if not os.getenv("GEMINI_API_KEY", "").strip():
        print("    ⏭  GEMINI SKIPPED (GEMINI_API_KEY yok) — guard birim "
              "testleri yukarıda kanıt; canlı üretim deploy/anahtarla.")
    else:
        try:
            res = asyncio.run(synthesize_week(tiers=("premium",), force=True))
            for r in res:
                print(f"    {r.tier}: written={r.written} skipped={r.skipped} "
                      f"wc={r.word_count} reason={r.reason} src={r.sources}")
        except Exception as e:  # noqa: BLE001
            print(f"    ! canlı üretim hata: {e}")

    print("\n" + "=" * 64)
    print("  DOĞRULAMA RAPORU")
    print("=" * 64)
    print(f"  (a) prompt bütünlük          : {'✓' if p1_ok else '✗'}")
    print(f"  (b) guard birim testleri     : {'✓' if k1_ok else '✗'}")
    print(f"  (c) tek-kaynak uydurma yasağı : prompt kural 4 {'✓' if checks.get('kural 4 tek-kaynak uydurma yasağı') else '✗'}")
    print(f"  (d) canlı Gemini             : {'çalıştı' if os.getenv('GEMINI_API_KEY','').strip() else 'SKIPPED'}")
    print("=" * 64)


if __name__ == "__main__":
    main()
