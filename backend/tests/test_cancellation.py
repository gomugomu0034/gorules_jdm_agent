"""Stopping a run: on request, on shutdown, and when nobody is left watching it.

The behaviour under test is mostly about *ordering*, which is why these drive real tasks
on a real loop rather than asserting on mocks. Three things used to be wrong:

  - `request_cancel` raised the cooperative stop flag and called `task.cancel()` in the
    same breath, so the cooperative path never once ran and every cancelled turn lost its
    work.
  - `agent_runtime.shutdown()` closed the checkpointer without stopping anything, so a
    node mid-write met a closed connection.
  - A client going away had no effect at all: closing the tab left the run burning model
    quota against a conversation nobody was reading.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.services import chat_runner, event_bus

THREAD = "thread-under-test"


@pytest.fixture(autouse=True)
def clean_registry():
    """`_runs`, `_cancelled` and `_abandoned` are process-wide; leaking one breaks the next
    test in a way that looks like a bug in the code rather than in the harness."""
    yield
    chat_runner._runs.clear()
    chat_runner._cancelled.clear()
    for timer in list(chat_runner._abandoned.values()):
        timer.cancel()
    chat_runner._abandoned.clear()
    event_bus._subscribers.clear()


def register(coro_fn) -> asyncio.Task:
    task = asyncio.create_task(coro_fn())
    chat_runner._runs[THREAD] = task
    return task


async def settle(seconds: float = 0.05) -> None:
    """Let the background enforcement task get a turn on the loop."""
    await asyncio.sleep(seconds)


# --------------------------------------------------------------------- on request

def test_a_stop_request_lets_the_run_end_itself_first():
    """The whole point of the cooperative flag. Cancelling in the same breath as raising it
    meant a node between attempts never got to return what it had built."""

    async def scenario():
        noticed = asyncio.Event()

        async def run():
            for _ in range(200):
                if chat_runner.is_cancelled(THREAD):
                    noticed.set()
                    return "stopped itself"
                await asyncio.sleep(0.005)
            return "never saw the flag"

        task = register(run)
        accepted = await chat_runner.request_cancel(THREAD)
        outcome = await asyncio.wait_for(task, timeout=3)
        await settle()
        return accepted, outcome, noticed.is_set(), task.cancelled()

    accepted, outcome, noticed, hard_cancelled = asyncio.run(scenario())

    assert accepted is True
    assert noticed, "the run must be told before it is killed"
    assert outcome == "stopped itself"
    assert not hard_cancelled, "a run that stops on its own must not also be torn down"


def test_a_run_that_ignores_the_flag_is_still_killed(monkeypatch):
    """An in-flight model call cannot be interrupted cooperatively, and the user is still
    entitled to a prompt stop. The grace period is a courtesy, not a veto."""
    monkeypatch.setattr(chat_runner, "CANCEL_GRACE_SECONDS", 0.05)

    async def scenario():
        task = register(lambda: asyncio.sleep(30))
        await chat_runner.request_cancel(THREAD)
        await settle(0.3)
        return task.cancelled()

    assert asyncio.run(scenario()) is True


def test_stopping_something_that_is_not_running_says_so():
    async def scenario():
        return await chat_runner.request_cancel("no-such-thread")

    assert asyncio.run(scenario()) is False


# --------------------------------------------------------------------- on shutdown

def test_shutdown_stops_every_run_it_owns():
    async def scenario():
        tasks = []
        for i in range(3):
            task = asyncio.create_task(asyncio.sleep(30))
            chat_runner._runs[f"t{i}"] = task
            tasks.append(task)

        stopped = await chat_runner.stop_all()
        await settle(0.1)
        return stopped, [t.done() for t in tasks]

    stopped, done = asyncio.run(scenario())

    assert stopped == 3
    assert all(done), "nothing may still be writing when the checkpointer closes"


def test_shutdown_stops_runs_before_closing_the_checkpointer(monkeypatch):
    """Ordering is the whole fix: closing the saver first is what leaves a half-written
    checkpoint for the next boot to read."""
    from backend import agent_runtime

    order: list[str] = []

    class Saver:
        async def __aexit__(self, *_):
            order.append("saver closed")

    async def fake_stop_all():
        order.append("runs stopped")
        return 0

    monkeypatch.setattr(chat_runner, "stop_all", fake_stop_all)
    monkeypatch.setattr(agent_runtime, "_saver_cm", Saver())
    monkeypatch.setattr(agent_runtime, "_graph", object())

    asyncio.run(agent_runtime.shutdown())

    assert order == ["runs stopped", "saver closed"]


def test_shutdown_survives_a_run_that_will_not_die(monkeypatch):
    """A stuck task must not stop the process from going down."""
    monkeypatch.setattr(chat_runner, "SHUTDOWN_GRACE_SECONDS", 0.05)

    async def scenario():
        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    # Deliberately refuses the first cancel, as a node running its own
                    # cleanup handler would.
                    await asyncio.sleep(0.02)
                    raise

        chat_runner._runs["stuck"] = asyncio.create_task(stubborn())
        return await asyncio.wait_for(chat_runner.stop_all(), timeout=3)

    assert asyncio.run(scenario()) == 1


# --------------------------------------------------------------------- on disconnect

def test_a_reload_does_not_stop_the_run(monkeypatch):
    """A refresh and a closed tab look identical from the server, so a disconnect starts a
    grace period rather than a cancellation - otherwise `from_seq` replay, which exists so
    a reload can rejoin a long build, would have nothing left to rejoin."""
    monkeypatch.setattr(chat_runner, "DISCONNECT_GRACE_SECONDS", 0.15)

    async def scenario():
        task = register(lambda: asyncio.sleep(5))
        queue = event_bus.subscribe(THREAD)

        event_bus.unsubscribe(THREAD, queue)      # the tab goes away
        chat_runner.watch_disconnect(THREAD)
        await asyncio.sleep(0.05)
        event_bus.subscribe(THREAD)               # ...and comes back, reloaded

        await asyncio.sleep(0.25)                 # past the grace period
        return chat_runner.is_cancelled(THREAD), task.done()

    cancelled, done = asyncio.run(scenario())

    assert not cancelled
    assert not done


def test_a_run_nobody_comes_back_for_is_stopped(monkeypatch):
    monkeypatch.setattr(chat_runner, "DISCONNECT_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(chat_runner, "CANCEL_GRACE_SECONDS", 0.05)

    async def scenario():
        task = register(lambda: asyncio.sleep(30))
        chat_runner.watch_disconnect(THREAD)
        await asyncio.sleep(0.4)
        return chat_runner.is_cancelled(THREAD), task.done()

    cancelled, done = asyncio.run(scenario())

    assert cancelled, "a conversation nobody is reading must not keep spending model calls"
    assert done


def test_a_second_tab_closing_does_not_end_a_watched_run(monkeypatch):
    monkeypatch.setattr(chat_runner, "DISCONNECT_GRACE_SECONDS", 0.1)

    async def scenario():
        register(lambda: asyncio.sleep(5))
        event_bus.subscribe(THREAD)               # one tab stays open
        second = event_bus.subscribe(THREAD)

        event_bus.unsubscribe(THREAD, second)
        chat_runner.watch_disconnect(THREAD)
        await asyncio.sleep(0.3)
        return chat_runner.is_cancelled(THREAD), THREAD in chat_runner._abandoned

    cancelled, timer_running = asyncio.run(scenario())

    assert not cancelled
    assert not timer_running, "no timer should even start while someone is still watching"


def test_a_new_turn_clears_a_timer_left_by_the_last_one(monkeypatch):
    """The timer outlives the turn it was started for. Left running, it would fire into
    whatever the user asked next."""
    monkeypatch.setattr(chat_runner, "DISCONNECT_GRACE_SECONDS", 0.2)

    async def scenario():
        register(lambda: asyncio.sleep(5))
        chat_runner.watch_disconnect(THREAD)
        assert THREAD in chat_runner._abandoned

        chat_runner._clear_cancel(THREAD)
        await asyncio.sleep(0.35)
        return chat_runner.is_cancelled(THREAD), THREAD in chat_runner._abandoned

    cancelled, timer_running = asyncio.run(scenario())

    assert not cancelled
    assert not timer_running
