"""Whether this process is on its way down, and how the rest of the app finds out.

Nothing in ASGI carries this. Uvicorn's graceful shutdown closes the listening socket,
tells each connection to stop keep-alive, and then *waits for in-flight responses to
finish* - and only after that does it send the lifespan shutdown event. A response that
never finishes on its own therefore blocks the wait, and the lifespan hook that might have
ended it never runs. Verified in the installed uvicorn (0.52.4): a connection with a live
response has `keep_alive` set to False and is sent no `http.disconnect`, so
`request.is_disconnected()` stays False, and `timeout_graceful_shutdown` defaults to None,
so the wait has no bound at all.

The chat stream is exactly that kind of response: it holds the connection open for as long
as a conversation is watched. One open browser tab was enough to hang `uvicorn --reload`
on "Waiting for connections to close" indefinitely - so a backend edit appeared to do
nothing, because the old process was still serving.

So the signal is where we have to learn it. The handler already in place is uvicorn's own,
installed before the app starts; this chains in front of it rather than replacing it, and
only when there is something to chain to - if nothing else is managing shutdown, taking
over the signal would be the app deciding not to exit.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from typing import Callable

logger = logging.getLogger(__name__)

SIGNALS = (signal.SIGINT, signal.SIGTERM)

# Recreated per loop by `startup()`: an Event belongs to the loop that first waits on it,
# and the test suite runs many loops through this module.
_closing: asyncio.Event | None = None


def closing() -> asyncio.Event:
    """The event that fires once, when shutdown begins."""
    global _closing
    if _closing is None:
        _closing = asyncio.Event()
    return _closing


def is_closing() -> bool:
    return _closing is not None and _closing.is_set()


def begin() -> None:
    """Say that shutdown has started. Safe to call twice."""
    closing().set()


def startup() -> Callable[[], None]:
    """Arm the flag for this loop and chain the signal handlers. Returns the undo.

    The undo matters as much as the arming: `TestClient` starts and stops the app many
    times in one process, and a handler left pointing at a dead loop would fire into it.
    """
    global _closing
    _closing = asyncio.Event()

    # `signal.signal` is main-thread only, and TestClient runs the lifespan on a portal
    # thread. Nothing is lost there - a test process is not gracefully shutting down.
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    loop = asyncio.get_running_loop()
    installed: list[tuple[int, object, object]] = []

    for sig in SIGNALS:
        previous = signal.getsignal(sig)
        # Only chain. An uncallable handler (SIG_DFL, SIG_IGN) means nobody else is
        # managing the shutdown, and swallowing the signal to set a flag would leave a
        # process that cannot be stopped.
        if not callable(previous):
            continue

        def handler(signum, frame, _previous=previous):
            # From a signal handler the loop must be poked, not touched: this runs
            # between bytecodes on the main thread, re-entrant with the loop itself.
            try:
                loop.call_soon_threadsafe(begin)
            except RuntimeError:  # the loop has already closed
                pass
            _previous(signum, frame)

        signal.signal(sig, handler)
        installed.append((sig, previous, handler))

    def undo() -> None:
        for sig, previous, ours in installed:
            # Only if it is still ours. Something else installed over the top is that
            # thing's business, and restoring underneath it would undo its work.
            if signal.getsignal(sig) is ours:
                signal.signal(sig, previous)

    return undo
