"""Per-IP rate limiting via slowapi.

Single shared `limiter` instance importable by routers. The actual
storage is in-process memory (no Redis) — acceptable while AXIOM runs
single-replica on Railway. Once we scale horizontally we'll swap the
backend to Redis via `slowapi.Limiter(storage_uri="redis://...")` but
that's a separate concern from the rate-limit policy itself.

Keying by remote IP (X-Forwarded-For respected when behind Railway's
reverse proxy via `get_remote_address`). Telegram-bot calls from our
own server originate from a single IP, so internal endpoints that the
bot calls heavily (e.g. /billing/checkout when many users /upgrade) get
a more generous limit; user-facing endpoints (/auth/login from the
dashboard) are tighter.

Endpoints get @limiter.limit(...) decorators in their router files;
this module just exposes the shared limiter and re-exports
RateLimitExceeded so main.py can install the handler.
"""
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Default off when env not set — explicit safety; tests/dev still work.
limiter = Limiter(key_func=get_remote_address)

__all__ = ["limiter", "RateLimitExceeded"]
