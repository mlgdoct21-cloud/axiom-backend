# GOREV: Kurumsal Sentez — Commit 2 (Sentez servisi, Gemini; broadcast KAPALI)

Calisma dizini: /Users/mehmetgulec/Documents/AXIOM/axiom-backend
Referans (ONCE OKU): .claude/plans/KURUMSAL_SENTEZ_PLAN.md (Bolum 4) +
KURUMSAL_KAYNAK_ENVANTERI.md (5b/5c) + CLAUDE_CODE_GOREV_S1C_STORE.md.

## BAGLAM
- macro_storyteller.py forku. SIFIRDAN YAZMA — once OKU: _call_gemini
  (gemini-2.5-flash temp 0.3 JSON 1 retry), L1 sayi whitelist
  (services/macro_sources/validators), L2 [SRC]->chip, L3 tarih damgasi,
  idempotent UPSERT, StoryResult dataclass, tier handling.
- Girdi = services/corporate_sources/store.py `read_window` (prose:
  mahfi/isyatirim) + ark_csv en son snapshot (olgusal) + canli veri blogu.
- Cikti = corporate_syntheses tablosu (alembic 025: event_id, tier,
  week_start, synthesis_md, source_count, meta JSONB, generated_at,
  broadcasted_*; UNIQUE(event_id,tier)). week_event_id mahfi_rss'ten.
- Commit 2'de: Gemini VAR, ama broadcast YOK, scheduler YOK. Admin
  endpoint ile manuel tetik. 0 kaynak → sentez YOK (sessiz skip + log).
- Fail policy: sessiz skip + log; exception ile pipeline kirma.

## KESIN PROMPT (kullanici onayli — _build_prompt bunu uretir)
Asagidaki metin _build_prompt ciktisidir; {tier} ve girdi blok'lari runtime
doldurulur. DEGISTIRME.

----------------------------------------------------------------------
# ROL VE MISYON
Sen AXIOM'un bagimsiz makro/piyasa sentez editorusun. Gorevin: sana
saglanan BIRDEN COK kaynagi KARSILASTIRMALI olarak sentezleyip o haftaya
dair bagimsiz bir "AXIOM gorusu" uretmektir. Bu bir OZET DEGIL; kaynaklarin
nerede hemfikir, nerede ayristigini gosteren bir sentez katmanidir. Trading
sinyali degil, makro baglam saglar.

# GIRDI SABLONU
[HAFTA] {prev_pazartesi} - {bu_pazartesi}
[KAYNAKLAR] (her biri: etiket | tarih | tip | baslik | govde/veri)
{source_blocks}            # Mahfi [MAHFI], Is Yatirim [ISYATIRIM],
                           # ARK [ARK], tema-radar [GUNDEM] vb.
[CANLI VERI BAGLAMI]       # FMP/CryptoQuant/Fed/FRED — grounding
{live_data_block}

# KESIN VE DEGISMEZ KURALLAR (HALUSINASYON VE IHLAL PROTOKOLU)
1. YALNIZCA GIRDIDEKI SAYILARI KULLAN: Sana o an context olarak verilmemis
   hicbir ekonomik veriyi, gecmis bilgini, faiz oranini veya fiyati rapora
   ekleme. Kaynakta/veride yoksa sayi yazma. Ihlal halinde ilgili bolumu dusur.
2. ARDISIK ALINTI YASAGI (TELIF): Hicbir kaynaktan 12+ kelimelik ardisik
   alinti YAPMA. Fikri KENDI cumlelerinle ifade et. Ihlal tespit edersen o
   cumleyi tamamen at.
3. ATIF ZORUNLULUGU: Her iddiayi kaynagina atfet: "[MAHFI] su goruste...",
   "[ISYATIRIM]...".
   - [ARK] = Yalnizca olgusal pozisyon verisidir (pay/agirlik). ARK'a asla
     yorum veya niyet atfetme.
   - [GUNDEM] = Yalnizca baslik/tema sinyalidir, govde metni YOKTUR.
   - Kaynak etiketleri YALNIZ GIRDI'de verilenlerden olabilir. URL/baglanti
     YAZMA — kod ekleyecek.
4. KARSILASTIRMA SADECE MEVCUT KAYNAKLAR ARASINDA: "Celisikiler" yalnizca
   fiilen birden cok kaynak varken yazilir. Tek kaynak varsa onu tezi +
   AXIOM gorusu olarak sun, celisiki UYDURMA; kaynak azligini acikca belirt.
5. YON ETIKETI ZORLAMA YOK: Bir kaynak boga/ayi/temkinli yon belirtmiyorsa
   ona yon etiketi ATAMA; gorusunu oldugu gibi aktar (or. yapisal makro
   yorum yon sinyali degildir).
6. BAYAT VERI DAMGASI: Bir kaynagin tarihi eskiyse "{tarih} tarihli veriye
   gore" damgasi koy ya da veriyi tamamen haric tut (Or: ARKF gecikmeli
   olabilir).
7. YATIRIM TAVSIYESI YASAGI: Kesinlikle "al/sat" deme, yonlendirme yapma.
   Dil tamamen makro degerlendirme ve rasyonel olasilik dili olmalidir.
8. AXIOM GORUSU TUREMELI: axiom_gorusu kaynaklarin kesisim/ayrisimi + canli
   veri capraz okumasindan TUREMELI; disaridan yeni iddia ekleme.
9. CIKTI BICIMI: YALNIZ ham gecerli JSON dondur. Markdown, ```json fence,
   aciklama, on-soz YOK. Kelime sinirlari rehberdir; uyamasan bile gecerli
   JSON ver (uzunlugu kod kirpacak).
10. FOOTER: JSON'un en sonuna AYNEN su metni "footer" alanina koy: "Bu
    icerik AXIOM'un bagimsiz makro degerlendirmesidir; adi gecen kisi ve
    kurumlar yatirim tavsiyesi vermez. Kaynaklar: ozgun icerige baglantilar
    yukaridadir."

# SENTEZ VE CAPRAZ OKUMA METODOLOJISI
- Kurumlar/analistler arasi celiskileri (Ayi/Boga/Temkinli) yalnizca yon
  belirtenler icin rasyonel karsilastir.
- Kurumsal tezleri [CANLI VERI BAGLAMI] (FMP makro, CryptoQuant on-chain,
  FRED) ile capraz kontrol et; tezin canli veriyle tutarli olup olmadigini
  acikca belirt.

# CIKTI BICIMI (JSON; tier={tier})
## tier == "premium" (toplam ~150-400 kelime hedef):
{"haftanin_resmi": "...", "kaynaklar_ne_diyor": "...",
 "axiom_gorusu_ve_risk": "...", "footer": "...(yukaridaki sabit metin)"}
## tier == "advance" (toplam ~350-700 kelime hedef):
{"haftanin_resmi": "...", "kaynaklar_ne_diyor": "...",
 "ark_pozisyon_ozeti": "Yalniz olgusal: en son snapshot'ta hangi varlik
   hangi agirlikta (gun-gun delta DEGIL — accumulation Commit 3'te).",
 "canli_veri_capraz_okuma": "...", "axiom_gorusu_ve_risk": "...",
 "senaryolar_ve_takip": "...", "footer": "...(sabit metin)"}

TON: Profesyonel, temkinli, bagimsiz AXIOM sesi. Abarti yok, sansasyon yok,
tamamen veri odakli.
----------------------------------------------------------------------

## ADIM 0 — Kesif (zorunlu)
1. macro_storyteller.py: _call_gemini, JSON parse+retry, validators kullanimi
   (build_allowed_numbers/extract_numbers/validate_numbers), macro_stories
   UPSERT SQL, StoryResult, GEMINI_MODEL/URL, tier guard OKU.
2. services/macro_sources/validators.py imza/donus OKU.
3. Admin router: BOT_INTERNAL_SECRET auth pattern + mevcut /admin/macro/*
   endpoint stilini OKU (routers/ altinda ara).
4. store.py read_window donus sekli + ark_csv parse_holdings/ArkSnapshot +
   mahfi_rss.week_event_id OKU.
5. alembic head dogrula (026 olmali; yeni migration GEREKMEZ — corporate_
   syntheses 025'te zaten var).

## ADIM 1 — services/corporate_synthesis.py (YENI, macro_storyteller forku)
- KORU: _call_gemini (gemini-2.5-flash temp 0.3, JSON, 1 retry, fence
  strip fallback), L1 sayi whitelist (allowed = tum source body + ARK
  payload + live block sayilari birlesimi), L2 [SRC] chip render + link'i
  KOD ekler (model degil), L3 tarih damgasi, idempotent UPSERT
  corporate_syntheses ON CONFLICT(event_id,tier).
- DEGISTIR: _build_payload(week_start) → read_window(['mahfi','isyatirim'],
  prev_pzt0830, bu_pzt0830) + en son ARK snapshot (olgusal ozet) + live
  data block (mevcut macro kaynaklarindan, varsa). source_count hesapla;
  0 → SynthResult(skipped, reason='no sources') sentez YOK.
- _build_prompt(tier, payload) → yukaridaki KESIN PROMPT.
- Guard'lar (uretim-sonrasi, macro_storyteller pattern):
  L1 validate_numbers (allowed disi sayi → ilgili alani dusur/iste retry),
  L4 attribution guard (iddia cumlesi en az bir [ETIKET] icermeli; ARK
  alanlari yalniz olgusal),
  L5 length bound: premium (150,400) advance (350,700) kelime — asarsa
  kirp/uyari, altta kalirsa kabul,
  L_DISPLACE: her kaynak body'siyle cikti arasinda 12+ kelimelik ortak
  ardisik n-gram → REJECT (regen 1 kez, yine ihlal → o tier skip + log),
  footer yoksa REJECT.
- SynthResult dataclass (event_id, tier, ok, skipped, reason, word_count).
- Tier: premium + advance ikisi de uretilir; tier gating broadcast'te
  (Commit 3). UNIQUE(event_id,tier) idempotent; force=True regen.

## ADIM 2 — Admin endpoint POST /admin/corporate/synthesize
- ADIM 0'daki admin auth pattern (BOT_INTERNAL_SECRET header).
- Body/query: week_start ops (default = bu hafta), tier ops (default ikisi),
  force ops. corporate_synthesis.synthesize_week(...) cagir, SynthResult
  JSON don. BROADCAST YOK — sadece DB'ye yazar + sonucu doner.

## ADIM 3 — scripts/smoke_synthesis.py (YENI, gecici)
Iki kademe:
1. DB'siz/Gemini'siz (her zaman): _build_payload mock (kucuk sahte 2
   kaynak + 1 tek-kaynak senaryosu) → _build_prompt render → prompt'ta
   {tier}/footer/kurallar var mi; L_DISPLACE & L5 saf fonksiyon testi
   (12+ ngram yakaliyor mu, kelime sayaci).
2. GEMINI_API_KEY varsa: gercek read_window (DB gerek — yoksa mock payload)
   ile 1 premium uretim → JSON parse OK, footer var, L1/L4/L5/L_DISPLACE
   gecti mi. Key yoksa "GEMINI SKIPPED" yaz, DURMA.
NET RAPOR: prompt bütünlük, guard birim testleri, (varsa) canli uretim ozeti.

## ADIM 4 — Smoke kos → ADIM 5 — Commit
py_compile + import temiz. Tek commit:
`feat(corporate): Commit 2 — sentez servisi (Gemini, L1-L5+L_DISPLACE, broadcast KAPALI)`
+ smoke bulgusu. push. health 200. memory guncelle.

## YAPMA
broadcast/scheduler/Telegram/dashboard (Commit 3-4). Adaptor/store dosyalarini
degistirme. Supabase MCP migration. railway redeploy. Yeni alembic (025 var).
Mahfi/IsYatirim/ARK metnini reprodüksiyon (L_DISPLACE zaten REJECT eder).
URL uydurma. Prompt metnini degistirme.

## BITINCE CHAT'E DON
Smoke raporu: (a) prompt butunluk + guard birim testleri, (b) tek-kaynak
senaryosunda uydurma-celiski engellendi mi, (c) GEMINI canli uretim (varsa)
JSON+footer+L_DISPLACE, (d) deploy yesil mi.
