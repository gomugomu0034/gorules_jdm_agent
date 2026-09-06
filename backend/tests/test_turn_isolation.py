"""What a finished turn must not leave behind for the next one.

Routing between turns was never the problem - a graph that has reached END starts again
from START on the next message, and every turn already re-enters the router. What did not
happen was any clearing of the *verdict*: after a build-approve-save, the checkpoint still
held `build_status='SUCCESS'`, `final_approval_status='APPROVED'` and the previous turn's
`jdm_json`, and the next turn inherited all of it.

That matters most where a value is read with a forgiving default. `output_node` treats a
missing `build_status` as SUCCESS, so a stale one is the difference between "your policy is
ready" and the truth.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from backend import lang_graph_agent as agent
from backend.tests.test_agent_flow import GOOD_DSL, planner_payload

APPROVE_PLAN = "Approve with above understanding & assumptions"

# The second turn carries a policy on the canvas, so it routes to EXPLAIN - a read-only
# intent that writes none of the reset fields. With an empty canvas the router settles on
# CREATE, triage runs, and `triage_status` is legitimately repopulated by the new turn,
# which is correct behaviour and useless for telling stale from fresh.
OPEN_POLICY = json.dumps({
    "contentType": "application/vnd.gorules.decision",
    "nodes": [
        {"id": "i", "name": "Request", "type": "inputNode", "content": {"schema": ""},
         "position": {"x": 0, "y": 0}},
        {"id": "o", "name": "Response", "type": "outputNode", "content": {"schema": ""},
         "position": {"x": 2, "y": 0}},
    ],
    "edges": [{"id": "e", "sourceId": "i", "targetId": "o", "type": "edge"}],
})


@pytest.fixture
def build_once(monkeypatch, tmp_path):
    """Drive one full CREATE turn to a saved policy, and hand back the graph and config so
    a second turn can be run on the same thread."""
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    def dispatch(sys_prompt, messages):
        if sys_prompt is agent.PROMPT_INTENT:
            return '{"intent": "CREATE", "confidence": 1.0}'
        if sys_prompt in (agent.PROMPT_PLANNER, agent.PROMPT_BUILDER):
            return planner_payload(GOOD_DSL)
        return json.dumps({"status": "READY_FOR_APPROVAL", "message": "Understood.",
                           "options": [APPROVE_PLAN]})

    monkeypatch.setattr(agent, "_dispatch", dispatch)
    graph = agent.build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def drive(payload):
        async for _ in graph.astream(payload, config=config):
            pass

    async def first_turn():
        await drive({"messages": [HumanMessage(content="Free shipping over $50")],
                     "canvas_jdm_json": ""})
        await drive(Command(resume=APPROVE_PLAN))
        await drive(Command(resume="Approve & Save"))
        return (await graph.aget_state(config)).values

    return graph, config, drive, asyncio.run(first_turn())


def test_the_first_turn_really_does_leave_a_verdict_behind(build_once):
    """The premise. Without this the rest of the file is testing nothing."""
    _, _, _, values = build_once

    assert values["build_status"] == "SUCCESS"
    assert values["final_approval_status"] == "APPROVED"
    assert values["build_attempts_used"] >= 1
    assert values["jdm_json"], "and the artifact it made"


@pytest.mark.parametrize("field,stale", [
    ("build_status", "SUCCESS"),
    ("final_approval_status", "APPROVED"),
    ("triage_status", "APPROVED"),
    ("jdm_json", ""),
    ("usecase_name", ""),
])
def test_the_next_turn_starts_without_the_last_ones_verdict(build_once, field, stale):
    graph, config, drive, _ = build_once

    asyncio.run(drive({"messages": [HumanMessage(content="Now explain this policy")],
                       "canvas_jdm_json": OPEN_POLICY}))
    values = asyncio.run(graph.aget_state(config)).values

    assert not values.get(field), f"{field} survived the turn boundary"


def test_the_repair_budget_starts_full_on_a_new_turn(build_once):
    """Nothing reset this between turns, so a second build began one repair down and a
    third began two - a quota shrinking for no reason anybody could see."""
    graph, config, drive, first = build_once
    assert first["build_attempts_used"] >= 1

    asyncio.run(drive({"messages": [HumanMessage(content="Explain it")],
                       "canvas_jdm_json": OPEN_POLICY}))

    assert asyncio.run(graph.aget_state(config)).values["build_attempts_used"] == 0


def test_a_stale_empty_plan_does_not_make_a_fresh_turn_think_it_is_retrying():
    """`planner_node` reads plan_status == EMPTY as "you are being asked again" and appends
    the re-plan instruction. Carried across a turn, the first call of a new request would
    open by being told its previous answer was unusable."""
    reset = agent._PER_TURN_RESET

    assert reset["plan_status"] == ""
    assert reset["plan_attempts_used"] == 0


def test_findings_from_the_previous_graph_do_not_ride_along(build_once):
    graph, config, drive, _ = build_once

    asyncio.run(drive({"messages": [HumanMessage(content="Explain it")],
                       "canvas_jdm_json": OPEN_POLICY}))
    values = asyncio.run(graph.aget_state(config)).values

    assert values.get("lint_findings") == []
    assert values.get("patch_log") == []
    assert values.get("test_regressions") == []


# ------------------------------------------------------------------ what must survive

def test_the_pre_loaded_test_suite_is_not_wiped_before_the_test_node_sees_it():
    """`chat_runner` puts the saved suite in the payload for a TEST turn. The router runs
    after that update is applied, so clearing the field here would overwrite the suite with
    nothing and the node would silently generate a fresh one instead of running theirs.
    """
    assert "test_suite_json" not in agent._PER_TURN_RESET


@pytest.mark.parametrize("field", [
    "messages", "canvas_jdm_json", "canvas_graph_id", "canvas_graph_name",
    "thread_id", "cancel_requested",
])
def test_context_is_not_cleared_only_verdict(field):
    """These are what the turn is about, not what a previous one concluded."""
    assert field not in agent._PER_TURN_RESET


def test_the_router_still_reports_what_it_decided():
    """The reset is merged under the router's own return, not over it."""
    result = agent.intent_router_node({
        "messages": [HumanMessage(content="explain this")],
        "canvas_jdm_json": json.dumps({
            "nodes": [{"id": "i", "type": "inputNode", "name": "In", "content": {}},
                      {"id": "o", "type": "outputNode", "name": "Out", "content": {}}],
            "edges": [{"id": "e", "sourceId": "i", "targetId": "o"}]}),
        "build_status": "SUCCESS",
    })

    assert result["intent"] == "EXPLAIN"
    assert result["existing_jdm_json"], "the canvas is context and survives"
    assert result["build_status"] == "", "the verdict does not"
