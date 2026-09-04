"""Coverage, boundary derivation, and execution explanations.

All three read the execution trace and involve no model, so they are exact and repeatable -
and they cost nothing against a rate-limited provider, which is why they are worth having
as deterministic tools rather than as prompts.
"""

from __future__ import annotations

import json

import pytest

from backend.tools.coverage import coverage, suggest_cases, thresholds
from backend.tools.explain_run import as_markdown, explain_run
from backend.tools.zen_evaluator import run_test_suite


@pytest.fixture
def graph():
    with open("backend/jdm_graphs/ShippingQuotePolicy_jdm.json") as handle:
        return json.load(handle)


@pytest.fixture
def tests():
    with open("backend/jdm_tests/ShippingQuotePolicy_tests.json") as handle:
        return json.load(handle)


def reached_row(graph_dict, node_name: str, payload: dict) -> int | None:
    report = run_test_suite(
        graph_dict, [{"name": "probe", "input": payload, "expectedOutput": {"__never__": 1}}],
        trace=True,
    )
    step = next((v for v in report["results"][0]["trace"].values() if v["name"] == node_name), None)
    data = (step or {}).get("traceData")
    return data["index"] + 1 if isinstance(data, dict) and data.get("index") is not None else None


# --------------------------------------------------------------------------- coverage

def test_a_complete_suite_reaches_every_rule(graph, tests):
    report = coverage(graph, tests)

    assert report["summary"]["percent"] == 100.0
    assert all(entry["uncovered"] == [] for entry in report["nodes"])


def test_coverage_names_the_rules_no_case_reaches(graph, tests):
    """Passing tests are not covered tests: a suite can be green and touch half the policy."""
    report = coverage(graph, tests[:2])

    assert report["summary"]["percent"] < 100
    gaps = {e["nodeName"]: e["uncovered"] for e in report["nodes"] if e["uncovered"]}
    assert gaps["ValidateOrder"] == [1, 2]
    assert gaps["WeightBand"] == [3]


def test_switch_branches_are_counted_too(graph, tests):
    report = coverage(graph, tests[:2])

    routing = next(e for e in report["nodes"] if e["nodeName"] == "RouteOnValidity")
    assert routing["kind"] == "branches"
    assert routing["uncovered"] == ["isValid == false"]


def test_coverage_of_an_empty_suite_is_zero_not_an_error(graph):
    report = coverage(graph, [])

    assert report["summary"]["covered"] == 0
    assert report["summary"]["total"] > 0


# --------------------------------------------------------------------------- boundaries

def test_thresholds_come_from_the_table_not_from_a_guess(graph):
    """The numbers are already in the graph; inventing them is how test suites drift."""
    found = {(t["node"], t["field"]) for t in thresholds(graph)}

    assert ("WeightBand", "weightKg") in found
    assert ("ValidateOrder", "orderTotal") in found


@pytest.mark.parametrize("operator,value,expected", [
    (">=", 100, 100), ("<=", 65, 65), ("==", 5, 5),
    (">", 5, 6), ("<", 0, -0.01), ("!=", 3, 4),
])
def test_a_suggested_value_satisfies_the_condition_it_targets(operator, value, expected):
    """`< 0` is not satisfied by 0. A case that lands on a different row than the one it
    claims to cover is worse than no suggestion at all."""
    from backend.tools.coverage import _satisfying

    assert _satisfying(operator, value) == expected


def test_suggestions_actually_reach_the_rows_they_name(graph, tests):
    """The property that makes them worth offering."""
    suggestions = suggest_cases(graph, tests[:2])
    assert suggestions

    for suggestion in suggestions:
        assert reached_row(graph, suggestion["node"], suggestion["input"]) == suggestion["row"], (
            f'{suggestion["name"]}: {suggestion["input"]}'
        )


def test_adding_the_suggestions_closes_the_gap(graph, tests):
    partial = tests[:2]
    filled = partial + [
        {"name": s["name"], "input": s["input"], "expectedOutput": {}}
        for s in suggest_cases(graph, partial)
    ]

    assert coverage(graph, filled)["summary"]["percent"] == 100.0


def test_a_covered_suite_suggests_nothing(graph, tests):
    assert suggest_cases(graph, tests) == []


# --------------------------------------------------------------------------- explanation

def test_an_explanation_follows_the_path_the_data_took(graph):
    explanation = explain_run(
        graph, {"weightKg": 7, "destination": "international",
                "orderTotal": 40, "membership": "gold"})

    names = [step["node"] for step in explanation["steps"]]
    assert names == ["Request", "ValidateOrder", "RouteOnValidity",
                     "WeightBand", "ApplyAdjustments", "Quote"]
    assert explanation["result"]["shippingCost"] == 24


def test_an_explanation_names_the_row_that_matched_and_why(graph):
    explanation = explain_run(
        graph, {"weightKg": 0, "destination": "domestic",
                "orderTotal": 40, "membership": "standard"})

    validate = next(s for s in explanation["steps"] if s["node"] == "ValidateOrder")
    assert "Row 1 matched" in validate["summary"]
    assert "Weight <= 0" in validate["summary"], "it must say what made the row match"


def test_an_explanation_reports_only_what_each_node_added(graph):
    """Nodes pass data through, so a raw output repeats everything upstream produced."""
    explanation = explain_run(
        graph, {"weightKg": 7, "destination": "domestic",
                "orderTotal": 40, "membership": "standard"})

    band = next(s for s in explanation["steps"] if s["node"] == "WeightBand")
    assert "baseCost=15" in band["summary"]
    assert "membership" not in band["summary"], "that was passed through, not decided here"


def test_the_rejected_path_is_explained_as_a_branch(graph):
    explanation = explain_run(
        graph, {"weightKg": 0, "destination": "domestic",
                "orderTotal": 40, "membership": "standard"})

    route = next(s for s in explanation["steps"] if s["node"] == "RouteOnValidity")
    assert "Took branch 1" in route["summary"]
    assert "isValid == false" in route["summary"]
    # Pricing never ran, so it must not appear in the account of what happened.
    assert not any(s["node"] == "WeightBand" for s in explanation["steps"])


def test_the_markdown_form_is_numbered_and_ends_with_the_result(graph):
    body = as_markdown(explain_run(
        graph, {"weightKg": 3, "destination": "domestic",
                "orderTotal": 40, "membership": "standard"}))

    assert body.startswith("Here is what the policy did")
    assert "1. **Request**" in body
    assert "Result:" in body
