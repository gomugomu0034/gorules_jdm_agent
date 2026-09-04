"""Drives the agent in the background and turns its execution into SSE events.

A run never happens inside the HTTP request that starts it: the builder loop can
issue eight sequential LLM calls, far beyond any reasonable request timeout. The
POST enqueues a task and returns 202; progress arrives on the thread's stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from backend.config import settings
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
TOTAL_STEPS = 6

_runs: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_cancelled: set[str] = set()


class ThreadBusy(Exception):
    """Raised when a run is already in flight for this thread."""


def is_running(thread_id: str) -> bool:
    task = _runs.get(thread_id)
    return task is not None and not task.done()


async def request_cancel(thread_id: str) -> bool:
    _cancelled.add(thread_id)
    task = _runs.get(thread_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


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
    payload = {"messages": [HumanMessage(content=text)], **_canvas_state(canvas)}

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

    return await _launch(thread_id, payload)


async def start_resume(thread_id: str, value: str, canvas) -> str:
    """Resume a paused run with a chip label or free text.

    The value is passed through byte for byte: the agent compares it against its
    own option strings, emoji included.
    """
    return await _launch(thread_id, Command(resume=value, update=_canvas_state(canvas) or None))


async def _launch(thread_id: str, payload) -> str:
    if is_running(thread_id):
        raise ThreadBusy(thread_id)

    run_id = str(uuid.uuid4())
    _cancelled.discard(thread_id)
    await dao.set_thread_status(thread_id, "running")

    task = asyncio.create_task(_run(thread_id, run_id, payload))
    _runs[thread_id] = task
    task.add_done_callback(lambda t: _runs.pop(thread_id, None) if _runs.get(thread_id) is t else None)
    return run_id


async def _run(thread_id: str, run_id: str, payload) -> None:
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
        await _finish(thread_id, emit, "awaiting_input")
        return

    await _finish(thread_id, emit, "completed")


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
        "of": TOTAL_STEPS,
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
    await dao.set_thread_status(thread_id, status if status == "awaiting_input" else "idle")
    await emit({"type": "done", "status": status})


def _error_code(exc: Exception) -> str:
    text = str(exc).lower()
    if "timeout" in text or "timed out" in text:
        return "LLM_TIMEOUT"
    if "not configured" in text or "api_key" in text or "credential" in text:
        return "LLM_ERROR"
    return "AGENT_ERROR"
