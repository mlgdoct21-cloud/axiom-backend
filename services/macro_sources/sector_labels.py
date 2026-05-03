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


_SECTOR_TICKERS_CACHE: Optional[dict[str, list[str]]] = None  # type: ignore


def load_sector_tickers() -> dict[str, list[str]]:
    """Load + cache the sector → tickers map from data/sector_tickers.yaml.
    Returns empty dict on any IO/parse failure (so callers degrade silently).
    """
    global _SECTOR_TICKERS_CACHE
    if _SECTOR_TICKERS_CACHE is not None:
        return _SECTOR_TICKERS_CACHE
    try:
        import os
        import yaml
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "sector_tickers.yaml",
        )
        with open(path, "r") as f:
            raw = yaml.safe_load(f) or {}
        _SECTOR_TICKERS_CACHE = {
            k: [str(t) for t in (v or []) if t]
            for k, v in raw.items()
        }
    except Exception:
        _SECTOR_TICKERS_CACHE = {}
    return _SECTOR_TICKERS_CACHE


def tickers_for_sectors(sector_keys: list) -> list[str]:
    """Flatten + dedupe ticker symbols for a list of sector keys.
    Returns up to 10 unique tickers (cap to keep Telegram messages tight).
    """
    tickers_map = load_sector_tickers()
    seen: list[str] = []
    for key in (sector_keys or []):
        for t in tickers_map.get(str(key), []):
            if t not in seen:
                seen.append(t)
            if len(seen) >= 10:
                return seen
    return seen
