"""Regression tests for the plan -> DSL -> JDM pipeline.

Each test here pins one defect that used to fail *silently*: the pipeline carried on and
produced a well-formed but wrong graph, so the builder's repair loop never engaged and the
user saw "could not build a working graph" after eight attempts that never had a chance.

The through-line: anything the parser cannot faithfully represent must raise, and anything
the model plausibly emits must be extractable.
"""

from __future__ import annotations

import json

import pytest

from backend import lang_graph_agent as agent
from backend.prompts.builder_node_prompt import PROMPT_BUILDER
from backend.prompts.planner_node_prompt import PROMPT_PLANNER
from backend.tools.markdown_dsl_parser import DslError, parse_markdown_dsl
from backend.tools.zen_evaluator import evaluate_against_zen
from backend.tests.test_agent_flow import (
    BROKEN_DSL,
    GOOD_DSL,
    TESTS,
    drain,
    new_thread,
    planner_payload,
)


def last_block(text: str, start: str, end: str) -> str:
    """The final marker pair - the first mention is in the prompt's own instructions."""
    return text.rsplit(start, 1)[1].rsplit(end, 1)[0]


# --------------------------------------------------------------------------- the examples
# A few-shot example is executable specification: the model reproduces what it is shown, so
# an example that does not compile teaches a graph that does not compile.

def test_the_planner_example_compiles_completely():
    """The example used spaced backticks, so it parsed to 6 nodes and *zero* edges."""
    graph = parse_markdown_dsl(last_block(PROMPT_PLANNER, "---DSL STARTS---", "---DSL ENDS---"))

    assert len(graph["nodes"]) == 6
    assert len(graph["edges"]) == 5, "the mermaid fence must be readable, or there are no edges"

    by_type = {n["type"]: n["content"] for n in graph["nodes"]}
    assert len(by_type["expressionNode"]["expressions"]) == 2, "the ```expressions fence must parse"
    assert by_type["decisionTableNode"]["rules"], "the table must have rules"
    assert by_type["functionNode"]["source"], "function bodies were dropped entirely"
    assert by_type["decisionNode"]["key"] == "pricing/regional"

    # The two conditional branches must reach the engine as handles, or the switch is inert.
    assert sum(1 for e in graph["edges"] if e.get("sourceHandle")) == 2


def test_the_planner_example_tests_assert_something():
    """Cases without expectedOutput are skipped, so the old example proved nothing."""
    block = last_block(PROMPT_PLANNER, "---TESTS STARTS---", "---TESTS ENDS---")
    cases = json.loads(block.strip().removeprefix("```json").removesuffix("```").strip())

    assert cases
    for case in cases:
        assert case.keys() >= {"name", "input", "expectedOutput"}
        assert case["expectedOutput"], "an empty expectedOutput is skipped, not asserted"


def test_the_builder_documents_the_markers_the_extractor_reads():
    """The prompt asked for ```markdown + ```json; the extractor required three markers."""
    for marker in ("---DSL STARTS---", "---DSL ENDS---", "---TESTS STARTS---",
                   "---TESTS ENDS---", "---USECASE NAME STARTS---", "---USECASE NAME ENDS---"):
        assert marker in PROMPT_BUILDER


# --------------------------------------------------------------------------- extraction

def test_a_mermaid_fence_survives_extraction():
    """The stripper removed any leading fence, turning ```mermaid into the bare word."""
    dsl = "```mermaid\nflowchart LR\nA --> B\n```"
    assert agent._unwrap_fence(dsl, "markdown") == dsl


def test_a_wrapping_fence_is_removed_but_inner_ones_are_kept():
    dsl = "# Structure\n```mermaid\nflowchart LR\nA --> B\n```\n\n# Nodes"
    assert agent._unwrap_fence(f"```markdown\n{dsl}\n```", "markdown") == dsl


@pytest.mark.parametrize("reply", [
    # The contract.
    "---DSL STARTS---\n{dsl}\n---DSL ENDS---\n---TESTS STARTS---\n{tests}\n---TESTS ENDS---",
    # What the old builder prompt actually asked for - previously unparseable.
    "```markdown\n{dsl}\n```\n\n```json\n{tests}\n```",
    # Neither markers nor fences.
    "{dsl}\n\n{tests}",
])
def test_near_miss_formats_are_still_extractable(reply):
    """A small model follows a long format imperfectly; that must not cost an attempt."""
    tests = json.dumps(TESTS)
    dsl, extracted_tests, _ = agent._extract_plan_blocks(
        reply.format(dsl=GOOD_DSL.strip(), tests=tests)
    )
    assert parse_markdown_dsl(dsl)["edges"], "the DSL must survive round-tripping"
    assert json.loads(extracted_tests) == TESTS


# --------------------------------------------------------------------------- strict parsing

def test_an_empty_plan_raises():
    """It used to return a valid, empty graph - success with nothing in it."""
    with pytest.raises(DslError):
        parse_markdown_dsl("")


def test_broken_fences_raise_rather_than_yielding_an_edgeless_graph():
    with pytest.raises(DslError) as excinfo:
        parse_markdown_dsl(GOOD_DSL.replace("```", "` ` `"))
    assert "mermaid" in str(excinfo.value)


def test_a_misspelled_node_reference_raises():
    """It used to be invented as a phantom output node, silently disconnecting the graph."""
    with pytest.raises(DslError) as excinfo:
        parse_markdown_dsl(GOOD_DSL.replace("Fee --> Response", "Fee --> Respnose"))
    assert "Respnose" in str(excinfo.value)


def test_an_unknown_node_type_raises():
    """It used to fall through to inputNode, or straight into the engine unmapped."""
    with pytest.raises(DslError) as excinfo:
        parse_markdown_dsl(GOOD_DSL.replace("type: expression", "type: lookupTable"))
    assert "lookupTable" in str(excinfo.value)


def test_declared_but_unconnected_nodes_raise():
    structure = "# Structure\n\n```mermaid\nflowchart LR\n```\n"
    body = GOOD_DSL[GOOD_DSL.index("# Nodes"):]
    with pytest.raises(DslError):
        parse_markdown_dsl(structure + "\n" + body)


def test_the_error_names_every_problem_at_once():
    """One repair pass should be able to fix the whole document, not one fault per round."""
    broken = GOOD_DSL.replace("type: expression", "type: lookupTable").replace(
        "Fee --> Response", "Fee --> Respnose"
    )
    with pytest.raises(DslError) as excinfo:
        parse_markdown_dsl(broken)
    assert len(excinfo.value.problems) >= 2


def test_a_chain_of_arrows_yields_every_edge():
    """`A --> B --> C` used to produce one edge and drop the rest."""
    chained = GOOD_DSL.replace("  Request --> Fee\n  Fee --> Response",
                               "  Request --> Fee --> Response")
    assert len(parse_markdown_dsl(chained)["edges"]) == 2


# --------------------------------------------------------------------------- assertions

def test_a_suite_that_asserts_nothing_is_not_a_pass():
    """Bare inputs are skipped, so the build was declared SUCCESS having checked nothing."""
    graph = json.dumps(parse_markdown_dsl(GOOD_DSL))
    bare = [{"orderTotal": 80}, {"orderTotal": 20}]

    ok, feedback = evaluate_against_zen(graph, bare)

    assert not ok
    assert "expectedOutput" in feedback, "the feedback must say how to fix the suite"

    # The same graph with real assertions still passes, so this is not just a blanket reject.
    ok, _ = evaluate_against_zen(graph, TESTS)
    assert ok


# --------------------------------------------------------------------------- blind repair

def test_the_builder_can_see_the_plan_it_is_repairing(monkeypatch):
    """The planner's write to `messages` was commented out.

    Without it the first repair call receives only the triage conversation and a bare
    "SYSTEM ERROR", and is asked to fix a graph it was never shown - so it reconstructs one
    from scratch and usually reintroduces the same fault.
    """
    seen: list[list] = []
    plans = [BROKEN_DSL, GOOD_DSL]

    def fake_llm(sys_prompt, messages, **_attribution):
        if sys_prompt is agent.PROMPT_BUILDER:
            seen.append(list(messages))
            return planner_payload(plans.pop(0) if plans else GOOD_DSL)
        if sys_prompt is agent.PROMPT_PLANNER:
            return planner_payload(plans.pop(0) if plans else GOOD_DSL)
        if sys_prompt is agent.PROMPT_INTENT:
            return '{"intent": "CREATE", "confidence": 1.0}'
        return json.dumps({
            "status": "READY_FOR_APPROVAL",
            "message": "Understood.",
            "options": ["Approve with above understanding & assumptions"],
        })

    monkeypatch.setattr(agent, "call_llm", fake_llm)

    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    graph = agent.build_graph()
    config = new_thread()
    canvas = {"canvas_jdm_json": "", "canvas_graph_id": "",
              "canvas_graph_name": "", "cancel_requested": False}

    drain(graph, {"messages": [HumanMessage(content="Free shipping over $50, else $6.")],
                  **canvas}, config)
    drain(graph, Command(resume="Approve with above understanding & assumptions",
                         update=dict(canvas)), config)

    assert seen, "the broken plan must have driven the builder into a repair call"
    context = "\n".join(str(m.content) for m in seen[0])

    # Assert on text that *only* the plan carries. The failure report quotes field names
    # and expected values, so asserting on "shippingFee" alone would pass either way.
    assert "# Structure" in context, "the repair call cannot see the DSL it must fix"
    assert "orderTotal > 500" in context, "specifically, the faulty expression itself"

    # And it must be told what went wrong, in a form it can act on: the kind of failure,
    # and the node responsible. A bare stringified exception named neither.
    assert "THE GRAPH RUNS, BUT DECIDES THE WRONG THING" in context
    assert "WRONG_VALUE" in context
    assert 'in node "Fee"' in context, "the diagnostic must name the node that decided it"


def test_the_repair_context_stays_out_of_the_chat(monkeypatch):
    """The plan is retry context, not a chat message: it must be tagged internal."""
    from backend.services.chat_runner import _is_internal

    message = agent._assistant_message_from_llm(planner_payload(GOOD_DSL), internal=True)
    assert _is_internal(message)
