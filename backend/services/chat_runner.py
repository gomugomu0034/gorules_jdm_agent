"""Drives the agent in the background and turns its execution into SSE events.

A run never happens inside the HTTP request that starts it: the builder loop can
issue eight sequential LLM calls, far beyond any reasonable request timeout. The
POST enqueues a task and returns 202; progress arrives on the thread's stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections import defaultdict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from backend.config import settings
from backend import corpus
from backend.db import dao
from backend.services import event_bus

logger = logging.getLogger(__name__)

NODE_LABELS = {
    "intent_router_node": "Working out what you need",
    "triage_node": "Reviewing your requirements",
    "modify_triage_node": "Checking the change against the current graph",
    "human_triage_review_node": "Waiting for your approval",
    "planner_node": "Planning the graph structure",
    "patch_node": "Editing the policy",
    "builder_node": "Building and testing the graph",
    "output_node": "Preparing the result",
    "human_final_approval_node": "Waiting for your approval",
    "save_files_node": "Saving the policy",
    "explain_node": "Reading the graph",
    "test_node": "Running the test suite",
    "lint_node": "Checking the graph for problems",
}
# Runs vary in length - a lint is one node, a build with retries is a dozen - so a fixed
# denominator produced "step 9 of 6". The count adapts to what the run has actually done.
MIN_TOTAL_STEPS = 6

# How long a run is given to notice the stop flag and return what it has before it is
# killed outright. A node checks between attempts, so this only has to outlast the
# bookkeeping around one - not the LLM call itself, which cannot be interrupted at all.
CANCEL_GRACE_SECONDS = float(os.getenv("AGENT_CANCEL_GRACE", "3"))

# How long a run keeps going with nobody watching it. A refresh and a closed tab are
# indistinguishable from the server, so neither is obeyed on its own: the run is held for
# this long in case a client comes back, which is what keeps `from_seq` replay meaningful.
DISCONNECT_GRACE_SECONDS = float(os.getenv("AGENT_DISCONNECT_GRACE", "45"))

# Runs are given this long to stop themselves when the process is going down, before they
# are cancelled. Short: the point is ordering, not patience.
SHUTDOWN_GRACE_SECONDS = float(os.getenv("AGENT_SHUTDOWN_GRACE", "5"))

_runs: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
# Threads the user has asked to stop. Written by `request_cancel`, read by the agent's
# long-running nodes between attempts so a run can end cleanly - reporting what it had -
# instead of only ever being killed mid-step by `task.cancel()`, which discards the turn.
_cancelled: set[str] = set()
# Grace timers watching threads whose last client went away, so a second disconnect does
# not stack a second timer and a reconnect can cancel the one already waiting.
_abandoned: dict[str, asyncio.Task] = {}


def is_cancelled(thread_id: str) -> bool:
    return thread_id in _cancelled


class ThreadBusy(Exception):
    """Raised when a run is already in flight for this thread."""


class WorkspaceBusy(Exception):
    """Raised when other model-backed work is already running for this policy.

    The agent has always been serialised per thread, and the read-only inspectors - run
    tests, lint, simulate - are local, cheap and safely concurrent. What was not guarded is
    the one other thing that calls the model: generating a test suite from the panel. Two
    conversations against a single API key means two claims on the same quota, and on a
    tier of 50 requests a day that is the difference between a build finishing and dying
    on a 429 the user has no way to connect back to what they clicked.
    """


def is_running(thread_id: str) -> bool:
    task = _runs.get(thread_id)
    return task is not None and not task.done()


# Graphs with model-backed work in flight that is not an agent run - today, test
# generation. Kept here rather than in the API so both directions of the check read one
# place: the agent asks before starting, and generation asks before starting.
_generating: set[str] = set()


def is_generating(graph_id: str) -> bool:
    return bool(graph_id) and graph_id in _generating


@contextlib.asynccontextmanager
async def generating(graph_id: str):
    """Marks a graph as having generation in flight for the duration of the block."""
    _generating.add(graph_id)
    try:
        yield
    finally:
        _generating.discard(graph_id)


async def running_thread_for_graph(owner: str, graph_id: str) -> str | None:
    """The thread, if any, currently running the agent against this policy."""
    if not graph_id:
        return None
    for thread in await dao.list_threads(owner, graph_id):
        if is_running(thread["id"]):
            return thread["id"]
    return None


async def request_cancel(thread_id: str) -> bool:
    """Ask a run to stop.

    The flag is set first so a node between steps can return a CANCELLED result with its
    work intact. The hard cancel still follows - an in-flight LLM call cannot be
    interrupted, and the user is entitled to a prompt stop either way - but only after a
    grace period, because cancelling in the same breath as raising the flag pre-empts the
    cooperative path entirely and throws away the turn's work every time.

    Returns promptly: the enforcement waits in the background rather than holding the
    request open for it.
    """
    _cancelled.add(thread_id)
    task = _runs.get(thread_id)
    if task is None or task.done():
        # Nothing in flight here. The thread may still be *recorded* as running - a run
        # lost with a previous process - and the client has no other signal that a turn is
        # over, so settle it rather than leaving a Stop button that does nothing.
        await _settle_orphan(thread_id)
        return False

    asyncio.create_task(_enforce_cancel(thread_id, task))
    return True


async def _enforce_cancel(thread_id: str, task: asyncio.Task) -> None:
    """Give the run a moment to stop itself; kill it if it will not."""
    done, _ = await asyncio.wait({task}, timeout=CANCEL_GRACE_SECONDS)
    if not done:
        logger.info("Run on %s did not stop within the grace period; cancelling.", thread_id)
        task.cancel()


async def _settle_orphan(thread_id: str) -> None:
    """Resolve a thread recorded as running that this process is not running.

    `status` is the only thing telling the client a turn has ended, so without this the
    conversation stays disabled forever behind a Stop that can never take effect.
    """
    thread = await dao.get_thread(thread_id)
    if not thread or thread["status"] != "running":
        return
    logger.info("Thread %s claimed to be running with no task behind it; settling.", thread_id)
    await dao.set_thread_status(thread_id, "idle")
    await event_bus.publish(
        thread_id, str(uuid.uuid4()),
        {"type": "done", "status": "cancelled"},
    )


async def stop_all() -> int:
    """Stop every run this process owns. Called on the way down.

    A run is an in-memory task and cannot outlive the process; the only question is whether
    it stops tidily or is torn down mid-write. This has to happen *before* the checkpointer
    closes, or a node writing a checkpoint meets a closed connection.
    """
    live = {tid: task for tid, task in _runs.items() if not task.done()}
    if not live:
        return 0

    logger.info("Stopping %s in-flight run(s) before shutdown.", len(live))
    _cancelled.update(live)
    _, pending = await asyncio.wait(set(live.values()), timeout=SHUTDOWN_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        # Let each one run its own cleanup - the handler is what records the turn as over.
        await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
    return len(live)


def watch_disconnect(thread_id: str) -> None:
    """Note that a client's event stream has gone away.

    A refresh, a sleeping laptop and a closed tab are indistinguishable from here, so none
    of them is obeyed on its own: the run is held for `DISCONNECT_GRACE_SECONDS` in case a
    client comes back, and only stopped if none does. Cancelling on disconnect outright
    would defeat the `from_seq` replay that deliberately lets a reload rejoin a long build.
    """
    if event_bus.subscriber_count(thread_id) or not is_running(thread_id):
        return
    if thread_id in _abandoned:
        return
    _abandoned[thread_id] = asyncio.create_task(_cancel_if_abandoned(thread_id))


async def _cancel_if_abandoned(thread_id: str) -> None:
    try:
        await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
        if event_bus.subscriber_count(thread_id) or not is_running(thread_id):
            return
        logger.info(
            "Nothing has watched %s for %ss; stopping its run.",
            thread_id, DISCONNECT_GRACE_SECONDS,
        )
        await request_cancel(thread_id)
    except asyncio.CancelledError:
        pass
    finally:
        _abandoned.pop(thread_id, None)


def _clear_cancel(thread_id: str) -> None:
    _cancelled.discard(thread_id)
    timer = _abandoned.pop(thread_id, None)
    if timer and not timer.done():
        timer.cancel()


def _canvas_state(canvas) -> dict:
    if canvas is None:
        return {}
    return {
        "canvas_jdm_json": json.dumps(canvas.content) if canvas.content else "",
        "canvas_graph_id": canvas.graph_id or "",
        "canvas_graph_name": canvas.name or "",
        "cancel_requested": False,
    }


async def start_message(thread_id: str, text: str, canvas) -> str:
    """Begin a new turn from a user message."""
    graph_id = getattr(canvas, "graph_id", None) if canvas is not None else None
    if graph_id and is_generating(graph_id):
        raise WorkspaceBusy("test generation")
    _clear_cancel(thread_id)
    payload = {"messages": [HumanMessage(content=text)], "thread_id": thread_id,
               **_canvas_state(canvas)}

    # Pre-load the saved suite so test_node runs what the user actually has,
    # rather than regenerating one every time.
    if canvas is not None and canvas.graph_id:
        tests = await dao.list_tests(canvas.graph_id)
        payload["test_suite_json"] = json.dumps(
            [
                {"name": t["name"], "input": t["input"], "expectedOutput": t["expectedOutput"]}
                for t in tests
                if t.get("enabled", True)
            ]
        )
        await dao.set_thread_graph(thread_id, canvas.graph_id)

    run_id = await _launch(thread_id, payload)
    # What was actually asked for, in their own words. The prompt half of every training
    # example, and until now it lived only in `chat_events`, which cascades away with the
    # conversation it belongs to.
    corpus.record_interaction(
        kind="requirement", thread_id=thread_id, graph_id=graph_id, response=text,
        run_id=run_id,
    )
    return run_id


async def start_resume(thread_id: str, value: str, canvas) -> str:
    """Resume a paused run with a chip label or free text.

    The value is passed through byte for byte: the agent compares it against its
    own option strings, emoji included.
    """
    run_id = await _launch(thread_id, Command(resume=value, update=_canvas_state(canvas) or None))
    # Closes the question the previous turn left open. Asking and answering are two events
    # a whole turn apart, and nothing connected them before.
    if not corpus.answer_open_question(thread_id, value):
        # No question was open - a resume that answers nothing is still the user speaking.
        corpus.record_interaction(kind="requirement", thread_id=thread_id,
                                  response=value, run_id=run_id)
    return run_id


async def _launch(thread_id: str, payload) -> str:
    if is_running(thread_id):
        raise ThreadBusy(thread_id)

    run_id = str(uuid.uuid4())
    # Clears the stop flag *and* any abandonment timer left by a client that dropped
    # during the previous turn; otherwise it would fire into this one.
    _clear_cancel(thread_id)
    await dao.set_thread_status(thread_id, "running")

    task = asyncio.create_task(_run(thread_id, run_id, payload))
    _runs[thread_id] = task
    task.add_done_callback(lambda t: _runs.pop(thread_id, None) if _runs.get(thread_id) is t else None)
    return run_id


async def _observe_identity(thread_id: str) -> None:
    """Note which graph and owner the finished turn belonged to.

    Read from the thread row rather than the payload, because a resume arrives as a
    `Command` with no canvas state on it - and afterwards rather than before, for two
    reasons. It is more accurate: a turn that starts on a blank canvas can attach a graph
    part-way through. And it keeps a database read off the front of the turn, where it
    delayed the first event long enough that a client polling for one could still be
    seeing the *previous* turn's log.
    """
    try:
        thread = await dao.get_thread(thread_id)
    except Exception:  # noqa: BLE001
        return
    if thread is not None:
        corpus.observe(graph_id=thread.get("graph_id"),
                       owner_hash=corpus.hash_owner(thread.get("owner_id")))


async def _run(thread_id: str, run_id: str, payload) -> None:
    """Execute one turn inside a corpus scope, so every model call it makes is attributed.

    The scope has to be entered here rather than deeper in: LangGraph dispatches sync nodes
    through `copy_context()`, so a context variable set on this task is visible inside the
    worker thread each node runs on - but only if it was set before the graph was streamed.
    Entering it costs no await, so the turn starts exactly as promptly as it did before.
    """
    with corpus.run_scope(run_id, thread_id=thread_id):
        try:
            await _execute_turn(thread_id, run_id, payload)
        finally:
            try:
                await _observe_identity(thread_id)
            except asyncio.CancelledError:
                # A hard stop arriving mid-read still has to cancel the task. Swallowing
                # it here would make the run silently uncancellable.
                raise
            except Exception:  # noqa: BLE001
                # Telemetry must never be what turns a finished turn into a failed one.
                pass


async def _execute_turn(thread_id: str, run_id: str, payload) -> None:
    """Execute one turn, translating graph activity into events."""
    from backend.agent_runtime import get_graph

    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id}}
    emit = lambda event: event_bus.publish(thread_id, run_id, event)  # noqa: E731

    # Only messages produced by this turn should be streamed to the client.
    seen_messages = _message_ids(await _state_values(graph, config))
    active: str | None = None
    step = 0

    await emit({"type": "run_started", "run_id": run_id, "thread_id": thread_id})

    async with _locks[thread_id]:
        try:
            await asyncio.wait_for(
                _drive(graph, config, payload, emit, seen_messages, active, step),
                timeout=settings.agent_run_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Run %s on thread %s exceeded the time budget", run_id, thread_id)
            await emit({
                "type": "error",
                "code": "AGENT_TIMEOUT",
                "message": f"The agent ran longer than {settings.agent_run_timeout}s and was stopped.",
                "recoverable": True,
            })
            await _finish(thread_id, emit, "error")
            return
        except asyncio.CancelledError:
            await emit({"type": "error", "code": "CANCELLED",
                        "message": "The run was cancelled.", "recoverable": True})
            await _finish(thread_id, emit, "cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Run %s failed", run_id)
            await emit({
                "type": "error",
                "code": _error_code(exc),
                "message": str(exc),
                "recoverable": True,
            })
            await _finish(thread_id, emit, "error")
            return

    # Decide how the turn ended.
    state = await graph.aget_state(config)
    # Only knowable now: the router settles the intent, and the artifact is whatever the
    # turn built. Both are what make a sample filterable later.
    corpus.observe(
        intent=state.values.get("intent"),
        mode=state.values.get("mode"),
        final_jdm=state.values.get("jdm_json"),
    )
    interrupt_payload = _pending_interrupt(state)

    # A freshly built graph is announced as soon as it exists - which is at the
    # final-approval pause, not after the turn completes.
    await _record_proposal(thread_id, state.values, emit)

    if interrupt_payload is not None:
        options = list(interrupt_payload.get("options") or [])
        await emit({
            "type": "interrupt",
            "prompt": interrupt_payload.get("prompt", ""),
            "options": options,
            "kind": "choice" if options else "text",
            "interrupt_id": run_id,
        })
        # Opened unanswered: the reply arrives on the *next* turn, and `start_resume`
        # closes this row when it does.
        corpus.record_interaction(
            kind="clarification",
            thread_id=thread_id,
            prompt=interrupt_payload.get("prompt", ""),
            options=options,
            answered=False,
        )
        await _finish(thread_id, emit, "awaiting_input")
        return

    # A run that stopped itself on request ended by agreement, not by finishing. Reporting
    # it as "completed" would leave the user's own Stop looking like it did nothing.
    stopped = state.values.get("build_status") == "CANCELLED"
    await _finish(thread_id, emit, "cancelled" if stopped else "completed")


async def _drive(graph, config, payload, emit, seen_messages, active, step) -> None:
    """Stream the graph, emitting node and progress events as they happen."""
    for node in (await graph.aget_state(config)).next or ():
        step += 1
        active = node
        await emit(_node_start(node, step))

    async for mode, chunk in graph.astream(
        payload, config=config, stream_mode=["updates", "custom"]
    ):
        if mode == "custom":
            await emit(chunk if isinstance(chunk, dict) else {"type": "progress", "message": str(chunk)})
            continue

        for node, delta in (chunk or {}).items():
            if node == "__interrupt__":
                continue
            if active != node:
                # The lookahead below cannot see the very first node of a run,
                # so announce it here rather than letting it go unreported.
                step += 1
                await emit(_node_start(node, step))
            await emit({"type": "node_end", "node": node, "status": "ok"})
            active = None
            for message in _new_messages(delta, seen_messages):
                await emit(message)

        nxt = (await graph.aget_state(config)).next or ()
        for node in nxt:
            if node != active:
                step += 1
                active = node
                await emit(_node_start(node, step))


def _node_start(node: str, step: int) -> dict:
    return {
        "type": "node_start",
        "node": node,
        "label": NODE_LABELS.get(node, node.replace("_node", "").replace("_", " ").title()),
        "step": step,
        "of": max(MIN_TOTAL_STEPS, step),
    }


def _message_ids(values: dict) -> set[str]:
    return {getattr(m, "id", None) for m in (values or {}).get("messages", []) if getattr(m, "id", None)}


# The builder keeps every raw LLM reply in state as its retry context, and each
# one is a DSL/JSON dump addressed to the model rather than to the reader. They
# must stay in the agent's memory but never reach the chat.
#
# The reliable signal is the `internal` flag the builder sets. The markers are a
# fallback for threads written before that flag existed, and for the case where
# the model wraps its answer differently than asked.
_INTERNAL_MARKERS = (
    "---DSL STARTS---",
    "---TESTS STARTS---",
    "---USECASE NAME STARTS---",
    "## Markdown DSL",
)


def _is_internal(message_or_text) -> bool:
    if isinstance(message_or_text, str):
        return any(m in message_or_text for m in _INTERNAL_MARKERS)
    if getattr(message_or_text, "additional_kwargs", {}).get("internal"):
        return True
    return any(m in str(message_or_text.content) for m in _INTERNAL_MARKERS)


def _new_messages(delta, seen: set[str]) -> list[dict]:
    """Emit assistant messages this turn produced, once each."""
    out = []
    if not isinstance(delta, dict):
        return out
    for message in delta.get("messages", []) or []:
        mid = getattr(message, "id", None)
        if mid in seen:
            continue
        if mid:
            seen.add(mid)
        if not isinstance(message, AIMessage):
            continue
        content = str(message.content).strip()
        if not content or _is_internal(message):
            continue
        out.append({
            "type": "message",
            "role": "assistant",
            "content": content,
            "message_id": mid or str(uuid.uuid4()),
        })
    return out


def _pending_interrupt(state) -> dict | None:
    if state.tasks and getattr(state.tasks[0], "interrupts", None):
        value = state.tasks[0].interrupts[0].value
        if isinstance(value, dict):
            return value
        return {"prompt": str(value), "options": []}
    return None


async def _state_values(graph, config) -> dict:
    try:
        return (await graph.aget_state(config)).values or {}
    except Exception:  # noqa: BLE001
        return {}


async def _record_proposal(thread_id: str, values: dict, emit) -> None:
    """Persist and announce a freshly built graph awaiting the user's decision."""
    raw_jdm = (values or {}).get("jdm_json") or ""
    if not raw_jdm.strip():
        return
    try:
        jdm = json.loads(raw_jdm)
        tests = json.loads((values.get("test_suite_json") or "[]"))
    except json.JSONDecodeError:
        return
    if not jdm.get("nodes"):
        return

    # The same graph can be observed at several pauses in one conversation;
    # announce it once.
    existing = await dao.get_proposal(thread_id)
    if existing and existing["jdm"] == jdm and existing["tests"] == tests:
        return

    thread = await dao.get_thread(thread_id)
    graph_id = thread.get("graph_id") if thread else None
    base_version = None
    if graph_id:
        stored = await dao.get_graph(graph_id)
        base_version = stored["current_version"] if stored else None

    report = None
    try:
        from backend.tools.zen_evaluator import run_test_suite

        report = run_test_suite(jdm, tests, trace=False)
    except Exception:  # noqa: BLE001
        pass

    usecase_name = values.get("usecase_name") or "Untitled policy"
    await dao.save_proposal(
        thread_id, jdm, tests, usecase_name,
        graph_id=graph_id, base_version=base_version, report=report,
    )
    await emit({
        "type": "graph_proposed",
        "jdm": jdm,
        "tests": tests,
        "usecase_name": usecase_name,
        "base_version": base_version,
        "test_report": report,
    })


async def _finish(thread_id: str, emit, status: str) -> None:
    # Every terminal path goes through here, including the timeout and error branches that
    # return before the state read - which is why the outcome is noted here and not there.
    corpus.observe(outcome=status)
    await dao.set_thread_status(thread_id, status if status == "awaiting_input" else "idle")
    await emit({"type": "done", "status": status})


def _error_code(exc: Exception) -> str:
    # Named separately because it is the most likely failure on a free tier and the only
    # one the user can act on. As AGENT_ERROR it read as a bug in the agent.
    if type(exc).__name__ == "RateLimited" or "429" in str(exc):
        return "LLM_RATE_LIMITED"
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "LLM_TIMEOUT"
    if "not configured" in text or "api_key" in text or "credential" in text:
        return "LLM_ERROR"
    return "AGENT_ERROR"
