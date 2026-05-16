# GOREV: Kurumsal Sentez Faz — Commit 1 (Mahfi kaynak adaptoru + schema)

Calisma dizini: /Users/mehmetgulec/Documents/AXIOM/axiom-backend
Referans plan: .claude/plans/KURUMSAL_SENTEZ_PLAN.md — ONCE ONU OKU.

## BAGLAM
- Bu, services/macro_storyteller.py + services/macro_sources/fed_rss.py pattern'inin
  forkudur. Sifirdan yazma; o iki dosyayi once OKU (dataclass, async httpx, feedparser,
  idempotent event_id, schema_guard CREATE IF NOT EXISTS, alembic down_revision zinciri).
- Commit 1'de Gemini YOK, broadcast YOK, scheduler YOK. Sadece: cek, parse, pencere, tablo, smoke.
- Fail policy: her sey sessiz skip + log. Exception ile pipeline kirma.

## ADIM 0 — Kesif (zorunlu)
1. git log --oneline -5 ve alembic heads -> en son alembic revision (yeni migration down_revision).
2. services/macro_sources/fed_rss.py OKU -> feedparser, ETag/If-Modified-Since, event_id sha1.
3. core/schema_guard.py OKU -> CREATE TABLE IF NOT EXISTS + index ekleme yeri.
4. fed_statement.py'deki regex HTML strip OKU -> BS4 KULLANMA, ayni yaklasim.

## ADIM 1 — services/corporate_sources/__init__.py (YENI)
Tek satir docstring'li bos paket.

## ADIM 2 — services/corporate_sources/mahfi_rss.py (YENI)
- FEED_URL = "https://www.mahfiegilmez.com/feeds/posts/default?alt=rss"
  ATOM_FALLBACK_URL = "https://www.mahfiegilmez.com/feeds/posts/default?alt=atom"
- @dataclass MahfiPost: title, link, published(tz-aware UTC), body_text, truncated.
- _strip_html(raw)->str: regex <[^>]+> strip + entity decode (&nbsp;&amp;&#39;&quot;&lt;&gt;
  + smart quotes) + whitespace collapse. BS4 YASAK.
- parse_feed(xml_bytes)->list[MahfiPost]: SAF (network yok). feedparser.parse; title+link;
  published_parsed||updated_parsed -> datetime(*t[:6],tzinfo=timezone.utc); body:
  content[0].value varsa (truncated=False) yoksa summary (truncated=True); ikisi yoksa skip;
  _strip_html; bos body skip.
- async fetch_feed(etag=None,last_modified=None)->tuple[bytes|None,dict]: httpx, gercekci
  User-Agent, If-None-Match/If-Modified-Since. 304->(None,{'status':304}).
  200->(body,{'status':200,'etag','last_modified'}). hata/>=400->(None,{'status','error'}).
  rss 0 entry -> ATOM_FALLBACK_URL bir kez daha.
- posts_in_window(posts,start,end): start<=p.published<end, DESC.
- week_event_id(monday_utc): sha1(("mahfi-week|"+monday_utc.date().isoformat()).encode())
  .hexdigest()[:16]
- corporate_source_state tablosundan ETag/Last-Modified oku/yaz (async, mevcut DB pool).

## ADIM 3 — alembic migration (YENI, versions/)
down_revision = ADIM 0'daki en son rev.
corporate_syntheses: id BIGSERIAL PK, event_id TEXT NOT NULL, tier TEXT NOT NULL,
week_start DATE NOT NULL, synthesis_md TEXT, source_count INT NOT NULL DEFAULT 0,
meta JSONB, generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
broadcasted_premium_at TIMESTAMPTZ, broadcasted_advance_at TIMESTAMPTZ,
UNIQUE(event_id,tier). Index: ix_corp_synth_week(week_start DESC), ix_corp_synth_eid(event_id).
corporate_source_state: source TEXT PK, etag TEXT, last_modified TEXT,
updated_at TIMESTAMPTZ NOT NULL DEFAULT now().
upgrade()+downgrade() ikisi de.

## ADIM 4 — core/schema_guard.py (DUZENLE)
Iki tablo icin CREATE TABLE IF NOT EXISTS + index'leri mevcut guard'larin yanina ekle.
(Railway alembic auto-run yok; migration kanonik, schema_guard runtime garanti.)

## ADIM 5 — scripts/smoke_mahfi.py (YENI, gecici)
Async: 1) fetch_feed status+byte 2) parse_feed toplam + ilk 3 title/published/truncated
3) truncated orani (KRITIK: Commit 2 prompt modunu belirler) 4) prev/this monday 08:30
Europe/Istanbul->UTC araligi 5) posts_in_window bu hafta kac post + basliklar
6) week_event_id 16-hex. Net DOGRULAMA RAPORU bas.

## ADIM 6 — Smoke kos
python scripts/smoke_mahfi.py. 403/SSL/parse hatasi -> ATOM fallback dogrula, gercekci
User-Agent. Olmazsa raporla, DURMA. "truncated mi full-text mi" bulgusunu commit mesajina not.

## ADIM 7 — Commit
python -c "import services.corporate_sources.mahfi_rss" temiz mi. alembic upgrade head
(local/staging) tablolar olustu mu. Tek commit:
feat(corporate): Commit 1 — Mahfi RSS adapter + corporate_syntheses schema (no Gemini/broadcast)
+ smoke bulgusu (full-text/truncated, bu hafta post sayisi). push. health 200 dogrula.

## YAPMA
BS4 import. railway redeploy. Supabase MCP apply_migration. Gemini/broadcast/scheduler
Commit 1'e koyma. Mahfi metnini kopyalama/saklama. Exception ile pipeline kirma.

## BITINCE CHAT'E DON
Smoke raporu: (a) full-text mi truncated mi (b) bu hafta post sayisi (c) deploy yesil mi.
