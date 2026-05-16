# KURUMSAL SENTEZ FAZI — Kilitlenmis Plan + Commit 1 Uygulama Rehberi

Tarih: 16 May 2026
Repo: /Users/mehmetgulec/Documents/AXIOM/axiom-backend (GitHub: mlgdoct21-cloud/axiom-backend)
Taban: services/macro_storyteller.py forku — sifirdan yazilmaz (minimum_diff_principle)

## 1. KILITLENEN KARARLAR

- Urun: Haftalik makro sentez raporu
- Yayin: Her Pazartesi 08:30 TR (BIST 10:00 acilisindan once)
- Pencere: Onceki Pzt 08:30 -> bu Pzt 08:30 (hafta sonu dahil)
- Kaynak (Faz 1): Yalnizca Mahfi Egilmez blog (Blogger Atom/RSS)
- Kaynak (Faz 2): Is Yatirim — telif-guvenli sinyal+link, ayri bot-duvari cozumuyle (ertelendi)
- Toplama: Penceredeki TUM yazilar (0-4) tek baglam -> cok-dokuman SENTEZ (ozet degil)
- Tier: Premium (kisa 150-400w) + Advance (derin 350-700w)
- 0 yazi: Sentez YOK — sessiz skip + admin log
- Az icerik: 1+ yazi yeterli, kalite prompt'a birakilir
- Fail policy: Sessiz skip + admin log (PPI 333-spam dersi)
- Telif: Mahfi cumleleri COGALTILMAZ; fikri "tartisma basligi" referans + [MAHFI] chip + link.
  Footer disclaimer ZORUNLU: "Mahfi Egilmez yatirim tavsiyesi vermez; bu AXIOM'un bagimsiz
  makro degerlendirmesidir."

Mahfi konumlandirma: Mahfi trading sinyali YAZMAZ, makro analiz yazar, duzensiz siklik
(haftada 0-4). Urun konumu = makro baglam katmani, trading sinyali degil (halusinasyon guard).

## 2. MIMARI — macro_storyteller forkundan SAPMA NOKTALARI

macro_storyteller = tek event -> tek hikaye.
corporate_synthesis = N dokuman -> 1 sentez.

services/corporate_sources/__init__.py
services/corporate_sources/mahfi_rss.py
services/corporate_synthesis.py        (macro_storyteller forku)
alembic/versions/0XX_corporate_syntheses.py
core/schema_guard.py                   (CREATE TABLE IF NOT EXISTS eki)
services/corporate_scheduler.py        (Commit 3)
routers/v1/...                         (admin endpoint, Commit 2)

## 3. COMMIT 1 — Kaynak Adaptoru + Schema (Gemini YOK, broadcast YOK)

mahfi_rss.py: FEED_URL blogger rss, ATOM fallback. MahfiPost dataclass (title, link,
published tz-aware UTC, body_text, truncated). _strip_html regex (BS4 YOK). parse_feed
saf fonksiyon. fetch_feed async httpx ETag/If-Modified-Since. posts_in_window. week_event_id
= sha1("mahfi-week|"+iso_date)[:16]. corporate_source_state tablosu ETag persist.

Tablo corporate_syntheses: id, event_id, tier, week_start, synthesis_md, source_count,
meta JSONB, generated_at, broadcasted_premium_at, broadcasted_advance_at,
UNIQUE(event_id,tier). schema_guard CREATE IF NOT EXISTS.

Smoke scripts/smoke_mahfi.py: fetch+parse+window+truncated orani raporla.
KRITIK BULGU: feed full-text mi truncated mi -> Commit 2 prompt modunu belirler.

## 4. COMMIT 2 — Sentez Servisi (Gemini, broadcast KAPALI)
corporate_synthesis.py macro_storyteller forku. KORUNAN: _call_gemini gemini-2.5-flash
temp 0.3 JSON 1 retry, L1 sayi whitelist, L2 [SRC]->[MAHFI] chip, L3 tarih damgasi,
idempotent UPSERT. DEGISEN: _build_payload N dokuman, _build_prompt cok-dokuman sentez
talimati + footer disclaimer zorunlu, L4 attribution guard, L5 bound
CORPORATE_SYNTH premium(150,400) advance(350,700), L_DISPLACE 12+ kelime ortak n-gram REJECT.
Admin POST /admin/corporate/synthesize.

## 5. COMMIT 3 — Scheduler + tier guard + broadcast
corporate_scheduler.py ETF cron forku, Pazartesi 08:30 Europe/Istanbul. 0 yazi skip+log.
Defense-in-depth: 24h cap + Gemini budget + idempotency stamp. Free 5dk gecikme+watermark.
Kill-switch CORPORATE_SYNTH_BROADCAST_ENABLED default OFF.

## 6. COMMIT 4 — Dashboard + Telegram
CorporateSynthesisCard (MacroStoryCard forku) + /api/corporate/latest proxy +
/sentez Telegram komutu + free->advance CTA.

## 7. BUTCE ~$0.02/ay. Net ek maliyet ~0.

## 8. REDDEDILEN: BS4 dep, her acilista scrape, Mahfi parafraze, Is Yatirim ham scrape,
Gemini'ye sayi/yorum serbest, railway redeploy, Supabase MCP apply_migration,
"Mahfi=haftalik piyasa yonu" konumlandirmasi.

## 9. ACIK DOGRULAMA BORCU (Commit 1 smoke'ta kapanir): feed 200 mu, full-text mi
truncated mi, published tz dogru mu, atom fallback gerekli mi.
