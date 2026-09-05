"""What the deterministic tools decided, recorded per attempt.

This is the objective half of the corpus. A model output that the parser rejected, that
the linter flagged, or that failed its own suite is different training data from one that
passed - and knowing which is which needs no human, because the linter and the engine
already answer it.

Before this, only the last verdict survived: `evaluation_feedback` held one string and
each attempt of the repair loop overwrote the one before. The failing attempt, which is
half of every preference pair, was discarded.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend import corpus
from backend import lang_graph_agent as agent
from backend.corpus import store
from backend.tests.test_agent_flow import BROKEN_DSL, GOOD_DSL, planner_payload


def rows(table: str) -> list[dict]:
    conn = sqlite3.connect(store.settings.corpus_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


def drive(monkeypatch, tmp_path, plans, *, run_id="run-1"):
    """Run one CREATE turn through the real compiled graph to final approval.

    `plans` is the sequence of DSL documents the model returns - the planner takes the
    first, and each builder repair takes the next.
    """
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))
    remaining = list(plans)

    def dispatch(sys_prompt, messages):
        if sys_prompt is agent.PROMPT_INTENT:
            return '{"intent": "CREATE", "confidence": 1.0}'
        if sys_prompt in (agent.PROMPT_PLANNER, agent.PROMPT_BUILDER):
            return planner_payload(remaining.pop(0) if remaining else GOOD_DSL)
        return json.dumps({
            "status": "READY_FOR_APPROVAL", "message": "Understood.",
            "options": ["Approve with above understanding & assumptions"],
        })

    monkeypatch.setattr(agent, "_dispatch", dispatch)
    graph = agent.build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def turn():
        with corpus.run_scope(run_id, thread_id="t-1"):
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


# ------------------------------------------------------------------ the happy path

def test_a_build_records_every_stage_it_ran(monkeypatch, tmp_path):
    drive(monkeypatch, tmp_path, [GOOD_DSL])

    tools = [r["tool"] for r in rows("tool_results") if r["node"] == "builder_node"]
    assert tools == ["parse_dsl", "check_format", "lint", "run_tests"], (
        "each stage is a separate check with its own verdict; collapsing them loses "
        "which one the output actually failed"
    )
    assert all(r["ok"] == 1 for r in rows("tool_results"))


def test_the_planner_records_whether_it_produced_a_design(monkeypatch, tmp_path):
    drive(monkeypatch, tmp_path, [GOOD_DSL])

    plan, = [r for r in rows("tool_results") if r["tool"] == "extract_plan"]
    assert plan["ok"] == 1
    assert json.loads(plan["output_json"])["dsl_chars"] > 0


def test_a_stage_records_what_it_measured(monkeypatch, tmp_path):
    drive(monkeypatch, tmp_path, [GOOD_DSL])

    by_tool = {r["tool"]: r for r in rows("tool_results") if r["node"] == "builder_node"}
    assert json.loads(by_tool["parse_dsl"]["output_json"]) == {"nodes": 3, "edges": 2}
    assert json.loads(by_tool["run_tests"]["output_json"])["passed"] == 2
    assert by_tool["lint"]["duration_ms"] is not None


# ------------------------------------------------------- the repair loop's own data

def test_a_failing_attempt_is_kept_alongside_the_one_that_fixed_it(monkeypatch, tmp_path):
    """The single most valuable thing this pipeline produces. `BROKEN_DSL` compiles and
    lints clean but gets the threshold wrong, so it fails its own suite; the repair passes.
    Same task, one rejected and one accepted, with a machine-checked reason - a preference
    pair with no human labelling in it.
    """
    drive(monkeypatch, tmp_path, [BROKEN_DSL, GOOD_DSL])

    verdicts = [r for r in rows("tool_results")
                if r["tool"] == "run_tests" and r["node"] == "builder_node"]

    assert len(verdicts) == 2, "the failing attempt was overwritten, not recorded"
    assert (verdicts[0]["ok"], verdicts[1]["ok"]) == (0, 1)
    assert verdicts[0]["attempt"] < verdicts[1]["attempt"]

    rejected = json.loads(verdicts[0]["output_json"])
    assert rejected["failed"] == 1, "and the reason it was rejected is recorded with it"


def test_the_reason_an_attempt_failed_is_structured_not_prose(monkeypatch, tmp_path):
    """`format_for_llm` writes for the model to read. A corpus needs to filter and count,
    which prose cannot do - so the `Diagnostic` records are kept as data."""
    drive(monkeypatch, tmp_path, [BROKEN_DSL, GOOD_DSL])

    failed = next(r for r in rows("tool_results") if r["ok"] == 0)
    diagnostics = json.loads(failed["diagnostics_json"])

    assert diagnostics, "a rejection with nothing to act on is not usable training data"
    first = diagnostics[0]
    assert {"kind", "code", "message"} <= set(first)
    assert first["code"] and first["kind"]


def test_each_attempts_verdicts_are_numbered_as_the_user_saw_them(monkeypatch, tmp_path):
    drive(monkeypatch, tmp_path, [BROKEN_DSL, GOOD_DSL])

    parses = [r for r in rows("tool_results")
              if r["tool"] == "parse_dsl" and r["node"] == "builder_node"]

    assert [r["attempt"] for r in parses] == [1, 2]


# --------------------------------------------------------------- attribution

def test_a_verdict_points_at_the_output_it_judged(monkeypatch, tmp_path):
    """The builder's first attempt validates the *planner's* DSL without asking the model
    anything, so its verdicts belong to the planner's sample - not to a builder call that
    never happened. Attribution by latest-sample gets that right by construction.
    """
    drive(monkeypatch, tmp_path, [BROKEN_DSL, GOOD_DSL])

    samples = {r["sample_id"]: r for r in rows("samples")}
    parses = [r for r in rows("tool_results")
              if r["tool"] == "parse_dsl" and r["node"] == "builder_node"]

    assert samples[parses[0]["sample_id"]]["node"] == "planner_node", (
        "attempt 1 compiles what the planner wrote"
    )
    assert samples[parses[1]["sample_id"]]["node"] == "builder_node", (
        "attempt 2 compiles what the repair call returned"
    )


def test_verdicts_are_grouped_with_the_run_that_produced_them(monkeypatch, tmp_path):
    drive(monkeypatch, tmp_path, [GOOD_DSL], run_id="run-xyz")

    verdicts = rows("tool_results")
    assert verdicts
    assert all(r["run_id"] == "run-xyz" for r in verdicts)
    assert [r["seq"] for r in verdicts] == list(range(1, len(verdicts) + 1))


# --------------------------------------------------------------- lint specifics

def test_lint_records_every_finding_not_only_the_blocking_ones(monkeypatch, tmp_path):
    """A graph that merely warns is worse training data than one that lints clean, and the
    difference is invisible if only errors are kept."""
    drive(monkeypatch, tmp_path, [GOOD_DSL])

    lint_row = next(r for r in rows("tool_results")
                    if r["tool"] == "lint" and r["node"] == "builder_node")
    counts = json.loads(lint_row["output_json"])

    assert lint_row["ok"] == 1, "nothing blocking, so the build proceeded"
    assert set(counts) == {"errors", "warnings", "hints"}
    assert sum(counts.values()) > 0, "the shipped skeleton is not lint-perfect"
    assert len(json.loads(lint_row["diagnostics_json"])) == sum(counts.values())


# --------------------------------------------------------------- direct unit checks

def test_a_parse_error_becomes_structured_diagnostics():
    """`DslError` collects every problem in a document rather than stopping at the first,
    so it already is a list - it only needed to not be flattened into a string."""
    from backend.tools.markdown_dsl_parser import DslError

    with corpus.run_scope("run-p", thread_id="t"):
        with pytest.raises(DslError):
            with agent._tool_run("parse_dsl", node="builder_node"):
                raise DslError(["no arrows in # Structure", "unknown type: decisiontable"])

    verdict, = rows("tool_results")
    assert verdict["ok"] == 0
    assert [d["message"] for d in json.loads(verdict["diagnostics_json"])] == [
        "no arrows in # Structure", "unknown type: decisiontable",
    ]


def test_an_exception_is_re_raised_untouched():
    """The repair loop is driven by these exceptions. Swallowing one to record it would
    change what the agent does, which telemetry must never do."""
    with corpus.run_scope("run-q", thread_id="t"):
        with pytest.raises(ValueError, match="the original message"):
            with agent._tool_run("lint", node="builder_node"):
                raise ValueError("the original message")


def test_recording_failure_does_not_break_the_tool(monkeypatch):
    monkeypatch.setattr(store, "_connect",
                        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))

    with corpus.run_scope("run-r", thread_id="t"):
        with agent._tool_run("lint", node="builder_node") as run:
            run.output = {"errors": 0}
            outcome = "the tool still ran"

    assert outcome == "the tool still ran"


def test_a_call_outside_a_run_still_records_a_verdict():
    """Lint is reachable from the API with no agent turn around it."""
    with agent._tool_run("lint", node="lint_node") as run:
        run.output = {"errors": 0}

    verdict, = rows("tool_results")
    assert verdict["run_id"] is None
    assert verdict["sample_id"] is None
    assert verdict["ok"] == 1
