"""Sector impact whitelist — loaded once at import time, cached.

Drives the hallucination guard: when macro_narrative.py validates Gemini's
output, any sector name not present in the active category's lists triggers
a reject + raw YAML fallback.

Lookups are case-insensitive on both category and sector. Missing categories
raise KeyError so callers can choose to fail loud or fall back.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml

from core.logger import get_logger

logger = get_logger("macro.sector_map")

_YAML_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "sector_impact_map.yaml"

_VALID_CATEGORIES = {
    "CPI_HOT", "CPI_COOL",
    "NFP_HOT", "NFP_COOL",
    "FOMC_HAWKISH", "FOMC_DOVISH",
    "PCE_HOT", "PCE_COOL",
}


@dataclass(frozen=True)
class SectorImpact:
    category: str
    description: str
    mechanism: str
    sectors_negative: tuple[str, ...]
    sectors_positive: tuple[str, ...]

    def all_sectors(self) -> frozenset[str]:
        return frozenset(self.sectors_negative) | frozenset(self.sectors_positive)


def _validate(raw: dict) -> dict[str, SectorImpact]:
    out: dict[str, SectorImpact] = {}
    missing = _VALID_CATEGORIES - set(raw.keys())
    if missing:
        raise ValueError(f"sector_impact_map.yaml missing categories: {sorted(missing)}")
    extra = set(raw.keys()) - _VALID_CATEGORIES
    if extra:
        # Don't fail — future-additive, just log so we notice typos quickly.
        logger.warning(f"sector_impact_map.yaml has unknown categories: {sorted(extra)}")
    for cat, body in raw.items():
        if cat not in _VALID_CATEGORIES:
            continue
        for required in ("description", "mechanism", "sectors_negative", "sectors_positive"):
            if required not in body:
                raise ValueError(f"{cat}: missing field '{required}'")
        out[cat] = SectorImpact(
            category=cat,
            description=str(body["description"]),
            mechanism=str(body["mechanism"]),
            sectors_negative=tuple(s.lower() for s in body["sectors_negative"]),
            sectors_positive=tuple(s.lower() for s in body["sectors_positive"]),
        )
    return out


@lru_cache(maxsize=1)
def load_sector_map(path: Path | None = None) -> dict[str, SectorImpact]:
    """Returns the validated sector impact map. Cached after first call.

    Tests can pass a custom path to override.
    """
    p = path or _YAML_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"sector_impact_map.yaml: expected mapping, got {type(raw).__name__}")
    return _validate(raw)


def is_known_sector(category: str, sector: str) -> bool:
    """True if `sector` (case-insensitive) is whitelisted under `category`."""
    impact = load_sector_map().get(category.upper())
    if not impact:
        return False
    return sector.lower() in impact.all_sectors()


def unknown_sectors(category: str, sectors: Iterable[str]) -> list[str]:
    """Returns the subset of `sectors` not present in `category`'s whitelist."""
    impact = load_sector_map().get(category.upper())
    if not impact:
        return [s for s in sectors]
    whitelist = impact.all_sectors()
    return [s for s in sectors if s.lower() not in whitelist]
