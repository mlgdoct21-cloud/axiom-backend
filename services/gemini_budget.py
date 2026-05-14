"""Rolling 24h Gemini API call budget.

Pathological loops (FMP variant collision, broken validator, infinite
retry) can burn through a Gemini budget in hours. This module is an
in-process kill-switch: each `_call_gemini` site checks the rolling
24h count before issuing a request. Cap hit → return None, loud log.

In-memory; per-process. Railway runs a single worker instance so the
counter is accurate. Restart resets it — acceptable since restarts are
operator-initiated. For a multi-worker deploy this needs Redis (out of
scope for the current single-instance setup).

Tunable via env: GEMINI_DAILY_CALL_CAP (default 500). The PPI Apr
incident burned ~50 narrative + 50 story Gemini calls in spam — 500 is
~10x typical daily usage and a generous ceiling for legitimate ops.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from core.logger import get_logger

logger = get_logger("gemini.budget")

_DAILY_CAP = int(os.getenv("GEMINI_DAILY_CALL_CAP", "500"))
_BUDGET_WINDOW = timedelta(hours=24)

_calls: list[datetime] = []
_lock = asyncio.Lock()


async def check_budget(caller: str = "unknown") -> tuple[bool, int, int]:
    """Atomically check + increment the rolling 24h call counter.

    Returns (allowed, used_after_increment, cap).
      - allowed=True  → caller may proceed with the Gemini request
      - allowed=False → cap hit, caller should skip (None response)

    `caller` is a free-text tag (module name) used in cap-hit logs.
    """
    async with _lock:
        now = datetime.now(timezone.utc)
        cutoff = now - _BUDGET_WINDOW
        # Trim expired entries; this also keeps the list bounded.
        global _calls
        _calls = [t for t in _calls if t >= cutoff]
        used = len(_calls)
        if used >= _DAILY_CAP:
            logger.error(
                f"🚨 GEMINI BUDGET CAP HIT — caller={caller} "
                f"used={used} cap={_DAILY_CAP} window=24h. "
                f"Skipping call. Likely a pathological loop "
                f"(variant collision, validator retry, etc.). "
                f"Restart the service to reset, or set "
                f"GEMINI_DAILY_CALL_CAP=<higher> in env."
            )
            return False, used, _DAILY_CAP
        _calls.append(now)
        return True, used + 1, _DAILY_CAP


def current_usage() -> dict:
    """Read-only snapshot for /admin debug endpoints."""
    now = datetime.now(timezone.utc)
    cutoff = now - _BUDGET_WINDOW
    used = sum(1 for t in _calls if t >= cutoff)
    return {
        "used_24h": used,
        "cap": _DAILY_CAP,
        "headroom": max(0, _DAILY_CAP - used),
        "window_hours": 24,
    }
