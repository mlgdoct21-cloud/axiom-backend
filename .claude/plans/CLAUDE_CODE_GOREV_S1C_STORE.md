# GOREV: Kurumsal Sentez — S1c Accumulation Store (corporate_posts + holdings)

Calisma dizini: /Users/mehmetgulec/Documents/AXIOM/axiom-backend
Referans planlar (ONCE OKU): .claude/plans/KURUMSAL_SENTEZ_PLAN.md +
.claude/plans/KURUMSAL_KAYNAK_ENVANTERI.md (bolum 5b/5c/6 kritik).

## BAGLAM
- Iki bagimsiz smoke kanitladi: fetch-at-synthesis sadece Mahfi (dusuk hacim)
  icin yeterli. Is Yatirim feed derinligi ~1 gun (~10 rapor/gun), ARK gunluk
  snapshot (delta icin gun-gun diff) → ikisi de **accumulation store** ister.
- Bu commit = TUM prose/structured kaynaklarin ortak borusu. S2/S3/S4'te
  eklenecek her kaynak ayni store'a akacak. Kaynak yol haritasi DEGISMEZ.
- Mevcut 3 adaptor (mahfi_rss.py / isyatirim_rss.py / ark_csv.py) DEGISTIRILMEZ
  — store ustlerine ince, duck-typed bir katman. Bu commit'te Gemini YOK,
  broadcast YOK, scheduler YOK. Scheduler = Commit 3.
- Fail policy: DB hatasi sessiz skip + log; exception ile pipeline kirma
  (PPI 333-spam dersi). Idempotency = anti-spam bel kemigi.

## KILITLENEN TASARIM (yeniden tartisma — kullanici onayladi)
- Iki tablo: prose → `corporate_posts`; ARK structured → ayri
  `corporate_holdings_snapshots`.
- external_id: link varsa `sha1(source|link)[:24]`, yoksa
  `sha1(source|title|published_iso)[:24]`. (Adaptorler guid expose etmiyor;
  adaptor DEGISMEYECEGI icin mapper turetir.)
- Revizyon: ON CONFLICT(source,external_id) DO UPDATE → title/body_text/
  truncated/author/meta/fetched_at guncellenir; **first_seen_at KORUNUR**
  (SET'e yazma).
- Poll cadence: kod-ici registry (DB kolonu degil) — Commit 3 kullanacak.
- Kapsam: sema + store + 3 adaptoru ingest'e bagla + smoke. Hepsi bu.

## ADIM 0 — Kesif (zorunlu)
1. Alembic head dogrula: `python3` ile versions/ parse → head = `025`
   (yeni migration `026`, down_revision `'025'`). 024/025 dosya stilini OKU
   (op.create_table, postgresql.JSONB, sa.text DESC index).
2. core/schema_guard.py sonunu OKU (kapanan `]` + Kurumsal Sentez 025
   guard'lari) — 026 guard'lari ayni patternle ARKASINA eklenecek.
3. mahfi_rss.py + isyatirim_rss.py dataclass alanlari (title/link/published/
   body_text/truncated/author) + read/write_source_state engine kullanimi OKU.
4. ark_csv.py: ArkSnapshot(fund,as_of,holdings,skipped_rows) + ArkHolding
   alanlari + snapshot_event_id OKU.

## ADIM 1 — alembic/versions/026_corporate_posts.py (YENI)
down_revision = '025'.
**corporate_posts**: id BIGSERIAL PK, source TEXT NOT NULL, external_id TEXT
NOT NULL, kind TEXT, title TEXT NOT NULL, link TEXT, published TIMESTAMPTZ
NOT NULL, body_text TEXT, truncated BOOLEAN NOT NULL DEFAULT FALSE, author
TEXT, meta JSONB, first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE(source, external_id) [ad: uq_corp_posts_src_eid].
Index: ix_corp_posts_src_pub (source, published DESC),
ix_corp_posts_pub (published DESC).
**corporate_holdings_snapshots**: id BIGSERIAL PK, source TEXT NOT NULL
DEFAULT 'ark', fund TEXT NOT NULL, as_of DATE NOT NULL, payload JSONB NOT
NULL, holding_count INT NOT NULL DEFAULT 0, fetched_at TIMESTAMPTZ NOT NULL
DEFAULT NOW(), UNIQUE(fund, as_of) [ad: uq_corp_hold_fund_asof].
Index: ix_corp_hold_fund_asof (fund, as_of DESC).
upgrade()+downgrade() ikisi de (024/025 stiliyle bire bir).

## ADIM 2 — core/schema_guard.py (DUZENLE)
Iki tablo + 3 index icin CREATE TABLE/INDEX IF NOT EXISTS, mevcut Kurumsal
Sentez 025 guard'larinin ARKASINA, kapanan `]`'den once. Postgres tipler
(BIGSERIAL/JSONB/TIMESTAMPTZ). (Railway alembic auto-run yok; runtime garanti.)

## ADIM 3 — services/corporate_sources/store.py (YENI)
- @dataclass CorporatePost: source, external_id, kind, title, link,
  published(tz-aware), body_text, truncated, author, meta(dict).
- `_external_id(source, link, title, published)` → yukaridaki sha1 kurali.
- `normalize_post(source, kind, obj) -> CorporatePost`: DUCK-TYPED —
  obj.title/.link/.published/.body_text/.truncated, getattr(obj,'author','').
  MahfiPost ve IsYatirimPost ikisini de import ETMEDEN isler.
- `async ingest_posts(rows: list[CorporatePost]) -> dict`:
  {'inserted':n,'updated':n,'skipped':n}. Tek `engine.begin()` icinde
  param'li INSERT ... ON CONFLICT(source,external_id) DO UPDATE SET
  title=,body_text=,truncated=,author=,meta=,fetched_at=NOW()
  (first_seen_at YOK). xmax=0 ile inserted/updated ayirt et. DB hatasi →
  log + kismi donus, exception firlatma.
- `async ingest_holdings_snapshot(snap) -> bool`: payload =
  [dataclasses.asdict(h) ... date→isoformat]; INSERT ... ON CONFLICT
  (fund,as_of) DO UPDATE SET payload=,holding_count=,fetched_at=NOW().
  snap.as_of None ise skip+log.
- `async read_window(sources: list[str], start, end) -> list[dict]`:
  SELECT ... WHERE source = ANY(:s) AND published>=:start AND published<:end
  ORDER BY published DESC. (Commit 2 sentez bunu okuyacak.)
- Tum DB ops fail-soft; smoke hatayi NET gosterir.

## ADIM 4 — Adaptorleri ingest'e bagla (mapper, adaptor DEGISMEZ)
store.py icinde (veya ayni dosyada) ince ornek kullanim YOK; sadece
normalize_post + ingest_holdings_snapshot API'si yeterli. Adaptor dosyalarina
DOKUNMA. Baglama gercek hayatta Commit 3 scheduler'da olacak; bu commit'te
yalniz smoke baglar.

## ADIM 5 — scripts/smoke_store.py (YENI, gecici)
Iki kademe:
1. **DB'siz (her zaman kosar)**: mahfi+isyatirim fetch+parse → normalize_post
   → external_id determinism testi (ayni post iki kez → ayni id);
   ARK fetch+parse → asdict serialize OK mi. Pure, DB yok.
2. **DB roundtrip (yalniz DATABASE_URL postgres ise)**: schema_guard.ensure_schema
   benzeri CREATE IF NOT EXISTS → ingest_posts(mahfi+isyatirim) → ayni listeyi
   TEKRAR ingest (idempotency: 2. turda inserted=0, updated=N) →
   ingest_holdings_snapshot(ARK 5 fon) → read_window(onceki Pzt 08:30→bu Pzt
   08:30 TR, ['mahfi','isyatirim']) sayisi. DATABASE_URL yoksa "DB roundtrip
   SKIPPED (no postgres) — schema_guard runtime garanti" yaz, DURMA.
NET DOGRULAMA RAPORU bas: (a) external_id deterministik mi, (b) idempotent
re-ingest 0 insert mi (veya skipped — DB yoksa), (c) read_window pencere
sayilari, (d) ARK snapshot upsert OK mi.

## ADIM 6 — Smoke kos
`DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:5432/x" python3 scripts/smoke_store.py`
(import icin; DB roundtrip otomatik SKIP). Hata → raporla, DURMA.
Bulguyu commit mesajina not.

## ADIM 7 — Commit
py_compile temiz + `import services.corporate_sources.store` temiz.
alembic head tek (`026`→`025` zinciri). Tek commit:
`feat(corporate): S1c — accumulation store (corporate_posts + holdings_snapshots, no scheduler)`
+ smoke bulgusu. push. health 200 dogrula. memory guncelle.

## YAPMA
Adaptor dosyalarini (mahfi/isyatirim/ark) degistirme. Gemini/broadcast/
scheduler ekleme. Supabase MCP apply_migration. railway redeploy. BS4 import.
Mahfi/IsYatirim/ARK metnini kopyalama (store body_text tutar — bu DB, telif
reprodüksiyon Commit 2 guard'inda). Exception ile pipeline kirma.

## BITINCE CHAT'E DON
Smoke raporu: (a) external_id deterministik mi, (b) idempotent re-ingest
sonucu, (c) read_window pencere sayilari (mahfi/isyatirim), (d) ARK snapshot
upsert, (e) deploy yesil mi.
