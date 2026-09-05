"""The human's side of the conversation, and their verdict on what came back.

Every signal here was already known to the studio and kept by none of it. The requirement
lived in `chat_events`, which cascades away with the conversation. The question and its
answer arrived as two unrelated events a whole turn apart, with nothing joining them. The
rejection reason was returned to the client and stored nowhere. And a person editing the
graph the agent had just written - the most expensive label there is - was sitting in
`graph_versions.author` all along, waiting to be read.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend import corpus
from backend.corpus import store
# `app_env` and `client` bring up the real API against a temp database; `read_events`
# waits for a turn to actually finish.
from backend.tests.test_chat_api import app_env, client, read_events  # noqa: F401

BLANK = {"contentType": "application/vnd.gorules.decision", "nodes": [], "edges": []}


def interactions(kind: str | None = None) -> list[dict]:
    conn = sqlite3.connect(store.settings.corpus_db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM interactions"
        args: tuple = ()
        if kind:
            sql += " WHERE kind = ?"
            args = (kind,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY rowid", args)]
    finally:
        conn.close()


def graph_with(nodes: list[dict], edges: list[dict] | None = None) -> dict:
    return {"contentType": "application/vnd.gorules.decision",
            "nodes": nodes, "edges": edges or []}


def fee_node(value: str, x: int = 400) -> dict:
    return {"id": "n-fee", "name": "Fee", "type": "expressionNode",
            "position": {"x": x, "y": 200},
            "content": {"expressions": [{"id": "e1", "key": "shippingFee", "value": value}]}}


# ------------------------------------------------------------------ conversation

def test_the_requirement_is_kept_in_the_users_own_words(client):
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(f"/api/chat/threads/{thread_id}/messages",
                json={"text": "Free shipping over $50, otherwise $6.",
                      "canvas": {"content": {"nodes": [], "edges": []}}})
    read_events(client, thread_id)

    asked, = interactions("requirement")
    assert asked["response"] == "Free shipping over $50, otherwise $6."
    assert asked["thread_id"] == thread_id
    assert asked["run_id"], "a requirement with no run cannot be joined to what it produced"


def test_a_question_and_its_answer_are_stored_as_one_pair(client):
    """They arrive a whole turn apart - the question ends one run and the answer starts the
    next - and the studio held nothing that connected them."""
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(f"/api/chat/threads/{thread_id}/messages",
                json={"text": "Create a shipping policy.",
                      "canvas": {"content": {"nodes": [], "edges": []}}})
    read_events(client, thread_id)

    open_question, = interactions("clarification")
    assert open_question["answered_at"] is None, "still waiting"
    assert open_question["prompt"]
    assert "Approve with above understanding & assumptions" in json.loads(
        open_question["options_json"]
    )

    client.post(f"/api/chat/threads/{thread_id}/resume",
                json={"value": "Approve with above understanding & assumptions"})
    read_events(client, thread_id)

    # Two rows now: the triage question, closed by the answer - and the final-approval
    # gate that answering it led to, open in its turn.
    rows = {r["interaction_id"]: r for r in interactions("clarification")}
    answered = rows[open_question["interaction_id"]]
    assert answered["response"] == "Approve with above understanding & assumptions"
    assert answered["answered_at"] is not None, "the same row was closed, not a new one"

    still_open = [r for r in rows.values() if r["answered_at"] is None]
    assert len(still_open) == 1
    assert "review" in still_open[0]["prompt"].lower(), "the approval gate is now waiting"


def test_the_chips_that_were_offered_are_kept_with_the_question(client):
    """Which options were on the table is what makes the answer interpretable: "Approve"
    means nothing without knowing what else could have been chosen."""
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(f"/api/chat/threads/{thread_id}/messages",
                json={"text": "Create a shipping policy.",
                      "canvas": {"content": {"nodes": [], "edges": []}}})
    read_events(client, thread_id)

    options = json.loads(interactions("clarification")[0]["options_json"])
    assert len(options) >= 2


def test_an_answer_to_nothing_is_still_the_user_speaking():
    """A resume with no question open is not a clarification, but it is still what the
    person said, and dropping it would lose a turn of the conversation."""
    with corpus.run_scope("run-1", thread_id="t-1"):
        assert corpus.answer_open_question("t-1", "actually make it $8") is False
    corpus.record_interaction(kind="requirement", thread_id="t-1",
                              response="actually make it $8")

    assert interactions("requirement")[0]["response"] == "actually make it $8"


# ---------------------------------------------------------------------- verdicts

def approved_proposal(client) -> str:
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(f"/api/chat/threads/{thread_id}/messages",
                json={"text": "Create a shipping policy.",
                      "canvas": {"content": {"nodes": [], "edges": []}}})
    read_events(client, thread_id)
    client.post(f"/api/chat/threads/{thread_id}/resume",
                json={"value": "Approve with above understanding & assumptions"})
    read_events(client, thread_id)
    return thread_id


def test_taking_a_proposal_is_recorded(client):
    thread_id = approved_proposal(client)

    accepted = client.post(f"/api/chat/threads/{thread_id}/proposal/accept", json={})
    assert accepted.status_code == 200

    approval, = interactions("approval")
    assert approval["response"] == "accepted"
    assert approval["thread_id"] == thread_id


def test_a_rejection_keeps_the_reason_it_was_rejected_for(client):
    """The reason was echoed back to the client and stored nowhere. A rejected proposal
    with the reason attached is the negative half of a preference pair; without it, it is
    only a shrug."""
    thread_id = approved_proposal(client)

    rejected = client.post(f"/api/chat/threads/{thread_id}/proposal/reject",
                           json={"reason": "the threshold should be $75, not $50"})
    assert rejected.status_code == 200

    rejection, = interactions("rejection")
    assert rejection["response"] == "the threshold should be $75, not $50"
    detail = json.loads(rejection["detail_json"])
    assert detail["reason"] == "the threshold should be $75, not $50"
    assert detail["jdm"], "the graph that was turned down is kept with the reason"


# ------------------------------------------------------------------- corrections

def test_editing_the_agents_graph_is_recorded_as_a_correction(client):
    from backend.db import dao

    created = client.post("/api/graphs", json={"name": "Shipping", "content": BLANK}).json()
    graph_id = created["id"]

    version = _run(dao.save_version(graph_id, graph_with([fee_node("orderTotal > 50 ? 0 : 6")]),
                                    message="Applied agent changes", author="agent"))

    corrected = graph_with([fee_node("orderTotal > 75 ? 0 : 6")])
    saved = client.put(f"/api/graphs/{graph_id}",
                       json={"content": corrected, "message": "fix the threshold"})
    assert saved.status_code == 200

    correction, = interactions("correction")
    detail = json.loads(correction["detail_json"])
    assert detail["from_version"] == version
    assert detail["change"]["nodes_changed"][0]["name"] == "Fee"
    # Both graphs in full: the corpus exists because the studio's own record can be
    # deleted, and a pair that stops being readable with its policy is worth nothing.
    assert detail["before"]["nodes"][0]["content"]["expressions"][0]["value"].endswith("50 ? 0 : 6")
    assert detail["after"]["nodes"][0]["content"]["expressions"][0]["value"].endswith("75 ? 0 : 6")


def test_moving_a_node_is_not_a_correction(client):
    """Opening a policy makes the editor normalise it and dragging a node writes a version.
    Neither is a person disagreeing with the model, and training on them would teach it
    that correct output was wrong."""
    from backend.db import dao

    created = client.post("/api/graphs", json={"name": "Shipping", "content": BLANK}).json()
    graph_id = created["id"]
    _run(dao.save_version(graph_id, graph_with([fee_node("orderTotal > 50 ? 0 : 6", x=400)]),
                          message="Applied agent changes", author="agent"))

    moved = graph_with([fee_node("orderTotal > 50 ? 0 : 6", x=900)])
    assert client.put(f"/api/graphs/{graph_id}", json={"content": moved}).status_code == 200

    assert interactions("correction") == []


def test_a_graph_the_agent_never_touched_produces_no_correction(client):
    created = client.post("/api/graphs", json={"name": "Hand drawn", "content": BLANK}).json()

    client.put(f"/api/graphs/{created['id']}",
               json={"content": graph_with([fee_node("1")]), "message": "first"})
    client.put(f"/api/graphs/{created['id']}",
               json={"content": graph_with([fee_node("2")]), "message": "second"})

    assert interactions("correction") == [], (
        "nothing here is a correction: the person is editing their own work"
    )


def test_a_correction_is_measured_from_the_agents_version_not_the_last_one(client):
    """A person keeps editing after the agent's version, so the previous version is soon
    their own. Comparing against that would measure them against themselves and lose the
    only pair worth having.
    """
    from backend.db import dao

    created = client.post("/api/graphs", json={"name": "Shipping", "content": BLANK}).json()
    graph_id = created["id"]
    agent_version = _run(dao.save_version(
        graph_id, graph_with([fee_node("orderTotal > 50 ? 0 : 6")]),
        message="Applied agent changes", author="agent",
    ))

    # Two separate human saves, each its own version.
    client.put(f"/api/graphs/{graph_id}",
               json={"content": graph_with([fee_node("orderTotal > 60 ? 0 : 6")]),
                     "message": "first pass"})
    client.put(f"/api/graphs/{graph_id}",
               json={"content": graph_with([fee_node("orderTotal > 75 ? 0 : 6")]),
                     "message": "second pass"})

    correction, = interactions("correction")
    detail = json.loads(correction["detail_json"])
    assert detail["from_version"] == agent_version
    assert detail["before"]["nodes"][0]["content"]["expressions"][0]["value"] == (
        "orderTotal > 50 ? 0 : 6"
    ), "the baseline is what the agent wrote, not the person's own previous save"
    assert detail["after"]["nodes"][0]["content"]["expressions"][0]["value"] == (
        "orderTotal > 75 ? 0 : 6"
    )


def test_a_settling_edit_replaces_its_own_earlier_snapshot(client):
    """A person's edit is not one event: consecutive autosaves coalesce into a single
    version row whose content keeps changing. Without replacement the corpus would hold a
    half-finished edit next to the finished one and no way to tell them apart."""
    from backend.db import dao

    created = client.post("/api/graphs", json={"name": "Shipping", "content": BLANK}).json()
    graph_id = created["id"]
    _run(dao.save_version(graph_id, graph_with([fee_node("orderTotal > 50 ? 0 : 6")]),
                          message="Applied agent changes", author="agent"))

    for value in ("orderTotal > 6 ? 0 : 6", "orderTotal > 7 ? 0 : 6",
                  "orderTotal > 75 ? 0 : 6"):
        client.put(f"/api/graphs/{graph_id}",
                   json={"content": graph_with([fee_node(value)]), "autosave": True})

    rows = interactions("correction")
    assert len(rows) == 1, "one correction per agent version, holding where it settled"
    after = json.loads(rows[0]["detail_json"])["after"]
    assert after["nodes"][0]["content"]["expressions"][0]["value"] == "orderTotal > 75 ? 0 : 6"


def test_a_failing_corpus_does_not_fail_the_save(client, monkeypatch):
    """Two separate ways this can break a save, so both are held down.

    The corpus write is protected by `store._never_fails`; the *lookup* that feeds it is a
    query against the studio's own database, outside that protection entirely.
    """
    from backend.api import graphs as graphs_api
    from backend.db import dao

    created = client.post("/api/graphs", json={"name": "Shipping", "content": BLANK}).json()
    graph_id = created["id"]
    _run(dao.save_version(graph_id, graph_with([fee_node("1")]),
                          message="agent", author="agent"))

    monkeypatch.setattr(store, "_connect",
                        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    assert client.put(f"/api/graphs/{graph_id}",
                      json={"content": graph_with([fee_node("2")])}).status_code == 200

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(graphs_api.dao, "latest_agent_version", unavailable)
    assert client.put(f"/api/graphs/{graph_id}",
                      json={"content": graph_with([fee_node("3")])}).status_code == 200


# --------------------------------------------------------------------------- util

def _run(coro):
    """Drive a dao coroutine from a sync test, on the loop the app is not using."""
    import asyncio

    return asyncio.run(coro)
