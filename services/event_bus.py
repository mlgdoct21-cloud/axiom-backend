"""
News Event Bus — in-process fanout for SSE/WebSocket subscribers.

Pattern:
    - Each dashboard client that opens /api/v1/news/stream registers an asyncio.Queue.
    - When the crawler finishes analyzing news, it calls `publish_news(news_dict)`.
    - Every active subscriber gets the message pushed to its queue.
    - Slow/disconnected subscribers are dropped when their queue fills (back-pressure).

This is intentionally in-memory (single process). For multi-instance Railway we'd
switch to Redis pub/sub — but Railway runs us as one replica today.
"""
import asyncio
import logging
from typing import AsyncGenerator, Set
from core.logger import get_logger

logger = get_logger("event_bus")

# Max items a slow subscriber can buffer before we drop it.
_QUEUE_MAXSIZE = 100


class NewsEventBus:
    def __init__(self) -> None:
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber. Returns the queue to read from."""
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers.add(q)
            count = len(self._subscribers)
        logger.info(f"📡 Yeni SSE abonesi bağlandı ({count} aktif)")
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(q)
            count = len(self._subscribers)
        logger.info(f"📡 SSE abonesi ayrıldı ({count} aktif)")

    async def publish(self, event_type: str, data: dict) -> None:
        """
        Fanout event to all subscribers. Non-blocking for each subscriber:
        if a queue is full (slow client), we drop the message for THAT client only.
        """
        payload = {"type": event_type, "data": data}
        # Snapshot to avoid mutation while iterating.
        async with self._lock:
            subs = list(self._subscribers)

        dropped = 0
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dropped += 1
                # Drop the oldest to make room (back-pressure policy).
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass
        if dropped:
            logger.debug(f"⚠️  {dropped} yavaş abone için mesaj düşürüldü")

    async def subscriber_count(self) -> int:
        async with self._lock:
            return len(self._subscribers)


# Module-level singleton. Import as: `from services.event_bus import bus`
bus = NewsEventBus()


async def sse_stream(q: asyncio.Queue, heartbeat_seconds: float = 15.0) -> AsyncGenerator[str, None]:
    """
    Convert a subscriber queue into an SSE byte stream.

    Sends an initial `ready` event, then news events as they arrive, with a
    periodic heartbeat so proxies (Railway, Cloudflare) don't close the connection.
    """
    import json
    # Initial handshake
    yield f"event: ready\ndata: {json.dumps({'ok': True})}\n\n"

    while True:
        try:
            payload = await asyncio.wait_for(q.get(), timeout=heartbeat_seconds)
        except asyncio.TimeoutError:
            # Heartbeat — comment line keeps the connection alive.
            yield ": heartbeat\n\n"
            continue
        except asyncio.CancelledError:
            raise

        event_type = payload.get("type", "message")
        data = payload.get("data", {})
        yield f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
