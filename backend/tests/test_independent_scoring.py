"""The exam the model did not write, and the four things that had to change with it.

Every objective label in this corpus rested on `tests_passed`, and the suite behind it came
out of the same reply as the graph. A model that misread the requirement wrote tests that
misread it identically and scored a clean pass - then landed in the training set as a gold
example, and as the *accepted* half of a preference pair.

These tests pin the fix and the three things that make it usable: a suite that cannot be
rewritten mid-repair, a harness that can run more than one requirement at a time, and an
export that can swap the 11,093-token prompt for a 234-token one.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from backend import corpus
from backend import lang_graph_agent as agent
from backend.corpus import export, replay, store, verify

REQUIREMENT = "Free shipping on orders over $50, otherwise a $6 flat fee."


def graph_with_threshold(n: int) -> dict:
    return {
        "contentType": "application/vnd.gorules.decision",
        "nodes": [
            {"id": "i", "name": "Request", "type": "inputNode",
             "content": {"schema": ""}, "position": {"x": 0, "y": 0}},
            {"id": "f", "name": "Fee", "type": "expressionNode", "position": {"x": 1, "y": 0},
             "content": {"expressions": [
                 {"id": "e", "key": "shippingFee", "value": f"orderTotal > {n} ? 0 : 6"}]}},
            {"id": "o", "name": "Response", "type": "outputNode",
             "content": {"schema": ""}, "position": {"x": 2, "y": 0}},
        ],
        "edges": [{"id": "e1", "sourceId": "i", "targetId": "f", "type": "edge"},
                  {"id": "e2", "sourceId": "f", "targetId": "o", "type": "edge"}],
    }


def rows(table: str) -> list[dict]:
    conn = sqlite3.connect(store.settings.corpus_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


@pytest.fixture
def author(monkeypatch):
    """A test author that probes the boundary the requirement states."""
    calls = {"n": 0}

    def dispatch(sys_prompt, messages):
        calls["n"] += 1
        return "---TESTS STARTS---" + json.dumps([
            {"name": "just over", "input": {"orderTotal": 51}, "expectedOutput": {"shippingFee": 0}},
            {"name": "exactly at", "input": {"orderTotal": 50}, "expectedOutput": {"shippingFee": 6}},
            {"name": "well under", "input": {"orderTotal": 20}, "expectedOutput": {"shippingFee": 6}},
        ]) + "---TESTS ENDS---"

    monkeypatch.setattr(agent, "_dispatch", dispatch)
    return calls


# ------------------------------------------------------- the exam it did not write

def test_the_interface_is_shared_but_the_logic_is_not():
    """Without the field names the author invents plausible ones and every assertion
    misses on spelling, which fails correct graphs for a reason unrelated to their logic.
    With the rules, there would be no independence left to have."""
    reads, writes = verify.interface_of(graph_with_threshold(50))

    assert reads == ["orderTotal"]
    assert writes == ["shippingFee"]


def test_an_intermediate_is_not_offered_as_an_input():
    """A field the graph writes and then reads back is its own workings. Offering it would
    invite tests that set it directly and bypass the policy entirely."""
    g = graph_with_threshold(50)
    g["nodes"][1]["content"]["expressions"].append(
        {"id": "e2", "key": "band", "value": "orderTotal > 100 ? 'big' : 'small'"})
    g["nodes"][1]["content"]["expressions"].append(
        {"id": "e3", "key": "surcharge", "value": "band == 'big' ? 1 : 0"})

    reads, writes = verify.interface_of(g)

    assert "band" not in reads
    assert {"shippingFee", "band", "surcharge"} == set(writes)


def test_a_graph_that_wrote_its_own_exam_still_fails_a_fair_one(author):
    """The whole point. This graph has the threshold wrong at 500, and a self-authored
    suite that tests 800 and 20 passes it cleanly."""
    wrong = graph_with_threshold(500)

    from backend.tools.zen_evaluator import run_test_suite
    self_marked = run_test_suite(json.dumps(wrong), [
        {"name": "free", "input": {"orderTotal": 800}, "expectedOutput": {"shippingFee": 0}},
        {"name": "paid", "input": {"orderTotal": 20}, "expectedOutput": {"shippingFee": 6}},
    ], trace=False)["summary"]
    assert self_marked["passed"] == 2 and self_marked["failed"] == 0, "its own exam passes"

    with corpus.run_scope("r1", thread_id="t"):
        summary = verify.verify(wrong, REQUIREMENT)

    assert summary["failed"] == 1, "the requirement's own boundary catches it"


def test_a_correct_graph_passes_the_independent_suite(author):
    with corpus.run_scope("r1", thread_id="t"):
        summary = verify.verify(graph_with_threshold(50), REQUIREMENT)

    assert summary["failed"] == 0 and summary["passed"] == 3


def test_the_verdict_is_recorded_beside_the_models_own_not_instead_of_it(author):
    """Keeping both is what measures how often a self-marked exam and a fair one disagree
    - the number that says whether any of this was worth doing."""
    with corpus.run_scope("r1", thread_id="t"):
        verify.verify(graph_with_threshold(500), REQUIREMENT)

    verdict, = [r for r in rows("tool_results") if r["tool"] == "independent_tests"]
    assert verdict["ok"] == 0
    assert json.loads(verdict["diagnostics_json"])[0]["code"] == "INDEPENDENT_FAIL"


def test_one_suite_is_authored_per_requirement_not_per_model(author):
    """A bake-off is only comparable if every model sits the same exam - and re-authoring
    per run would also multiply the cost by however many models you try."""
    for run in ("a", "b", "c"):
        with corpus.run_scope(run, thread_id="t"):
            verify.verify(graph_with_threshold(50), REQUIREMENT)

    assert author["n"] == 1, "authored once"
    assert len(rows("suites")) == 1
    assert len([r for r in rows("tool_results") if r["tool"] == "independent_tests"]) == 3


def test_a_graph_with_nothing_to_assert_on_is_declined(author):
    """Guessing at an exam for a graph that names no output would be worse than not
    marking it."""
    empty = {"nodes": [{"id": "i", "type": "inputNode", "content": {}}], "edges": []}

    with corpus.run_scope("r1", thread_id="t"):
        assert verify.verify(empty, REQUIREMENT) is None
    assert author["n"] == 0, "no call was made"


# --------------------------------------------------------------- the frozen exam

def test_a_blank_suite_can_still_be_filled_in():
    assert agent._has_cases("[]") is False
    assert agent._has_cases('[{"name": "a"}]') is True


def test_a_repair_cannot_rewrite_the_exam_it_is_judged_by(monkeypatch, tmp_path):
    """A repair that emits new tests used to *replace* the suite it was being marked
    against, so a model that could not fix the graph could pass by weakening the
    assertions - and that run was recorded as a success.
    """
    from backend.tests.test_agent_flow import BROKEN_DSL, GOOD_DSL, planner_payload

    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))
    # The planner sets a real exam. The repair keeps the broken graph and ships a suite
    # that agrees with it - the exact move being closed off.
    weakened = json.dumps([{"name": "anything", "input": {"orderTotal": 1},
                            "expectedOutput": {"shippingFee": 6}}])
    seen = {"n": 0}

    def dispatch(sys_prompt, messages):
        if sys_prompt is agent.PROMPT_INTENT:
            return '{"intent": "CREATE", "confidence": 1.0}'
        if sys_prompt is agent.PROMPT_PLANNER:
            return planner_payload(BROKEN_DSL)
        if sys_prompt is agent.PROMPT_BUILDER:
            seen["n"] += 1
            return ("---USECASE NAME STARTS---\nShipping\n---USECASE NAME ENDS---\n"
                    f"---DSL STARTS---\n{BROKEN_DSL}\n---DSL ENDS---\n"
                    f"---TESTS STARTS---\n{weakened}\n---TESTS ENDS---\n")
        return json.dumps({"status": "READY_FOR_APPROVAL", "message": "ok",
                           "options": ["Approve with above understanding & assumptions"]})

    monkeypatch.setattr(agent, "_dispatch", dispatch)

    from langchain_core.messages import HumanMessage
    from langgraph.types import Command
    graph = agent.build_graph()
    config = {"configurable": {"thread_id": "freeze-1"}}

    async def turn():
        with corpus.run_scope("r-freeze", thread_id="t"):
            async for _ in graph.astream(
                {"messages": [HumanMessage(content=REQUIREMENT)], "canvas_jdm_json": ""},
                config=config):
                pass
            async for _ in graph.astream(
                Command(resume="Approve with above understanding & assumptions"),
                config=config):
                pass
            return (await graph.aget_state(config)).values

    values = asyncio.run(turn())

    assert seen["n"] > 0, "the builder did attempt a repair"
    assert values.get("build_status") != "SUCCESS", (
        "weakening the exam must not buy a pass"
    )
    refused = [r for r in rows("tool_results") if r["tool"] == "rewrite_tests"]
    assert refused and refused[0]["ok"] == 0, (
        "and reaching for the exam is counted, because it is its own failure mode"
    )


# ---------------------------------------------------------------- concurrency

def test_the_harness_runs_more_than_one_at_a_time(monkeypatch, tmp_path):
    """Sequentially, 1,500 requirements at ~30s each is 12.5 hours."""
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))
    live = {"now": 0, "peak": 0}

    async def slow(graph, requirement, **kw):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return replay.Outcome(requirement.id, "run", "built")

    monkeypatch.setattr(replay, "_run_one", slow)
    reqs = [replay.Requirement(id=f"r{i}", text="x") for i in range(6)]

    started = time.monotonic()
    outcomes = asyncio.run(replay.replay(reqs, replay.Budget(), concurrency=3))
    elapsed = time.monotonic() - started

    assert len(outcomes) == 6
    assert live["peak"] == 3, "three in flight, and not more than asked for"
    assert elapsed < 0.05 * 6, "which is the point - it did not run them end to end"


def test_concurrent_runs_do_not_share_a_corpus_scope(monkeypatch, tmp_path):
    """`run_scope` rides a context variable. If asyncio did not copy it per task, every
    sample would land under whichever run happened to start last."""
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    async def record(graph, requirement, **kw):
        with corpus.run_scope(f"run-{requirement.id}", thread_id="t", source="replay"):
            await asyncio.sleep(0.02)
            corpus.record_llm_call(node="planner_node", system_prompt="SYS",
                                   messages=[{"role": "user", "content": requirement.id}],
                                   completion="a design")
            corpus.observe(outcome="completed")
        return replay.Outcome(requirement.id, f"run-{requirement.id}", "built")

    monkeypatch.setattr(replay, "_run_one", record)
    reqs = [replay.Requirement(id=f"r{i}", text="x") for i in range(4)]
    asyncio.run(replay.replay(reqs, replay.Budget(), concurrency=4))

    pairs = {(json.loads(s["messages_json"])[0]["content"], s["run_id"])
             for s in rows("samples")}
    assert pairs == {(f"r{i}", f"run-r{i}") for i in range(4)}


def test_the_budget_still_stops_a_concurrent_run(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    async def quick(graph, requirement, **kw):
        return replay.Outcome(requirement.id, "run", "built")

    monkeypatch.setattr(replay, "_run_one", quick)
    reqs = [replay.Requirement(id=f"r{i}", text="x") for i in range(10)]
    budget = replay.Budget(max_runs=3)

    outcomes = asyncio.run(replay.replay(reqs, budget, concurrency=2))

    built = [o for o in outcomes if o.status == "built"]
    # The budget is checked as each run starts, so it can overshoot by up to the
    # concurrency - bounded, and documented on the flag.
    assert 3 <= len(built) <= 3 + 2
    assert any(o.status == "skipped" for o in outcomes)


# ------------------------------------------------------------- the short prompt

def test_the_export_can_swap_the_prompt_it_trains_against(tmp_path):
    """Collect with the 11,093-token instruction because the teacher needs it; train with
    a 234-token one, because a fine-tuned model has the format in its weights."""
    long_prompt = "A" * 40_000
    with corpus.run_scope("r1", thread_id="t"):
        corpus.record_llm_call(node="planner_node", system_prompt=long_prompt,
                               messages=[{"role": "user", "content": REQUIREMENT}],
                               completion="---DSL STARTS---...")
        store.record_tool_result(tool="parse_dsl", node="builder_node", ok=True,
                                 run_id="r1", sample_id=rows("samples")[0]["sample_id"])

    from backend.corpus import score
    score.score_all()
    conn = store._connect()

    as_recorded, = export.export_sft(conn, export.Filters())
    assert as_recorded["messages"][0]["content"] == long_prompt

    as_trained, = export.export_sft(conn, export.Filters(system="Design JDM graphs."))
    assert as_trained["messages"][0]["content"] == "Design JDM graphs."
    assert as_trained["messages"][-1] == as_recorded["messages"][-1], (
        "the target is unchanged - only the input shrinks"
    )


def test_the_short_prompt_is_actually_short():
    from backend.prompts.planner_node_prompt import PROMPT_PLANNER
    from backend.prompts.planner_short_prompt import PROMPT_PLANNER_SHORT

    assert len(PROMPT_PLANNER_SHORT) < len(PROMPT_PLANNER) / 20
    # It still has to name the contract, or the examples have nothing to anchor to.
    for marker in ("---DSL STARTS---", "---TESTS STARTS---", "---USECASE NAME STARTS---"):
        assert marker in PROMPT_PLANNER_SHORT


# ------------------------------------------------------------------- comparing

def test_prompt_generations_can_be_compared(capsys):
    """The pass rate says whether an edit helped; the failure breakdown says what it
    traded for what."""
    for prompt, ok, code in (("v1", False, "MONOLITHIC_GRAPH"), ("v1", False, "MONOLITHIC_GRAPH"),
                             ("v2 with more guidance", True, None),
                             ("v2 with more guidance", False, "DSL_ERROR")):
        rid = corpus.new_run_id()
        with corpus.run_scope(rid, thread_id="t"):
            sid = corpus.record_llm_call(node="planner_node", system_prompt=prompt,
                                         messages=[{"role": "user", "content": "x"}],
                                         completion="out")
            store.record_tool_result(tool="lint", node="builder_node", ok=ok, run_id=rid,
                                     sample_id=sid,
                                     diagnostics=[{"code": code}] if code else None)

    export.print_compare("planner_node")
    printed = capsys.readouterr().out

    assert "50.0%" in printed and "0.0%" in printed, "both generations scored"
    assert "MONOLITHIC_GRAPH" in printed and "DSL_ERROR" in printed, "and what each got wrong"


def test_comparing_says_so_when_there_is_only_one_generation(capsys):
    rid = corpus.new_run_id()
    with corpus.run_scope(rid, thread_id="t"):
        sid = corpus.record_llm_call(node="planner_node", system_prompt="only one",
                                     messages=[{"role": "user", "content": "x"}],
                                     completion="out")
        store.record_tool_result(tool="lint", node="builder_node", ok=True,
                                 run_id=rid, sample_id=sid)

    export.print_compare()

    assert "Only one generation" in capsys.readouterr().out


def test_an_expression_graph_still_offers_input_names():
    """Expression nodes declare only what they write. This codebase builds far more of
    them than tables, so what they read has to be recovered from the expression text or an
    expression-only graph leaves the author with nothing to work from."""
    g = {"nodes": [{"id": "f", "type": "expressionNode", "content": {"expressions": [
        {"key": "shippingFee", "value": "orderTotal > 50 ? 0 : 6"},
        {"key": "total", "value": "round(shippingFee + surcharge, 2)"}]}}]}

    reads, writes = verify.interface_of(g)

    assert reads == ["orderTotal", "surcharge"]
    assert writes == ["shippingFee", "total"]
    assert "round" not in reads, "a function call is not a field"
    assert "shippingFee" not in reads, "nor is a value the graph produces itself"


def test_string_literals_are_not_mistaken_for_fields():
    """`condition == 'Digital'` names one field, not two - and a table full of string
    cells otherwise yields a field list made mostly of its own answers."""
    g = {"nodes": [{"id": "f", "type": "expressionNode", "content": {"expressions": [
        {"key": "band", "value": "weight > 5 ? 'heavy' : 'light'"}]}}]}

    assert verify.interface_of(g)[0] == ["weight"]


def test_a_rules_row_id_is_not_read_as_six_fields():
    """`_id` holds a UUID, and splitting one on its hyphens yields half a dozen things
    that look exactly like field names."""
    g = {"nodes": [{"id": "t", "type": "decisionTableNode", "content": {
        "inputs": [{"field": "score"}], "outputs": [{"field": "tier"}],
        "rules": [{"_id": "a0bc2222-a807-4e2f-88c3-ab537eb2ae2f", "c1": "> 700"}]}}]}

    reads, writes = verify.interface_of(g)

    assert reads == ["score"]
    assert writes == ["tier"]


@pytest.mark.parametrize("name", ["RefundPolicy", "LoanApprovalPolicy"])
def test_the_shipped_graphs_yield_a_usable_interface(name):
    """The end-to-end check on extraction: real graphs, no junk."""
    with open(f"backend/jdm_graphs/{name}_jdm.json") as handle:
        reads, writes = verify.interface_of(json.load(handle))

    assert reads and writes
    for field in reads + writes:
        assert field.isidentifier(), f"{field!r} is not a field name"
        assert len(field) > 2, f"{field!r} looks like a fragment"
