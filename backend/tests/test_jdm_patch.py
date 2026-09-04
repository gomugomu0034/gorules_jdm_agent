"""Tests for editing a graph by patch rather than regenerating it.

The property every test here is really about: **what you did not name does not move.**

The old modify path rebuilt the graph from a plan, and `parse_markdown_dsl` mints a fresh
`uuid4()` for every node and edge on every parse - so an edit replaced the whole graph in a
new id space. The canvas jumped, `$nodes` references broke, saved tests detached, and any
logic the model did not happen to re-emit was silently dropped.
"""

from __future__ import annotations

import json

import pytest

from backend import lang_graph_agent as agent
from backend.tools.jdm_patch import PatchError, apply_patch, describe
from backend.tools.zen_evaluator import run_test_suite


@pytest.fixture
def graph():
    with open("backend/jdm_graphs/ShippingQuotePolicy_jdm.json") as handle:
        return json.load(handle)


@pytest.fixture
def tests():
    with open("backend/jdm_tests/ShippingQuotePolicy_tests.json") as handle:
        return json.load(handle)


def ids(graph_dict) -> set[str]:
    return {n["id"] for n in graph_dict["nodes"]} | {e["id"] for e in graph_dict["edges"]}


def cells(graph_dict, node_name: str) -> list[dict]:
    node = next(n for n in graph_dict["nodes"] if n["name"] == node_name)
    return node["content"]["rules"]


# --------------------------------------------------------------------- the whole point

def test_an_edit_changes_only_what_it_names(graph):
    """One cell in, one cell out - and every id survives."""
    before = json.dumps(graph, sort_keys=True)

    after = apply_patch(graph, [
        {"op": "set_cell", "node": "WeightBand", "row": 3, "column": "baseCost", "value": "20"}
    ])

    assert ids(after) == ids(graph), "node and edge ids must survive an edit"
    # Exactly one value differs across the whole document.
    differences = [
        (a, b) for a, b in zip(
            json.dumps(graph, indent=2, sort_keys=True).splitlines(),
            json.dumps(after, indent=2, sort_keys=True).splitlines(),
        ) if a != b
    ]
    assert len(differences) == 1, differences
    assert json.dumps(graph, sort_keys=True) == before, "the original must not be mutated"


def test_a_failed_patch_changes_nothing(graph):
    """All or nothing: a bad edit must not half-apply and strand the policy."""
    before = json.dumps(graph, sort_keys=True)

    with pytest.raises(PatchError):
        apply_patch(graph, [
            {"op": "set_cell", "node": "WeightBand", "row": 1, "column": "baseCost", "value": "7"},
            {"op": "set_cell", "node": "NoSuchNode", "row": 1, "column": "x", "value": "1"},
        ])

    assert json.dumps(graph, sort_keys=True) == before


def test_an_edit_keeps_the_saved_suite_working(graph, tests):
    """Ids are stable, so the tests that already passed still address the same graph."""
    after = apply_patch(graph, [
        {"op": "set_cell", "node": "WeightBand", "row": 1, "column": "baseCost", "value": "6"}
    ])

    report = run_test_suite(json.dumps(after), tests)
    # Exactly the cases that exercise the edited band move; nothing else does. That is the
    # property an edit has to have - a rebuild could not promise it, because the ids the
    # suite was written against would all have changed.
    failed = {r["name"] for r in report["results"] if r["status"] == "failed"}
    touches_light_band = {
        t["name"] for t in tests
        # Priced (not rejected), in the sub-1kg band, and not free-shipped.
        if "shippingCost" in t["expectedOutput"]
        and 0 < t["input"].get("weightKg", 99) < 1
        and t["input"].get("orderTotal", 0) <= 150
    }
    assert failed == touches_light_band, sorted(failed ^ touches_light_band)
    assert report["summary"]["errored"] == 0


# --------------------------------------------------------------------- addressing

def test_nodes_are_addressable_by_name_id_and_case(graph):
    node_id = next(n["id"] for n in graph["nodes"] if n["name"] == "WeightBand")

    for ref in ("WeightBand", node_id, "weightband"):
        after = apply_patch(graph, [
            {"op": "set_cell", "node": ref, "row": 1, "column": "baseCost", "value": "6"}
        ])
        assert cells(after, "WeightBand")[0] != cells(graph, "WeightBand")[0]


def test_an_unknown_node_says_what_the_graph_actually_has(graph):
    with pytest.raises(PatchError) as excinfo:
        apply_patch(graph, [{"op": "set_cell", "node": "WeightBnd", "row": 1,
                             "column": "baseCost", "value": "6"}])

    message = str(excinfo.value)
    assert "WeightBnd" in message
    assert "WeightBand" in message, "the error must list the real names to choose from"


def test_an_unknown_operation_lists_the_known_ones(graph):
    with pytest.raises(PatchError) as excinfo:
        apply_patch(graph, [{"op": "frobnicate", "node": "WeightBand"}])

    assert "set_cell" in str(excinfo.value)


def test_an_empty_patch_is_an_error(graph):
    with pytest.raises(PatchError):
        apply_patch(graph, [])


# --------------------------------------------------------------------- operations

def test_a_rule_can_be_inserted_above_the_catch_all(graph):
    """Order is priority under first-hit, so placement is the whole point of `after`."""
    original = len(cells(graph, "WeightBand"))

    after = apply_patch(graph, [{
        "op": "add_rule", "node": "WeightBand", "after": 2,
        "cells": {"Weight": "[5..10]", "baseCost": "12"},
    }])

    rules = cells(after, "WeightBand")
    assert len(rules) == original + 1
    weight = next(c["id"] for c in
                  next(n for n in after["nodes"] if n["name"] == "WeightBand")["content"]["inputs"])
    assert rules[2][weight] == "[5..10]", "the new rule must land where it was asked to"


def test_a_rename_follows_through_to_node_references(graph):
    """Node names are the addressing scheme for $nodes; a rename that stops at the node
    itself leaves every reference silently resolving to null."""
    after = apply_patch(graph, [{"op": "rename", "node": "WeightBand", "name": "WeightTier"}])

    adjust = next(n for n in after["nodes"] if n["name"] == "ApplyAdjustments")
    values = " ".join(e["value"] for e in adjust["content"]["expressions"])
    assert "$nodes.WeightTier" in values
    assert "$nodes.WeightBand" not in values


def test_a_new_column_leaves_existing_rules_matching(graph):
    """A new column must default to a wildcard, or every existing rule stops firing."""
    after = apply_patch(graph, [{
        "op": "add_column", "node": "WeightBand", "kind": "input",
        "name": "Zone", "field": "destination",
    }])

    node = next(n for n in after["nodes"] if n["name"] == "WeightBand")
    zone = next(c["id"] for c in node["content"]["inputs"] if c["name"] == "Zone")
    assert all(rule[zone] == "" for rule in node["content"]["rules"])


def test_removing_a_node_removes_its_edges(graph):
    after = apply_patch(graph, [{"op": "remove_node", "node": "ApplyAdjustments"}])

    gone = next(n["id"] for n in graph["nodes"] if n["name"] == "ApplyAdjustments")
    assert all(e["sourceId"] != gone and e["targetId"] != gone for e in after["edges"])


def test_connecting_twice_is_not_an_error(graph):
    after = apply_patch(graph, [{"op": "connect", "from": "Request", "to": "ValidateOrder"}])
    assert len(after["edges"]) == len(graph["edges"])


def test_the_action_log_reads_as_english():
    lines = describe([
        {"op": "set_cell", "node": "Pricing", "row": 2, "column": "Discount", "value": "0.20"},
        {"op": "rename", "node": "Old", "name": "New"},
    ])

    assert lines == ["Set Discount on row 2 of Pricing to 0.20", "Renamed Old to New"]


# --------------------------------------------------------------------- routing

def test_editing_an_existing_policy_routes_to_the_patch_node():
    approved = {"triage_status": "APPROVED", "mode": "EXISTING",
                "existing_jdm_json": '{"nodes": [{"id": "a"}], "edges": []}'}

    assert agent.route_after_human_review(approved) == "patch_node"


def test_building_a_new_policy_still_routes_to_the_planner():
    assert agent.route_after_human_review(
        {"triage_status": "APPROVED", "mode": "NEW"}) == "planner_node"
    # An "existing" policy with nothing in it is a new build, not an edit.
    assert agent.route_after_human_review(
        {"triage_status": "APPROVED", "mode": "EXISTING", "existing_jdm_json": ""}) == "planner_node"


# --------------------------------------------------------------------- reporting an edit

def _edit_result(graph, **extra) -> str:
    state = {
        "jdm_json": json.dumps(graph),
        "build_status": "SUCCESS",
        "usecase_name": "Shipping Quote Policy",
        "patch_log": ["Set zoneMultiplier in ApplyAdjustments to 1.5"],
        "test_suite_json": "[]",
        **extra,
    }
    return agent.output_node(state)["messages"][0].content


def test_an_edit_reports_what_changed_not_the_whole_graph(graph):
    """After a one-cell change, listing every node buries the one line that matters."""
    body = _edit_result(graph)

    assert "What changed:" in body
    assert "Set zoneMultiplier" in body
    assert "(decision table)" not in body, "that is the shape of a fresh-build report"


def test_an_unverified_edit_says_so(graph):
    """"0 tests still passing" reads like a result. It is the absence of one."""
    body = _edit_result(graph)

    assert "no saved tests" in body
    assert "0 tests still passing" not in body


def test_tests_that_disagree_are_reported_as_a_decision_not_a_failure(graph, tests):
    """The change was requested, so cases pinning the old behaviour are meant to disagree.
    Treating that as an error would have the agent fight the instruction it was given."""
    body = _edit_result(
        graph,
        test_suite_json=json.dumps(tests),
        test_regressions=["International doubles the base rate"],
    )

    assert "now expect the old behaviour" in body
    assert "International doubles the base rate" in body
    assert "Approve if the policy is right" in body
    assert "could not" not in body.lower() and "failed" not in body.lower()
