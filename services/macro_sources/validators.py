"""Hallucination guards for macro_narrative output.

Two checks, both strict — fail-shut policy:

- `validate_numbers`: every numeric token in the narrative must appear in the
  whitelist (input JSON's known numbers + sentinel anchors). Tolerance is
  expressed in absolute Decimal units so 4.25 matches 4.250000.
- `validate_sectors`: every sector name in the model's sectors_negative /
  sectors_positive lists must exist in `sector_impact_map.yaml` under the
  active category.

Caller decides what to do with the unknown lists — narrative.py rejects to
the raw YAML fallback rather than retrying.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from services.macro_sources.sector_map import unknown_sectors

# Numeric tokens: optional sign, digits, optional fractional part with . or ,
# Optional trailing % is consumed but tracked separately for percent-scaling.
_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?%?")


def extract_numbers(text: str) -> set[Decimal]:
    """Pull every numeric literal from `text` as Decimal.

    Percent tokens (`4.5%`) are stored as their decimal form (0.045) AND as
    the bare number (4.5) so the validator accepts both spellings without
    forcing a single convention on the LLM.
    """
    out: set[Decimal] = set()
    for raw in _NUM_RE.findall(text):
        is_pct = raw.endswith("%")
        body = raw[:-1] if is_pct else raw
        body = body.replace(",", ".")
        try:
            d = Decimal(body)
        except InvalidOperation:
            continue
        out.add(d)
        if is_pct:
            out.add(d / Decimal("100"))
    return out


def _within_tolerance(needle: Decimal, allowed: set[Decimal], tol: Decimal) -> bool:
    return any(abs(needle - a) <= tol for a in allowed)


def validate_numbers(
    narrative: str,
    allowed: set[Decimal],
    tolerance: Decimal = Decimal("0.01"),
) -> list[str]:
    """Return numeric tokens in `narrative` not present in `allowed`.

    `allowed` should already include both sides of any percent ambiguity
    (caller responsibility — see `build_allowed_numbers` below).
    """
    found = extract_numbers(narrative)
    return [str(n) for n in sorted(found, key=lambda x: (x, str(x)))
            if not _within_tolerance(n, allowed, tolerance)]


def build_allowed_numbers(values: list) -> set[Decimal]:
    """Convert a heterogeneous list of input numbers into the validator
    whitelist, automatically adding percent variants.

    Accepts ints, floats, Decimals, strings parseable as numbers, and None
    (skipped). For each finite number x we whitelist:
      x, x*100, x/100  (handles 0.62 vs 62 vs 0.0062 representations)
    Plus fixed sentinels {0, 100} for boilerplate percentages.
    """
    out: set[Decimal] = {Decimal("0"), Decimal("100")}
    for v in values:
        if v is None:
            continue
        try:
            d = Decimal(str(v))
        except InvalidOperation:
            continue
        out.add(d)
        out.add(d * Decimal("100"))
        out.add(d / Decimal("100"))
    return out


def validate_sectors(
    category: str,
    sectors_negative: list[str],
    sectors_positive: list[str],
) -> list[str]:
    """Combined unknown-sector list across both buckets for `category`."""
    unk_neg = unknown_sectors(category, sectors_negative or [])
    unk_pos = unknown_sectors(category, sectors_positive or [])
    seen: set[str] = set()
    out: list[str] = []
    for s in (*unk_neg, *unk_pos):
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
