"""Kalshi public API — Fed funds rate after FOMC meeting probability snapshot.

Stand-in for CME FedWatch (which CME hard-blocks free scraping of). Kalshi's
KXFED series binary markets ("upper bound > X%") give a trader-implied
probability distribution over the post-meeting target rate. Public REST,
no auth.

We snapshot the next-meeting markets per probe; release_detect / narrative
later read append-only `macro_market_pricing` rows for before/after deltas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from core.logger import get_logger

logger = get_logger("macro.kalshi_fed")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXFED"

_USER_AGENT = "AXIOM-Macro/0.1 (+https://axiom-dashboard-sigma.vercel.app)"
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


@dataclass
class KalshiSnapshot:
    """One probe's view of the next-meeting Fed rate distribution."""
    success: bool = False
    meeting_ticker: Optional[str] = None      # e.g. "KXFED-26JUN"
    strike_date: Optional[str] = None         # ISO timestamp string
    snapshot_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    modal_rate_pct: Optional[float] = None    # lower edge of highest-prob bin
    modal_prob: Optional[float] = None        # 0..1
    distribution: list[dict] = field(default_factory=list)  # [{rate, prob}]
    payload_bytes: int = 0
    http_status: Optional[int] = None
    error: Optional[str] = None


def _yes_mid(market: dict) -> Optional[float]:
    """Midpoint of yes bid/ask in dollar units (0..1). Falls back to last."""
    bid = market.get("yes_bid_dollars")
    ask = market.get("yes_ask_dollars")
    try:
        b = float(bid) if bid is not None else None
        a = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        b = a = None
    if b is not None and a is not None and a >= b:
        return (b + a) / 2.0
    last = market.get("last_price_dollars")
    try:
        return float(last) if last is not None else None
    except (TypeError, ValueError):
        return None


def _build_distribution(markets: list[dict]) -> list[dict]:
    """Convert "rate > X" binaries into bin-probability list.

    Sort strikes ascending; bin (s_i, s_{i+1}] gets P(>s_i) - P(>s_{i+1}).
    Negative deltas are clamped to 0 (bid/ask noise across thin strikes).
    """
    rows = []
    for m in markets:
        strike = m.get("floor_strike")
        if strike is None:
            continue
        try:
            s = float(strike)
        except (TypeError, ValueError):
            continue
        prob = _yes_mid(m)
        if prob is None:
            continue
        rows.append((s, prob))
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])

    dist: list[dict] = []
    for i, (s, p) in enumerate(rows):
        next_p = rows[i + 1][1] if i + 1 < len(rows) else 0.0
        bin_prob = max(0.0, p - next_p)
        dist.append({"rate": s, "prob": round(bin_prob, 4)})
    # The lowest strike's "below" mass: 1 - P(>lowest). Surface as a synthetic
    # bin at rate = lowest_strike - 0.25 to keep the distribution summing ~1.
    lowest_strike, lowest_p = rows[0]
    below = max(0.0, 1.0 - lowest_p)
    if below > 0.005:
        dist.insert(0, {"rate": round(lowest_strike - 0.25, 3), "prob": round(below, 4)})
    return dist


async def _http_get_json(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> tuple[int, dict, int]:
    """Returns (status, json_or_empty, raw_bytes)."""
    url = f"{KALSHI_BASE}{path}"
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    resp = await client.get(url, params=params, headers=headers)
    raw_bytes = len(resp.content)
    if resp.status_code != 200:
        return resp.status_code, {}, raw_bytes
    try:
        return resp.status_code, resp.json(), raw_bytes
    except Exception:
        return resp.status_code, {}, raw_bytes


async def _next_meeting_ticker(client: httpx.AsyncClient) -> tuple[Optional[str], Optional[str], int]:
    """Find the soonest open KXFED meeting. Returns (event_ticker, strike_date, payload_bytes)."""
    status, body, n = await _http_get_json(
        client,
        "/events",
        params={"series_ticker": KALSHI_SERIES, "status": "open", "limit": 50},
    )
    if status != 200:
        return None, None, n
    events = body.get("events") or []
    now = datetime.now(timezone.utc)
    candidates: list[tuple[datetime, str, str]] = []
    for ev in events:
        sd_raw = ev.get("strike_date")
        if not sd_raw:
            continue
        try:
            sd = datetime.fromisoformat(sd_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if sd < now:
            continue
        ticker = ev.get("event_ticker")
        if not ticker:
            continue
        candidates.append((sd, ticker, sd_raw))
    if not candidates:
        return None, None, n
    candidates.sort(key=lambda c: c[0])
    _, ticker, sd_raw = candidates[0]
    return ticker, sd_raw, n


async def fetch_kalshi_fed(meeting_ticker: Optional[str] = None) -> KalshiSnapshot:
    """Snapshot the next FOMC meeting's implied target-rate distribution.

    On error: success=False, error populated, http_status set when known.
    """
    snap = KalshiSnapshot()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            total_bytes = 0
            if meeting_ticker is None:
                meeting_ticker, strike_date, n = await _next_meeting_ticker(client)
                total_bytes += n
                if not meeting_ticker:
                    snap.error = "no open KXFED meetings"
                    snap.http_status = 200
                    snap.payload_bytes = total_bytes
                    return snap
                snap.strike_date = strike_date

            status, body, n = await _http_get_json(
                client,
                "/markets",
                params={"event_ticker": meeting_ticker, "limit": 200},
            )
            total_bytes += n
            snap.payload_bytes = total_bytes
            snap.http_status = status
            snap.meeting_ticker = meeting_ticker

            if status != 200:
                snap.error = f"HTTP {status}"
                return snap

            markets = body.get("markets") or []
            if not markets:
                snap.error = "no markets"
                return snap

            distribution = _build_distribution(markets)
            if not distribution:
                snap.error = "empty distribution"
                return snap

            modal = max(distribution, key=lambda d: d["prob"])
            snap.distribution = distribution
            snap.modal_rate_pct = float(modal["rate"])
            snap.modal_prob = float(modal["prob"])
            snap.success = True
            return snap
    except httpx.HTTPError as e:
        snap.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        logger.error(f"kalshi_fed fetch failed: {snap.error}")
        return snap
    except Exception as e:
        snap.error = f"{type(e).__name__}: {e}"
        logger.error(f"kalshi_fed unexpected error: {snap.error}")
        return snap
