"""End-to-end tests for the refactored agent graph.

The LLM is stubbed, so these run offline and assert the wiring: intent routing,
the interrupt/resume protocol, and the builder's self-healing loop.
"""

from __future__ import annotations

import json
import uuid

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend import lang_graph_agent as agent

GOOD_DSL = """# Structure

```mermaid
flowchart LR
  Request --> Fee
  Fee --> Response
```

# Nodes

## Request
type: input

## Fee
type: expression

```expressions
shippingFee = orderTotal > 50 ? 0 : 6
```

## Response
type: output
"""

# Same graph, but the threshold is wrong so the suite fails on the first pass.
BROKEN_DSL = GOOD_DSL.replace("orderTotal > 50 ? 0 : 6", "orderTotal > 500 ? 0 : 6")

TESTS = [
    {"name": "free over 50", "input": {"orderTotal": 80}, "expectedOutput": {"shippingFee": 0}},
    {"name": "paid under 50", "input": {"orderTotal": 20}, "expectedOutput": {"shippingFee": 6}},
]


def planner_payload(dsl: str) -> str:
    return (
        "---USECASE NAME STARTS---\nShipping Fees\n---USECASE NAME ENDS---\n"
        f"---DSL STARTS---\n{dsl}\n---DSL ENDS---\n"
        f"---TESTS STARTS---\n{json.dumps(TESTS)}\n---TESTS ENDS---\n"
    )


class FakeLLM:
    """Dispatches on the system prompt, and records what was asked."""

    def __init__(self, dsl_sequence: list[str]):
        self.dsl_sequence = list(dsl_sequence)
        self.calls: list[str] = []
        # What each call said it was. `call_llm` takes this so every model call can be
        # attributed to the node that made it; recording it here lets a test assert the
        # attribution without standing up a provider.
        self.nodes: list[str] = []

    def __call__(self, sys_prompt: str, messages: list, *, node: str = "unknown",
                 attempt: int = 1, purpose: str = "") -> str:
        self.nodes.append(node)
        # Match on prompt identity; substring sniffing is too fragile because
        # these prompts share a lot of vocabulary.
        if sys_prompt is agent.PROMPT_INTENT:
            self.calls.append("intent")
            return '{"intent": "MODIFY", "confidence": 0.9}'

        if sys_prompt in (agent.PROMPT_PLANNER, agent.PROMPT_BUILDER):
            self.calls.append("planner/builder")
            dsl = self.dsl_sequence.pop(0) if self.dsl_sequence else GOOD_DSL
            return planner_payload(dsl)

        if sys_prompt is agent.PROMPT_TEST:
            self.calls.append("test-gen")
            return f"---TESTS STARTS---\n{json.dumps(TESTS)}\n---TESTS ENDS---"

        self.calls.append("triage")
        return json.dumps(
            {
                "status": "READY_FOR_APPROVAL",
                "message": "Understood: free shipping over $50, otherwise $6.",
                "options": ["Approve with above understanding & assumptions", "Custom clarification"],
            }
        )


@pytest.fixture
def stub_llm(monkeypatch):
    def install(dsl_sequence: list[str]) -> FakeLLM:
        fake = FakeLLM(dsl_sequence)
        monkeypatch.setattr(agent, "call_llm", fake)
        return fake

    return install


def new_thread() -> dict:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def drain(graph, payload, config) -> None:
    for _ in graph.stream(payload, config=config):
        pass


def pending_interrupt(graph, config) -> dict | None:
    state = graph.get_state(config)
    if state.tasks and getattr(state.tasks[0], "interrupts", None):
        return state.tasks[0].interrupts[0].value
    return None


# --------------------------------------------------------------------------

def test_create_flow_reaches_save(stub_llm, tmp_path, monkeypatch):
    """CREATE -> triage -> approve -> planner -> builder -> approve -> save."""
    stub_llm([GOOD_DSL])
    monkeypatch.setattr(agent, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    graph = agent.build_graph()
    config = new_thread()

    drain(
        graph,
        {
            "messages": [HumanMessage(content="Create a shipping policy: free over $50, else $6.")],
            "canvas_jdm_json": "",
        },
        config,
    )

    # Triage pauses for approval.
    payload = pending_interrupt(graph, config)
    assert payload is not None
    assert "Approve with above understanding & assumptions" in payload["options"]
    assert graph.get_state(config).values["intent"] == "CREATE"

    drain(graph, Command(resume="Approve with above understanding & assumptions"), config)

    # Second gate: the built graph is offered for final approval.
    payload = pending_interrupt(graph, config)
    assert payload is not None
    assert "Approve & Save" in payload["options"]

    values = graph.get_state(config).values
    assert values["build_status"] == "SUCCESS"
    assert json.loads(values["jdm_json"])["nodes"]

    drain(graph, Command(resume="Approve & Save"), config)

    saved = tmp_path / "backend" / "jdm_graphs" / "Shipping_Fees_jdm.json"
    assert saved.is_file(), "save_files_node must write under the repo root, not the cwd"
    assert (tmp_path / "backend" / "jdm_tests" / "Shipping_Fees_tests.json").is_file()


def test_builder_self_heals_failing_tests(stub_llm, tmp_path, monkeypatch):
    """A graph that compiles but fails its assertions must be retried, not accepted."""
    fake = stub_llm([BROKEN_DSL, GOOD_DSL])
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    graph = agent.build_graph()
    config = new_thread()

    drain(
        graph,
        {"messages": [HumanMessage(content="Create a shipping policy.")], "canvas_jdm_json": ""},
        config,
    )
    drain(graph, Command(resume="Approve with above understanding & assumptions"), config)

    values = graph.get_state(config).values
    assert values["build_status"] == "SUCCESS"
    # The first DSL compiled fine but failed its expectations; the loop had to
    # call the LLM again to fix it.
    assert fake.calls.count("planner/builder") >= 2


def test_modify_intent_routes_to_modify_triage(stub_llm):
    """A request against a populated canvas goes to modify_triage, not triage."""
    stub_llm([GOOD_DSL])
    canvas = open("backend/jdm_graphs/RefundPolicy_jdm.json", encoding="utf-8").read()

    graph = agent.build_graph()
    config = new_thread()

    drain(
        graph,
        {
            "messages": [HumanMessage(content="add a rule for VIP customers")],
            "canvas_jdm_json": canvas,
            "canvas_graph_name": "Refund Policy",
        },
        config,
    )

    values = graph.get_state(config).values
    assert values["intent"] == "MODIFY"
    assert values["mode"] == "EXISTING"
    # The canvas is what downstream prompts see.
    assert values["existing_jdm_json"] == canvas
    assert pending_interrupt(graph, config) is not None


def test_test_intent_runs_saved_suite(stub_llm):
    """TEST runs the supplied suite and reports a deterministic verdict."""
    stub_llm([])
    canvas = open("backend/jdm_graphs/LoanApprovalPolicy_jdm.json", encoding="utf-8").read()
    tests = open("backend/jdm_tests/LoanApprovalPolicy_tests.json", encoding="utf-8").read()

    graph = agent.build_graph()
    config = new_thread()

    drain(
        graph,
        {
            "messages": [HumanMessage(content="run the tests")],
            "canvas_jdm_json": canvas,
            "canvas_graph_name": "Loan Approval Policy",
            "test_suite_json": tests,
        },
        config,
    )

    values = graph.get_state(config).values
    assert values["intent"] == "TEST"
    summary = json.loads(values["evaluation_feedback"])
    # The shipped policy passes its own suite. It used to fail all 11 - every output cell
    # was an unquoted label, so the fields were silently dropped - which the linter found
    # and this fixture now guards against regressing.
    assert summary["passed"] == summary["total"] > 0
    assert summary["failed"] == 0

    report = values["messages"][-1].content
    assert "tests passed" in report and "✅" in report


def test_two_replies_in_a_row_do_not_collide(stub_llm):
    """The clarification path resumes twice against one checkpoint.

    The triage gate pauses for a chip; choosing "Custom clarification" pauses
    again for free text. Every resume carries the live canvas with it, and both
    land on the *same* checkpoint, so LangGraph accumulates two writes per
    canvas key into a single step. With plain channels the second reply died
    with INVALID_CONCURRENT_GRAPH_UPDATE; the reducer keeps the newer canvas.
    """
    stub_llm([GOOD_DSL])
    graph = agent.build_graph()
    config = new_thread()

    canvas = {
        "canvas_jdm_json": "",
        "canvas_graph_id": "",
        "canvas_graph_name": "",
        "cancel_requested": False,
    }
    drain(
        graph,
        {"messages": [HumanMessage(content="Create a shipping policy.")], **canvas},
        config,
    )

    # First reply: the chip that asks for a text box.
    drain(graph, Command(resume="Custom clarification", update=dict(canvas)), config)
    payload = pending_interrupt(graph, config)
    assert payload is not None
    assert payload["options"] == [], "the second pause must be a free-text prompt"

    # Second reply, on the same checkpoint, with the canvas as it now stands.
    edited = {**canvas, "canvas_jdm_json": '{"nodes": [], "edges": []}'}
    drain(graph, Command(resume="Also charge $12 to PO boxes", update=edited), config)

    values = graph.get_state(config).values
    assert values["canvas_jdm_json"] == '{"nodes": [], "edges": []}', "newest canvas wins"
    assert any(
        getattr(m, "content", "") == "Also charge $12 to PO boxes" for m in values["messages"]
    ), "the typed clarification must reach the agent"


def test_intent_router_never_interrupts(stub_llm):
    """The entry node must do work immediately rather than pausing for input."""
    stub_llm([GOOD_DSL])
    graph = agent.build_graph()
    config = new_thread()

    drain(graph, {"messages": [HumanMessage(content="Create a policy")], "canvas_jdm_json": ""}, config)

    payload = pending_interrupt(graph, config)
    # The first pause belongs to triage, not to a welcome or action-selection chip.
    assert payload is not None
    assert "Welcome" not in payload["prompt"]
    assert graph.get_state(config).next != ("intent_router_node",)


def test_every_key_sent_on_a_resume_can_be_written_twice():
    """A resume payload key without a reducer is a latent INVALID_CONCURRENT_GRAPH_UPDATE.

    A node that pauses twice - the clarification chip, then the free-text box - is resumed
    twice against the *same* checkpoint, and LangGraph accumulates both writes into one
    step. A plain last-value channel rejects that. This guards the whole payload rather
    than the keys that happened to break, so adding a new one cannot quietly reintroduce it.
    """
    from typing import Annotated, get_args, get_origin, get_type_hints

    from backend.services import chat_runner

    class Canvas:
        content = {"nodes": [], "edges": []}
        graph_id = "g"
        name = "n"

    carried = set(chat_runner._canvas_state(Canvas())) | {"thread_id"}
    hints = get_type_hints(agent.AgentState, include_extras=True)

    unguarded = [
        key for key in sorted(carried)
        if get_origin(hints.get(key)) is not Annotated or len(get_args(hints[key])) < 2
    ]
    assert not unguarded, (
        f"these keys travel on every resume but have no reducer: {unguarded}. "
        "Annotate them with `_latest`."
    )


# --------------------------------------------------------------------------
# A plan that never arrived
#
# The edge from planner to builder used to be unconditional, so a reply with no design in
# it went straight on and the builder spent its whole repair budget asking the model to fix
# a document that was never written - then reported a *build* failure for what was really a
# planning one. That is the "cannot generate even after multiple tries" case.
# --------------------------------------------------------------------------

NO_PLAN_PROSE = (
    "This requirement is quite involved. Before I can design the graph I would need to "
    "know how the tiers interact with the regional overrides."
)


class PlannerFake(FakeLLM):
    """Answers the planner and the builder differently.

    `FakeLLM` deliberately treats them as one prompt, because in the normal flow they
    produce the same document. These tests are about a planner reply the builder never gets
    to see, so the two have to be separable - and the planner's replies go through raw,
    since the point is that they are not in the expected format.
    """

    def __init__(self, planner_replies: list[str]):
        super().__init__([])
        self.planner_replies = list(planner_replies)
        self.planner_prompts: list[list] = []

    def __call__(self, sys_prompt: str, messages: list, *, node: str = "unknown",
                 attempt: int = 1, purpose: str = "") -> str:
        if sys_prompt is agent.PROMPT_PLANNER:
            self.nodes.append(node)
            self.calls.append("planner")
            self.planner_prompts.append(list(messages))
            return self.planner_replies.pop(0) if self.planner_replies else ""
        if sys_prompt is agent.PROMPT_BUILDER:
            self.nodes.append(node)
            self.calls.append("builder")
            return planner_payload(GOOD_DSL)
        return super().__call__(sys_prompt, messages, node=node, attempt=attempt,
                                purpose=purpose)


def approved_run(monkeypatch, fake, tmp_path):
    """Drive a CREATE turn through triage approval and into planning."""
    monkeypatch.setattr(agent, "call_llm", fake)
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    graph = agent.build_graph()
    config = new_thread()
    drain(
        graph,
        {"messages": [HumanMessage(content="Build something complicated.")],
         "canvas_jdm_json": ""},
        config,
    )
    drain(graph, Command(resume="Approve with above understanding & assumptions"), config)
    return graph, config


@pytest.mark.parametrize("reply,label", [("", "an empty reply"), (NO_PLAN_PROSE, "prose")])
def test_a_planner_that_produces_no_design_never_starts_a_build(
    reply, label, monkeypatch, tmp_path
):
    fake = PlannerFake([reply, reply])
    graph, config = approved_run(monkeypatch, fake, tmp_path)

    assert "builder" not in fake.calls, (
        f"{label} is not something the builder can repair; entering it burns the "
        "whole budget discovering that"
    )
    values = graph.get_state(config).values
    assert values["plan_status"] == "EMPTY"
    assert values["build_failed"] is True
    # No build ran, so there is no build status to report - which is the point. The turn
    # must not end up on the approval gate offering a graph that was never made.
    assert "build_status" not in values
    assert not values.get("jdm_json")
    assert pending_interrupt(graph, config) is None


def test_the_second_attempt_is_told_why_the_first_was_rejected(monkeypatch, tmp_path):
    """At temperature 0 an unchanged prompt returns an unchanged answer, so a bare retry
    would reproduce the same non-answer. The added instruction is the whole point of it."""
    fake = PlannerFake(["", ""])
    approved_run(monkeypatch, fake, tmp_path)

    assert len(fake.planner_prompts) == 2
    first, second = (" ".join(str(m.content) for m in p) for p in fake.planner_prompts)

    assert "NO_PLAN" in second
    assert "# Structure" in second, "it must say what the missing shape looks like"
    assert first != second, "an identical retry at temperature 0 is a wasted call"


def test_replanning_is_bounded(monkeypatch, tmp_path):
    fake = PlannerFake(["", "", "", ""])
    graph, config = approved_run(monkeypatch, fake, tmp_path)

    assert fake.calls.count("planner") == agent.MAX_PLAN_ATTEMPTS
    assert graph.get_state(config).values["plan_attempts_used"] == agent.MAX_PLAN_ATTEMPTS


def test_a_successful_replan_goes_on_to_build(monkeypatch, tmp_path):
    """The retry has to actually be worth making."""
    fake = PlannerFake([NO_PLAN_PROSE, planner_payload(GOOD_DSL)])
    graph, config = approved_run(monkeypatch, fake, tmp_path)

    values = graph.get_state(config).values
    assert fake.calls.count("planner") == 2
    assert values["plan_status"] == "OK"
    assert values["build_status"] == "SUCCESS"
    assert json.loads(values["jdm_json"])["nodes"]


def test_a_failed_turn_does_not_leave_the_previous_graph_in_state(monkeypatch, tmp_path):
    """`jdm_json` is what the studio announces as a proposal. A turn that designed nothing
    must not hand back the last turn's policy as though it had just produced it."""
    monkeypatch.setattr(agent, "call_llm", PlannerFake([NO_PLAN_PROSE]))

    result = agent.planner_node({
        "messages": [HumanMessage(content="Build something complicated.")],
        "jdm_json": '{"nodes": [{"id": "stale"}], "edges": []}',
    })

    assert result["plan_status"] == "EMPTY"
    assert result["jdm_json"] == ""


def test_a_fresh_design_does_not_inherit_a_spent_retry_budget(monkeypatch):
    """A rejected final approval re-enters the planner. That is a new attempt at a design,
    not a third try at a failed one, and it must not start out of budget."""
    monkeypatch.setattr(agent, "call_llm", PlannerFake([planner_payload(GOOD_DSL)]))

    result = agent.planner_node({
        "messages": [HumanMessage(content="Try again, differently.")],
        "plan_status": "OK",
        "plan_attempts_used": agent.MAX_PLAN_ATTEMPTS,
    })

    assert result["plan_attempts_used"] == 1


def test_the_router_sends_a_design_on_and_holds_a_non_design_back():
    assert agent.route_after_planner({"plan_status": "OK"}) == "builder_node"
    assert agent.route_after_planner(
        {"plan_status": "EMPTY", "plan_attempts_used": 1}) == "planner_node"
    assert agent.route_after_planner(
        {"plan_status": "EMPTY", "plan_attempts_used": agent.MAX_PLAN_ATTEMPTS}) == "output_node"
    # An older thread, checkpointed before `plan_status` existed, still has a plan.
    assert agent.route_after_planner({}) == "builder_node"


@pytest.mark.parametrize("planner_calls", range(1, 4))
def test_a_replan_shrinks_the_repair_budget_it_shares_a_clock_with(planner_calls):
    """Every planner call is one fewer builder call the run can afford. Sizing the loop as
    though planning were always a single call is how a run gets killed mid-repair.

    Deliberately parametrised past `MAX_PLAN_ATTEMPTS`: this asserts the arithmetic holds
    for the contract, so raising that cap cannot quietly overrun the run's wall clock.
    """
    budget = agent._max_build_attempts(planner_calls)
    spent = (planner_calls + budget - 1) * agent.LLM_TIMEOUT_SECONDS

    assert spent < agent.AGENT_RUN_TIMEOUT_SECONDS
    assert budget <= agent._max_build_attempts(1)


def test_a_turn_with_no_design_reports_the_step_that_actually_failed():
    body = agent.output_node({
        "plan_status": "EMPTY",
        "usecase_name": "Tier Pricing",
        "evaluation_feedback": "The planner replied without a design:\n\nI would need to know...",
    })["messages"][0].content

    assert "planning step" in body
    assert "What the planner returned:" in body
    assert "I would need to know" in body, "its own words say more than a summary would"
    assert "test cases" not in body, "that describes a build failure; the build never ran"
