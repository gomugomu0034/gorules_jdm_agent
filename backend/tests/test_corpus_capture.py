"""What the training corpus records, and what it must never do to a run.

These drive the real `call_llm` and, where it matters, the real compiled graph. The fake
is installed at `_dispatch` - the provider dispatch *inside* `call_llm` - rather than over
`call_llm` itself, because replacing `call_llm` is replacing the thing under test. That is
also why the rest of the suite's fakes cannot be reused here.

The assumption everything else rests on is that a context variable set on the async run
task is visible inside the worker thread a synchronous node executes on. LangGraph
dispatches through `copy_context()`, so it holds - but it is someone else's implementation
detail, and `test_the_run_scope_reaches_a_node_through_the_real_graph` pins it rather than
trusting it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from backend import corpus
from backend import lang_graph_agent as agent
from backend.corpus import store
from backend.tests.test_agent_flow import BROKEN_DSL, GOOD_DSL, planner_payload

# `isolated_corpus` in conftest.py already points the store at a per-test tmp file.


def rows(table: str, columns: str = "*") -> list[dict]:
    conn = sqlite3.connect(store.settings.corpus_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT {columns} FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


@pytest.fixture
def answers(monkeypatch):
    """Install a provider that replies from a script, leaving `call_llm` itself real."""

    def install(replies, *, reasoning=None):
        seen = list(replies)

        def dispatch(sys_prompt, messages):
            reply = seen.pop(0) if seen else ""
            if isinstance(reply, Exception):
                raise reply
            return agent.LLMResponse(reply, reasoning_details=reasoning)

        monkeypatch.setattr(agent, "_dispatch", dispatch)

    return install


# --------------------------------------------------------------- one call, recorded

def test_a_model_call_is_recorded_with_the_node_that_made_it(answers):
    answers(["a design"])

    agent.call_llm("SYSTEM", [HumanMessage(content="build a shipping policy")],
                   node="planner_node", attempt=2, purpose="replan")

    sample, = rows("samples")
    assert sample["node"] == "planner_node"
    assert sample["attempt"] == 2
    assert sample["purpose"] == "replan"
    assert sample["completion"] == "a design"
    assert json.loads(sample["messages_json"]) == [
        {"role": "user", "content": "build a shipping policy"}
    ]
    assert sample["latency_ms"] is not None
    assert sample["error"] is None


def test_the_provider_and_model_are_recorded(answers, monkeypatch):
    """Which model produced a sample is the difference between a corpus you can filter and
    a pile of completions. It lived only in environment variables before."""
    answers(["ok"])
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", "openrouter")
    monkeypatch.setattr(agent, "OPENROUTER_MODEL_NAME", "some-vendor/some-model:free")

    agent.call_llm("SYSTEM", [HumanMessage(content="hi")], node="triage_node")

    sample, = rows("samples")
    assert sample["provider"] == "openrouter"
    assert sample["model_requested"] == "some-vendor/some-model:free"
    assert sample["temperature"] == agent.LLM_TEMPERATURE


def test_the_models_thinking_is_kept(answers):
    """The one signal the pipeline already collected and then dropped on the floor."""
    thinking = [{"type": "reasoning.text", "text": "Two bands, so a switch."}]
    answers(["a design"], reasoning=thinking)

    agent.call_llm("SYSTEM", [HumanMessage(content="hi")], node="planner_node")

    sample, = rows("samples")
    assert json.loads(sample["reasoning_json"]) == thinking


def test_a_refused_call_is_recorded_as_a_refusal(answers):
    """A rate limit is a sample too: without it the corpus cannot tell a model that failed
    from a turn that was never attempted."""
    answers([agent.RateLimited("free-models-per-day exhausted")])

    with pytest.raises(agent.RateLimited):
        agent.call_llm("SYSTEM", [HumanMessage(content="hi")], node="builder_node")

    sample, = rows("samples")
    assert sample["completion"] is None
    assert "RateLimited" in sample["error"]
    assert "free-models-per-day" in sample["error"]


def test_the_response_is_handed_back_untouched(answers):
    """`_call_openrouter` returns an `LLMResponse` carrying the reasoning trace. Recording
    must not flatten it to a plain string - every later turn re-sends that trace."""
    answers(["a design"], reasoning=[{"text": "because"}])

    result = agent.call_llm("SYSTEM", [HumanMessage(content="hi")], node="planner_node")

    assert isinstance(result, agent.LLMResponse)
    assert result.reasoning_details == [{"text": "because"}]


# --------------------------------------------------------------- prompts as versions

def test_a_system_prompt_is_stored_once_however_many_calls_use_it(answers):
    """PROMPT_PLANNER renders to 46KB and PROMPT_BUILDER to 32KB. Stored per call, a
    hundred runs is megabytes of one repeated string."""
    answers(["a", "b", "c"])
    for _ in range(3):
        agent.call_llm(agent.PROMPT_BUILDER, [HumanMessage(content="x")], node="builder_node")

    assert len(rows("samples")) == 3
    assert len(rows("prompts")) == 1, "one prompt, one row"
    assert len({s["prompt_hash"] for s in rows("samples")}) == 1


def test_editing_a_prompt_mints_a_new_version(answers):
    """The hash is the prompt's version. Without it, samples recorded either side of a
    prompt edit are indistinguishable, and a training set silently mixes two tasks."""
    answers(["a", "b"])
    agent.call_llm("You plan graphs.", [HumanMessage(content="x")], node="planner_node")
    agent.call_llm("You plan graphs. Prefer switches.", [HumanMessage(content="x")],
                   node="planner_node")

    assert len(rows("prompts")) == 2
    first, second = rows("samples")
    assert first["prompt_hash"] != second["prompt_hash"]


def test_the_prompt_kind_is_readable(answers):
    answers(["a"])
    agent.call_llm("SYSTEM", [HumanMessage(content="x")], node="builder_node")
    assert rows("prompts")[0]["kind"] == "builder"


# --------------------------------------------------------------- runs

def test_a_run_groups_the_calls_made_inside_it(answers):
    answers(["one", "two"])

    with corpus.run_scope("run-a", thread_id="t-1"):
        agent.call_llm("S", [HumanMessage(content="x")], node="triage_node")
        agent.call_llm("S", [HumanMessage(content="y")], node="planner_node")
        corpus.observe(outcome="completed", intent="CREATE")

    run, = rows("runs")
    assert run["run_id"] == "run-a"
    assert run["outcome"] == "completed"
    assert run["intent"] == "CREATE"
    assert [s["run_id"] for s in rows("samples")] == ["run-a", "run-a"]
    assert [s["seq"] for s in rows("samples")] == [1, 2], "ordered within the run"


def test_a_run_nobody_reported_on_is_recorded_as_an_error():
    """The default matters: a scope that exits without an outcome left through an
    exception, and saying so is more useful than leaving the row open forever."""
    with corpus.run_scope("run-b", thread_id="t-1"):
        pass

    assert rows("runs")[0]["outcome"] == "error"


def test_a_guest_identity_is_never_stored_raw():
    """A guest owner is `guest:<secrets.token_urlsafe(16)>` - the visitor's own session
    identity. The corpus needs to tell owners apart, not to hold their tokens."""
    secret = "guest:6Qf3xkNq_l2AaZzTt1PpQw"

    with corpus.run_scope("run-c", thread_id="t-1", owner=secret):
        corpus.observe(outcome="completed")

    run, = rows("runs")
    assert secret not in json.dumps(run)
    assert run["owner_hash"] and run["owner_hash"] != secret


def test_a_call_outside_any_run_is_still_kept(answers):
    """Test generation is reachable straight from the API. A sample with no run is worth
    more than no sample."""
    answers(["ok"])
    agent.call_llm("S", [HumanMessage(content="x")], node="test_generation")

    sample, = rows("samples")
    assert sample["run_id"] is None
    assert sample["node"] == "test_generation"


# --------------------------------------------------------------- never breaks a run

def test_capture_failure_never_reaches_the_caller(answers, monkeypatch):
    """Capture sits in the path of every model call the app makes. Instrumentation that
    can take down what it instruments is worse than none."""
    answers(["the answer"])
    monkeypatch.setattr(store, "_connect",
                        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))

    result = agent.call_llm("S", [HumanMessage(content="x")], node="planner_node")

    assert result == "the answer"


def test_capture_gives_up_rather_than_failing_forever(monkeypatch):
    monkeypatch.setattr(store, "_connect",
                        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))

    for _ in range(store._MAX_FAILURES):
        store.start_run("r", thread_id="t")

    assert store.enabled() is False, "a broken corpus must stop retrying on every call"


def test_capture_can_be_turned_off(answers, monkeypatch):
    answers(["ok"])
    monkeypatch.setattr(store, "settings",
                        store.settings.model_copy(update={"corpus_capture": "off"}))

    with corpus.run_scope("run-d", thread_id="t-1"):
        agent.call_llm("S", [HumanMessage(content="x")], node="planner_node")

    with pytest.raises(sqlite3.OperationalError):
        rows("samples")  # the file was never even created


def test_content_that_will_not_serialise_does_not_cost_a_sample(answers):
    """A message carrying something json cannot encode must degrade to a recorded sample,
    not to a strike against capture."""
    answers(["ok"])

    agent.call_llm("S", [AIMessage(content="draft",
                                   additional_kwargs={"reasoning_details": {uuid.uuid4()}})],
                   node="builder_node")

    assert len(rows("samples")) == 1


# --------------------------------------------------------------- through the real graph

def test_the_run_scope_reaches_a_node_through_the_real_graph(monkeypatch, tmp_path):
    """The assumption the whole design rests on.

    Nodes are synchronous functions that LangGraph runs on a worker thread. If the context
    variable did not survive that hop, every sample would land with a NULL run_id and the
    corpus would be a pile of unattributed completions. Driven through the real compiled
    graph, not a stand-in, because it is LangGraph's dispatch that is under test.
    """
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    def dispatch(sys_prompt, messages):
        if sys_prompt is agent.PROMPT_INTENT:
            return '{"intent": "CREATE", "confidence": 1.0}'
        if sys_prompt in (agent.PROMPT_PLANNER, agent.PROMPT_BUILDER):
            return planner_payload(GOOD_DSL)
        return json.dumps({
            "status": "READY_FOR_APPROVAL",
            "message": "Understood.",
            "options": ["Approve with above understanding & assumptions"],
        })

    monkeypatch.setattr(agent, "_dispatch", dispatch)

    graph = agent.build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def turn():
        with corpus.run_scope("run-real", thread_id="t-real"):
            async for _ in graph.astream(
                {"messages": [HumanMessage(content="Free shipping over $50, else $6.")],
                 "canvas_jdm_json": ""},
                config=config,
            ):
                pass
            async for _ in graph.astream(
                Command(resume="Approve with above understanding & assumptions"),
                config=config,
            ):
                pass
            corpus.observe(outcome="completed")

    asyncio.run(turn())

    captured = rows("samples")
    assert captured, "the graph made model calls and none were recorded"
    assert all(s["run_id"] == "run-real" for s in captured), \
        "a sample with no run means the context did not survive the thread hop"

    nodes = [s["node"] for s in captured]
    assert "triage_node" in nodes
    assert "planner_node" in nodes
    assert "unknown" not in nodes, "every call site must name the node that made it"
    # `intent_router_node` is absent on purpose: an empty canvas is settled by rule, and
    # `_classify_intent` reaches the model only when the rules cannot decide. That path is
    # covered by `test_the_intent_router_is_attributed_when_it_does_ask`.


def test_each_repair_attempt_is_its_own_sample(monkeypatch, tmp_path):
    """The builder's repair loop is a preference dataset: attempt N fails an objective
    check, attempt N+1 passes. That is only usable if the failed attempt is kept, and
    before this the loop overwrote it - only the last one survived in state.
    """
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))
    plans = [BROKEN_DSL, GOOD_DSL]

    def dispatch(sys_prompt, messages):
        if sys_prompt is agent.PROMPT_INTENT:
            return '{"intent": "CREATE", "confidence": 1.0}'
        if sys_prompt in (agent.PROMPT_PLANNER, agent.PROMPT_BUILDER):
            return planner_payload(plans.pop(0) if plans else GOOD_DSL)
        return json.dumps({
            "status": "READY_FOR_APPROVAL",
            "message": "Understood.",
            "options": ["Approve with above understanding & assumptions"],
        })

    monkeypatch.setattr(agent, "_dispatch", dispatch)

    graph = agent.build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def turn():
        with corpus.run_scope("run-repair", thread_id="t-repair"):
            async for _ in graph.astream(
                {"messages": [HumanMessage(content="Free shipping over $50, else $6.")],
                 "canvas_jdm_json": ""},
                config=config,
            ):
                pass
            async for _ in graph.astream(
                Command(resume="Approve with above understanding & assumptions"),
                config=config,
            ):
                pass
            corpus.observe(outcome="completed")

    asyncio.run(turn())

    repairs = [s for s in rows("samples") if s["node"] == "builder_node"]
    assert repairs, "the builder repaired a failing graph and it went unrecorded"
    # The planner's first design fails its own suite, so the builder is asked to fix it;
    # each ask is a distinct attempt, numbered as the user saw it.
    assert [r["attempt"] for r in repairs] == sorted(r["attempt"] for r in repairs)
    assert all(r["purpose"] == "repair" for r in repairs)


def test_the_intent_router_is_attributed_when_it_does_ask(answers):
    """`_classify_intent` settles most requests by rule and reaches the model only for the
    ambiguous ones. That call is easy to miss precisely because it is conditional."""
    answers(['{"intent": "MODIFY", "confidence": 0.9}'])

    intent, _ = agent._classify_intent("what about the gold tier", has_graph=True)

    assert intent == "MODIFY"
    sample, = rows("samples")
    assert sample["node"] == "intent_router_node"


def test_a_blank_path_override_does_not_silently_discard_everything(monkeypatch):
    """`CORPUS_DB_PATH=` left blank is what copying .env.example produces. Unresolved it
    becomes `Path("")`, which is the *directory* `.` - SQLite cannot open it, so every
    write fails and capture quietly switches itself off for the process.
    """
    from backend.config import _DEFAULT_CORPUS_DB

    blank = store.settings.model_copy(update={"corpus_db_path": "  "})

    assert str(blank.corpus_db_file) == _DEFAULT_CORPUS_DB


def test_the_commit_is_resolved_without_the_git_binary(tmp_path, monkeypatch):
    """The container has no git installed, so shelling out returns nothing there and every
    Docker-recorded run would carry a blank `git_sha`. The repository is bind-mounted, so
    the commit is readable straight out of `.git`.
    """
    monkeypatch.setattr(store, "subprocess", None)  # any use of it would raise

    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("a039fc9012345678901234567890123456789012\n")

    assert store._sha_from_git_dir(tmp_path) == "a039fc90"


def test_a_packed_ref_is_still_resolved(tmp_path):
    """`git gc` moves refs into `packed-refs`, and the loose file disappears."""
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "deadbeef12345678901234567890123456789012 refs/heads/main\n"
    )

    assert store._sha_from_git_dir(tmp_path) == "deadbeef"


def test_a_detached_head_carries_the_sha_itself(tmp_path):
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("c0ffee1234567890123456789012345678901234\n")

    assert store._sha_from_git_dir(tmp_path) == "c0ffee12"
