"""Layer 0A — Endpoint URL Audit + Layer 1 — Lexicon Enforcement tests.

Bu testler `services/cryptoquant_service.py`'deki her `_fetch_*` fonksiyonun
`_cq_get("PATH", ...)` çağrısının `data/metric_contracts.py`'deki
`source` ile EŞLEŞTİĞİNİ doğrular.

Eski bug: stablecoin fetcher /inflow endpoint'i kullanıyordu, contract
ise /netflow göstermeli. Bu test PR'da fail edip kabul etmemeli.
"""
import ast
import re
from pathlib import Path

import pytest

from data.metric_contracts import CONTRACTS, is_label_compliant

SERVICE_FILE = Path(__file__).parent.parent / "services" / "cryptoquant_service.py"


# Map: contract metric_key → fetcher function name
# (bazı metric'ler birden fazla fetcher'la bağlanmıyor; explicit eşleme)
METRIC_TO_FETCHER: dict[str, str] = {
    "exchange_netflow":     "_fetch_exchange_netflow",
    "whale_ratio":          "_fetch_whale_ratio",
    "miner_outflow":        "_fetch_miner_outflow",
    "miner_reserve":        "_fetch_miner_reserve",
    "stablecoin_inflow":    "_fetch_stablecoin_inflow",
    "funding_rates":        "_fetch_funding_rates",
    "open_interest":        "_fetch_open_interest",
    "sopr":                 "_fetch_sopr",
    "coinbase_premium":     "_fetch_coinbase_premium",
    "mvrv":                 "_fetch_mvrv",
    "nupl":                 "_fetch_nupl",
    "mpi":                  "_fetch_mpi",
    "puell":                "_fetch_puell_multiple",
    "leverage_ratio":       "_fetch_leverage_ratio",
    "realized_price":       "_fetch_realized_price",
    "hash_rate":            "_fetch_hash_rate",
    "spot_taker":           "_fetch_spot_taker_ratio",
    "sopr_ratio":           "_fetch_sopr_ratio",
    "btc_liquidations":     "_fetch_btc_liquidations",
    "korean_premium":       "_fetch_korean_premium",
    "eth_supply_ratio":     "_fetch_eth_exchange_supply_ratio",
    "eth_active_addresses": "_fetch_eth_active_addresses",
    "xrp_liquidations":     "_fetch_xrp_liquidations",
    "xrp_taker_buy_sell":   "_fetch_xrp_taker_buy_sell",
    "xrp_supply_ratio":     "_fetch_xrp_supply_ratio",
    "xrp_nvt":              "_fetch_xrp_nvt",
    "xrp_tx_count":         "_fetch_xrp_tx_count",
}


def _extract_cq_get_paths(source: str) -> dict[str, list[str]]:
    """AST scan: her async def _fetch_* içindeki _cq_get("PATH", ...)
    çağrısının ilk argümanı string literal olarak çek."""
    tree = ast.parse(source)
    paths: dict[str, list[str]] = {}

    class FetcherVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.current_fn: str | None = None

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node.name.startswith("_fetch_"):
                self.current_fn = node.name
                paths[node.name] = []
                self.generic_visit(node)
                self.current_fn = None
            else:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                self.current_fn
                and isinstance(node.func, ast.Name)
                and node.func.id == "_cq_get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                paths[self.current_fn].append(node.args[0].value)
            self.generic_visit(node)

    FetcherVisitor().visit(tree)
    return paths


@pytest.fixture(scope="module")
def fetcher_paths() -> dict[str, list[str]]:
    return _extract_cq_get_paths(SERVICE_FILE.read_text())


# ─── Layer 0A: endpoint URL contract enforcement ─────────────────────────

@pytest.mark.parametrize(
    "metric_key,fetcher_name", list(METRIC_TO_FETCHER.items()),
)
def test_fetcher_uses_contract_endpoint(metric_key, fetcher_name, fetcher_paths):
    """Her fetcher'ın çağırdığı _cq_get path'i contract.source ile eşleşmeli."""
    contract = CONTRACTS.get(metric_key)
    assert contract is not None, f"Contract eksik: {metric_key}"

    expected = contract["source"]
    if expected.startswith("("):  # internal source (örn etf_flow)
        pytest.skip(f"{metric_key}: internal source, fetcher yok")

    actual_paths = fetcher_paths.get(fetcher_name, [])
    assert actual_paths, (
        f"{fetcher_name}: hiç _cq_get çağrısı bulunamadı (AST'ta görünmüyor)"
    )
    assert expected in actual_paths, (
        f"{metric_key}: contract.source={expected!r} ama "
        f"{fetcher_name} bu path'i çağırmıyor. Görülen path'ler: {actual_paths}"
    )


# ─── Layer 1: contract self-consistency ──────────────────────────────────

def test_all_contracts_have_required_fields():
    required = {"source", "measures", "window", "vehicle", "display_tr"}
    for key, contract in CONTRACTS.items():
        missing = required - set(contract.keys())
        assert not missing, f"{key}: contract'ta eksik alan(lar): {missing}"


def test_can_and_cannot_claim_disjoint():
    """Aynı kelime hem CAN hem CANNOT'ta olamaz — kontrat tutarsız olur."""
    for key, contract in CONTRACTS.items():
        can = set(w.lower() for w in contract.get("CAN_claim", []))
        cannot = set(w.lower() for w in contract.get("CANNOT_claim", []))
        overlap = can & cannot
        assert not overlap, f"{key}: CAN ∩ CANNOT_claim çakışması: {overlap}"


# ─── Regression test: kullanıcının yakaladığı 2 bug ──────────────────────

def test_coinbase_premium_label_cannot_claim_kurumsal():
    """Day 28 part 9 regression — 'ABD Kurumsal Satış' label'ı bir daha
    coinbase_premium için kullanılamaz."""
    bad_labels = [
        "🔴 ABD Kurumsal Satış",
        "🟢 ABD Kurumsal Alım",
        "🔴 Kurumsal Çıkış",
        "Institutional outflow signal",
    ]
    for label in bad_labels:
        ok, viol = is_label_compliant("coinbase_premium", label)
        assert not ok, f"coinbase_premium: '{label}' geçmemeliydi ama geçti"
        assert viol, f"coinbase_premium: '{label}' için violation listesi boş"


def test_mpi_display_tr_not_satis_baskisi():
    """Day 28 part 9 regression — MPI çevirisi 'satış baskısı endeksi' YASAK."""
    contract = CONTRACTS["mpi"]
    display = contract["display_tr"].lower()
    assert "satış baskısı endeksi" not in display, (
        f"MPI display_tr 'satış baskısı endeksi' içermemeli — yön sinyali değil "
        f"eşik metriği. Mevcut: {display!r}"
    )


def test_stablecoin_uses_netflow_not_inflow():
    """Day 28 part 9 regression — stablecoin endpoint /inflow değil /netflow."""
    contract = CONTRACTS["stablecoin_inflow"]
    assert "netflow" in contract["source"], (
        f"stablecoin_inflow contract.source NETFLOW kullanmalı (gross "
        f"inflow yanıltıcı). Mevcut: {contract['source']!r}"
    )
