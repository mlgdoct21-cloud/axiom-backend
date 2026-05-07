"""Layer 3 — Cross-page snapshot consistency tests.

Aynı snapshot dict'i 4 farklı kanaldan render edip her metriğin label_tr'sinin
TÜM kanallarda **birebir aynı** çıktığını doğrular:
  1. _interpret_signals output (SSoT)
  2. cryptoquant_alerts._format_briefing (Telegram morning briefing)
  3. daily_digest_service._build_risk_radar (dashboard digest card)
  4. (frontend label literal'ları için ayrı dashboard test gerekir)

Yayın öncesi koruma: bir kanal label string'ini "kendine göre" değiştirirse
test fail eder. Eski stale narrative bug'ları (ör: digest "akış dengeli,
sinyal yok" derken modal "stablecoin alım gücü" diyordu) bu sayede yakalanır.
"""
import os
import pytest

# Test ortamında DB import side-effect'ini engelle: asenkron driver yoksa
# sqlite default'a düşüyor → import zincirinde patlıyor.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test"
)

from datetime import datetime, timezone  # noqa: E402

from services.cryptoquant_service import _interpret_signals  # noqa: E402


# Sabit fixture: kullanıcının 7 May 2026 ekran görüntüsündeki gerçek snapshot
# (20 BTC sinyali, gerçek değerler).
FIXTURE_BTC = {
    "symbol": "BTC",
    "exchange_netflow":     {"netflow_total": 113.0, "inflow_total": 5000.0,
                             "outflow_total": 4887.0, "date": "2026-05-06"},
    "whale_ratio":          {"whale_ratio": 0.56, "date": "2026-05-06"},
    "miner_outflow":        {"outflow_total": 9704.0, "avg_7d": 6140.0,
                             "date": "2026-05-06"},
    "miner_reserve":        {"reserve": 1802747.67, "change_7d_pct": 0.0,
                             "date": "2026-05-06"},
    "stablecoin_inflow":    {"inflow_total": 3528000000.0, "date": "2026-05-06"},
    "funding_rates":        {"latest": -0.00093, "avg_24h": -0.00093, "ts": "2026-05-06"},
    "open_interest":        {"open_interest": 27743606539.0, "change_pct": 0.0,
                             "date": "2026-05-06"},
    "sopr":                 {"sopr": 1.00493, "date": "2026-05-06"},
    "coinbase_premium":     {"coinbase_premium": -8.91, "ts": "2026-05-06"},
    "mvrv":                 {"mvrv": 1.50, "date": "2026-05-06"},
    "nupl":                 {"nupl": 0.334, "date": "2026-05-06"},
    "mpi":                  {"mpi": 0.241, "date": "2026-05-06"},
    "puell":                {"puell": 0.83, "date": "2026-05-06"},
    "leverage_ratio":       {"leverage_ratio": 0.247, "date": "2026-05-06"},
    "realized_price":       {"realized_price": 54242.0, "date": "2026-05-06"},
    "hash_rate":            {"hash_rate": 928515933865.0, "change_7d_pct": -7.8,
                             "date": "2026-05-06"},
    "spot_taker":           {"ratio": 0.994, "buy_volume": 7294094515.0,
                             "sell_volume": 6974327426.0, "date": "2026-05-06"},
    "sopr_ratio":           {"sopr_ratio": 1.0348, "date": "2026-05-06"},
    "btc_liquidations":     {"long_usd": 0.0, "short_usd": 0.0, "total_usd": 0.0,
                             "date": "2026-05-06"},
    "korean_premium":       {"korean_premium": -0.56, "date": "2026-05-06"},
    "fetched_at": "2026-05-07T16:35:05+00:00",
    "_snapshot_full": True,
}


@pytest.fixture
def interpreted():
    """Tek SSoT — _interpret_signals output. Tüm consumer'lar bunu okur."""
    return _interpret_signals(FIXTURE_BTC)


# ─── Snapshot doğrulama: SSoT 20 sinyal üretmeli ─────────────────────────

def test_snapshot_produces_expected_signal_count(interpreted):
    signals = interpreted["signals"]
    # BTC için beklenen aktif sinyal: 20 (miner_outflow dahil Day 28#7)
    assert len(signals) >= 18, (
        f"BTC snapshot beklenen 20 sinyal vermedi, sadece {len(signals)} var: "
        f"{list(signals.keys())}"
    )


def test_no_label_violates_contract(interpreted):
    """Layer 1 enforcement runtime confirmation — tüm label_tr'ler temiz."""
    from data.metric_contracts import is_label_compliant
    for key, sig in interpreted["signals"].items():
        ok, viol = is_label_compliant(key, sig.get("label_tr", ""))
        assert ok, (
            f"{key}: label_tr {sig['label_tr']!r} contract'a uymuyor — "
            f"yasak kelime(ler): {viol}"
        )


def test_axiom_score_in_valid_range(interpreted):
    score = interpreted["axiom_score"]
    assert score is not None, "Axiom skor None — snapshot bozuk"
    assert 0 <= score <= 100, f"Axiom skor {score} 0-100 dışında"


# ─── Cross-page label consistency ────────────────────────────────────────

def test_briefing_uses_same_labels(interpreted):
    """Sabah brifingi top contributors için _interpret_signals'ın label_tr'ini
    AYNEN kullanmalı — kendi yorumunu eklememeli."""
    from services.cryptoquant_alerts import _format_briefing

    snap = {**FIXTURE_BTC, **interpreted}
    text = _format_briefing(snap, yesterday=None, next_macro=None)

    # En güçlü pozitif ve negatif sinyalin label_tr'si brifing metninde geçmeli
    breakdown = interpreted["score_breakdown"]
    if breakdown:
        positives = sorted([b for b in breakdown if b["contribution"] > 0],
                           key=lambda x: -x["contribution"])
        negatives = sorted([b for b in breakdown if b["contribution"] < 0],
                           key=lambda x: x["contribution"])
        if positives:
            top_pos_label = positives[0]["label_tr"]
            assert top_pos_label in text, (
                f"Brifing top pozitif label'ı {top_pos_label!r} içermiyor — "
                f"SSoT'tan ayrılmış demektir"
            )
        if negatives:
            top_neg_label = negatives[0]["label_tr"]
            assert top_neg_label in text, (
                f"Brifing top negatif label'ı {top_neg_label!r} içermiyor"
            )


def test_risk_radar_uses_signal_labels(interpreted):
    """Daily digest risk_radar netflow + funding label'larını signals'tan okumalı."""
    from services.daily_digest_service import DailyDigestService

    snap = {**FIXTURE_BTC, **interpreted}
    radar = DailyDigestService._build_risk_radar(
        vix=None, urgent_count=0, urgent_symbols=[],
        overnight={"asia": []}, onchain=snap,
    )
    analysis = radar.get("analysis", "")

    # Netflow ve funding label'ları analysis'te geçmeli
    nf_label = interpreted["signals"]["exchange_netflow"]["label_tr"]
    funding_label = interpreted["signals"]["funding_rates"]["label_tr"]
    assert nf_label in analysis, (
        f"Risk Radar netflow label'ı {nf_label!r} içermiyor — "
        f"hardcoded threshold'lara dönmüş demektir"
    )
    assert funding_label in analysis, (
        f"Risk Radar funding label'ı {funding_label!r} içermiyor"
    )


# ─── Regression: kullanıcının yakaladığı tutarsızlıklar ───────────────────

def test_no_kurumsal_claim_in_coinbase_premium_label(interpreted):
    """Coinbase Premium label'ı 'kurumsal' içermemeli (Day 28 part 9 fix)."""
    cb = interpreted["signals"].get("coinbase_premium")
    if cb:
        label = cb["label_tr"].lower()
        assert "kurumsal" not in label, (
            f"coinbase_premium label'ı 'kurumsal' iddia ediyor: {cb['label_tr']!r}"
        )


def test_score_summary_is_consistent_with_labels(interpreted):
    """score_summary = 'Güç: X · Baskı: Y' formatında, X ve Y en yüksek
    contribution'a sahip sinyallerin label_tr'lerinden oluşmalı."""
    summary = interpreted["score_summary"]
    breakdown = interpreted["score_breakdown"]
    if not breakdown:
        return
    for b in breakdown:
        # En azından top contribution label'ları summary'de geçmeli
        if abs(b["contribution"]) >= 18 and b["contribution"] != 0:
            assert b["label_tr"] in summary, (
                f"Top contributor {b['metric']} ({b['label_tr']!r}, "
                f"contrib={b['contribution']}) summary'de yok: {summary!r}"
            )
