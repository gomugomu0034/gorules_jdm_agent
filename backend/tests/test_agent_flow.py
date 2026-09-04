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

    def __call__(self, sys_prompt: str, messages: list) -> str:
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
