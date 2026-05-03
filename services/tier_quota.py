"""Per-tier daily quotas for heavy Telegram commands.

The cheap commands (/start, /tier, /upgrade, /tags, /takip*) stay
un-throttled — only AI-bound paths (/report, /haber) eat quota since
they hit Gemini + dashboard endpoints with real cost per call.

Quota table (UTC-day rolling):

| tier     | /report | /haber |
|----------|---------|--------|
| free     | 5       | 10     |
| premium  | 30      | 50     |
| advance  | ∞       | ∞      |

In-memory counter keyed by (utc_date, telegram_id, command). Single-
instance; if/when we go multi-replica this needs to migrate to Redis.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


# (tier, command) → daily limit. None = unlimited.
_LIMITS: dict[tuple[str, str], Optional[int]] = {
    ("free",    "/report"): 5,
    ("free",    "/haber"):  10,
    ("premium", "/report"): 30,
    ("premium", "/haber"):  50,
    ("advance", "/report"): None,
    ("advance", "/haber"):  None,
}

# (utc_date_str, telegram_id, command) → count
_COUNTS: dict[tuple[str, str, str], int] = {}


@dataclass
class QuotaResult:
    allowed: bool
    used: int
    limit: Optional[int]   # None = unlimited
    tier: str
    command: str

    @property
    def remaining(self) -> Optional[int]:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _normalize_tier(tier: Optional[str]) -> str:
    t = (tier or "free").lower()
    if t not in ("free", "premium", "advance"):
        return "free"
    return t


def _limit_for(tier: str, command: str) -> Optional[int]:
    if (tier, command) in _LIMITS:
        return _LIMITS[(tier, command)]
    # Unknown command → no quota (don't accidentally lock a new feature).
    return None


def check_and_consume(user_id, tier: Optional[str], command: str) -> QuotaResult:
    """Atomic check + increment. Returns whether the call is allowed and
    how much budget remains. The caller should send a 'quota exceeded'
    message and skip the work when allowed=False.

    Always increments on allowed=True so retries cost the user — we don't
    decrement on failure since most failures are user-fault (bad symbol,
    Gemini upstream timeout) and we don't want a single user to drain
    Gemini by retrying invalid input.
    """
    t = _normalize_tier(tier)
    limit = _limit_for(t, command)
    today = _today_key()
    key = (today, str(user_id), command)
    used = _COUNTS.get(key, 0)
    if limit is not None and used >= limit:
        return QuotaResult(allowed=False, used=used, limit=limit, tier=t, command=command)
    _COUNTS[key] = used + 1
    return QuotaResult(allowed=True, used=used + 1, limit=limit, tier=t, command=command)


def peek(user_id, tier: Optional[str], command: str) -> QuotaResult:
    """Read-only — useful for /tier output. Doesn't consume."""
    t = _normalize_tier(tier)
    limit = _limit_for(t, command)
    today = _today_key()
    used = _COUNTS.get((today, str(user_id), command), 0)
    return QuotaResult(allowed=(limit is None or used < limit), used=used, limit=limit, tier=t, command=command)


def garbage_collect(*, keep_days: int = 2) -> int:
    """Drop counter rows older than today − keep_days. Cheap O(N) scan;
    call on a slow timer or on probe-loop tick. Returns rows dropped."""
    today = datetime.now(timezone.utc).date()
    keep_keys = {(today.isoformat(),)}
    for i in range(1, keep_days + 1):
        d = (today.fromordinal(today.toordinal() - i)).isoformat()
        keep_keys.add((d,))
    dead = [k for k in _COUNTS if (k[0],) not in keep_keys]
    for k in dead:
        _COUNTS.pop(k, None)
    return len(dead)
