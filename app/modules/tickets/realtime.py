"""Real-time fan-out for the support chat.

A sync request handler calls `tickets_realtime.publish(ticket_id, event)` right after it commits a new
message (reply, note, status, forward, assign). Every WebSocket subscribed to that ticket then receives the
event at once, instead of the UI waiting for its 20-second poll.

This is an *in-process* bus: it only reaches sockets connected to the same worker. With more than one uvicorn
worker, a reply handled on worker A will not reach a socket on worker B — then put a Redis pub/sub bus behind
`publish()` and the subscriber. It fails open: if the loop is gone or a client is slow, the event is dropped
and the client still catches up on its periodic refresh, so nothing breaks.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_LIMIT = 64  # per connection; a client slower than this re-syncs on its fallback refresh


class TicketBroadcaster:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[uuid.UUID, set[asyncio.Queue]] = {}
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once on startup (lifespan), from inside the running event loop."""
        self._loop = loop

    def subscribe(self, ticket_id: uuid.UUID) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_LIMIT)
        with self._lock:
            self._subs.setdefault(ticket_id, set()).add(queue)
        return queue

    def unsubscribe(self, ticket_id: uuid.UUID, queue: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subs.get(ticket_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    self._subs.pop(ticket_id, None)

    def publish(self, ticket_id: uuid.UUID, event: dict[str, Any]) -> None:
        """Deliver `event` to every socket on this ticket. Safe to call from a sync request thread; never
        raises into the caller."""
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            queues = list(self._subs.get(ticket_id, ()))
        for queue in queues:
            try:
                loop.call_soon_threadsafe(self._offer, queue, event)
            except RuntimeError:
                pass  # the loop is closing

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow consumer; it re-syncs on its next refresh


tickets_realtime = TicketBroadcaster()
