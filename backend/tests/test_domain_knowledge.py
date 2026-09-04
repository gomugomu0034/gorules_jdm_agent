"""Guards on the knowledge base the agent is prompted with.

A prompt is not documentation - it is the model's only source of truth, and the model will
faithfully reproduce whatever it is told. So the guidance is checked the same way generated
output is: against the real engine.

These tests exist because the knowledge base used to contradict itself in four places and
state one outright falsehood, and the builder node is the single consumer of the text that
carried it - meaning the debugging prompt could inject a syntax error while repairing one.
"""

from __future__ import annotations

import json

import zen

from backend.prompts.gorules_domain_knowledge.gorules_jdm_knowledge_base import (
    CRITICAL_CONSISTENCY_RULES,
    CRITICAL_DEBUGGING_KNOWLEDGE,
    GORULES_KNOWLEDGE_BASE,
)

ALL_TEXT = GORULES_KNOWLEDGE_BASE + CRITICAL_CONSISTENCY_RULES + CRITICAL_DEBUGGING_KNOWLEDGE


# --------------------------------------------------------------------- ZEN is not JavaScript

def test_zen_rejects_the_operator_the_guidance_used_to_recommend():
    """The engine is the oracle: `||` does not lex, so no prompt may recommend it."""
    assert zen.validate_expression("tier == 'gold' || tier == 'platinum'") is not None
    assert zen.validate_expression("tier == 'gold' or tier == 'platinum'") is None


def test_the_switch_redundancy_advice_is_valid_zen():
    """The exact expression the debugging text now offers must evaluate."""
    assert zen.evaluate_expression("tier == 'gold' or tier == 'platinum'", {"tier": "platinum"})
    assert zen.evaluate_expression("tier in ['gold', 'platinum']", {"tier": "gold"})


def test_javascript_operators_are_only_ever_shown_inside_a_javascript_block():
    """`&&`, `===` and `||` are legal in a functionNode and nowhere else."""
    for line in ALL_TEXT.splitlines():
        if "||" in line or "&&" in line or "===" in line:
            # Either it is the JS example, or it is prose explaining that ZEN lacks these.
            assert (
                "javascript" in line.lower()
                or "creditRating" in line
                or "JavaScript" in line
                or "no**" in line or "has **no**" in line
                or "fails to lex" in line
                or "belong here and nowhere else" in line
            ), f"unqualified JS operator in ZEN guidance: {line.strip()[:120]}"


# --------------------------------------------------------------------- the four contradictions

def test_wildcard_guidance_names_the_layer_it_applies_to():
    """One text said "never write _", the other said "always write _"; both were right
    about different layers - the DSL you author versus the JSON the engine reads."""
    assert "Markdown DSL" in CRITICAL_CONSISTENCY_RULES
    # The old blanket prohibition must be gone.
    assert 'Do not add "_" as the value in any cell' not in GORULES_KNOWLEDGE_BASE
    assert 'Do not add "<root>" as inputField or outputPath' not in GORULES_KNOWLEDGE_BASE


def test_multiple_output_nodes_are_permitted_because_the_engine_permits_them():
    """The rules banned them outright; the engine accepts them, so it is a preference."""
    graph = {
        "contentType": "application/vnd.gorules.decision",
        "nodes": [
            {"id": "in", "type": "inputNode", "name": "In",
             "position": {"x": 0, "y": 0}, "content": {"schema": ""}},
            {"id": "sw", "type": "switchNode", "name": "Route", "position": {"x": 1, "y": 0},
             "content": {"hitPolicy": "first", "statements": [
                 {"id": "s1", "condition": "amount > 100", "isDefault": False},
                 {"id": "s2", "condition": "", "isDefault": True}]}},
            {"id": "ok", "type": "outputNode", "name": "Approved",
             "position": {"x": 2, "y": 0}, "content": {"schema": ""}},
            {"id": "no", "type": "outputNode", "name": "Rejected",
             "position": {"x": 2, "y": 1}, "content": {"schema": ""}},
        ],
        "edges": [
            {"id": "e1", "sourceId": "in", "targetId": "sw", "type": "edge"},
            {"id": "e2", "sourceId": "sw", "targetId": "ok", "sourceHandle": "s1", "type": "edge"},
            {"id": "e3", "sourceId": "sw", "targetId": "no", "sourceHandle": "s2", "type": "edge"},
        ],
    }
    decision = zen.ZenEngine().create_decision(json.dumps(graph))
    assert decision.validate() is None
    assert decision.evaluate({"amount": 500})["result"] == {"amount": 500}

    assert "SINGLE OUTPUT NODE" not in CRITICAL_CONSISTENCY_RULES
    assert "PREFER ONE OUTPUT NODE" in CRITICAL_CONSISTENCY_RULES


def test_validation_guidance_agrees_with_itself():
    """One text made the validation pattern mandatory, the other forbade it entirely."""
    assert "NO INPUT VALIDATION" not in CRITICAL_CONSISTENCY_RULES
    assert "VALIDATE ONLY WHAT WAS ASKED FOR" in CRITICAL_CONSISTENCY_RULES
    assert "never invent validation the requirements did not ask for" in GORULES_KNOWLEDGE_BASE


# --------------------------------------------------------------------- decomposition

def test_the_rules_ask_for_decomposition_not_a_single_giant_table():
    """Rule 1 used to say "combine multiple conditions into a single node ... to minimize
    the total number of nodes", which is how both shipped examples became one-table
    monoliths - the very shape the linter is meant to flag."""
    assert "minimize the total number of nodes" not in CRITICAL_CONSISTENCY_RULES
    assert "ONE RESPONSIBILITY PER NODE" in CRITICAL_CONSISTENCY_RULES
    # ...without swinging to the opposite error of inserting empty pass-through steps.
    assert "Do not add nodes that carry no logic" in CRITICAL_CONSISTENCY_RULES
