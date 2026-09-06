"""Letting go of the chat stream when the process is going down.

The stream holds its connection open for as long as a conversation is watched, which is
the point of it - a build can run for minutes and a reader should not have to poll. It is
also, on shutdown, a response that never finishes: uvicorn closes the listening socket,
tells each connection to stop keep-alive, and then waits for in-flight responses before it
sends the lifespan shutdown event. One open browser tab was enough to hang
`uvicorn --reload` on "Waiting for connections to close" for as long as the tab stayed
open - so a backend edit appeared to do nothing at all, because the old process was still
the one answering.

Nothing in ASGI reports this. Confirmed against the installed uvicorn (0.52.4): a
connection with a live response is sent no `http.disconnect`, and
`timeout_graceful_shutdown` defaults to None, so the wait is unbounded. The signal is
where it has to be learned.
"""

from __future__ import annotations

import asyncio
import signal
import uuid

import pytest

from backend.api import chat
from backend.services import event_bus, lifecycle


@pytest.fixture(autouse=True)
def clean_lifecycle():
    """`startup()` chains real signal handlers, and these tests call it on the main thread
    where that actually takes effect. Left in place they would accumulate across the file,
    each one closing over a loop that has since closed."""
    handlers = {sig: signal.getsignal(sig) for sig in lifecycle.SIGNALS}
    yield
    for sig, handler in handlers.items():
        signal.signal(sig, handler)
    lifecycle._closing = None
    event_bus._subscribers.clear()


class Connected:
    """A client that is still there. The whole difficulty is that on shutdown it is."""

    async def is_disconnected(self) -> bool:
        return False


async def open_stream(thread_id: str):
    """The endpoint's own body iterator, with the ownership check stubbed out."""
    response = await chat.stream(thread_id, Connected(), from_seq=0, owner="owner")
    return response.body_iterator


@pytest.fixture
def any_thread(monkeypatch):
    """A thread that exists, with no database behind it.

    What is under test is the loop, not storage: the persistence `publish` does is
    covered where the event log is.
    """
    seq = iter(range(1, 1000))

    async def exists(thread_id, owner=None):
        return {"id": thread_id, "status": "idle"}

    async def no_events(thread_id, from_seq=0):
        return []

    async def numbered(thread_id, run_id, event):
        return next(seq)

    monkeypatch.setattr(chat, "require_thread", exists)
    monkeypatch.setattr(chat.dao, "list_events", no_events)
    monkeypatch.setattr(event_bus.dao, "append_event", numbered)
    return str(uuid.uuid4())


def test_the_stream_ends_itself_when_shutdown_begins(any_thread):
    async def scenario():
        lifecycle.startup()
        frames = await open_stream(any_thread)

        # Live: an event published to the thread comes straight down the wire.
        await event_bus.publish(any_thread, "run-1", {"type": "node_end", "node": "x"})
        assert "node_end" in await anext(frames)

        # Now the process starts going down. Nothing publishes anything, and nothing
        # disconnects - the only signal is the flag.
        reader = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0)
        assert not reader.done(), "the stream should be waiting, not spinning"

        lifecycle.begin()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(reader, timeout=1)

    asyncio.run(scenario())


def test_it_does_not_wait_out_the_keepalive_first(any_thread, monkeypatch):
    """A stop noticed on the next 15s tick would still hold a reload for 15 seconds per
    open tab. The wait has to be woken, not polled."""
    monkeypatch.setattr(chat, "KEEPALIVE_SECONDS", 30)

    async def scenario():
        lifecycle.startup()
        frames = await open_stream(any_thread)
        reader = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0)

        started = asyncio.get_running_loop().time()
        lifecycle.begin()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(reader, timeout=1)
        assert asyncio.get_running_loop().time() - started < 0.5

    asyncio.run(scenario())


def test_an_event_waiting_to_be_read_is_not_thrown_away(any_thread, monkeypatch):
    """The keepalive tick used to be a fresh `queue.get()` every time. A `get` cancelled
    after it has taken an item drops that item, and an event dropped here is an event the
    client never sees - the reason the one waiter is kept across iterations."""
    monkeypatch.setattr(chat, "KEEPALIVE_SECONDS", 0.02)

    async def scenario():
        lifecycle.startup()
        frames = await open_stream(any_thread)

        # Two keepalive rounds with nothing to read, then a real event.
        for _ in range(2):
            assert (await anext(frames)).startswith(":")
        await event_bus.publish(any_thread, "run-1", {"type": "done", "status": "completed"})
        assert "done" in await anext(frames)

    asyncio.run(scenario())


def test_a_stream_that_ends_on_shutdown_arms_no_abandonment_timer(any_thread, monkeypatch):
    """`watch_disconnect` starts a 45-second timer to stop a run nobody is watching. On
    the way down there is nothing left to watch it with, and `stop_all` is about to take
    every run anyway."""
    armed: list[str] = []
    monkeypatch.setattr(chat.chat_runner, "watch_disconnect", armed.append)

    async def scenario():
        lifecycle.startup()
        frames = await open_stream(any_thread)
        reader = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0)
        lifecycle.begin()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(reader, timeout=1)

    asyncio.run(scenario())
    assert armed == []


# ------------------------------------------------------------------ the signal itself

def test_the_shutdown_signal_is_chained_not_replaced():
    """Uvicorn's handler is what actually stops the server. Taking the signal and keeping
    it would leave a process that sets a flag and then carries on serving."""
    called: list[int] = []

    def theirs(signum, frame):
        called.append(signum)

    async def scenario():
        previous = signal.signal(signal.SIGTERM, theirs)
        try:
            undo = lifecycle.startup()
            ours = signal.getsignal(signal.SIGTERM)
            assert ours is not theirs, "the flag would never be set"

            ours(signal.SIGTERM, None)
            await asyncio.sleep(0)  # let `call_soon_threadsafe` land

            assert called == [signal.SIGTERM], "the server was never told to stop"
            assert lifecycle.is_closing()

            undo()
            assert signal.getsignal(signal.SIGTERM) is theirs
        finally:
            signal.signal(signal.SIGTERM, previous)

    asyncio.run(scenario())


def test_nothing_is_taken_over_when_nobody_else_is_managing_the_stop():
    """With no handler in place a SIGTERM kills the process, which is correct. Replacing
    that with a flag-setter would make the app unstoppable."""
    async def scenario():
        previous = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            lifecycle.startup()
            assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
        finally:
            signal.signal(signal.SIGTERM, previous)

    asyncio.run(scenario())
