# GOREV: Kurumsal Sentez — Commit 4 (Dashboard + Telegram)

İki repo / iki deploy → iki commit:
- **4a (axiom-backend)**: public tier-gated endpoint + /sentez komutu.
- **4b (axiom-dashboard)**: /api/corporate/latest proxy + CorporateSynthesisCard
  (MacroStoryCard forku) + sayfaya bağlama + browser doğrulama.

Referans: KURUMSAL_SENTEZ_PLAN.md Bölüm 6. macro_public.py `_peek_tier`
+ `/latest` forku; telegram_bot.py process_report_command + dispatcher
forku; dashboard src/app/api/macro/latest/route.ts + MacroStoryCard forku.

## 4a — ADIM 1: routers/v1/corporate_public.py (YENI)
- `_peek_tier(authorization)` macro_public'ten birebir fork (AuthService).
- `GET /corporate/latest` (optional-auth, Header Authorization):
  - en güncel hafta: corporate_syntheses week_start DESC.
  - advance kullanıcı → tier='advance' satır (yoksa premium fallback).
  - premium → tier='premium' satır.
  - free/anon → premium satırının ilk ~280 char teaser + locked=true +
    upgrade CTA metni; tam synthesis_md DÖNME.
  - satır yok → {week_start:null, synthesis:null, locked:false}.
  - Cache-Control public 60s (macro pattern). 200 döner (404 değil).
- routers/v1/__init__.py: import + include_router (etf/macro yanına).

## 4a — ADIM 2: services/telegram_bot.py /sentez
- `process_sentez_command(chat_id, user_id)`:
  - tier = await _get_user_tier(user_id).
  - corporate_syntheses'ten tier'a uygun en güncel satırı oku (advance→
    advance|premium, premium→premium, free→premium teaser).
  - satır yok → "🏛️ Haftalık Kurumsal Sentez henüz üretilmedi. Her
    Pazartesi 08:30'da yayınlanır." + DURMA.
  - free → teaser (ilk ~400 char) + "💎 Tam sürüm için /upgrade" CTA.
  - premium/advance → synthesis_md (Telegram ~3800 kırp) + dashboard
    deeplink.
  - send_telegram_message. Gemini YOK (DB read) → ağır quota YOK; hafif
    rate-limit opsiyonel.
- dispatcher (≈1443 civarı /login yanına):
  `elif text.lower().startswith("/sentez"): await process_sentez_command(chat_id, user_id)`
- /start yardım metnine "/sentez — Haftalık kurumsal makro sentez" satırı
  (varsa _help listesine; minimum-diff).

## 4a — ADIM 3: scripts/smoke_corporate_public.py (YENI, gecici)
DB'siz: endpoint fonksiyonunu import + _peek_tier('')=='free' birim;
process_sentez_command importable; teaser kırpma saf mantık testi.
DB roundtrip otomatik SKIP (önceki commit deseni). NET RAPOR.

## 4a — ADIM 4: Commit
py_compile + import (main.py) temiz. Tek commit:
`feat(corporate): Commit 4a — /corporate/latest tier-gated endpoint + /sentez Telegram komutu`
+ smoke. push. health 200. memory.

## 4b — (sonraki adım, ayrı repo) ADIM'lar
1. axiom-dashboard src/app/api/corporate/latest/route.ts — macro/latest
   proxy forku (backend /corporate/latest'e; Authorization header pass).
2. CorporateSynthesisCard.tsx — MacroStoryCard forku; locked state →
   blur + upgrade CTA; markdown render synthesis_md.
3. Sayfaya bağla (SummaryDetailModal veya dashboard kart alanı — minimum-diff).
4. preview_* ile browser doğrulama (locked/premium state, network).
5. Ayrı commit + push (axiom-dashboard remote) + Vercel deploy doğrula.

## YAPMA
4a'da dashboard dosyası. Yeni alembic. Adaptör/scheduler/synthesis/
broadcaster davranışı değiştirme. Supabase MCP. railway redeploy.
broadcast kill-switch'e dokunma (Commit 3 OFF kalır). Mahfi/ARK metni
reprodüksiyonu (synthesis_md zaten guard'lı).

## BITINCE CHAT'E DON (4a)
(a) endpoint tier-gate (free teaser/locked vs premium/advance tam),
(b) /sentez komutu + "henüz yok" davranışı, (c) smoke, (d) deploy yeşil.
