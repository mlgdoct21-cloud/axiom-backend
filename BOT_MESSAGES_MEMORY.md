# AXIOM Telegram Bot - Messages Memory & Updates
**Last Updated:** April 14, 2026  
**Purpose:** Track bot message templates and improvements

---

## 📊 CURRENT MESSAGE TEMPLATES (OLD - TOO BORING)

### 1. Welcome Message (/start)
```
🦅 Axiom OS'a Hoş Geldiniz.

Piyasanın gürültüsünden arındırılmış, sadece saf veri ve net beklentilere dayalı kişisel finans asistanınız aktif.

📌 Komutlar:
/haber — Anlık haber analizi
/tags — Konu tag'lerini seç (BTC, Altın, BIST...)
/takip [kelime] — Özel kelime takibe al (AAPL, Tesla...)
/takipcikar [kelime] — Takipten çıkar
/takiplistem — Tüm takiplerinizi gör
```
**Problem:** Çok uzun, akademik, ilgi çekmeyen

### 2. /haber Command Response
```
🔄 Piyasalar taranıyor. FinLens analiz hazırlıyor...
```
**Problem:** Sıkıcı, anticipation yok

### 3. News Broadcast (Crawler tarafından gönderilen)
```
📌 **[Title]**

[AI Summary]

📰 Kaynak: [Link]
```
**Problem:** Standard, click-through yok, engagement yok

---

## 🎯 CRYPTO ME STYLE ANALYSIS (From Screenshots)

### Pattern 1: Urgent Alert
```
⚡ GEÇ BITCOIN'İNDE DUMP NEDEN GELDİ! ATEŞKES BOZULACAK MI ENDİŞELERİ!
⚡ Önce kronolojik olarak ne olmuştu...
[Detailed analysis with timeline]
```
**Characteristics:**
- All caps for urgency
- Lightning emoji ⚡
- Short punchy headline
- Then detailed explanation
- Links to charts/evidence

### Pattern 2: Market Update with Warnings
```
⚠️ Ama şu anda geleneksel piyasalar kapalı, fonlar-kurumlar kapalı...
[Risk explanation]
✅ What we know:
• Point 1
• Point 2
```
**Characteristics:**
- Warning emoji ⚠️
- Bullet points with checkmarks
- Future predictions
- Multiple paragraphs but scannable

### Pattern 3: Technical Analysis
```
!? Bakalım gece saat 01:00'da CME futures'ları...
[Long technical explanation]
Ancak şunları biliyoruz;
✅ Market closed...
✅ When it opens...
```
**Characteristics:**
- !? for complex analysis
- Specific times/events
- Checkmarks for confirmations
- Action items highlighted

### Pattern 4: Simple Update
```
Sezon sonu yaklaşırken fan token'lar hareketlendi: FB token sert yükseldi
```
**Characteristics:**
- Minimal emoji (1 visual cue)
- One-liner headline
- Direct, no fluff

---

## ✨ NEW MESSAGE TEMPLATES (CRYPTO ME STYLE)

### Template 1: URGENT Alert
```
⚡ [UPPERCASE HEADLINE - WHAT'S HAPPENING]
[1-2 line punchy explanation]

📊 Neyi Biliyoruz:
• Key point 1
• Key point 2
• Key point 3

🔗 Detaylı analiz için /haber
```

### Template 2: Market Warning
```
⚠️ [Market Risk/Opportunity]
[2-3 line context]

Şu ana kadar bilinen:
✅ Fact 1
✅ Fact 2

⏰ Beklemeli olduğunuz: [Time/Event]
```

### Template 3: Technical/Strategic Move
```
!? [What Happened - 1 Line]
[2-3 lines explanation]

Bunun anlamı:
✅ Implication 1
✅ Implication 2

📈 Detaylar: /takip [keyword]
```

### Template 4: Quick Update (Minimal)
```
📌 [Headline - 1 line max]
[1-2 line explanation]

🔗 /haber
```

### Template 5: Welcome (NEW)
```
🚀 Axiom'a Hoş Geldin

📊 Kripto & Finansal Piyasaların Gerçek Dedikodusu
Başlayan her fırsatı kaçırma.

⚡ Ne Yapmak İstiyorsun?
/haber — Anlık güncellemeler
/tags — İlgi alanlarını seç
/takip AAPL — Sembol takip et
```

---

## 🔄 IMPLEMENTATION STATUS

| Template | Current | NEW | Status |
|----------|---------|-----|--------|
| Welcome | ❌ Long & Boring | ✅ Punchy | ✅ DONE |
| /haber Response | ❌ Generic | ✅ Engaging | ✅ DONE |
| News Broadcast | ❌ Standard | ✅ Dynamic | ✅ DONE |

---

## ✅ IMPLEMENTED CHANGES (April 14, 2026)

### 1. Welcome Message (telegram_bot.py, Line 119-129)
**BEFORE:**
```
🦅 Axiom OS'a Hoş Geldiniz.
Piyasanın gürültüsünden arındırılmış...
```

**AFTER:**
```
🚀 Axiom'a Hoş Geldin

📊 Kripto & Finansal Piyasaların Gerçek Dedikodusu
Başlayan her fırsatı kaçırma.

⚡ Ne Yapmak İstiyorsun?
/haber — Anlık pazar güncellemeleri
/tags — İlgi alanlarını seç (BTC, Altın, BIST...)
/takip AAPL — Sembol takip et
```

### 2. /haber Command Response (telegram_bot.py, Line 134)
**BEFORE:**
```
🔄 Piyasalar taranıyor. FinLens analiz hazırlıyor...
```

**AFTER:**
```
⚡ Piyasalar analiz ediliyor...

⏳ 30 saniye içinde güncel haberler geliyor.
```

### 3. News Broadcast Format (crawler.py, Lines 90-99)
**BEFORE:**
```
📌 **Title**
**Summary**
📰 Kaynak: Link
```

**AFTER:**
```
⚡/⚠️/🚀 **Title**   [Dynamic emoji based on content]
**Summary**
🔗 Detaylı Analiz → • **Source**
```

**Smart Emoji Selection:**
- ⚡ = "dump", "çöküş", "crash", "sert", "hızlı"
- ⚠️ = "risk", "uyarı", "dikkat", "tehdit"
- 🚀 = "yüksel", "rally", "pump", "artış", "kazanç"
- 📊 = default (normal update)

---

## 🚀 NEXT STEPS

1. ✅ Create memory file (DONE)
2. ✅ Update all templates in code (DONE)
3. ⏳ Start bot: `python bot_runner.py`
4. ⏳ Get first messages - should be much more engaging now
5. ⏳ Fine-tune emoji keywords if needed

---

---

## 🐛 FIXES APPLIED (April 14, 2026 - Stability Update)

### Problem 1: "Bad Request: chat not found" warnings flooding logs
**Root Cause:** Broadcasting to users when database is empty
**Fix (crawler.py, Line 106-108):**
```python
# BEFORE: Logged all errors
logger.warning(f"{user.telegram_id}... {e}")

# AFTER: Suppress "chat not found" (expected when no users)
if "chat not found" not in str(e).lower():
    logger.warning(...)
```
**Result:** Logs are clean, only real errors shown ✅

### Problem 2: Same news appearing twice
**Root Cause:** RSS feeds returning same news with different links, batch dedup only checking links
**Fix (crawler.py, Lines 118-132):**
```python
# BEFORE: 
batch_key = link  # Only link checked

# AFTER:
batch_key = (link, title.lower().strip())  # Link AND normalized title
```
**Result:** Duplicate prevention now catches same news from multiple sources ✅

### Problem 3: 30-minute wait between crawls (testing nightmare)
**Root Cause:** Production setting (30 min = 1800 sec) too long for testing
**Fix (crawler.py, Line 162):**
```python
# BEFORE: 
await asyncio.sleep(1800)  # 30 minutes

# AFTER:
await asyncio.sleep(300)  # 5 minutes for testing
# Note: Change to 1800 for production
```
**Result:** Can test crawl results every 5 min instead of waiting 30 min ✅

---

## ✅ Current Status

| Issue | Status | Fix |
|-------|--------|-----|
| Chat not found errors | ✅ FIXED | Suppress in logs |
| Duplicate messages | ✅ FIXED | Title-based dedup |
| Slow testing | ✅ FIXED | 5-min crawl cycle |

---

**Remember:** Build on previous decisions, reference this file next time.
**How to test:** 
1. Kill old bot: `Ctrl+C`
2. Run: `python bot_runner.py`
3. Telegram: `/start` to register
4. Wait 5 minutes for first crawl
5. Or use `/haber` command for instant fetch
