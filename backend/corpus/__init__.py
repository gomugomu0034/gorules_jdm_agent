"""Capture of agent behaviour for fine-tuning.

Records what the model was asked, what it answered, what it was thinking, and which turn
it belonged to - into `data/corpus.db`, separate from the studio's own database so that
deleting a conversation does not delete the training data taken from it.

Two entry points. `run_scope` opens a turn, from `chat_runner` for an agent run or from an
API handler for a model call made outside one. `record_llm_call` writes one sample and is
called from exactly one place: the `call_llm` wrapper in `lang_graph_agent`, which is the
single chokepoint all nine model calls in the codebase pass through.

Nothing here raises. See `store._never_fails`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from backend.corpus import corrections, store
from backend.corpus.context import RunContext, bind, current, hash_owner, observe, unbind

__all__ = [
    "RunContext",
    "current",
    "enabled",
    "hash_owner",
    "new_run_id",
    "open_at_boot",
    "observe",
    "answer_open_question",
    "record_correction",
    "record_interaction",
    "record_llm_call",
    "record_tool_result",
    "run_scope",
    "store",
]

enabled = store.enabled
open_at_boot = store.open_now


def new_run_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def run_scope(
    run_id: str,
    *,
    thread_id: str = "",
    graph_id: str | None = None,
    owner: str | None = None,
    source: str = "live",
) -> Iterator[RunContext | None]:
    """Mark everything inside as belonging to one turn.

    Opens the `runs` row on entry and closes it on exit, with whatever `observe()` has
    learned in between. The default outcome is `error`: a scope that leaves without anyone
    saying how the turn ended left through an exception, and recording that honestly is
    more useful than recording nothing.

    Yields None when capture is off, so callers need no conditional.
    """
    if not store.enabled():
        yield None
        return

    context = RunContext(
        run_id=run_id,
        thread_id=thread_id,
        graph_id=graph_id,
        owner_hash=hash_owner(owner),
    )
    store.start_run(
        run_id,
        thread_id=thread_id,
        graph_id=graph_id,
        owner_hash=context.owner_hash,
        source=source,
    )
    token = bind(context)
    try:
        yield context
    finally:
        unbind(token)
        seen = context.observed
        store.finish_run(
            run_id,
            outcome=seen.get("outcome", "error"),
            intent=seen.get("intent", ""),
            mode=seen.get("mode", ""),
            final_jdm=seen.get("final_jdm"),
            graph_id=seen.get("graph_id", context.graph_id),
            owner_hash=seen.get("owner_hash", context.owner_hash),
        )


def record_llm_call(
    *,
    node: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    completion: str | None = None,
    reasoning: Any = None,
    provider: str = "",
    model_requested: str = "",
    temperature: float | None = None,
    reasoning_enabled: bool = False,
    latency_ms: int | None = None,
    error: str | None = None,
    attempt: int = 1,
    purpose: str = "",
    model_served: str = "",
    generation_id: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    upstream_provider: str = "",
    cost: float | None = None,
) -> str | None:
    """Record one model call against the run in scope, if there is one.

    A call outside a run - test generation reached straight from the API - is still worth
    keeping, and lands with a NULL `run_id` rather than being dropped.
    """
    context = current()
    sample_id = store.record_sample(
        node=node,
        system_prompt=system_prompt,
        messages=messages,
        completion=completion,
        reasoning=reasoning,
        provider=provider,
        model_requested=model_requested,
        temperature=temperature,
        reasoning_enabled=reasoning_enabled,
        latency_ms=latency_ms,
        error=error,
        attempt=attempt,
        purpose=purpose,
        model_served=model_served,
        generation_id=generation_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        upstream_provider=upstream_provider,
        cost=cost,
        run_id=context.run_id if context else None,
    )
    if context is not None and sample_id is not None:
        # So the tool verdicts that follow know which output they are judging.
        context.last_sample_id = sample_id
    return sample_id


def record_tool_result(
    *,
    tool: str,
    node: str,
    ok: bool,
    attempt: int = 1,
    diagnostics: list | None = None,
    output: Any = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> str | None:
    """Record what a deterministic tool decided about the run's most recent model output.

    Attribution is by position rather than by argument: the tools run immediately after
    the call whose output they inspect, and the builder's first attempt deliberately
    inspects the planner's DSL without making a call of its own.
    """
    context = current()
    return store.record_tool_result(
        tool=tool,
        node=node,
        ok=ok,
        attempt=attempt,
        diagnostics=diagnostics,
        output=output,
        error=error,
        duration_ms=duration_ms,
        run_id=context.run_id if context else None,
        sample_id=context.last_sample_id if context else None,
    )


# --------------------------------------------------------------------------- humans

def record_interaction(
    *,
    kind: str,
    thread_id: str = "",
    graph_id: str | None = None,
    prompt: str | None = None,
    options: list | None = None,
    response: str | None = None,
    detail: Any = None,
    answered: bool = True,
    run_id: str | None = None,
) -> str | None:
    """Record something a person did, against the run in scope if there is one.

    `run_id` is for the caller that knows the turn but is not inside it: a requirement is
    recorded by the request handler that launched the run, and the run's own scope lives
    on the task, in a context this side of the call cannot see.
    """
    context = current()
    return store.record_interaction(
        kind=kind,
        thread_id=thread_id or (context.thread_id if context else ""),
        graph_id=graph_id if graph_id is not None else (context.graph_id if context else None),
        prompt=prompt,
        options=options,
        response=response,
        detail=detail,
        answered=answered,
        run_id=run_id or (context.run_id if context else None),
        sample_id=context.last_sample_id if context else None,
    )


def answer_open_question(thread_id: str, response: str) -> bool:
    """Attach a reply to the question this thread is waiting on, if it is waiting on one."""
    return store.answer_open_question(thread_id, response) or False


def record_correction(
    *,
    thread_id: str,
    graph_id: str,
    before: Any,
    after: Any,
    from_version: int | None = None,
    to_version: int | None = None,
) -> str | None:
    """Record a person editing what the agent produced, if they changed anything real.

    Both graphs are stored in full rather than referenced. The corpus exists precisely
    because the studio's own record can be deleted, and a correction pair that stops being
    readable when its policy is deleted would be worth nothing.
    """
    change = corrections.describe_change(before, after)
    if change is None:
        # Layout, or the editor's mount-time normalisation. Recording it would teach the
        # model that correct output was corrected.
        return None
    if from_version is not None:
        # Replace rather than accumulate: the same edit is re-recorded as it settles.
        store.forget_correction(graph_id, from_version)
    return record_interaction(
        kind="correction",
        thread_id=thread_id,
        graph_id=graph_id,
        response="edited the agent's graph",
        detail={
            "from_version": from_version,
            "to_version": to_version,
            "change": change,
            "before": before,
            "after": after,
        },
    )
