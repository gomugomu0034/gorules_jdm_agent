"""Tests for typed diagnostics.

Every failure used to reach the model as `SYSTEM ERROR: {str(exception)}` - a parse error,
a dangling edge, a bad expression and a wrong business answer all looked identical and none
named a node. These tests pin the three things that changed: the kind is distinguished, the
node is named, and the advice is specific enough to act on.

The false-positive tests matter most. A diagnostic that fires on a correct graph is worse
than no diagnostic, because it sends the repair loop off to "fix" working logic.
"""

from __future__ import annotations

import json

import pytest
import zen

from backend.tools.diagnostics import (
    Diagnostic,
    attribute_failure,
    check_expressions,
    check_structure,
    diagnose,
    format_for_llm,
    parse_engine_error,
)
from backend.tools.markdown_dsl_parser import parse_markdown_dsl
from backend.tools.zen_evaluator import run_test_suite
from backend.tests.test_agent_flow import BROKEN_DSL, GOOD_DSL, TESTS

TABLE_DSL = """# Structure
```mermaid
flowchart LR
  In --> Price
  Price --> Out
```

# Nodes
## In
type: input

## Price
type: decisionTable
hitPolicy: first

| in weight [Weight] | out cost |
| --- | --- |
| < 1 | 5 |
| >= 1 and <= 5 | 9 |

## Out
type: output
"""


@pytest.fixture
def table_graph():
    return parse_markdown_dsl(TABLE_DSL)


# --------------------------------------------------------------------- engine errors

def test_the_node_id_is_read_from_the_error_not_guessed():
    """Zen's evaluation errors are JSON carrying an explicit nodeId; it was thrown away."""
    graph = {"nodes": [{"id": "n1", "name": "Fee", "type": "expressionNode"}], "edges": []}
    raw = json.dumps({"type": "NodeError", "source": "Failed to evaluate expression",
                      "nodeId": "n1", "trace": None})

    diagnostic = parse_engine_error(raw, graph)

    assert diagnostic.kind == "engine"
    assert diagnostic.node_id == "n1"
    assert diagnostic.node_name == "Fee"


def test_structural_errors_are_reported_as_sentences_not_json_blobs():
    raw = json.dumps({"type": "InvalidGraph", "source": {"type": "missingNode", "nodeId": "ghost"}})

    diagnostic = parse_engine_error(raw, {"nodes": [], "edges": []})

    assert diagnostic.kind == "structure"
    assert diagnostic.code == "missingNode"
    assert "ghost" in diagnostic.message
    assert "{" not in diagnostic.message, "the engine's JSON must not be echoed at the model"


def test_an_unknown_node_type_is_recognised_from_the_serde_message():
    raw = "Invalid JSON\n\nCaused by:\n    nodes[0]: unknown variant `weirdNode`, expected one of ..."

    diagnostic = parse_engine_error(raw, {})

    assert diagnostic.code == "UNKNOWN_NODE_TYPE"
    assert "weirdNode" in diagnostic.message


# --------------------------------------------------------------------- structure

def test_validate_catches_the_dangling_edge_that_compiling_misses():
    """`create_decision` only deserializes: it accepts this graph happily."""
    graph = parse_markdown_dsl(GOOD_DSL)
    graph["edges"][0]["targetId"] = "does-not-exist"

    zen.ZenEngine().create_decision(json.dumps(graph))  # compiles, proving the gap

    problems = check_structure(graph)
    assert [d.code for d in problems] == ["missingNode"]
    assert problems[0].kind == "structure"


def test_a_sound_graph_produces_no_structural_diagnostics(table_graph):
    assert check_structure(table_graph) == []


# --------------------------------------------------------------------- expressions

def test_an_invalid_expression_is_caught_before_evaluation():
    graph = parse_markdown_dsl(GOOD_DSL)
    for node in graph["nodes"]:
        if node["type"] == "expressionNode":
            node["content"]["expressions"][0]["value"] = "orderTotal > 50 || orderTotal < 10"

    problems = check_expressions(graph)

    assert len(problems) == 1
    assert problems[0].code == "PARSE_ERROR"
    assert problems[0].node_name == "Fee"
    assert "||" in problems[0].fix_hint


def test_a_broken_table_cell_names_its_row_and_column(table_graph):
    node = next(n for n in table_graph["nodes"] if n["type"] == "decisionTableNode")
    column = node["content"]["inputs"][0]["id"]
    node["content"]["rules"][0][column] = ">> 1"

    problems = check_expressions(table_graph)

    assert len(problems) == 1
    assert problems[0].node_name == "Price"
    assert "row 1" in problems[0].message
    assert "Weight" in problems[0].message


@pytest.mark.parametrize("cell", [
    ">= 100", "'US','CA'", "[1..10]", "len($) > 5", "> 10 and < 50",
    "'gold'", "!= 'new'", "in ['a','b']", "",
])
def test_valid_unary_cells_are_never_flagged(table_graph, cell):
    """The guard that matters.

    `zen.validate_unary_expression` rejects `>= 100`, `'US','CA'` and `> 10 and < 50` -
    all of which the evaluator accepts - so linting cells with it would flag most correct
    tables and send the repair loop chasing working logic.
    """
    node = next(n for n in table_graph["nodes"] if n["type"] == "decisionTableNode")
    node["content"]["rules"][0][node["content"]["inputs"][0]["id"]] = cell

    assert check_expressions(table_graph) == []


def test_a_correct_graph_produces_no_expression_diagnostics(table_graph):
    assert check_expressions(table_graph) == []
    assert check_expressions(parse_markdown_dsl(GOOD_DSL)) == []


# --------------------------------------------------------------------- assertions

def test_a_wrong_answer_is_blamed_on_the_node_that_decided_it():
    graph = parse_markdown_dsl(BROKEN_DSL)
    report = run_test_suite(json.dumps(graph), TESTS, trace=True)

    problems = [d for d in diagnose(graph, report) if d.kind == "assertion"]

    assert problems
    assert problems[0].code == "WRONG_VALUE"
    assert problems[0].node_name == "Fee", "the trace names the node that produced the field"
    assert "do not change the test" in problems[0].fix_hint


def test_a_failing_table_reports_which_row_matched(table_graph):
    report = run_test_suite(
        json.dumps(table_graph),
        [{"name": "3kg", "input": {"weight": 3}, "expectedOutput": {"cost": 99}}],
        trace=True,
    )

    problems = [d for d in diagnose(table_graph, report) if d.kind == "assertion"]

    assert "row 2 matched" in problems[0].message, "the trace records the matched row index"


def test_no_row_matching_is_distinguished_from_a_missing_column(table_graph):
    """Two different bugs that both show up as an absent field, with different fixes."""
    no_match = run_test_suite(
        json.dumps(table_graph),
        [{"name": "heavy", "input": {"weight": 20}, "expectedOutput": {"cost": 15}}],
        trace=True,
    )
    undeclared = run_test_suite(
        json.dumps(table_graph),
        [{"name": "surcharge", "input": {"weight": 0.5}, "expectedOutput": {"surcharge": 2}}],
        trace=True,
    )

    a = [d for d in diagnose(table_graph, no_match) if d.kind == "assertion"][0]
    b = [d for d in diagnose(table_graph, undeclared) if d.kind == "assertion"][0]

    assert a.code == b.code == "FIELD_NEVER_PRODUCED"
    assert "no row matched" in a.message
    assert "catch-all" in a.message
    assert 'No node declares "surcharge"' in b.fix_hint


# --------------------------------------------------------------------- escalation

def test_only_the_most_fundamental_kind_is_reported():
    """Downstream failures are usually consequences; showing all of them at once makes
    the model rewrite the graph rather than make the one change that matters."""
    rendered = format_for_llm([
        Diagnostic(kind="assertion", code="WRONG_VALUE", message="expected 1 got 2"),
        Diagnostic(kind="structure", code="missingNode", message="an edge dangles"),
        Diagnostic(kind="expression", code="PARSE_ERROR", message="bad cell"),
    ])

    assert "THE GRAPH IS NOT WIRED CORRECTLY" in rendered
    assert "an edge dangles" in rendered
    assert "expected 1 got 2" not in rendered
    assert "2 further problem(s)" in rendered


def test_nothing_wrong_renders_nothing():
    assert format_for_llm([]) == ""
