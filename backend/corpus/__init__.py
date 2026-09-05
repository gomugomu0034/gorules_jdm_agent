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

from backend.corpus import store
from backend.corpus.context import RunContext, bind, current, hash_owner, observe, unbind

__all__ = [
    "RunContext",
    "current",
    "enabled",
    "hash_owner",
    "new_run_id",
    "open_at_boot",
    "observe",
    "record_llm_call",
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
) -> str | None:
    """Record one model call against the run in scope, if there is one.

    A call outside a run - test generation reached straight from the API - is still worth
    keeping, and lands with a NULL `run_id` rather than being dropped.
    """
    context = current()
    return store.record_sample(
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
        run_id=context.run_id if context else None,
    )
