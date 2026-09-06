"""Tests for the graph linter.

Two halves, and the second matters more. The first checks each rule fires on a graph that
genuinely has that fault. The second checks that correct graphs stay silent - a linter that
fires on working logic is worse than no linter, because in the build loop it sends the model
off to "fix" something that was never broken, and in the editor it teaches people to ignore
the panel.
"""

from __future__ import annotations

import json

import pytest

from backend.tools.jdm_linter import blocking, lint
from backend.tools.markdown_dsl_parser import parse_markdown_dsl
from backend.tools.zen_evaluator import run_test_suite
from backend.tests.test_agent_flow import GOOD_DSL

TABLE_DSL = """# Structure
```mermaid
flowchart LR
  Request --> ShippingBand
  ShippingBand --> Result
```

# Nodes
## Request
type: input

## ShippingBand
type: decisionTable
hitPolicy: first

| in weight [Weight] | out cost |
| --- | --- |
| < 1 | 5 |
| _ | 9 |

## Result
type: output
"""


@pytest.fixture
def graph():
    return parse_markdown_dsl(TABLE_DSL)


def codes(graph_dict) -> set[str]:
    return {d.code for d in lint(graph_dict)}


def table_of(graph_dict) -> dict:
    return next(n for n in graph_dict["nodes"] if n["type"] == "decisionTableNode")


def unquote_outputs(graph_dict) -> dict:
    """Reintroduce the bug the shipped examples used to have.

    Every output cell was an unquoted label. That is valid ZEN - a variable reference - so
    nothing raises; it resolves to null and the key is dropped. The graphs now ship fixed,
    so the broken shape is built here rather than depending on a policy staying broken.
    """
    for node in graph_dict["nodes"]:
        content = node.get("content") or {}
        for column in content.get("outputs") or []:
            for rule in content.get("rules") or []:
                value = (rule.get(column["id"]) or "").strip()
                if value.startswith("'") and value.endswith("'"):
                    rule[column["id"]] = value[1:-1]
    return graph_dict


# --------------------------------------------------------------------------- no false positives

def test_a_sound_graph_has_no_errors_or_warnings_worth_blocking(graph):
    """The bar every other test is measured against."""
    assert blocking(lint(graph)) == []


@pytest.mark.parametrize("cell", [
    ">= 100", "'US','CA'", "[1..10]", "len($) > 5", "> 10 and < 50",
    "'gold'", "!= 'new'", "in ['a','b']", "",
])
def test_valid_unary_cells_are_never_flagged(graph, cell):
    """`zen.validate_unary_expression` rejects several of these, which is why the linter
    probes the evaluator instead. Linting with it would flag most correct tables."""
    table = table_of(graph)
    table["content"]["rules"][0][table["content"]["inputs"][0]["id"]] = cell

    assert blocking(lint(graph)) == []


@pytest.mark.parametrize("cell", ["0.15", "true", "'Approved'", "amount * 0.1", "weight"])
def test_valid_output_cells_are_never_flagged(graph, cell):
    """`weight` is a real field in this graph, so it is a reference, not a missing quote."""
    table = table_of(graph)
    table["content"]["rules"][0][table["content"]["outputs"][0]["id"]] = cell

    assert "UNQUOTED_STRING_CELL" not in codes(graph)


# --------------------------------------------------------------------------- errors

def test_an_unreachable_node_is_an_error(graph):
    """A real traversal from the input, not a degree-zero orphan check: this node has an
    edge, and is still dead."""
    graph["nodes"].append(
        {"id": "orphan", "type": "expressionNode", "name": "Stranded",
         "position": {"x": 0, "y": 0},
         "content": {"expressions": [{"id": "e", "key": "x", "value": "1"}]}}
    )
    graph["nodes"].append(
        {"id": "orphan2", "type": "outputNode", "name": "StrandedOut",
         "position": {"x": 0, "y": 0}, "content": {"schema": ""}}
    )
    graph["edges"].append(
        {"id": "e9", "sourceId": "orphan", "targetId": "orphan2", "type": "edge"}
    )

    unreachable = [d for d in lint(graph) if d.code == "UNREACHABLE_NODE"]
    assert {d.node_name for d in unreachable} == {"Stranded", "StrandedOut"}
    assert all(d.severity == "error" for d in unreachable)


def test_an_empty_node_is_an_error(graph):
    table_of(graph)["content"]["rules"] = []
    assert "EMPTY_BLOCK" in {d.code for d in blocking(lint(graph))}


def test_a_duplicate_node_id_is_an_error(graph):
    graph["nodes"][1]["id"] = graph["nodes"][0]["id"]
    assert "DUPLICATE_NODE_ID" in {d.code for d in blocking(lint(graph))}


def test_a_graph_with_no_input_node_is_an_error(graph):
    graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "inputNode"]
    assert "MISSING_INPUT_NODE" in {d.code for d in blocking(lint(graph))}


# --------------------------------------------------------------------------- warnings

def test_a_switch_without_a_catch_all_is_a_warning(graph):
    graph["nodes"].append({
        "id": "sw", "type": "switchNode", "name": "Route", "position": {"x": 0, "y": 0},
        "content": {"hitPolicy": "first", "statements": [
            {"id": "s1", "condition": "cost > 5", "isDefault": False}]},
    })
    graph["edges"].append({"id": "e8", "sourceId": table_of(graph)["id"],
                           "targetId": "sw", "type": "edge"})

    assert "MISSING_DEFAULT_BRANCH" in codes(graph)


def test_a_first_hit_table_without_a_catch_all_row_is_a_warning(graph):
    table = table_of(graph)
    # Give the last row a condition, so nothing handles the remaining cases.
    table["content"]["rules"][-1][table["content"]["inputs"][0]["id"]] = "> 100"

    assert "MISSING_CATCH_ALL_ROW" in codes(graph)


def test_a_reference_to_a_node_that_does_not_exist_is_a_warning(graph):
    graph["nodes"].append({
        "id": "ex", "type": "expressionNode", "name": "Total", "position": {"x": 0, "y": 0},
        "content": {"expressions": [
            {"id": "e", "key": "total", "value": "$nodes.NoSuchNode.value + 1"}]},
    })
    graph["edges"].append({"id": "e7", "sourceId": table_of(graph)["id"],
                           "targetId": "ex", "type": "edge"})

    assert "UNKNOWN_NODE_REFERENCE" in codes(graph)


# --------------------------------------------------------------------------- hints

def test_the_shipped_examples_pass_their_own_suites():
    """A template that fails its own tests teaches the model to write broken graphs."""
    for name in ("LoanApprovalPolicy", "RefundPolicy", "ShippingQuotePolicy"):
        with open(f"backend/jdm_graphs/{name}_jdm.json") as handle:
            graph = json.load(handle)
        with open(f"backend/jdm_tests/{name}_tests.json") as handle:
            tests = json.load(handle)
        summary = run_test_suite(json.dumps(graph), tests)["summary"]
        assert summary["passed"] == summary["total"] > 0, f"{name}: {summary}"


def test_the_decomposed_example_lints_clean():
    """The template worth imitating: validate, branch, band, adjust - and nothing to fix."""
    with open("backend/jdm_graphs/ShippingQuotePolicy_jdm.json") as handle:
        graph = json.load(handle)

    assert lint(graph) == [], [d.code for d in lint(graph)]
    assert len([n for n in graph["nodes"] if n["type"] in
                ("decisionTableNode", "expressionNode", "switchNode")]) >= 3


def test_both_bundled_examples_are_flagged_as_monolithic():
    """They are the templates the model imitates, and both cram the whole policy into one
    decision table - the shape this rule exists to discourage."""
    for name in ("LoanApprovalPolicy", "RefundPolicy"):
        with open(f"backend/jdm_graphs/{name}_jdm.json") as handle:
            assert "MONOLITHIC_GRAPH" in codes(json.load(handle)), name


def test_a_decomposed_graph_is_not_flagged_as_monolithic(graph):
    graph["nodes"].append({
        "id": "ex", "type": "expressionNode", "name": "ApplyDiscount",
        "position": {"x": 0, "y": 0},
        "content": {"expressions": [{"id": "e", "key": "final", "value": "cost * 0.9"}]},
    })
    graph["edges"].append({"id": "e7", "sourceId": table_of(graph)["id"],
                           "targetId": "ex", "type": "edge"})

    assert "MONOLITHIC_GRAPH" not in codes(graph)


def test_an_oversized_table_is_hinted(graph):
    table = table_of(graph)
    column = table["content"]["inputs"][0]["id"]
    template = table["content"]["rules"][0]
    table["content"]["rules"] = [
        {**template, "_id": f"r{i}", column: f"== {i}"} for i in range(30)
    ]

    assert "TABLE_TOO_LARGE" in codes(graph)


def test_a_generic_node_name_is_hinted(graph):
    table_of(graph)["name"] = "Table 1"
    assert "UNDESCRIPTIVE_NAME" in codes(graph)


def test_a_row_that_can_never_fire_is_hinted(graph):
    table = table_of(graph)
    table["content"]["rules"].append(dict(table["content"]["rules"][0], _id="dupe"))

    assert "REDUNDANT_TABLE_ROW" in codes(graph)


def test_a_column_no_rule_uses_is_hinted(graph):
    table = table_of(graph)
    table["content"]["inputs"].append({"id": "unused", "name": "Zone", "field": "zone"})

    assert "NON_DISCRIMINATING_COLUMN" in codes(graph)


# --------------------------------------------------------------------------- the real bug

def test_the_linter_diagnoses_the_unquoted_output_bug():
    """The bug both shipped examples had, and the round trip out of it.

    LoanApprovalPolicy failed 11/11 for exactly this reason. Unquote its outputs again and
    the suite collapses; apply what the linter reports, change no tests, and it comes back.
    """
    with open("backend/jdm_graphs/LoanApprovalPolicy_jdm.json") as handle:
        graph = unquote_outputs(json.load(handle))
    with open("backend/jdm_tests/LoanApprovalPolicy_tests.json") as handle:
        tests = json.load(handle)

    before = run_test_suite(json.dumps(graph), tests)["summary"]
    assert before["passed"] == 0 and before["failed"] == 11

    unquoted = [d for d in lint(graph) if d.code == "UNQUOTED_STRING_CELL"]
    assert unquoted, "the linter must find the cause without running a single test"

    for diagnostic in unquoted:
        node = next(n for n in graph["nodes"] if n["id"] == diagnostic.node_id)
        for column in node["content"]["outputs"]:
            for rule in node["content"]["rules"]:
                value = (rule.get(column["id"]) or "").strip()
                if value and value[0].isalpha() and "'" not in value:
                    rule[column["id"]] = f"'{value}'"

    after = run_test_suite(json.dumps(graph), tests)["summary"]
    assert after["passed"] == 11, "the linter's diagnosis must actually be the fix"


def test_the_good_fixture_stays_clean():
    assert blocking(lint(parse_markdown_dsl(GOOD_DSL))) == []


# --------------------------------------------------------------------------- the chat route

@pytest.mark.parametrize("text", [
    "is this graph well built?", "check my policy", "any problems with this?",
    "review the policy", "lint it", "anything wrong here?", "improve this graph",
    "is it well-structured?", "quality check",
])
def test_asking_about_quality_routes_to_the_linter(text):
    """Resolved by rule, never by the LLM: a rate-limited free tier should not be spent
    working out that "check my policy" is a review request."""
    from backend import lang_graph_agent as agent

    assert agent._classify_intent(text, has_graph=True)[0] == "LINT"
    assert agent.route_after_intent({"intent": "LINT"}) == "lint_node"


@pytest.mark.parametrize("text", [
    "run the tests", "explain this", "add a rule for gold members",
    "create a refund policy", "test the boundary cases", "describe what this does",
])
def test_other_requests_do_not_route_to_the_linter(text):
    from backend import lang_graph_agent as agent

    assert agent._classify_intent(text, has_graph=True)[0] != "LINT"


def test_the_lint_node_reports_findings_without_calling_the_llm(monkeypatch):
    from backend import lang_graph_agent as agent

    def explode(*_args, **_kwargs):
        raise AssertionError("linting is deterministic and must not call the LLM")

    monkeypatch.setattr(agent, "call_llm", explode)
    with open("backend/jdm_graphs/LoanApprovalPolicy_jdm.json") as handle:
        graph = json.dumps(unquote_outputs(json.load(handle)))

    result = agent.lint_node({"existing_jdm_json": graph, "selected_file": "Loan Approval"})

    body = result["messages"][0].content
    assert "Loan Approval" in body
    assert "UNQUOTED_STRING_CELL" in body
    assert "MONOLITHIC_GRAPH" in body


def test_the_lint_node_says_so_when_there_is_nothing_open():
    from backend import lang_graph_agent as agent

    result = agent.lint_node({"existing_jdm_json": "", "canvas_jdm_json": ""})

    assert "no policy open" in result["messages"][0].content


# --------------------------------------------------------------------------
# Reaching the linter at all
# --------------------------------------------------------------------------

def test_every_intent_the_router_can_return_has_an_edge():
    """The invariant the lint crash slipped through.

    `route_after_intent` is a dict lookup, and the existing tests called it directly - so
    they happily confirmed it returns "lint_node" for a LINT intent while the compiled
    graph had no such edge. Asking the agent to lint anything raised KeyError('lint_node')
    and took the whole turn down with it.

    Checked against the compiled graph rather than the source, because the path map is the
    thing that was wrong.
    """
    from backend import lang_graph_agent as agent

    reachable = {
        edge.target for edge in agent.build_graph().get_graph().edges
        if edge.source == "intent_router_node"
    }
    for intent in ("CREATE", "MODIFY", "TEST", "EXPLAIN", "LINT"):
        destination = agent.route_after_intent({"intent": intent})
        assert destination in reachable, (
            f"{intent} routes to {destination!r}, which the graph cannot reach"
        )


def test_asking_the_agent_to_lint_actually_lints():
    """End to end through the real compiled graph. A unit test on the router would have
    passed throughout the entire period this was broken."""
    import asyncio
    import uuid

    from langchain_core.messages import HumanMessage

    from backend import lang_graph_agent as agent

    graph_json = json.dumps({
        "contentType": "application/vnd.gorules.decision",
        "nodes": [
            {"id": "i", "name": "Request", "type": "inputNode",
             "content": {"schema": ""}, "position": {"x": 0, "y": 0}},
            {"id": "f", "name": "Fee", "type": "expressionNode", "position": {"x": 1, "y": 0},
             "content": {"expressions": [
                 {"id": "e", "key": "fee", "value": "total > 50 ? 0 : 6"}]}},
            {"id": "o", "name": "Response", "type": "outputNode",
             "content": {"schema": ""}, "position": {"x": 2, "y": 0}},
        ],
        "edges": [{"id": "e1", "sourceId": "i", "targetId": "f", "type": "edge"},
                  {"id": "e2", "sourceId": "f", "targetId": "o", "type": "edge"}],
    })

    compiled = agent.build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def turn():
        ran = []
        async for chunk in compiled.astream(
            {"messages": [HumanMessage(content="lint this policy")],
             "canvas_jdm_json": graph_json},
            config=config,
        ):
            ran.extend(k for k in chunk if k != "__interrupt__")
        return ran, (await compiled.aget_state(config)).values

    ran, values = asyncio.run(turn())

    assert ran == ["intent_router_node", "lint_node"]
    assert "Checked" in values["messages"][-1].content


def test_linting_still_costs_no_model_call(monkeypatch):
    """It is deterministic, and the point of routing it separately is that it is free."""
    import asyncio
    import uuid

    from langchain_core.messages import HumanMessage

    from backend import lang_graph_agent as agent

    def explode(*_args, **_kwargs):
        raise AssertionError("linting must not reach the model")

    monkeypatch.setattr(agent, "_dispatch", explode)

    with open("backend/jdm_graphs/RefundPolicy_jdm.json") as handle:
        graph_json = json.dumps(json.load(handle))

    compiled = agent.build_graph()
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def turn():
        async for _ in compiled.astream(
            {"messages": [HumanMessage(content="is this well built?")],
             "canvas_jdm_json": graph_json},
            config=config,
        ):
            pass
        return (await compiled.aget_state(config)).values

    assert "Checked" in asyncio.run(turn())["messages"][-1].content


# --------------------------------------------------------------------------
# Graded against what the engine actually refuses
# --------------------------------------------------------------------------

def shipping_graph(schema: str = "") -> dict:
    return {
        "contentType": "application/vnd.gorules.decision",
        "nodes": [
            {"id": "i", "name": "Request", "type": "inputNode",
             "content": {"schema": schema}, "position": {"x": 0, "y": 0}},
            {"id": "f", "name": "Fee", "type": "expressionNode", "position": {"x": 1, "y": 0},
             "content": {"expressions": [
                 {"id": "e", "key": "fee", "value": "total > 50 ? 0 : 6"}]}},
            {"id": "o", "name": "Response", "type": "outputNode",
             "content": {"schema": ""}, "position": {"x": 2, "y": 0}},
        ],
        "edges": [{"id": "a", "sourceId": "i", "targetId": "f", "type": "edge"},
                  {"id": "b", "sourceId": "f", "targetId": "o", "type": "edge"}],
    }


def severity_of(graph: dict, code: str) -> str | None:
    from backend.tools.jdm_linter import lint

    for finding in lint(graph):
        if finding.code == code:
            return finding.severity
    return None


def runs_in_zen(graph: dict, context: dict) -> bool:
    """What the engine makes of it - which is the only authority on severity here."""
    import zen

    try:
        zen.ZenEngine().create_decision(json.dumps(graph)).evaluate(context)
        return True
    except Exception:  # noqa: BLE001
        return False


def test_a_missing_schema_is_advice_because_the_engine_does_not_care():
    graph = shipping_graph()

    assert runs_in_zen(graph, {"total": 80}), "the engine is happy without one"
    assert severity_of(graph, "MISSING_INPUT_SCHEMA") == "hint"


def test_a_missing_output_node_is_advice_for_the_same_reason():
    graph = shipping_graph()
    graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "outputNode"]
    graph["edges"] = [e for e in graph["edges"] if e["targetId"] != "o"]

    assert runs_in_zen(graph, {"total": 80}), "the engine returns what the last node made"
    assert severity_of(graph, "MISSING_OUTPUT_NODE") == "hint"


def test_a_missing_input_node_stays_an_error_because_the_engine_refuses_it():
    """The one of the three that must not be downgraded. Zen will not compile the graph
    at all, so grading it as advice would have the linter bless something unrunnable."""
    graph = shipping_graph()
    graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "inputNode"]
    graph["edges"] = [e for e in graph["edges"] if e["sourceId"] != "i"]

    assert not runs_in_zen(graph, {"total": 80}), "InvalidGraph / invalidInputCount"
    assert severity_of(graph, "MISSING_INPUT_NODE") == "error"


def test_a_schema_requiring_a_field_nothing_reads_is_an_error():
    """A schema is optional, but once written it is enforced. Requiring a field the policy
    never looks at rejects every sensible call before the graph runs, and the linter used
    to say nothing at all about it."""
    graph = shipping_graph('{"type": "object", "required": ["customerId"]}')

    assert not runs_in_zen(graph, {"total": 80}), 'NodeError: "customerId" is required'
    assert severity_of(graph, "SCHEMA_REQUIRES_UNUSED_FIELD") == "error"


def test_a_schema_requiring_a_field_the_policy_uses_is_fine():
    graph = shipping_graph('{"type": "object", "required": ["total"]}')

    assert runs_in_zen(graph, {"total": 80})
    assert severity_of(graph, "SCHEMA_REQUIRES_UNUSED_FIELD") is None


def test_a_schema_the_engine_cannot_parse_is_not_reported():
    """Zen ignores an unparseable schema entirely, so it breaks nothing - and a finding
    for something with no consequence is noise."""
    graph = shipping_graph("{oops}")

    assert runs_in_zen(graph, {"total": 80})
    assert severity_of(graph, "SCHEMA_REQUIRES_UNUSED_FIELD") is None


def test_the_demotion_changes_what_is_shown_not_what_builds():
    """Worth being exact about. `blocking()` gates on errors alone, so these two never
    stopped a build even as warnings - the demotion changes how they are presented, which
    is what was asked for, and nothing about which graphs compile.
    """
    from backend.tools.jdm_linter import blocking, lint

    findings = lint(shipping_graph())
    demoted = [f for f in findings
               if f.code in ("MISSING_INPUT_SCHEMA", "MISSING_OUTPUT_NODE")]

    assert demoted, "still reported - hidden is not the same as demoted"
    assert all(f.severity == "hint" for f in demoted)
    assert blocking(findings) == []


def test_the_new_schema_rule_does_block_a_build():
    """The one change here that moves the gate, and it tightens rather than loosens it:
    a graph that cannot accept a normal call now fails the build instead of shipping."""
    from backend.tools.jdm_linter import blocking, lint

    graph = shipping_graph('{"type": "object", "required": ["customerId"]}')
    blockers = blocking(lint(graph))

    assert [b.code for b in blockers] == ["SCHEMA_REQUIRES_UNUSED_FIELD"]
