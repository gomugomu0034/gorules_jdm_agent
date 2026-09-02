"""In-process fan-out of agent events to SSE subscribers.

Every event is also persisted with a monotonic sequence number, so a client that
reconnects (or reloads the page) replays what it missed instead of losing it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from backend.db import dao

logger = logging.getLogger(__name__)

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)


async def publish(thread_id: str, run_id: str, event: dict) -> int:
    """Persist an event and hand it to every live subscriber."""
    seq = await dao.append_event(thread_id, run_id, event)
    payload = {"seq": seq, **event}
    for queue in list(_subscribers.get(thread_id, ())):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("Dropping event for a stalled subscriber on %s", thread_id)
    return seq


def subscribe(thread_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subscribers[thread_id].add(queue)
    return queue


def unsubscribe(thread_id: str, queue: asyncio.Queue) -> None:
    subs = _subscribers.get(thread_id)
    if not subs:
        return
    subs.discard(queue)
    if not subs:
        _subscribers.pop(thread_id, None)


def subscriber_count(thread_id: str) -> int:
    return len(_subscribers.get(thread_id, ()))
