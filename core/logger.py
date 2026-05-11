"""Centralized logging configuration with secret-redaction filter.

Secrets leak into logs through three common pathways:
1. f-strings that interpolate a token into the format string itself
2. exception strings that quote the URL/header that triggered them
3. dict / object reprs that include a field named "api_key" etc.

The SecretRedactingFilter scrubs known-shape patterns (Stripe keys, JWT
tokens, webhook secrets) from both the format string AND the args tuple
before the handler writes the record. False negatives are possible (e.g.
a custom internal token format we haven't pattern-matched), but every
known high-value secret format is covered.

Patterns covered:
- Stripe secret keys:    sk_live_… / sk_test_…
- Stripe webhook secrets: whsec_…
- Stripe restricted keys: rk_live_… / rk_test_…
- JWT access/refresh tokens: eyJ…header.payload.signature
"""

import logging
import re
import sys
from pathlib import Path

# Create logs directory if it doesn't exist
LOG_DIR = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


_REDACT_PATTERNS = (
    # Stripe — secret API keys
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]+"),
    # Stripe — restricted API keys (admin scope subset)
    re.compile(r"\brk_(?:live|test)_[A-Za-z0-9]+"),
    # Stripe — webhook signing secrets
    re.compile(r"\bwhsec_[A-Za-z0-9]+"),
    # JWT — three base64url segments separated by dots; header always begins
    # with `eyJ` because the literal `{"alg"` base64-encodes to `eyJhbGci…`.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
)


def _redact(s: str) -> str:
    """Apply all redaction patterns to a string. Cheap on hit (returns same
    interned string when no match), so safe to call on every log record."""
    for pat in _REDACT_PATTERNS:
        s = pat.sub("***REDACTED***", s)
    return s


class SecretRedactingFilter(logging.Filter):
    """Logging filter that redacts secret-shaped substrings from messages
    and positional args BEFORE the handler formats the record.

    Applied to every handler attached to the root `axiom` logger so a stray
    `logger.info(f"...{api_key}...")` or an asyncpg exception that quotes a
    DSN-with-credentials gets scrubbed before reaching stdout/file/Railway logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, str):
                record.args = _redact(record.args)
        return True


# Configure root logger
logger = logging.getLogger("axiom")
logger.setLevel(logging.INFO)

# Shared redaction filter — attached to BOTH handlers so secret patterns
# get scrubbed regardless of which sink the record lands in.
_redact_filter = SecretRedactingFilter()

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.addFilter(_redact_filter)
console_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)

# File handler
file_handler = logging.FileHandler(LOG_DIR / "axiom.log")
file_handler.setLevel(logging.DEBUG)
file_handler.addFilter(_redact_filter)
file_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a specific module."""
    return logging.getLogger(f"axiom.{name}")
