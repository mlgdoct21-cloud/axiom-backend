# KURUMSAL KAYNAK EDİNİLEBİLİRLİK ENVANTERİ

Tarih: 2026-05-16
Karar bağlamı: Kullanıcı **kaynak-önce** sıralamayı seçti (sentez/Commit 2
beklemede). Bu doküman çok-kaynak ingestion'ın temelidir.
Referans: `KURUMSAL_SENTEZ_PLAN.md`, `subscriptions_data_sources.md`.

## 0. ÖNCEDEN KİLİTLENEN GERÇEKLER (tekrar tartışılmaz)

- **Ham proprietary sell-side araştırma (Goldman/JPM tarzı PDF notlar) ERİŞİLEMEZ.**
  2026-05-03 analist yol haritasında valide edildi. FactSet/LSEG $50k+/yıl. Bu
  kapsamda DEĞİL — kullanıcıya "kurum X'in araştırma raporunu çekiyoruz" sözü
  verilmez.
- **FMP Premium zaten abone** (MCP, 27 tool). Analist aksiyonu (grades / price
  target / estimates) + kurumsal pozisyon (`form13F`) + haber akışı bu katmandan
  geliyor. ARK 13F = FMP'de zaten var, YENİDEN scrape etme.
- **Evrensel telif kuralı (9 kaynağın hepsine):** tezi sentezle, atfet (`[KAYNAK]`
  chip), linkle, **metni asla çoğaltma**. Footer disclaimer zorunlu. L_DISPLACE
  n-gram REJECT guard'ı (Commit 2) her kaynak için geçerli.

## 1. EDİNİLEBİLİRLİK MATRİSİ

| # | Kaynak | Public ürün | Kanal | Erişim | Zorluk | Önerilen yöntem |
|---|--------|-------------|-------|--------|--------|-----------------|
| 1 | **İş Yatırım** | Günlük Raporlar / Piyasalarda Bugün | **RSS (WP, content:encoded)** | feed AÇIK 200 | **Easy** | Mahfi-tipi RSS adaptör |
| 2 | **Mahfi Eğilmez** | Blog (canlı) | RSS/Atom | açık | Easy | ✅ Commit 1 yapıldı |
| 3 | **ARK Invest** | Günlük holdings/trade CSV | **CSV (CDN)** | açık 200 | **Easy** | Direct CSV fetch+parse |
| 4 | **JP Morgan** | Eye on the Market (Cembalest) | Public PDF | açık 200 | Medium | Index scrape→slug→PDF parse |
| 5 | **Morgan Stanley** | Thoughts on the Market (transcript) | HTML transcript / podcast | WAF aralıklı, login yok | Medium | Headless scrape / podcast RSS |
| 6 | **BlackRock** | Investment Institute Weekly Commentary | Public HTML | Cloudflare WAF (browser UA 200) | Medium | Headless scrape |
| 7 | **Investing.com** | Analysis/Opinion | **Resmi RSS** | RSS açık 200 | Easy* | RSS — **yalnız tema-radar, metin ASLA** |
| 8 | **Fidelity** | Viewpoints / Weekly Update | Public HTML | WAF tüm UA'lara 403 | **Hard** | DROP aday (en düşük ROI) |
| 9 | **Garanti BBVA Yatırım** | Günlük Bülten | Gated | **login zorunlu**, public tier yok | **Hard** | **DROP — edinilemez** |

\* Investing.com teknik olarak kolay ama ToS **yeniden yayını açıkça yasaklıyor**
→ sadece başlık/tema sinyali + link-out; gövde metni hiçbir koşulda kullanılmaz.

## 2. KESİŞEN BULGULAR

- **Gerçek Mahfi-sınıfı kolay kazanımlar:** İş Yatırım RSS + ARK CSV. Mevcut
  `fed_rss`/`mahfi_rss` pipeline'ı neredeyse aynen yeniden kullanılır.
- **DROP:** Garanti (login duvarı, public tier yok), Fidelity (WAF tüm UA'lara
  dirençli, ROI düşük). İkisi de baştan kapsam dışı bırakılır — scraper'a yatırım
  yapılmaz.
- **PDF parse gerektiren:** JPM Eye on the Market — PDF açık ama issue-başına slug
  değişiyor; önce EOTM index sayfasından güncel dosya adı keşfedilmeli.
- **Headless gerektiren (WAF, login yok):** BlackRock weekly commentary, Morgan
  Stanley transcript. CoinGlass Playwright supervisor pattern'i ile yapılabilir.
- **ARK günlük trade-delta CSV benzersiz:** tek makine-yapılı (structured) kaynak,
  login yok, sabit URL pattern (`assets.ark-funds.com/.../ARKK_HOLDINGS.csv`).
  Prose yorumdan ayrı ele al — "ARK bu hafta ne aldı/sattı" kantitatif widget'ı.
  13F ile çakışmaz (FMP'deki 13F gecikmeli; CSV günlük).
- **Açık iş — 2. TR yorumcu UNCONFIRMED:** Mahfi dışında temiz kişisel RSS'i
  doğrulanmış 2. ekonomist yok. Uğur Gürses / Atilla Yeşilada / Ozan Bingöl
  blog/Substack feed'leri build öncesi ayrı bir probe ister.

## 3. ÖNERİLEN KAYNAK-ÖNCE BUILD SIRASI

Faz S1 — **Kolay RSS/CSV** (mevcut pipeline forku, ~düşük risk):
  - S1a: İş Yatırım RSS adaptörü (`corporate_sources/isyatirim_rss.py`) — mahfi_rss
    birebir forku, `corporate_source_state` source='isyatirim'.
  - S1b: ARK CSV adaptörü (`corporate_sources/ark_csv.py`) — httpx + csv parse,
    5 ETF (ARKK/ARKW/ARKG/ARKQ/ARKF), günlük delta.
  - S1c: 2. TR yorumcu feed probe → doğrulanırsa RSS adaptör.

Faz S2 — **PDF** (orta risk):
  - JPM Eye on the Market: index keşif + PDF fetch + pdfplumber (mevcut SEP
    parser pattern'i, `services/macro_sources/fed_statement` PDF tarafı).

Faz S3 — **Headless** (CoinGlass Playwright supervisor forku):
  - BlackRock weekly commentary + Morgan Stanley transcript.

Faz S4 — **Tema-radar (metin yok):**
  - Investing.com RSS yalnız başlık/tema sinyali — sentez prompt'una "gündem"
    girdisi, gövde reprodüksiyonu YOK.

DROP (build edilmez): Garanti, Fidelity, tüm gated proprietary research.

## 4. SENTEZ MOTORUNA ETKİSİ (Commit 2 yeniden tasarımı)

Kaynak-önce kararıyla Commit 2 artık tek-kaynak değil **heterojen N-kaynak**
sentezi olacak: girdi tipleri = {TR-blog prose, broker-RSS prose, kurumsal
weekly-commentary prose, ARK structured delta, Investing tema-başlıkları} +
mevcut canlı veri (FMP/CryptoQuant/Fed/FRED). Sentez prompt'u **karşılaştırmalı**
("X boğa, Y temkinli, ARK şunu aldı, AXIOM görüşü Z") olacak. Her kaynak tipi
için ayrı `truncated`/`structured` bayrağı; `mahfi_rss`'teki MahfiPost yerine
kaynak-agnostik `CorporateDoc(source, kind, title, link, published, body|data,
truncated)` ortak dataclass'a terfi.

## 5. KULLANICI KARARLARI (2026-05-16) + ATILLA YEŞİLADA PROBE

Kararlar:
- 2. TR yorumcu = **Atilla Yeşilada** (probe edildi, aşağı bak).
- Fidelity + Garanti = **sonraya bırakıldı** (DROP değil, açık madde — şimdilik
  build edilmez, başka kaynaklara odak).
- Investing.com = **sadece tema-radar onaylandı** (RSS başlık/gündem; gövde ASLA;
  link-out). Faz S4.

**ATILLA YEŞİLADA — edinilebilirlik (probe sonucu, web doğrulandı):**
Mahfi-sınıfı temiz tam-metin RSS'i **YOK**:
- `paraanaliz.com/rss.xml` = 5-item teaser (gövde ~26 char, yazar alanı yok,
  content:encoded yok) → sentez için kullanılamaz; WP `/feed/` 404.
- `atillayesilada.com` = 301/ölü feed, içerik bayat.
- Düzenli köşe yazıları paraanaliz.com'da feed'siz → **HTML scrape** gerekir
  (Cloudflare/WAF markörü görülmedi, browser UA ile muhtemel; www/path
  build-time çözülecek). Zorluk: **Medium**.
- **"Mesele Ekonomi" podcast** `media.rss.com/mesele-ekonomi/feed.xml` = 200,
  **çok aktif (günlük, 2026-05-16 item)**, ama **audio-only**, show-notes
  ~157 char. Episode **başlıkları tez-yoğun** ("Bu Ekonomi Seçim Erteletir! &
  Merkez Bankası Havlu Mu Attı | Atilla Yeşilada"). İki kullanım: (a) Easy
  tema-radar (yalnız başlık, Investing.com tier'i ile aynı mekanizma),
  (b) tam değer ancak STT transkripsiyon (Whisper — infra/maliyet, ertelenir).

Aggregatörler (qoshe.com, paraborsa.net) = republisher, telif belirsiz, kullanma.

⇒ Yeşilada **easy-win değil**. Sınıf: Medium (HTML scrape) | Easy (podcast
başlık-radar) | Hard (STT). S1 onu beklememeli.

**AÇIK SORU (kullanıcı kararı bekler):** Yeşilada nasıl alınsın?
A) paraanaliz HTML scrape (Medium, tam metin),
B) podcast başlık-radar şimdi + STT sonra (önerilen — S4 tema-radar
   mekanizmasını yeniden kullanır, ~sıfır ek maliyet, S1'i bloklamaz),
C) şimdilik ertele, S1 = yalnız İş Yatırım + ARK.

## 5b. KRİTİK BULGU — İş Yatırım feed derinliği (S1a smoke, 2026-05-16)

S1a adaptörü yazıldı + smoke koşuldu. Sonuç:
- ✅ fetch 200 + ETag, parse 10 rapor, **hepsi FULL-TEXT** (content:encoded,
  truncated=0/10), `author` (dc:creator) dolu. Adaptör doğru çalışıyor.
- ⚠️ **Feed derinliği ≈ 1 gün.** İş Yatırım günde ~10 rapor yayınlıyor; RSS
  yalnız son 10 item'i tutuyor. Smoke'ta tüm 10 item 2026-05-15 tarihli →
  haftalık pencerede (önceki Pzt→bu Pzt) **0 rapor** çıktı. Bu adaptör hatası
  DEĞİL; feed-derinliği vs. pencere genişliği uyumsuzluğunun doğru yansıması.

**Mimari sonuç (Commit 2/3 girdisi):** İş Yatırım Mahfi gibi "Pazartesi bir
kez çek, haftayı sentezle" modeline UYMAZ. Yüksek-hacimli kaynaklar
**artımlı poll + biriktirme** gerektirir: scheduler İş Yatırım'ı sık (günlük
veya 2x/gün) çeker, postları kalıcı bir store'a (yeni tablo, ör.
`corporate_posts`) idempotent UPSERT eder; sentez zamanı store'dan haftalık
pencere okunur. Mahfi düşük-hacim olduğu için fetch-at-synthesis yeterli.
→ Commit 3 scheduler tasarımı: kaynak başına `poll_cadence` + accumulation
layer. ARK CSV (günlük snapshot) da benzer biriktirme ister.

## 5c. S1b — ARK CSV bulgusu (smoke, 2026-05-16)

S1b adaptörü (`ark_csv.py`) yazıldı + smoke koşuldu:
- ✅ **5/5 fon** 200 (ARKK/ARKW/ARKG/ARKQ/ARKF). ARKQ dosya adı tahmin
  değil probe ile bulundu: `ARK_AUTONOMOUS_TECH._%26_ROBOTICS_ETF_ARKQ...`.
- ✅ Parse sağlıklı: her fonda **ağırlık toplamı %100.0**, footer/disclaimer
  satırı atlandı (skipped=1), boş-ticker (warrant/yabancı) korunuyor.
- ⚠️ **ARKF bayat:** as_of=2026-01-02; diğer 4 fon 2026-05-15. ARKF CSV
  ~4.5 ay güncellenmemiş. Adaptör hatası değil (as_of doğru yüzeye
  çıkıyor). **Sonuç:** per-fon **staleness guard** şart — macro storyteller'ın
  veri-yaşı rozeti pattern'i: as_of bugüne göre N günden eskiyse sentezden
  dışla/işaretle. Commit 2/3 girdisi.
- **Telif (footer'da doğrulandı):** "no part ... reproduced ... or referred
  to ... without written permission". Kullanım = yalnız olgusal holdings
  (pay/ağırlık/değer faktları); ARK prose ASLA. Commit 2 attribution
  guard'ında ARK için özel atıf metni.

## 6. GÜNCEL FAZ S1 KAPSAMI (kilitlendi)

S1a İş Yatırım RSS adaptörü — **kesin easy-win, full-text**, ilk iş.
S1b ARK günlük holdings CSV adaptörü — kesin easy-win, structured.
S1c Atilla Yeşilada — yöntem kullanıcı kararına bağlı (yukarıdaki A/B/C).
S2 JPM Eye on the Market PDF · S3 BlackRock+MS headless · S4 Investing tema-radar.
DROP/ertelendi: Fidelity, Garanti, tüm gated proprietary research.
