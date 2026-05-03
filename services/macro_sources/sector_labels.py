"""Shared Turkish display labels for the YAML sector keys.

`sector_impact_map.yaml` uses snake_case English keys (`growth_stocks`,
`consumer_discretionary`) so that the validator / whitelist stays unambiguous.
For user-facing surfaces (Telegram, dashboard chips) we want a clean Turkish
label. The dashboard has its own parallel TS map at
`src/lib/macro-sector-labels.ts` — keep both in sync when adding sectors.
"""
from __future__ import annotations


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
