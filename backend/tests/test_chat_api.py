"""End-to-end chat API tests: streaming, resume, proposals, and persistence."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import lang_graph_agent as agent
from backend.tests.test_agent_flow import GOOD_DSL, TESTS, FakeLLM


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setattr(agent, "_repo_path", lambda *p: tmp_path.joinpath(*p))

    from backend.config import get_settings

    get_settings.cache_clear()
    import backend.config as config_module

    config_module.settings = get_settings()

    # Rebind the modules that captured `settings` at import time.
    for module_name in ("backend.db.connection", "backend.db.bootstrap", "backend.agent_runtime",
                        "backend.services.chat_runner"):
        import importlib

        module = importlib.import_module(module_name)
        if hasattr(module, "settings"):
            monkeypatch.setattr(module, "settings", config_module.settings)

    monkeypatch.setattr(agent, "call_llm", FakeLLM([GOOD_DSL]))

    from backend.main import app

    with TestClient(app) as c:
        yield c


def read_events(client, thread_id: str, from_seq: int = 0, timeout: float = 30.0) -> list[dict]:
    """Wait for the in-flight turn to finish, then return its events.

    Assertions read the persisted event log rather than the SSE socket: the
    events are identical (the stream is a live view of this table), and a real
    browser disconnect cannot be simulated through TestClient. Actual streaming
    is covered against a live server in test_sse_stream.py.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = client.get(
            f"/api/chat/threads/{thread_id}/events?from_seq={from_seq}"
        ).json()["events"]
        if events and events[-1]["type"] == "done":
            return events
        time.sleep(0.05)
    raise AssertionError(f"Turn on {thread_id} did not finish within {timeout}s")


def test_create_policy_end_to_end(client):
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]

    accepted = client.post(
        f"/api/chat/threads/{thread_id}/messages",
        json={
            "text": "Create a shipping policy: free over $50, otherwise $6.",
            "canvas": {"content": {"nodes": [], "edges": []}},
        },
    )
    assert accepted.status_code == 202
    assert accepted.json()["run_id"]

    events = read_events(client, thread_id)
    kinds = [e["type"] for e in events]
    assert "run_started" in kinds
    assert "node_start" in kinds
    assert kinds[-1] == "done"

    # The turn stopped at a human gate, and the chips came through verbatim.
    interrupt = next(e for e in events if e["type"] == "interrupt")
    assert interrupt["kind"] == "choice"
    assert "Approve with above understanding & assumptions" in interrupt["options"]
    assert events[-1]["status"] == "awaiting_input"

    state = client.get(f"/api/chat/threads/{thread_id}").json()
    assert state["status"] == "awaiting_input"
    assert state["pending_interrupt"]["options"] == interrupt["options"]

    # Resuming with the exact chip label drives it through build to the next gate.
    resumed = client.post(
        f"/api/chat/threads/{thread_id}/resume",
        json={"value": "Approve with above understanding & assumptions"},
    )
    assert resumed.status_code == 202

    events = read_events(client, thread_id)
    progress = [e for e in events if e["type"] == "progress"]
    assert progress, "the builder loop must report live progress"
    assert progress[0]["node"] == "builder_node"
    assert progress[0]["max_attempts"] == 8

    proposed = next(e for e in events if e["type"] == "graph_proposed")
    assert proposed["jdm"]["nodes"]
    assert proposed["test_report"]["summary"]["passed"] == len(TESTS)

    # Accepting writes a real, agent-authored version.
    accept = client.post(f"/api/chat/threads/{thread_id}/proposal/accept", json={}).json()
    graph = client.get(f"/api/graphs/{accept['graph_id']}").json()
    assert graph["name"] == "Shipping Fees"
    assert graph["test_count"] == len(TESTS)

    versions = client.get(f"/api/graphs/{accept['graph_id']}/versions").json()["versions"]
    assert versions[0]["author"] == "agent"


def test_resume_rejected_when_not_awaiting_input(client):
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    response = client.post(f"/api/chat/threads/{thread_id}/resume", json={"value": "hi"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NOT_AWAITING_INPUT"


def test_events_replay_from_seq(client):
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(
        f"/api/chat/threads/{thread_id}/messages",
        json={"text": "Create a shipping policy.", "canvas": {"content": {"nodes": [], "edges": []}}},
    )
    all_events = read_events(client, thread_id)
    assert len(all_events) > 2

    cutoff = all_events[1]["seq"]
    replayed = client.get(
        f"/api/chat/threads/{thread_id}/events?from_seq={cutoff}"
    ).json()["events"]

    assert all(e["seq"] > cutoff for e in replayed)
    assert len(replayed) == len(all_events) - 2


def test_proposal_downloads_before_acceptance(client):
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(
        f"/api/chat/threads/{thread_id}/messages",
        json={"text": "Create a shipping policy.", "canvas": {"content": {"nodes": [], "edges": []}}},
    )
    read_events(client, thread_id)
    client.post(
        f"/api/chat/threads/{thread_id}/resume",
        json={"value": "Approve with above understanding & assumptions"},
    )
    read_events(client, thread_id)

    bundle = client.get(f"/api/chat/threads/{thread_id}/proposal/export?format=bundle")
    assert bundle.status_code == 200
    assert bundle.headers["content-disposition"].endswith('.zip"')
    assert bundle.content[:2] == b"PK"

    jdm = client.get(f"/api/chat/threads/{thread_id}/proposal/export?format=jdm")
    assert json.loads(jdm.content)["nodes"]


def test_thread_not_found(client):
    assert client.get("/api/chat/threads/missing").status_code == 404
    assert client.get("/api/chat/threads/missing").json()["error"]["code"] == "THREAD_NOT_FOUND"
