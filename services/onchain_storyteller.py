"""Axiom Analistleri — Gemini ile snapshot'taki ham sayıları
Türkçe 6-bloklu karar çerçevesine çevirir (CryptoMe tarzı).

Pipeline:
1. get_onchain_snapshot(symbol) → 8-16 sinyal + axiom_score + breakdown
2. Snapshot'tan kompakt LLM context kur (sayıların kaynağı sadece bu)
3. Gemini 2.5-flash JSON → { headline, paragraphs[3], footer }
4. cryptoquant_cache (metric_key="story") 12h TTL ile sakla
5. Cache miss → yeniden üret

Tasarım:
- Sadece yatırım tavsiyesi DEĞİL, "ne oluyor" anlatımı.
- Sayılar yalnızca context'te geçenlerden kullanılır (LLM uydurmasın).
- Cache 12h: brifing günde 1 kez yenilenir, dashboard hızlı açılır.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

from core.logger import get_logger
from services.cryptoquant_service import (
    get_onchain_snapshot,
    _cache_get,
    _cache_set,
    _is_configured,
)
from services.etf_flow_cache_service import get_latest_etf_flow

logger = get_logger("crypto.storyteller")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = httpx.Timeout(45.0, connect=8.0)
_CACHE_TTL = timedelta(hours=12)
_SUPPORTED = ("BTC", "ETH", "XRP")


# ── Snapshot → LLM context ────────────────────────────────────────────────

def _compact_signals(snapshot: dict) -> list[dict]:
    """Snapshot içindeki signals dict'ini sıkıştırılmış liste olarak döner.
    LLM kısa bir tablo görsün — her satırda metrik adı, değer, ne diyor."""
    out: list[dict] = []
    signals = snapshot.get("signals") or {}
    for key, s in signals.items():
        if not isinstance(s, dict):
            continue
        out.append({
            "metric": key,
            "value": s.get("value_str", ""),
            "label": s.get("label_tr", ""),
            "direction": s.get("signal", "NEUTRAL"),
        })
    return out


def _top_contributors(snapshot: dict, n: int = 3) -> dict:
    """En etkili 3 pozitif + 3 negatif signal — Gemini odağı belirlesin diye."""
    breakdown = snapshot.get("score_breakdown") or []
    pos = sorted(
        [b for b in breakdown if (b.get("contribution") or 0) > 0],
        key=lambda x: -(x.get("contribution") or 0),
    )[:n]
    neg = sorted(
        [b for b in breakdown if (b.get("contribution") or 0) < 0],
        key=lambda x: (x.get("contribution") or 0),
    )[:n]
    return {
        "supports": [
            {"metric": p["metric"], "label": p.get("label_tr", "")}
            for p in pos
        ],
        "pressures": [
            {"metric": n["metric"], "label": n.get("label_tr", "")}
            for n in neg
        ],
    }


def _direction_source(snapshot: dict) -> Optional[dict]:
    """Spot vs Türev yön kaynağı analizi.
    funding negatif + spot taker yüksek = spot-led (sağlıklı)
    funding pozitif + spot taker düşük = kaldıraç-led (kırılgan)
    """
    funding = snapshot.get("funding_rates")
    spot = snapshot.get("spot_taker")
    if not funding and not spot:
        return None
    f_val = funding.get("latest") if funding else None
    s_val = spot.get("ratio") if spot and spot.get("ratio") is not None else None
    return {
        "funding_rate": f_val,
        "spot_taker_ratio": s_val,
        "futures_open_interest": (
            snapshot.get("open_interest", {}).get("change_pct")
            if snapshot.get("open_interest") else None
        ),
    }


def _etf_verdict_tr(net_flow_usd: Optional[float], symbol: str) -> str:
    """ETF flow için tek-kaynak verdict. BTC daha büyük ölçek (>$100M güçlü),
    ETH daha küçük (>$30M güçlü). Çelişki önlemek için Gemini bunu
    aynen kullanır — kendi yorumu yapmaz."""
    if net_flow_usd is None:
        return "📊 Veri yok"
    v = float(net_flow_usd)
    thresholds = (100_000_000, 20_000_000) if symbol == "BTC" else (30_000_000, 5_000_000)
    strong, mild = thresholds
    if v >= strong:
        return "🟢 Güçlü Kurumsal Birikim (günlük)"
    if v >= mild:
        return "🟢 Hafif Kurumsal Giriş (günlük)"
    if v <= -strong:
        return "🔴 Güçlü Kurumsal Çıkış (günlük)"
    if v <= -mild:
        return "🔴 Hafif Kurumsal Çıkış (günlük)"
    return "🟡 Nötr Kurumsal Akış (günlük)"


async def _etf_block(symbol: str) -> Optional[dict]:
    """BTC + ETH için ETF flow özeti — spot kurumsal talep göstergesi.
    Gemini'ye GÜNLÜK pencere etiketi + tek-kaynak verdict ile gider;
    Coinbase Premium (anlık spot) ile karıştırılmasın."""
    if symbol not in ("BTC", "ETH"):
        return None
    try:
        flow = await get_latest_etf_flow(symbol)
    except Exception as e:
        logger.warning(f"etf flow read failed for {symbol}: {e}")
        return None
    if not flow:
        return None
    net_usd = flow.get("net_flow_usd")
    return {
        "net_flow_usd": net_usd,
        "net_flow_coins": flow.get("net_flow_coins"),
        "scraped_at": flow.get("scraped_at"),
        "is_fresh": flow.get("is_fresh"),
        "age_hours": flow.get("age_hours"),
        "window": "günlük (T-1)",
        "verdict_tr": _etf_verdict_tr(net_usd, symbol),
    }


async def _build_context(snapshot: dict) -> dict:
    sym = snapshot.get("symbol")
    return {
        "symbol": sym,
        "axiom_score": snapshot.get("axiom_score"),
        "score_zone": snapshot.get("score_zone_tr"),
        "score_summary": snapshot.get("score_summary"),
        "overall": snapshot.get("overall_tr"),
        "signals": _compact_signals(snapshot),
        "drivers": _top_contributors(snapshot),
        "direction_source": _direction_source(snapshot),
        "etf_flow": await _etf_block(sym) if sym else None,
        "fetched_at": snapshot.get("fetched_at"),
    }


# ── Prompt ─────────────────────────────────────────────────────────────────

_SYMBOL_PERSONALITY = {
    "BTC": (
        "BTC dilinde 'döngü', 'kurumsal akış', 'spot ETF', 'madenci', 'kohort' "
        "(STH/LTH) anahtar kavramlardır. Bitcoin'i bir 'rezerv varlık' olarak "
        "konumlandır; perakende heyecanı yerine uzun-soluklu sermaye davranışına "
        "odaklan."
    ),
    "ETH": (
        "ETH dilinde 'staking', 'gas/aktif kullanım', 'L2', 'spot ETH ETF', "
        "'beacon chain çıkışları', 'borsa arz oranı' anahtar kavramlardır. "
        "Ethereum'u 'üretken bir altyapı varlığı' olarak konumlandır; ağ kullanım "
        "ve doğrulayıcı davranışı vurgu ön planda."
    ),
    "XRP": (
        "XRP dilinde 'türev tarafı dominasyonu', 'likidasyon kaskadı', 'düşük "
        "doğrulayıcı maliyeti', 'tx hacmi', 'tek noktalı whale dağılımı' anahtar "
        "kavramlardır. XRP'yi 'yüksek-volatil türev oyun alanı' olarak konumlandır; "
        "spot ETF veya madenci kavramı KULLANMA — XRP PoS değil, PoW de değil, "
        "RPCA konsensüsü ile çalışır."
    ),
}


_METRIC_WINDOW_TR: dict[str, str] = {
    # Per-metric zaman penceresi etiketi — Gemini'nin "kurumsal alım" gibi
    # ifadeleri farklı zamanlarda ölçülen metriklerle karıştırmaması için.
    "exchange_netflow":  "anlık (son gün)",
    "whale_ratio":       "anlık (son gün)",
    "miner_outflow":     "anlık (gün) — 7G ortalamaya göre",
    "miner_reserve":     "trend (7G değişim)",
    "stablecoin_inflow": "anlık (son gün)",
    "funding_rates":     "anlık (son gün)",
    "open_interest":     "anlık (son gün)",
    "sopr":              "günlük (zincir-üstü tüketim)",
    "sopr_ratio":        "trend (LTH/STH dengesi)",
    "coinbase_premium":  "anlık (spot fiyat farkı)",
    "korean_premium":    "anlık (spot fiyat farkı)",
    "mvrv":              "trend (network değerleme)",
    "nupl":              "trend (network kâr/zarar)",
    "mpi":               "anlık (madenci satış endeksi)",
    "puell":             "trend (madenci kâr katsayısı)",
    "leverage_ratio":    "anlık (türev kaldıraç)",
    "realized_price":    "trend (network maliyet ortalaması)",
    "hash_rate":         "trend (7G değişim)",
    "spot_taker":        "anlık (son gün)",
    "btc_liquidations":  "anlık (son gün)",
}


def _annotate_signals_with_window(ctx: dict) -> dict:
    """signals[].window alanını ekle — Gemini metriklerin ölçtüğü zamanı
    metinde açıkça belirtsin. Ayrıca direction_source'a 'window: anlık'
    bandrolü vurgu için ekle."""
    out = dict(ctx)
    sigs = list(out.get("signals") or [])
    for s in sigs:
        m = s.get("metric")
        if m and m in _METRIC_WINDOW_TR:
            s["window"] = _METRIC_WINDOW_TR[m]
    out["signals"] = sigs
    if out.get("direction_source"):
        out["direction_source"] = {**out["direction_source"], "window": "anlık (son gün)"}
    return out


def _build_prompt(ctx: dict) -> str:
    sym = ctx.get("symbol", "?")
    personality = _SYMBOL_PERSONALITY.get(sym, "")
    has_etf = bool(ctx.get("etf_flow"))
    annotated = _annotate_signals_with_window(ctx)
    return (
        f"Sen 'Axiom Analistleri' takımısın — {sym} için on-chain veriyi "
        "Türk kripto yatırımcısına KARAR ÇERÇEVESİ olarak sunuyorsun. "
        "CryptoMe akademi yazarının üslubunu örnek al: numaralı engel/aşama "
        "dizimi, 'eğer X olursa Y' koşullu cümleler, kendi pozisyonunu "
        "açıkça ilan etme + revizyon koşulu, sıcak ama kurumsal Türkçe.\n\n"
        f"### {sym} kişiliği:\n{personality}\n\n"
        f"### INPUT JSON (TÜM sayılar buradan; başka kaynak YASAK):\n"
        f"{json.dumps(annotated, ensure_ascii=False)}\n\n"
        "### ÇIKTI ŞEMASI (sadece JSON):\n"
        "{\n"
        '  "headline": string,        // max 120 karakter; başlık-soru veya net iddia\n'
        '  "paragraphs": [string × 6],\n'
        '  "footer": string\n'
        "}\n\n"
        "### 6 BLOK (sırayla, her biri 2-4 cümle):\n"
        "1) BAŞLIK SORUSU + NET CEVAP\n"
        "   Headline'daki soruyu/iddiayı NET cevapla. Axiom skoru + bölge "
        "   (Güvenli/Dikkatli/Riskli/Fırsat) burada geçsin.\n"
        "2) AŞILAN EŞİKLER (✅)\n"
        "   drivers.supports'tan EN AZ 2 sinyali 'engelleri geçtik' "
        "   çerçevesinde anlat. SOPR Ratio varsa ve 1.05'in altındaysa 'kohortlar "
        "   dengede, taze para hareketleniyor' nuansını ver; 1.15'in üstündeyse "
        "   'uzun vadeci dağıtım baskın — tepe uyarı bölgesi' diye uyar.\n"
        "3) HENÜZ AŞILMAYAN EŞİKLER (⏳)\n"
        "   drivers.pressures veya NEUTRAL'lardan 1-2 madde. 'Şu seviyeye "
        "   gelirse şu anlama gelir' formatı şart. Korean Premium negatifse "
        "   'Asya tarafı henüz teyit etmedi — Upbit primi sıfırı geçince ralli "
        "   onayı tamamlanır' gibi bir koşul ekleyebilirsin.\n"
        "4) YÖN KAYNAĞI: SPOT MU TÜREV Mİ? (yeni)\n"
        "   direction_source bloğunu yorumla:\n"
        "   • funding NEGATIF + spot_taker_ratio >1.0 = SPOT-led rally "
        "     (sağlıklı, kurumsal alıcı baskın)\n"
        "   • funding POZİTİF + spot_taker düşük = KALDIRAÇ-led "
        "     (kırılgan, short squeeze tehdidi)\n"
        "   • btc_liquidations sinyali varsa: long tasfiyesi baskın = forced "
        "     sell biteliyor (dip onayı), short sıkışması baskın = ralli "
        "     yapay olabilir; çift yönlü tasfiye = türev tarafı temizleniyor.\n"
        + (
            "   • etf_flow.net_flow_usd ve etf_flow.verdict_tr alanlarını "
            "AYNEN kullan (örn '🟢 Güçlü Kurumsal Birikim (günlük)'). Verdict'i "
            "kendin yeniden yorumlama. ETF rakamını da açıkça yaz "
            "(örn '$478M giriş'). ETF flow GÜNLÜK pencere; Coinbase Premium "
            "ANLIK spot fiyat farkı — ikisini KARIŞTIRMA.\n"
            if has_etf else ""
        ) +
        "   Bu blok yoksa çıkarma — 'yön kaynağı net okunmuyor' diye yaz.\n"
        "5) TETİKLEYİCİ + REVİZYON KOŞULU\n"
        "   İki yönlü: (a) 'Eğer [metrik] [eşik]'i geçerse Axiom'un kanaati "
        "   [şu yöne] döner', (b) 'Aşağıda [metrik] [eşik]'in altına düşerse "
        "   erken uyarı veririz'. EN AZ 1 yukarı + 1 aşağı koşul.\n"
        "6) AXIOM'UN POZİSYONU + İZLENECEK TEK METRİK\n"
        "   'Şu an Axiom skoru X — [bölge]. Yukarı doğru [pratik anlam], "
        "   aşağı [pratik anlam]. Önümüzdeki günlerde özellikle [TEK metrik] "
        "   izleyin.'\n\n"
        "### DİL ÇEŞİTLİLİĞİ — KESİN KURAL:\n"
        "Şu kalıp başlıkları KULLANMA (her sembolde tekrar ediyor, kötü):\n"
        "  ✗ 'X için boğa momentumu geri mi dönüyor?'\n"
        "  ✗ 'Karışık sinyallerle dikkatli bir dönem'\n"
        "  ✗ 'X için yükseliş rüzgarı esiyor mu?'\n"
        "Bunun yerine sembolün KENDİ kişiliğine özgü başlık ürün. Örn:\n"
        "  ✓ BTC: 'Spot ETF girişi mi, kaldıraçlı sıçrama mı? STH-SOPR cevap veriyor.'\n"
        "  ✓ ETH: 'Borsa arzı düşüyor, doğrulayıcı sırası uzuyor — ama funding henüz uyumadı.'\n"
        "  ✓ XRP: 'Türev tarafı çift yönlü temizleniyor; spot agresörler hâlâ kararsız.'\n\n"
        "### JARGON SÖZLÜĞÜ (mutlaka çevir):\n"
        "- netflow → 'borsa akışı (giren-çıkan fark)'\n"
        "- funding rate → 'fonlama oranı — kaldıraçlı pozisyonların yön ücreti'\n"
        "- whale ratio → 'balina oranı'\n"
        "- MVRV → 'gerçekleşmiş kâr/zarar oranı'\n"
        "- SOPR → 'satılan paraların kâr katsayısı'\n"
        "- SOPR Ratio → 'uzun/kısa vadeci kâr dengesi'\n"
        "- liquidations → 'tasfiye (zorla kapatılan kaldıraçlı pozisyonlar)'\n"
        "- Korean Premium → 'Upbit primi (Asya retail iştahı)'\n"
        "- MPI → 'madenci satış baskısı endeksi'\n"
        "- spot taker ratio → 'spot alıcı/satıcı oranı'\n"
        "- open interest → 'açık pozisyon hacmi'\n"
        "- ETF flow → 'spot ETF net girişi/çıkışı'\n\n"
        "### ÇELİŞKİ UZLAŞTIRMA (kritik — yayın öncesi):\n"
        "Aşağıdaki çiftlerden biri varsa AYNI paragrafta bahsediyorsan "
        "ZAMAN PENCERESİ farkını AÇIK CÜMLE ile uzlaştırmak ZORUNDASIN. "
        "İki sinyal birbirine zıt görünebilir ama aslında farklı şeyleri "
        "ölçer:\n"
        "  • ETF flow (günlük kurumsal birikim) ↔ Coinbase Premium (anlık "
        "    spot fiyat farkı): 'ETF kanalında günlük kurumsal alım sürerken "
        "    Coinbase spot tarafı kısa vadeli satış baskısı' gibi.\n"
        "  • miner_outflow (anlık günlük transfer) ↔ miner_reserve (7G "
        "    trend değişimi): 'Bugün borsalara madenci transferi yüksek "
        "    olsa da 7G rezerv trendi sabit; tek günlük spike, trend değil' "
        "    gibi.\n"
        "  • stablecoin_inflow (anlık) ↔ axiom_score'un genel havası: "
        "    'Stablecoin akışı düşük olmasına rağmen sinyal dengesi pozitifte' "
        "    diye uzlaştır.\n"
        "  • exchange_netflow ↔ whale_ratio: birinin bullish, diğerinin "
        "    bearish/neutral olması olağan; balina davranışı netflow'dan "
        "    bağımsız okunabilir.\n"
        "ASLA aynı paragrafta uzlaştırma cümlesi olmadan zıt verdict bırakma. "
        "İki sinyalin verdict'i çelişiyorsa SEMBOL adı + zaman penceresi + "
        "uzlaştırma cümlesi şart.\n\n"
        "### SİNYAL ETİKETİ KULLANIM KURALI (SSoT):\n"
        "INPUT'taki her signals[].label etiketi (örn '🔴 Madenci Satıyor', "
        "'🟢 ABD Kurumsal Alım', '🟡 Dengeli') Axiom'un TEK GERÇEKLİĞİDİR. "
        "Bunları YENİDEN YORUMLAMA. Eğer label '🔴 Düşük Giriş' diyorsa "
        "metinde 'düşük giriş' veya eşdeğer ifade kullan; ASLA 'yüksek "
        "stablecoin akışı' yazma. Bir metriğin label'ı ile metnindeki "
        "verdict'in BİRBİRİYLE TUTARLI olmak zorunda.\n\n"
        "### KESİN YASAKLAR:\n"
        "- Sayı UYDURMA — INPUT'taki signals[].value veya axiom_score "
        "  veya direction_source veya etf_flow alanlarından gelmeyen sayı YOK.\n"
        "- JSON LİTERAL KOPYALAMA — INPUT'taki { \"key\": value } parçalarını "
        "  asla aynen yazma. Sayıları düz Türkçe metin içinde göster. ÖRN:\n"
        "    ✗ 'Balina oranı { \"value\": \"0.56\" } ile normal seviyelerde'\n"
        "    ✓ 'Balina oranı 0,56 ile normal seviyelerde' (veya '%56' uygunsa)\n"
        "    ✗ '{ \"axiom_score\": 72.3 } puan'\n"
        "    ✓ '72,3 puan' veya 'Axiom skoru 72'\n"
        "  Türkçe yazıda ondalık ayraç virgül (0,56), binlik nokta (1.000).\n"
        "- 'Al', 'sat', 'tut', 'pozisyon aç', 'hedef fiyat', 'stop koy', "
        "  'long aç', 'short aç' YASAK. Yerine 'izleyin', 'fikrimiz değişir', "
        "  'erken uyarı veririz' kullan.\n"
        "- Emoji veya markdown başlığı kullanma. Sadece (✅), (⏳) inline "
        "  durum işaretleri paragraf içinde 1-2 kez kullanılabilir.\n"
        "- 'belki', 'olabilir' tahmin dilini SINIRLA — koşullu 'eğer-ise' tercih et.\n"
        "- 'dostlar', 'arkadaşlar' hitap YASAK. Kullanıcıyı 'siz' diye çağır.\n"
        "- Diğer sembollerin kişiliğini KARIŞTIRMA: BTC'de XRP terimi yok, "
        "  XRP'de madenci terimi yok.\n"
        "- footer her zaman: 'Bu analiz on-chain veriyi yorumlar; pozisyon "
        "  kararı sizindir. Yatırım tavsiyesi değildir.'\n"
    )


# ── Gemini call ───────────────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("GEMINI_API_KEY missing for storyteller")
        return None
    url = GEMINI_URL.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.55,
            # 4500 → 8000 → 12000: 6 Türkçe paragraf JSON-encode edilince
            # 3000+ char çıkıyor, lower limitler mid-string truncate ediyordu.
            "maxOutputTokens": 12000,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"storyteller gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"storyteller gemini call failed: {e}")
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        return None
    raw = (
        candidates[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
        .strip()
    )
    if not raw:
        return None
    fence = re.search(r"\{[\s\S]*\}", raw)
    payload = fence.group(0) if fence else raw
    try:
        return json.loads(payload)
    except Exception as e:
        logger.warning(f"storyteller json parse failed: {e}; raw={raw[:200]}")
        return None


# ── Halüsinasyon önleyici: numeric validator ──────────────────────────────

# Genel kabul gören metrik eşikleri ve sentinel değerler.
# Bunlar prompt'ta açıkça eşik olarak geçtiği için (örn 'SOPR 1.0', 'whale 0.85')
# whitelist'e baştan ekleniyor.
_THRESHOLD_SENTINELS = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "12", "15", "20", "24", "25", "30", "31", "50", "51", "60", "70", "71",
    "86", "100", "155", "365",
    "0.5", "0.7", "0.85", "0.9", "0.95", "0.97", "0.98",
    "1.0", "1.02", "1.03", "1.05", "1.5", "2.0", "3.0", "3.7", "4.0",
    "0.10", "0.13", "0.18", "0.25", "0.50", "0.75",
}

_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:[,.]\d{3})*(?:[.,]\d+)?")


def _normalize_num(s: str) -> Optional[float]:
    """'+989.000.000' → 989000000.0  ·  '$67M' → 67  ·  '%-5.2' → 5.2"""
    s = s.strip().lstrip("+").lstrip("$").rstrip("%").rstrip("M").rstrip("B").rstrip("K")
    if not s or s in {"-", "+", "."}:
        return None
    try:
        # '989.000.000' Avrupa formatı? Birden fazla nokta varsa ayraç kabul et
        if s.count(".") > 1:
            s = s.replace(".", "")
        # Karışık virgül-nokta: en sağdaki ondalık say
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            # Tek virgül → ondalık ayraç (TR formatı) ya da binlik (EN)
            # Eğer 1-3 hane varsa ondalık say
            after = s.split(",")[-1]
            if len(after) <= 2:
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")
        return float(s)
    except (ValueError, AttributeError):
        return None


def _collect_allowed_numbers(ctx: dict) -> set[float]:
    """Snapshot context'inden TÜM sayıları topla — uydurma kontrolü için
    whitelist. Eşik sentinel'leri + signals[].value içindeki tüm sayılar +
    direction_source + etf_flow + axiom_score."""
    allowed: set[float] = set()
    for s in _THRESHOLD_SENTINELS:
        v = _normalize_num(s)
        if v is not None:
            allowed.add(v)

    score = ctx.get("axiom_score")
    if score is not None:
        try:
            allowed.add(float(score))
        except (TypeError, ValueError):
            pass

    for sig in ctx.get("signals", []) or []:
        v_str = sig.get("value", "") or ""
        for m in _NUM_RE.findall(v_str):
            n = _normalize_num(m)
            if n is None:
                continue
            allowed.add(n)
            allowed.add(abs(n))
            # ölçek varyantları: M=milyon, B=milyar, K=bin → ham sayı
            if "M" in v_str.upper():
                allowed.add(n * 1_000_000)
            if "B" in v_str.upper():
                allowed.add(n * 1_000_000_000)
            if "K" in v_str.upper():
                allowed.add(n * 1_000)

    ds = ctx.get("direction_source") or {}
    for k in ("funding_rate", "spot_taker_ratio", "futures_open_interest"):
        v = ds.get(k)
        if v is not None:
            try:
                allowed.add(float(v))
                allowed.add(float(v) * 100)
            except (TypeError, ValueError):
                pass

    ef = ctx.get("etf_flow") or {}
    for k in ("net_flow_usd", "net_flow_coins", "age_hours"):
        v = ef.get(k)
        if v is not None:
            try:
                f = float(v)
                allowed.add(f)
                allowed.add(abs(f))
                # USD → milyon ölçek (LLM '$478M' yazabilir)
                if k == "net_flow_usd":
                    allowed.add(f / 1_000_000)
                    allowed.add(abs(f / 1_000_000))
            except (TypeError, ValueError):
                pass

    return allowed


def _find_hallucinated_numbers(
    text: str, allowed: set[float], tol_rel: float = 0.02
) -> list[float]:
    """text'teki sayılardan whitelist'e (±%2 tolerans) düşmeyenleri döner.
    Telemetri amaçlı; bu liste boş değilse Gemini'yi 1 kez yeniden çağırırız."""
    found: set[float] = set()
    for raw in _NUM_RE.findall(text):
        n = _normalize_num(raw)
        if n is None or n == 0:
            continue
        # Tek hane (1-9) sayılar genelde sıralama/sayma — affet
        if abs(n) < 10 and n == int(n):
            continue
        found.add(n)

    bad: list[float] = []
    for n in found:
        ok = False
        for a in allowed:
            if a == 0:
                if n == 0:
                    ok = True
                    break
                continue
            if abs(n - a) <= tol_rel * max(abs(a), abs(n)):
                ok = True
                break
        if not ok:
            bad.append(n)
    return sorted(bad)


def _validate_against_context(
    out: dict, ctx: dict, *, max_bad: int = 2
) -> tuple[Optional[dict], list[float]]:
    """Önce şema validate, sonra numeric. max_bad'dan fazla uydurma sayı
    varsa None döner (caller retry yapsın). Returns (parsed, hallucinated_list)."""
    parsed = _validate(out)
    if not parsed:
        return None, []
    full_text = " ".join([parsed["headline"], *parsed["paragraphs"], parsed["footer"]])
    allowed = _collect_allowed_numbers(ctx)
    bad = _find_hallucinated_numbers(full_text, allowed)
    if len(bad) > max_bad:
        return None, bad
    return parsed, bad


# ── Çelişki validator: aynı paragrafta zıt verdict ────────────────────────
#
# Aynı paragrafta uzlaştırma cümlesi olmadan birbirine zıt iki ifade
# bulunursa retry tetikler. Day 28 part 8: kullanıcı sayfalar arası
# tutarsızlık şikayeti — bu Gemini'nin aynı paragrafta hem bullish hem
# bearish söylemesini engeller.

# (positive_pattern, negative_pattern, reconcile_keywords)
_CONTRADICTION_PAIRS: list[tuple[str, str, tuple[str, ...]]] = [
    # Madenci: satış vs tutma
    (
        r"madenci\w*\s+(sat|çıkış|baskı|dağıtım)",
        r"madenci\w*\s+(tut|birik|akümül|güven|stabil)",
        ("trend", "anlık", "7g", "7 g", "uzlaştır", "günlük", "tek gün", "bağımsız"),
    ),
    # Stablecoin akışı: giriş vs çıkış
    (
        r"stablecoin\w*\s+(giriş|inflow|alım gücü|akış arttı|akış yüksek)",
        r"stablecoin\w*\s+(çıkış|outflow|düşük giriş|akış düşük)",
        ("zıt", "aslında", "birbirin", "rağmen", "ancak"),
    ),
    # Kurumsal: alım vs çıkış (en kritik — ETF + Coinbase Premium çakışması)
    (
        r"(kurumsal|institutional)\s*(alım|giriş|birikim|talep)",
        r"(kurumsal|institutional)\s*(satış|çıkış|kaçış)",
        ("etf", "coinbase", "spot", "günlük", "anlık", "rağmen", "ancak", "farklı", "ayrı"),
    ),
    # Boğa vs ayı aynı paragrafta uzlaştırmasız
    (
        r"\b(boğa|bull|yükseliş)",
        r"\b(ayı|bear|düşüş|kapitülasyon)",
        ("ancak", "rağmen", "fakat", "ama", "öte yandan", "yine de", "buna karşın"),
    ),
]


def _find_contradictions(paragraphs: list[str]) -> list[dict]:
    """Her paragrafta zıt verdict çiftlerini ara. Uzlaştırma kelimelerinden
    biri varsa skip — yazar zaten farkın altını çizmiş demektir.
    Returns: [{paragraph_idx, pair_idx, pos_match, neg_match}]."""
    out: list[dict] = []
    for i, p in enumerate(paragraphs):
        if not isinstance(p, str):
            continue
        low = p.lower()
        for pi, (pos_re, neg_re, recon_kws) in enumerate(_CONTRADICTION_PAIRS):
            pos = re.search(pos_re, low)
            neg = re.search(neg_re, low)
            if not (pos and neg):
                continue
            # Uzlaştırma anahtarı paragrafta var mı?
            if any(kw in low for kw in recon_kws):
                continue
            out.append({
                "paragraph_idx": i,
                "pair_idx": pi,
                "pos": pos.group(0)[:40],
                "neg": neg.group(0)[:40],
                "excerpt": p[:140],
            })
    return out


def _validate(out: dict) -> Optional[dict]:
    if not isinstance(out, dict):
        return None
    headline = (out.get("headline") or "").strip()
    paragraphs = out.get("paragraphs") or []
    footer = (out.get("footer") or "").strip()
    if not headline or not isinstance(paragraphs, list) or len(paragraphs) < 2:
        return None
    paragraphs = [p.strip() for p in paragraphs if isinstance(p, str) and p.strip()]
    # 6 blok hedef; 4'ten azsa rejekt — model en azından
    # cevap+aşılan+kalan+revizyon dörtlüsünü vermeli.
    if len(paragraphs) < 4:
        return None
    if not footer:
        footer = "Bu analiz on-chain veriyi yorumlar; pozisyon kararı sizindir. Yatırım tavsiyesi değildir."
    return {
        "headline": headline[:180],
        "paragraphs": paragraphs[:6],
        "footer": footer[:240],
    }


# ── Public API ────────────────────────────────────────────────────────────

async def get_onchain_story(symbol: str = "BTC", *, force: bool = False) -> dict:
    """Public entry — cache veya yeniden üret. 503 dönmez; envelope ile error verir."""
    sym = (symbol or "BTC").upper().strip()
    if sym not in _SUPPORTED:
        return {
            "error": "symbol_not_supported",
            "symbol": sym,
            "supported": list(_SUPPORTED),
        }
    if not _is_configured():
        return {"error": "cryptoquant_not_configured", "symbol": sym}

    if not force:
        cached = await _cache_get("story", sym, "day")
        if cached and cached.get("headline"):
            return cached

    snapshot = await get_onchain_snapshot(sym)
    if not snapshot or snapshot.get("error"):
        return {"error": "snapshot_unavailable", "symbol": sym}

    ctx = await _build_context(snapshot)
    if not ctx.get("axiom_score") and not ctx.get("signals"):
        return {"error": "no_signals", "symbol": sym}

    # 1. tur — normal prompt
    prompt = _build_prompt(ctx)
    raw_out = await _call_gemini(prompt)
    parsed, bad = _validate_against_context(raw_out or {}, ctx)
    contradictions = _find_contradictions(parsed["paragraphs"]) if parsed else []

    # 2. tur — sayı uydurma VEYA uzlaştırılmamış çelişki varsa retry
    needs_retry = (not parsed and bad) or (parsed and contradictions)
    if needs_retry:
        retry_notes = []
        if bad:
            retry_notes.append(
                f"Şu sayılar INPUT JSON'da YOK ve uydurma kabul edildi: {bad[:8]}\n"
                "Sadece INPUT'taki signals[].value, axiom_score, direction_source, "
                "etf_flow alanlarındaki sayıları kullan."
            )
        if contradictions:
            samples = "; ".join(
                f"P{c['paragraph_idx']+1}: '{c['pos']}' + '{c['neg']}' "
                f"→ '{c['excerpt']}…'"
                for c in contradictions[:3]
            )
            logger.warning(
                f"storyteller {sym}: contradictions detected: {samples}"
            )
            retry_notes.append(
                "Aynı paragrafta uzlaştırma cümlesi olmadan zıt verdict var:\n"
                f"  {samples}\n"
                "ÇELİŞKİ UZLAŞTIRMA bölümündeki kuralı uygula: zıt görünen "
                "iki ifadeyi ya AYRI paragraflara taşı ya da AÇIK uzlaştırma "
                "cümlesi ekle (zaman penceresi farkı, vehicle farkı, vs)."
            )
        if bad:
            logger.warning(
                f"storyteller {sym}: hallucinated numbers: {bad[:5]} — retrying"
            )
        retry_prompt = (
            prompt
            + "\n\n### KRİTİK DÜZELTME (önceki cevabınız reddedildi):\n"
            + "\n\n".join(retry_notes)
            + "\nYeniden üret."
        )
        raw_out2 = await _call_gemini(retry_prompt)
        parsed2, bad2 = _validate_against_context(raw_out2 or {}, ctx, max_bad=4)
        # 2. tur biraz daha gevşek (max_bad=4): kullanıcıya hiç hikaye
        # vermemektense ufak şüpheli sayılı bir hikaye iyi.
        contradictions2 = (
            _find_contradictions(parsed2["paragraphs"]) if parsed2 else []
        )
        if parsed2 and len(contradictions2) <= len(contradictions):
            parsed, bad, contradictions = parsed2, bad2, contradictions2

    if not parsed:
        # 1. tur şema-fail veya 2 tur halüsinasyon — yine de düşmemek için
        # ham çıktıdan şema-only cevap kabul et
        parsed = _validate(raw_out or {})
    if not parsed:
        return {"error": "story_generation_failed", "symbol": sym}

    payload = {
        **parsed,
        "symbol": sym,
        "axiom_score": ctx.get("axiom_score"),
        "score_zone": ctx.get("score_zone"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "_validator": {
            "hallucinated_count": len(bad),
            "samples": [str(n) for n in bad[:5]],
            "contradiction_count": len(contradictions),
            "contradiction_samples": [
                {"p_idx": c["paragraph_idx"], "pair": c["pair_idx"],
                 "pos": c["pos"], "neg": c["neg"]}
                for c in contradictions[:3]
            ],
        },
    }
    await _cache_set("story", sym, "day", payload, _CACHE_TTL)
    return payload


async def refresh_story(symbol: str) -> dict:
    return await get_onchain_story(symbol, force=True)
