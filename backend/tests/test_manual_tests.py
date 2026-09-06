"""Putting a hand-written case to the policy before committing to it.

The useful question when typing a test is not "does it pass" - the author already believes
it should - but "which of us is wrong, the policy or my expectation". So the check answers
three things separately, because a case can be wrong in three separate ways: the graph
will not run the input at all, the expectation names a field the graph cannot produce, or
it ran and disagreed.

The middle one is what earns the endpoint. A misspelt field name reads as `expected 0, got
null`, which looks like a policy defect - and misspelling a field is the mistake people
actually make at a JSON textarea.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.tests.test_chat_api import app_env, client  # noqa: F401

CONTENT = {
    "contentType": "application/vnd.gorules.decision",
    "nodes": [
        {"id": "i", "name": "Request", "type": "inputNode",
         "content": {"schema": ""}, "position": {"x": 0, "y": 0}},
        {"id": "f", "name": "Fee", "type": "expressionNode", "position": {"x": 1, "y": 0},
         "content": {"expressions": [
             {"id": "e", "key": "shippingFee", "value": "orderTotal > 50 ? 0 : 6"}]}},
        {"id": "o", "name": "Response", "type": "outputNode",
         "content": {"schema": ""}, "position": {"x": 2, "y": 0}},
    ],
    "edges": [{"id": "a", "sourceId": "i", "targetId": "f", "type": "edge"},
              {"id": "b", "sourceId": "f", "targetId": "o", "type": "edge"}],
}


def check(client, inp, expected, content=None):
    return client.post("/api/tests/check", json={
        "content": content or CONTENT, "input": inp, "expectedOutput": expected,
    }).json()


def test_an_expectation_the_policy_agrees_with(client):
    r = check(client, {"orderTotal": 80}, {"shippingFee": 0})

    assert r["ran"] is True
    assert r["matches"] is True
    assert r["actual"] == {"shippingFee": 0}
    assert r["mismatches"] == []


def test_an_honest_disagreement_is_reported_as_one(client):
    r = check(client, {"orderTotal": 80}, {"shippingFee": 6})

    assert r["ran"] is True and r["matches"] is False
    assert r["unknown_fields"] == [], "the field is real; the value is what differs"
    assert r["mismatches"][0]["path"] == "shippingFee"


def test_a_misspelt_field_is_called_a_misspelling_not_a_policy_bug(client):
    """Without this it comes back as `expected 0, got null`, which reads as the policy
    being broken."""
    r = check(client, {"orderTotal": 80}, {"shipingFee": 0})

    assert r["unknown_fields"] == ["shipingFee"]
    assert r["produces"] == ["shippingFee"], "and says what it could have meant"


def test_asserting_a_field_the_policy_never_writes(client):
    r = check(client, {"orderTotal": 80}, {"shippingFee": 0, "tax": 2})

    assert r["unknown_fields"] == ["tax"]


def test_an_empty_expectation_is_not_a_pass(client):
    """A case with nothing asserted is skipped rather than passed. Reporting that as a
    match would let an empty form show a green tick."""
    r = check(client, {"orderTotal": 80}, {})

    assert r["ran"] is True
    assert r["matches"] is False


def test_an_input_the_policy_refuses_says_so_instead_of_comparing(client):
    """A schema that rejects the input means nothing came back, so there is no actual to
    set beside the expectation."""
    guarded = json.loads(json.dumps(CONTENT))
    guarded["nodes"][0]["content"]["schema"] = json.dumps(
        {"type": "object", "required": ["customerId"]})

    r = check(client, {"orderTotal": 80}, {"shippingFee": 0}, content=guarded)

    assert r["ran"] is False
    assert r["error"]
    assert r["actual"] is None


def test_the_interface_comes_back_so_the_author_is_not_guessing(client):
    """The field names the graph reads and writes, which the form shows beside each box -
    spelling from memory is exactly what goes wrong here."""
    r = check(client, {"orderTotal": 80}, {"shippingFee": 0})

    assert r["accepts"] == ["orderTotal"]
    assert r["produces"] == ["shippingFee"]


def test_checking_saves_nothing(client):
    """Stateless, like the ad-hoc run beside it. A draft being tried out must not appear in
    anybody's suite."""
    created = client.post("/api/graphs", json={"name": "Draft check", "content": CONTENT}).json()

    check(client, {"orderTotal": 80}, {"shippingFee": 0})

    assert client.get(f"/api/graphs/{created['id']}/tests").json()["tests"] == []
