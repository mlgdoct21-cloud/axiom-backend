"""Shared Turkish display labels + ticker map for the YAML sector keys.

`sector_impact_map.yaml` uses snake_case English keys (`growth_stocks`,
`consumer_discretionary`) so that the validator / whitelist stays unambiguous.
For user-facing surfaces (Telegram, dashboard chips) we want a clean Turkish
label. The dashboard has its own parallel TS map at
`src/lib/macro-sector-labels.ts` — keep both in sync when adding sectors.

Also exposes `tickers_for_sectors()` — flatten a list of sector keys to top
US tickers (sourced from `data/sector_tickers.yaml`) for the "Etkilenen
hisseler" inline keyboard callback.
"""
from __future__ import annotations

from typing import Optional


SECTOR_LABELS_TR: dict[str, str] = {
    "commodities": "Emtia",
    "consumer_discretionary": "İhtiyari Tüketim",
    "consumer_staples": "Temel Tüketim",
    "defensives": "Defansif Hisseler",
    "em_exposure": "Gelişen Piyasalar",
    "energy": "Enerji",
    "financials": "Bankalar",
    "growth_stocks": "Büyüme Hisseleri",
    "industrials": "Sanayi",
    "materials": "Hammadde",
    "real_estate": "Gayrimenkul",
    "small_caps": "Küçük Ölçek",
    "tech": "Teknoloji",
    "utilities": "Kamu Hizmetleri",
}


def label_tr(sector_key: str) -> str:
    """Map a YAML key to its Turkish label, or pretty-print the raw key."""
    if not sector_key:
        return ""
    if sector_key in SECTOR_LABELS_TR:
        return SECTOR_LABELS_TR[sector_key]
    # Unknown key (new in YAML, not yet in map): humanise the snake_case.
    return sector_key.replace("_", " ").title()


_TICKER_CACHES: dict[str, dict[str, list[str]]] = {}


def _load_ticker_yaml(filename: str) -> dict[str, list[str]]:
    """Load + cache one ticker-map YAML by filename. Returns empty dict on
    any IO/parse failure (callers degrade silently)."""
    if filename in _TICKER_CACHES:
        return _TICKER_CACHES[filename]
    try:
        import os
        import yaml
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", filename,
        )
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}
        _TICKER_CACHES[filename] = {
            k: [str(t) for t in (v or []) if t]
            for k, v in raw.items()
        }
    except Exception:
        _TICKER_CACHES[filename] = {}
    return _TICKER_CACHES[filename]


def load_sector_tickers() -> dict[str, list[str]]:
    """US large-caps per sector (data/sector_tickers.yaml)."""
    return _load_ticker_yaml("sector_tickers.yaml")


def load_sector_bist_tickers() -> dict[str, list[str]]:
    """BIST counterparts per sector (data/sector_bist_tickers.yaml)."""
    return _load_ticker_yaml("sector_bist_tickers.yaml")


def _flatten_tickers(sector_keys: list, source_map: dict[str, list[str]], cap: int) -> list[str]:
    seen: list[str] = []
    for key in (sector_keys or []):
        for t in source_map.get(str(key), []):
            if t not in seen:
                seen.append(t)
            if len(seen) >= cap:
                return seen
    return seen


def tickers_for_sectors(sector_keys: list) -> list[str]:
    """US flatten + dedupe (10-cap)."""
    return _flatten_tickers(sector_keys, load_sector_tickers(), cap=10)


def bist_tickers_for_sectors(sector_keys: list) -> list[str]:
    """BIST flatten + dedupe (8-cap to stay tighter on Telegram lines)."""
    return _flatten_tickers(sector_keys, load_sector_bist_tickers(), cap=8)
