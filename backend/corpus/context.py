"""Which run a model call belongs to.

`call_llm` is a synchronous function reached from thirteen graph nodes, none of which is
given the run it is executing. Passing a run id down through every node signature would
mean touching `AgentState` and every call site for a value that is pure telemetry, so it
travels in a context variable instead.

That works because LangGraph dispatches sync nodes through `copy_context()`
(`langgraph/pregel/_executor.py`), and `anyio.to_thread.run_sync` copies the context too -
so a scope opened in the async run task is visible inside the worker thread the node
actually executes on. `test_agent_corpus.py` pins that behaviour against the real compiled
graph rather than trusting it.
"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from dataclasses import dataclass, field

_CURRENT: ContextVar["RunContext | None"] = ContextVar("corpus_run", default=None)


@dataclass
class RunContext:
    """The turn a sample belongs to.

    `observed` is filled in as the turn reveals what it was: `intent_router_node` settles
    the intent, and the outcome is only known at whichever of the five exit paths in
    `chat_runner._run` the turn happens to take. The scope writes whatever has accumulated
    when it closes, so a path that observes nothing still produces a row.
    """

    run_id: str
    thread_id: str = ""
    graph_id: str | None = None
    owner_hash: str = ""
    observed: dict = field(default_factory=dict)
    # The most recent model output in this run, and so the one a tool verdict is about.
    # Not always the call made in the same breath: the builder's first attempt validates
    # the *planner's* DSL without asking the model anything, and its verdicts belong to
    # the planner's sample. Pointing at the latest sample gets that right by construction.
    last_sample_id: str | None = None


def current() -> RunContext | None:
    """The run in scope, or None for a call made outside one."""
    return _CURRENT.get()


def bind(context: RunContext):
    """Enter `context`. Returns the token to pass to `unbind`."""
    return _CURRENT.set(context)


def unbind(token) -> None:
    _CURRENT.reset(token)


def hash_owner(owner: str | None) -> str:
    """A stable, non-reversing handle for an owner id.

    A guest owner is `guest:<secrets.token_urlsafe(16)>` - the visitor's identity, minted
    by `auth.get_owner` and carried in their session cookie. The corpus needs to tell one
    visitor's runs from another's; it has no use for the token itself, so it never holds
    one. Truncated to 16 hex characters, which is far more than enough to separate the
    handful of owners a local studio ever sees.
    """
    if not owner:
        return ""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def observe(**facts) -> None:
    """Note something about the run in scope. Silently ignored outside one.

    Used for facts that are not available when the run opens: the intent the router
    settled on, the artifact the turn produced, and how it ended.
    """
    context = current()
    if context is not None:
        context.observed.update({k: v for k, v in facts.items() if v not in (None, "")})
