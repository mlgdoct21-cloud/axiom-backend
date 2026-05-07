"""
Metric Contracts — Single Source of Truth for every CryptoQuant metric.

Day 28 part 9: kullanıcı şikayet — "ABD Kurumsal Satış" iddiası teknik
olarak yanlıştı (Coinbase Premium SADECE anlık spot fiyat farkı, kim
alıp sattığını söyleyemez); ayrıca stablecoin "alım gücü" label'ı INFLOW
endpoint'inden geliyordu, NETFLOW olmalıydı (gross inflow yüksek olsa da
net çıkış olabilir). Bu dosya tüm metrikler için:

  - source endpoint (CI'da fetcher'lar bu URL'i kullanıyor mu kontrol)
  - measures (ne ölçüyor; technical doğru tanım)
  - window (anlık vs trend; story'de zaman penceresi etiketi şart)
  - vehicle (spot/türev/ETF; "kurumsal" iddia hangi vehicle'lara izinli)
  - CAN_claim / CANNOT_claim (label_tr'da izin verilen/yasak kelime hazinesi)
  - expected_fields (response shape — fetch sonrası schema validation)
  - value_range (sanity bounds; outlier'ları yakala)
  - display_tr (UI'da görünen metrik adı; misleading çeviri yasağı)
  - reconcile_with (yan yana çıkarsa açıklama şart olan metrikler)

İlgili katmanlar bu kontratı okur:
  - Layer 0A: tests/test_endpoint_contracts.py — fetcher source eşleşmesi
  - Layer 0D: services/cryptoquant_service._fetch_cached → schema validate
  - Layer 0C: services/data_anomaly_detector — value_range bound check
  - Layer 1:  services/cryptoquant_service._interpret_signals → label_tr
              CAN/CANNOT_claim lexicon zorunluluğu
  - Layer 2:  services/onchain_storyteller — auditor agent context
  - Layer 3:  tests/test_cross_page_consistency.py — snapshot doğrulama

KESIN KURAL: Yeni metrik eklenirken bu dosya GÜNCELLEN­MEDEN fetcher
PR'ı CI'dan geçmez (Layer 0A enforcement).
"""
from __future__ import annotations

from typing import Optional, TypedDict


class MetricContract(TypedDict, total=False):
    source: str                  # CryptoQuant endpoint path
    measures: str                # Technical definition (English)
    window: str                  # "anlık" | "günlük" | "7G trend" | "trend"
    vehicle: str                 # "spot" | "türev" | "ETF" | "ağ" | "borsa"
    display_tr: str              # UI'da görünen ad — misleading çeviri yasağı
    CAN_claim: list[str]         # label_tr'da izin verilen kelimeler
    CANNOT_claim: list[str]      # YASAK kelimeler — overclaim önlemek için
    expected_fields: list[str]   # Response'da bulunması gereken field'lar
    value_range: tuple[float, float]  # Sanity bounds (anomaly threshold)
    reconcile_with: list[str]    # Yan yana çıkarsa açıklama şart
    notes: str                   # Geliştirici notu


# ─────────────────────────────────────────────────────────────────────────
# BTC METRICS (20)
# ─────────────────────────────────────────────────────────────────────────

CONTRACTS: dict[str, MetricContract] = {

    "exchange_netflow": {
        "source": "/btc/exchange-flows/netflow",
        "measures": "Net BTC flow into/out of all exchanges (in - out)",
        "window": "anlık (son gün)",
        "vehicle": "borsa (spot)",
        "display_tr": "Borsa Net Akışı",
        "CAN_claim": ["borsa akışı", "giriş", "çıkış", "birikim", "satış baskısı",
                      "dağıtım", "borsalara", "borsalardan", "net akış"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci", "balina özel", "spot premium"],
        "expected_fields": ["netflow_total", "inflow_total", "outflow_total"],
        "value_range": (-100_000, 100_000),  # BTC; ±100k günlük hayatta görmedim
        "reconcile_with": ["whale_ratio", "miner_outflow"],
        "notes": "GROSS inflow/outflow değil net — yön ölçer.",
    },

    "whale_ratio": {
        "source": "/btc/flow-indicator/exchange-whale-ratio",
        "measures": "Top 10 inflows / total inflows ratio (whale dominance proxy)",
        "window": "anlık (son gün)",
        "vehicle": "borsa (spot)",
        "display_tr": "Balina Oranı",
        "CAN_claim": ["balina", "büyük transfer", "konsantre akış", "dağılım",
                      "perakende", "retail"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci", "spesifik cüzdan"],
        "expected_fields": ["whale_ratio"],
        "value_range": (0.0, 1.0),
        "notes": "Yüksek = az cüzdan büyük transferler yapıyor; spesifik kim "
                 "olduğunu söyleyemeyiz.",
    },

    "miner_outflow": {
        "source": "/btc/miner-flows/outflow",
        "measures": "Total BTC volume sent FROM miner wallets (per day)",
        "window": "anlık (gün) + 7G ortalamaya göre sapma",
        "vehicle": "madenci cüzdanları",
        "display_tr": "Madenci Çıkışı",
        "CAN_claim": ["madenci transfer", "madenci satış baskısı", "madenci akışı",
                      "madenci tutum", "outflow", "çıkış"],
        "CANNOT_claim": ["kurumsal", "etf", "stablecoin", "balina"],
        "expected_fields": ["outflow_total"],
        "value_range": (0, 1_000_000),  # BTC; tarihsel max ~50k/gün
        "reconcile_with": ["miner_reserve"],
        "notes": "Anlık spike — 7G ortalamaya göre yorumla. miner_reserve TREND.",
    },

    "miner_reserve": {
        "source": "/btc/miner-flows/reserve",
        "measures": "Total BTC held in miner wallets (7-day change %)",
        "window": "trend (7G değişim)",
        "vehicle": "madenci cüzdanları",
        "display_tr": "Madenci Rezerv",
        "CAN_claim": ["madenci stoğu", "rezerv", "trend", "uzun vade", "birikim",
                      "tasfiye", "stabil"],
        "CANNOT_claim": ["kurumsal", "etf", "anlık satış", "günlük spike"],
        "expected_fields": ["reserve", "change_7d_pct"],
        "value_range": (-50.0, 50.0),  # %; haftalık ±50% mantıklı sınır
        "reconcile_with": ["miner_outflow"],
        "notes": "TREND metriği — günlük outflow ile ZIT görünebilir, ZAMAN "
                 "PENCERESI farkı açıklanmalı.",
    },

    "stablecoin_inflow": {
        # ⚠️ Day 28 part 9: önce /inflow kullanılıyordu (gross), netflow'a geçildi.
        # Net flow gerçek "alım gücü" göstergesi; gross inflow yanıltıcı çünkü
        # outflow'u görmezden geliyor ($3.5B inflow + $3.7B outflow = -$200M net).
        "source": "/stablecoin/exchange-flows/netflow",
        "measures": "Net stablecoin flow (USDT+USDC+all) into exchanges",
        "window": "anlık (son gün, NET)",
        "vehicle": "stablecoin (USDT+USDC+DAI+BUSD karışık)",
        "display_tr": "Stablecoin Net Akışı",
        "CAN_claim": ["stablecoin", "alım gücü", "satış gücü", "tetikte para",
                      "borsalara giriş", "borsalardan çıkış", "net akış"],
        "CANNOT_claim": ["sadece USDT", "kurumsal", "etf", "madenci"],
        "expected_fields": ["netflow_total"],
        "value_range": (-10_000_000_000, 10_000_000_000),  # USD; ±10B/gün outlier sınırı
        "notes": "NETFLOW (in - out). Gross inflow $3B olsa bile net negatifse "
                 "alım gücü YOK. USDT+USDC karışık — 'USDT' demeyin.",
    },

    "funding_rates": {
        "source": "/btc/market-data/funding-rates",
        "measures": "Perpetual futures funding rate (Binance, daily avg)",
        "window": "anlık (son gün)",
        "vehicle": "türev (perpetual futures)",
        "display_tr": "Funding Rate",
        "CAN_claim": ["fonlama", "kaldıraçlı", "long pozisyon", "short pozisyon",
                      "türev tarafı", "squeeze riski", "perp"],
        "CANNOT_claim": ["spot", "kurumsal", "etf", "madenci"],
        "expected_fields": ["funding_rates"],
        "value_range": (-0.1, 0.1),  # ±10% günlük outlier
        "notes": "TÜREV metriği — spot dinamiği için spot_taker'a bak.",
    },

    "open_interest": {
        "source": "/btc/market-data/open-interest",
        "measures": "Aggregate open futures contracts (USD value)",
        "window": "anlık (son gün)",
        "vehicle": "türev (futures)",
        "display_tr": "Açık Pozisyon",
        "CAN_claim": ["açık pozisyon", "kaldıraç hacmi", "türev hacmi", "OI",
                      "pozisyonlanma"],
        "CANNOT_claim": ["spot", "kurumsal akış", "etf", "madenci"],
        "expected_fields": ["open_interest"],
        "value_range": (0, 200_000_000_000),  # USD; ±200B üstü outlier
        "notes": "Türev tarafı boyut — yön değil hacim ölçer.",
    },

    "sopr": {
        "source": "/btc/market-indicator/sopr",
        "measures": "Spent Output Profit Ratio — coins moved at profit/loss",
        "window": "günlük (zincir-üstü tüketim)",
        "vehicle": "ağ (network-wide)",
        "display_tr": "SOPR",
        "CAN_claim": ["kâr", "zarar", "satılan paralar", "tüketim", "başabaş",
                      "kapitülasyon", "kâr realizasyonu"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci", "spesifik vehicle"],
        "expected_fields": ["sopr"],
        "value_range": (0.5, 2.0),
        "notes": ">1 kâr realizasyonu; <1 zararına satış (panik).",
    },

    "coinbase_premium": {
        # ⚠️ Day 28 part 9: "ABD Kurumsal" iddiası KALDIRILDI — Coinbase Premium
        # SADECE iki borsanın spot fiyat farkıdır. Kurumsal mı retail mi
        # arbitrajcı mı söyleyemeyiz. Gerçek kurumsal göstergesi: ETF flow.
        "source": "/btc/market-data/coinbase-premium-index",
        "measures": "Coinbase BTC price - Binance BTC price (spot differential)",
        "window": "anlık (spot fiyat farkı)",
        "vehicle": "spot (Coinbase tezgahı)",
        "display_tr": "Coinbase Spot Premium",
        "CAN_claim": ["coinbase tezgah", "abd spot", "spot fiyat farkı", "anlık",
                      "premium", "iskonto", "abd borsası"],
        "CANNOT_claim": ["kurumsal", "institutional", "etf", "akış", "flow",
                         "alım gücü", "kurumsal alım", "kurumsal satış"],
        "expected_fields": ["coinbase_premium_gap", "coinbase_premium_index"],
        "value_range": (-1000, 1000),  # USD spread
        "reconcile_with": ["etf_flow"],
        "notes": "TEKNIK olarak sadece spread. 'Kurumsal' iddia ETF flow'a "
                 "bırakın — orası gerçek doğrulanmış institutional kanal.",
    },

    "mvrv": {
        "source": "/btc/market-indicator/mvrv",
        "measures": "Market value / realized value — overheating indicator",
        "window": "trend (network değerleme)",
        "vehicle": "ağ (network-wide)",
        "display_tr": "MVRV",
        "CAN_claim": ["değerleme", "tepe bölgesi", "dip bölgesi", "adil değer",
                      "aşırı değerli", "ucuz"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci spesifik"],
        "expected_fields": ["mvrv"],
        "value_range": (0.3, 10.0),
        "notes": ">3.7 tarihsel tepe; <1 dip.",
    },

    "nupl": {
        "source": "/btc/network-indicator/nupl",
        "measures": "Net Unrealized Profit/Loss — investor sentiment phase",
        "window": "trend (network kâr/zarar)",
        "vehicle": "ağ (network-wide)",
        "display_tr": "NUPL",
        "CAN_claim": ["yatırımcı duygusu", "coşku", "iyimserlik", "korku",
                      "umut", "kapitülasyon", "inanç"],
        "CANNOT_claim": ["kurumsal", "etf", "akış"],
        "expected_fields": ["nupl"],
        "value_range": (-0.5, 1.0),
        "notes": ">0.75 euforya; <0 kapitülasyon.",
    },

    "mpi": {
        # ⚠️ Day 28 part 9: "satış baskısı endeksi" yanlış çeviri kaldırıldı.
        # MPI raw değer YÖN sinyali değil, eşik metriği. +0.24 = güven, +2 üstü = uyarı.
        "source": "/btc/flow-indicator/mpi",
        "measures": "Miner Position Index — miner sell-pressure indicator",
        "window": "anlık (madenci pozisyon eşiği)",
        "vehicle": "madenci cüzdanları",
        "display_tr": "Madenci Pozisyon Endeksi (MPI)",  # NOT "satış baskısı endeksi"
        "CAN_claim": ["madenci pozisyon", "eşik", "satış uyarısı", "güven",
                      "birikim", "kritik bölge"],
        "CANNOT_claim": ["yön sinyali", "satış baskısı endeksi"],
        "expected_fields": ["mpi"],
        "value_range": (-5.0, 10.0),
        "notes": "Eşik metriği — '+0.5 üstü uyarı, +2 üstü kritik'. ÇEVIRI "
                 "olarak 'Madenci Pozisyon Endeksi' KORUNMALI; 'satış baskısı "
                 "endeksi' YANLIŞ (yön sinyali değil eşik metriği).",
    },

    "puell": {
        "source": "/btc/network-indicator/puell-multiple",
        "measures": "Miner revenue / 365-day MA — miner profitability cycle",
        "window": "trend (madenci kâr katsayısı)",
        "vehicle": "madenci ekonomisi",
        "display_tr": "Puell Multiple",
        "CAN_claim": ["madenci kârı", "kapitülasyon", "tepe bölgesi", "düşük kâr",
                      "döngü"],
        "CANNOT_claim": ["kurumsal", "etf", "spot premium"],
        "expected_fields": ["puell_multiple", "puell"],
        "value_range": (0.1, 10.0),
        "notes": ">4 tepe; <0.5 dip (kapitülasyon).",
    },

    "leverage_ratio": {
        "source": "/btc/market-indicator/estimated-leverage-ratio",
        "measures": "Estimated leverage in derivatives market",
        "window": "anlık (türev kaldıraç)",
        "vehicle": "türev",
        "display_tr": "Kaldıraç Oranı",
        "CAN_claim": ["kaldıraç", "tasfiye riski", "türev", "long/short büyüklük"],
        "CANNOT_claim": ["spot", "kurumsal", "etf"],
        "expected_fields": ["estimated_leverage_ratio", "elr"],
        "value_range": (0.0, 1.0),
        "notes": ">0.36 likidasyon flush riski; <0.20 düşük risk.",
    },

    "realized_price": {
        "source": "/btc/market-indicator/realized-price",
        "measures": "Average price coins last moved at (network cost basis)",
        "window": "trend (network maliyet ortalaması)",
        "vehicle": "ağ",
        "display_tr": "Gerçekleşmiş Fiyat",
        "CAN_claim": ["maliyet ortalaması", "gerçekleşmiş fiyat", "destek bölgesi",
                      "ağ ortalama"],
        "CANNOT_claim": ["spot fiyat", "kurumsal", "etf"],
        "expected_fields": ["realized_price"],
        "value_range": (1_000, 1_000_000),  # USD
        "notes": "Pasif gösterge; yön sinyali değil bağlam.",
    },

    "hash_rate": {
        "source": "/btc/network-data/hashrate",
        "measures": "Network hashrate (mining security indicator)",
        "window": "trend (7G değişim)",
        "vehicle": "ağ (madencilik)",
        "display_tr": "Hash Rate",
        "CAN_claim": ["hash gücü", "ağ güvenliği", "madenci kapasitesi", "trend"],
        "CANNOT_claim": ["spot", "kurumsal", "etf", "fiyat"],
        "expected_fields": ["hashrate", "hash_rate"],
        "value_range": (0, 10_000_000_000_000_000),  # raw network hash count
        "notes": "Pasif uzun-vade güvenlik göstergesi.",
    },

    "spot_taker": {
        "source": "/btc/market-data/taker-buy-sell-stats",
        "measures": "Spot taker buy / sell volume ratio (aggressor side)",
        "window": "anlık (son gün)",
        "vehicle": "spot",
        "display_tr": "Spot Alıcı/Satıcı",
        "CAN_claim": ["spot alıcı", "spot satıcı", "agresörlük", "spot tarafı",
                      "yön kaynağı"],
        "CANNOT_claim": ["türev", "funding", "kurumsal spesifik", "etf"],
        "expected_fields": ["taker_buy_volume", "taker_sell_volume"],
        "value_range": (0.0, 5.0),
        "notes": ">1.05 spot alıcı baskın; <0.95 satıcı baskın.",
    },

    "sopr_ratio": {
        "source": "/btc/market-indicator/sopr-ratio",
        "measures": "LTH-SOPR / STH-SOPR ratio (cohort balance)",
        "window": "trend (LTH/STH dengesi)",
        "vehicle": "ağ (kohort)",
        "display_tr": "Kohort Dengesi (LTH/STH)",
        "CAN_claim": ["uzun vadeci", "kısa vadeci", "kohort", "dağıtım", "birikim",
                      "taze para"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci"],
        "expected_fields": ["sopr_ratio", "ratio"],
        "value_range": (0.5, 3.0),
        "notes": ">1.15 LTH dağıtımı (tepe uyarı); <1 STH baskın.",
    },

    "btc_liquidations": {
        "source": "/btc/market-data/liquidations",
        "measures": "Forced liquidation volume (long + short, USD)",
        "window": "anlık (son gün)",
        "vehicle": "türev",
        "display_tr": "BTC Tasfiyeler",
        "CAN_claim": ["tasfiye", "long tasfiyesi", "short sıkışması", "forced sell",
                      "türev temizliği"],
        "CANNOT_claim": ["spot", "kurumsal", "etf", "madenci"],
        "expected_fields": ["long_liquidations_usd", "short_liquidations_usd"],
        "value_range": (0, 10_000_000_000),  # USD
        "notes": "Asimetri yön sinyali — long > short × 2 = forced sell bitti.",
    },

    "korean_premium": {
        "source": "/btc/market-data/korea-premium-index",
        "measures": "Upbit BTC price vs Binance (Korean retail proxy)",
        "window": "anlık (spot fiyat farkı)",
        "vehicle": "spot (Upbit tezgahı)",
        "display_tr": "Kore Primi (Upbit)",
        "CAN_claim": ["upbit", "korean retail", "asya retail", "spot fiyat farkı",
                      "fomo", "kapitülasyon"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci", "global spot"],
        "expected_fields": ["korea_premium_index", "korea_premium"],
        "value_range": (-20.0, 20.0),  # %
        "notes": ">3% Korean FOMO (local top); <-2% kapitülasyon.",
    },

    # ─────────────────────────────────────────────────────────────────
    # ETH METRICS (7) — PoS olduğu için BTC'nin miner/MVRV/SOPR'u yok
    # ─────────────────────────────────────────────────────────────────

    "eth_supply_ratio": {
        "source": "/eth/flow-indicator/exchange-supply-ratio",
        "measures": "ETH on exchanges / total supply ratio",
        "window": "anlık (son gün)",
        "vehicle": "borsa (spot)",
        "display_tr": "Borsa Arz Oranı",
        "CAN_claim": ["borsa arzı", "stok dağılımı", "borsa stoku"],
        "CANNOT_claim": ["kurumsal", "etf", "madenci"],
        "expected_fields": ["exchange_supply_ratio", "supply_ratio"],
        "value_range": (0.0, 1.0),
        "notes": "<0.10 düşük (bullish), >0.18 yüksek (bearish).",
    },

    "eth_active_addresses": {
        "source": "/eth/network-data/addresses-count",
        "measures": "Daily active ETH wallet addresses (7G change %)",
        "window": "trend (7G değişim)",
        "vehicle": "ağ",
        "display_tr": "ETH Aktif Cüzdanlar",
        "CAN_claim": ["ağ kullanımı", "aktif cüzdan", "trend", "kullanıcı aktivitesi"],
        "CANNOT_claim": ["spot", "kurumsal", "etf"],
        "expected_fields": ["addresses_count"],
        "value_range": (0, 10_000_000),
    },

    # ─────────────────────────────────────────────────────────────────
    # XRP METRICS (8) — derivatives ağırlıklı
    # ─────────────────────────────────────────────────────────────────

    "xrp_liquidations": {
        "source": "/xrp/market-data/liquidations",
        "measures": "XRP forced liquidation volume",
        "window": "anlık (son gün)",
        "vehicle": "türev",
        "display_tr": "XRP Tasfiyeler",
        "CAN_claim": ["tasfiye", "türev temizliği"],
        "CANNOT_claim": ["spot", "kurumsal", "madenci"],
        "expected_fields": ["long_liquidations_usd", "short_liquidations_usd"],
        "value_range": (0, 1_000_000_000),
    },

    "xrp_taker_buy_sell": {
        "source": "/xrp/market-data/taker-buy-sell-stats",
        "measures": "XRP taker buy/sell ratio",
        "window": "anlık (son gün)",
        "vehicle": "spot",
        "display_tr": "XRP Spot Alıcı/Satıcı",
        "CAN_claim": ["spot alıcı", "agresörlük"],
        "CANNOT_claim": ["türev", "kurumsal"],
        "expected_fields": ["taker_buy_ratio"],
        "value_range": (0.0, 1.0),
    },

    "xrp_supply_ratio": {
        "source": "/xrp/flow-indicator/exchange-supply-ratio",
        "measures": "XRP on exchanges / total supply",
        "window": "anlık (son gün)",
        "vehicle": "borsa",
        "display_tr": "XRP Borsa Arzı",
        "CAN_claim": ["borsa stoku", "supply oranı"],
        "CANNOT_claim": ["kurumsal", "etf"],
        "expected_fields": ["exchange_supply_ratio", "supply_ratio"],
        "value_range": (0.0, 1.0),
    },

    "xrp_nvt": {
        "source": "/xrp/network-indicator/nvt",
        "measures": "Network value to transactions ratio",
        "window": "trend",
        "vehicle": "ağ",
        "display_tr": "XRP NVT",
        "CAN_claim": ["değerleme", "ağ kullanımı"],
        "CANNOT_claim": ["spot", "kurumsal"],
        "expected_fields": ["nvt"],
        "value_range": (1.0, 1000.0),
    },

    "xrp_tx_count": {
        "source": "/xrp/network-data/transactions-count",
        "measures": "Daily XRP transaction count (7G change)",
        "window": "trend",
        "vehicle": "ağ",
        "display_tr": "XRP İşlem Sayısı",
        "CAN_claim": ["ağ aktivitesi", "tx hacmi"],
        "CANNOT_claim": ["spot", "kurumsal"],
        "expected_fields": ["transactions_count"],
        "value_range": (0, 100_000_000),
    },

    # ─────────────────────────────────────────────────────────────────
    # NON-CryptoQuant METRICS (Layer 2 auditor için bilgi)
    # ─────────────────────────────────────────────────────────────────

    "etf_flow": {
        "source": "(coinglass scraper, internal)",
        "measures": "Daily net spot ETF inflow/outflow (BlackRock, Fidelity, etc)",
        "window": "günlük (T-1, raporlu)",
        "vehicle": "spot ETF (regulated kurumsal kanal)",
        "display_tr": "Spot ETF Net Akışı",
        "CAN_claim": ["kurumsal", "institutional", "etf", "spot etf",
                      "günlük kurumsal birikim", "kurumsal çıkış", "blackrock", "ibit"],
        "CANNOT_claim": ["coinbase premium", "anlık spot", "spot tezgah"],
        "expected_fields": ["net_flow_usd", "net_flow_coins"],
        "value_range": (-2_000_000_000, 2_000_000_000),  # ±2B/gün
        "reconcile_with": ["coinbase_premium"],
        "notes": "GERÇEK doğrulanmış kurumsal göstergesi. 'Kurumsal' iddiası "
                 "için TEK izinli kaynak.",
    },
}


def get_contract(metric_key: str) -> Optional[MetricContract]:
    return CONTRACTS.get(metric_key)


def all_contracts() -> dict[str, MetricContract]:
    return dict(CONTRACTS)


def is_label_compliant(metric_key: str, label_tr: str) -> tuple[bool, list[str]]:
    """Label'ın CANNOT_claim listesindeki yasak kelime içerip içermediğini
    kontrol et. Returns (compliant, list_of_violations).

    Layer 1 zorunluluk: _interpret_signals her label_tr üretimini bu fonksiyonla
    geçirir; ihlal varsa exception fırlatır (compile-time enforcement)."""
    contract = CONTRACTS.get(metric_key)
    if not contract:
        return True, []  # bilinmeyen metrik için kısıt yok
    forbidden = contract.get("CANNOT_claim", [])
    low = label_tr.lower()
    violations = [w for w in forbidden if w.lower() in low]
    return len(violations) == 0, violations


def is_value_in_range(metric_key: str, value: float) -> tuple[bool, str]:
    """Layer 0C anomaly check — value contract'taki value_range içinde mi?"""
    contract = CONTRACTS.get(metric_key)
    if not contract or "value_range" not in contract:
        return True, ""
    lo, hi = contract["value_range"]
    if lo <= value <= hi:
        return True, ""
    return False, f"out_of_range[{lo}, {hi}]: {value}"


def has_required_fields(metric_key: str, response_dict: dict) -> tuple[bool, list[str]]:
    """Layer 0D schema check — response'ta beklenen field'lar var mı?
    En az 1 expected_field varsa pass (CryptoQuant alternative key isimleri var)."""
    contract = CONTRACTS.get(metric_key)
    if not contract:
        return True, []
    expected = contract.get("expected_fields", [])
    if not expected:
        return True, []
    found = [f for f in expected if f in response_dict]
    if found:
        return True, []
    return False, expected
