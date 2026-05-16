# GOREV: Kurumsal Sentez — Commit 3 (Scheduler + poll/accumulation + broadcast; kill-switch OFF)

Calisma dizini: /Users/mehmetgulec/Documents/AXIOM/axiom-backend
Referans (OKU): KURUMSAL_SENTEZ_PLAN.md Bolum 5 + KURUMSAL_KAYNAK_ENVANTERI.md
(5b feed derinligi) + CLAUDE_CODE_GOREV_S1C_STORE.md + CLAUDE_CODE_GOREV_COMMIT2.md.

## BAGLAM
- coinglass_scheduler.py (cron supervisor) + macro_broadcaster.py (tier
  broadcast + stamp + free 5dk delay) forku. SIFIRDAN YAZMA.
- S1c kanitladi: yuksek-hacim kaynak (Is Yatirim feed ~1 gun) artimli
  poll + accumulation ister → scheduler hem POLL hem haftalik sentez.
- En riskli commit (broadcast = kullanici-gorunur, para). EMNIYET:
  kill-switch `CORPORATE_SYNTH_BROADCAST_ENABLED` default **"false"** →
  ship etmek spam atmaz; aciklama acilana kadar yalniz DB'ye yazar.
- Fail policy: sessiz skip + log; exception ile supervisor/pipeline kirma.
  asyncio.create_task → strong-ref set (GC guard, memory feedback).

## ADIM 1 — services/corporate_synthesis.py (DUZENLE, minimum-diff)
ARK gun-gun delta ekle (S1b notu: as_of birikince "ne aldi/satti").
- `async _ark_delta(fund, start, end) -> Optional[dict]`: corporate_
  holdings_snapshots'tan window icindeki en eski + en yeni snapshot'i oku
  (>=2 gerek). payload JSON'lardan ticker→weight map cikar; weight_pct
  farki +/- esikli (>=0.5 puan) "artan/azalan", yeni giren/cikan liste.
  DB yoksa/<2 snapshot → None (fail-soft).
- build_payload: her ARK fonu icin _ark_delta dene; varsa pl.ark[i]'ye
  "delta" alani ekle (added/removed/increased/decreased), yoksa mevcut
  tek-snapshot "top" davranisi KALIR (Commit 2 bozulmaz).
- _source_blocks: delta varsa [ARK] blogunda "Hafta ici hareket: ..."
  satiri ekle (yalniz olgusal: ticker + yon + puan). prompt'ta
  ark_pozisyon_ozeti artik delta-aware (kural metni degismez).
- _allowed_numbers: delta puan/agirlik sayilarini whitelist'e ekle.

## ADIM 2 — services/corporate_broadcaster.py (YENI, macro_broadcaster forku)
- KILL-SWITCH: `CORPORATE_SYNTH_BROADCAST_ENABLED` default **"false"**
  → false/0/no ise {skipped_disabled}. (macro default "true"; bu OFF.)
- `async broadcast_synthesis(event_id, tier, *, force=False) -> dict`:
  - tier in (premium,advance) degilse invalid.
  - corporate_syntheses satirini (event_id,tier) oku; yoksa missing_row.
  - stamp col = broadcasted_premium_at | broadcasted_advance_at;
    force degilse doluysa skipped_already_broadcasted (idempotency).
  - 24h CAP (defense-in-depth): force degilse son 24 saatte herhangi bir
    corporate broadcast stamp'i varsa skipped_24h_cap.
  - mesaj = baslik (💎/🚀 + hafta) + synthesis_md (Telegram limit ~3800
    kirp) + footer zaten md icinde. _FREE_WATERMARK forku.
  - kullanicilar: AsyncSessionLocal select(User); tier-strict (premium
    sadece premium, advance sadece advance). Free → 5dk gecikmeli
    watermark + upgrade keyboard (strong-ref _CORP_INFLIGHT).
  - STAMP BEFORE FANOUT (macro pattern; partial fail re-broadcast storm
    engeli). send_telegram_message sync → asyncio.to_thread.
- `async broadcast_synthesis_safe(...)` never-raise wrapper.

## ADIM 3 — services/corporate_scheduler.py (YENI, coinglass forku)
- Sabitler: CORP_POLL_INTERVAL_SECONDS default 10800 (3h),
  Europe/Istanbul = sabit UTC+3 (tzdata yok), WEEKLY = Pazartesi 08:30 TR.
- `async _poll_once()`: 
  - mahfi/isyatirim: read/write_source_state ETag → fetch_feed →
    parse_feed → store.normalize_post → store.ingest_posts; write_source_state.
  - ARK 5 fon: fetch_holdings → parse_holdings → store.ingest_holdings_snapshot.
  - Hepsi fail-soft (kaynak basina try/except + log).
- `_next_weekly_tick(now_utc)` → bir sonraki Pzt 08:30 TR'nin UTC'si.
- `async corporate_supervisor()`:
  - startup catch-up: _poll_once() hemen; sonra "bu haftanin Pzt 08:30
    TR'si gectiyse ve week_event_id yazilmamissa" synthesize+broadcast.
  - while True: sleep(CORP_POLL_INTERVAL); _poll_once(); eger now >=
    bu hafta Pzt0830 ve o hafta sentezlenmemisse → synthesize_week()
    → her uretilen/var olan (event_id,tier) icin broadcast_synthesis_safe
    (kill-switch zaten OFF; guvenli). asyncio.CancelledError → break.
  - Exception → log + devam (supervisor kirilmaz).

## ADIM 4 — main.py (DUZENLE) lifespan wiring
- import corporate_supervisor. try/except create_task → corporate_task.
  shutdown cancel listesine ekle. Diger supervisor'lar pattern'i bire bir.

## ADIM 5 — scripts/smoke_corporate_scheduler.py (YENI, gecici)
DB'siz/Gemini'siz kademe (her zaman):
- _next_weekly_tick: bilinen Cumartesi girdi → gelecek Pzt 08:30 TR/UTC
  dogru mu (gun=0, saat 05:30 UTC).
- broadcast kill-switch: env unset → broadcast_synthesis {skipped_disabled}
  (kanit: default OFF). 
- _poll_once çağrılabilir mi (network var; ingest DB yoksa fail-soft 0).
- _ark_delta DB yoksa None (fail-soft).
GEMINI/DB roundtrip otomatik SKIP (onceki commit deseni). NET RAPOR.

## ADIM 6 — Smoke + Commit
py_compile + import temiz (main.py dahil). Tek commit:
`feat(corporate): Commit 3 — scheduler (poll+accumulation) + weekly Mon 08:30 + broadcast (kill-switch default OFF) + ARK delta`
+ smoke bulgusu. push. health 200. memory guncelle.

## YAPMA
Dashboard/Telegram komutu (Commit 4). Yeni alembic (broadcasted_* 025'te
var). Adaptor/store/Commit2 prompt degistirme (ARK delta minimum-diff ek).
Supabase MCP migration. railway redeploy. Kill-switch'i default ON yapma.
Broadcast'i force/test amacli canli kullaniciya gonderme.

## BITINCE CHAT'E DON
Smoke raporu: (a) weekly tick hesabi dogru mu, (b) broadcast kill-switch
default OFF kanit, (c) poll fail-soft, (d) ARK delta fail-soft, (e) deploy
yesil mi + KESIN: broadcast hala OFF (kullaniciya gitmedi).
