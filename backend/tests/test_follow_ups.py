"""What the assistant offers once a turn is over.

A finished turn used to end in silence. The linter was the clearest casualty: reachable in
conversation since it was built, offered nowhere, and so - from the outside - absent.

The chips are only worth having if pressing one does what it says, and that is not a
property of the chip alone: it is a property of the chip *and* the intent router's
patterns, which can drift apart silently. `test_every_chip_routes_where_it_claims` is the
join between them.
"""

from __future__ import annotations

import json

import pytest

from backend import lang_graph_agent as agent
from backend.services import suggestions

POLICY = json.dumps({"nodes": [{"id": "a", "type": "inputNode"}]})

# What each chip promises to do, by the prompt it sends.
ROUTES = {
    suggestions.RUN_TESTS["prompt"]: "TEST",
    suggestions.CHECK_PROBLEMS["prompt"]: "LINT",
    suggestions.EXPLAIN["prompt"]: "EXPLAIN",
    suggestions.FIX_FAILURES["prompt"]: "MODIFY",
    suggestions.FIX_FINDINGS["prompt"]: "MODIFY",
}


@pytest.mark.parametrize("prompt,expected", sorted(ROUTES.items()))
def test_every_chip_routes_where_it_claims(prompt, expected, monkeypatch):
    """And does it on the rules, not the model.

    `_classify_intent` falls back to an LLM call when its patterns cannot decide. A chip
    that needs that fallback costs a request out of the day's allowance to work out what
    the user meant by a sentence the app wrote itself, so the model is made unavailable
    here: reaching it is the failure.
    """
    def refuse(*args, **kwargs):
        raise AssertionError(f"{prompt!r} did not match any rule and fell through to the model")

    monkeypatch.setattr(agent, "call_llm", refuse)
    intent, confidence = agent._classify_intent(prompt, has_graph=True)
    assert intent == expected, f"{prompt!r} routes to {intent}, not {expected}"
    assert confidence >= 0.8


def test_the_change_chip_does_not_send():
    """"Change this policy so that " is half a sentence. Sending it would spend a turn
    asking the question the user was about to answer."""
    assert suggestions.CHANGE["send"] is False
    assert all(chip["send"] for chip in (suggestions.RUN_TESTS, suggestions.CHECK_PROBLEMS,
                                         suggestions.EXPLAIN, suggestions.FIX_FAILURES,
                                         suggestions.FIX_FINDINGS))


def labels(values: dict) -> list[str]:
    return [chip["label"] for chip in suggestions.follow_ups(values)]


def test_nothing_is_offered_when_there_is_no_policy():
    """Every chip is a thing to do *to* a policy."""
    assert suggestions.follow_ups({"intent": "CREATE"}) == []
    assert suggestions.follow_ups({"intent": "CREATE", "jdm_json": "{}"}) == []


@pytest.mark.parametrize("intent", ["CREATE", "MODIFY", "EXPLAIN"])
def test_a_policy_you_have_just_read_or_received_is_offered_the_checks(intent):
    offered = labels({"intent": intent, "jdm_json": POLICY})
    assert offered == [suggestions.RUN_TESTS["label"],
                       suggestions.CHECK_PROBLEMS["label"],
                       suggestions.CHANGE["label"]]


def test_a_failing_run_leads_with_the_failures():
    offered = labels({
        "intent": "TEST",
        "existing_jdm_json": POLICY,
        "evaluation_feedback": json.dumps({"total": 3, "passed": 1, "failed": 2, "errored": 0}),
    })
    assert offered[0] == suggestions.FIX_FAILURES["label"]


def test_a_passing_run_does_not_offer_to_fix_anything():
    offered = labels({
        "intent": "TEST",
        "existing_jdm_json": POLICY,
        "evaluation_feedback": json.dumps({"total": 3, "passed": 3, "failed": 0, "errored": 0}),
    })
    assert suggestions.FIX_FAILURES["label"] not in offered
    assert offered == [suggestions.EXPLAIN["label"], suggestions.CHANGE["label"]]


def test_a_run_that_could_not_report_a_summary_claims_no_failure():
    """`evaluation_feedback` is prose when the engine could not run the graph at all. An
    unreadable summary is not evidence of a failing case."""
    offered = labels({"intent": "TEST", "existing_jdm_json": POLICY,
                      "evaluation_feedback": "the engine could not run this graph"})
    assert suggestions.FIX_FAILURES["label"] not in offered


def test_lint_offers_the_fix_only_when_it_found_something():
    clean = {"intent": "LINT", "existing_jdm_json": POLICY, "lint_findings": []}
    dirty = {"intent": "LINT", "existing_jdm_json": POLICY,
             "lint_findings": [{"code": "MONOLITHIC_GRAPH", "severity": "hint"}]}

    assert labels(clean) == [suggestions.RUN_TESTS["label"], suggestions.CHANGE["label"]]
    assert labels(dirty)[0] == suggestions.FIX_FINDINGS["label"]


def test_the_linter_is_offered_on_a_policy_the_user_just_opened():
    """The reported gap: nothing on the opening screen of an existing policy mentioned it.

    The empty-state list lives in the frontend, so this reads it there rather than
    describing it twice.
    """
    from pathlib import Path

    pane = Path("frontend/components/chat/ChatPane.tsx").read_text(encoding="utf-8")
    block = pane.split("const EXISTING_POLICY_SUGGESTIONS = [", 1)[1].split("];", 1)[0]
    assert suggestions.CHECK_PROBLEMS["prompt"] in block, (
        "an existing policy is opened with no way of discovering the linter"
    )
