"""End-to-end chat API tests: streaming, resume, proposals, and persistence."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend import lang_graph_agent as agent
from backend.tests.test_agent_flow import GOOD_DSL, TESTS, FakeLLM


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """The configured app, not yet started.

    Kept separate from `client` so a test can start it more than once against the same
    database file, which is the only way to exercise what a restart does.
    """
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

    return app


@pytest.fixture
def client(app_env):
    with TestClient(app_env) as c:
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
    # The repair budget is derived from the run's wall clock rather than fixed, so assert
    # the property that matters: it is reported, and it cannot outlive the run it is spent
    # inside. A hardcoded ceiling was how 8 x 120s came to exceed the 900s run timeout.
    budget = progress[0]["max_attempts"]
    assert budget >= 1
    assert (budget - 1) * agent.LLM_TIMEOUT_SECONDS < agent.AGENT_RUN_TIMEOUT_SECONDS

    proposed = next(e for e in events if e["type"] == "graph_proposed")
    assert proposed["jdm"]["nodes"]
    assert proposed["test_report"]["summary"]["passed"] == len(TESTS)

    # Accepting with persist writes a real, agent-authored version.
    accept = client.post(
        f"/api/chat/threads/{thread_id}/proposal/accept", json={"persist": True}
    ).json()
    assert accept["draft"] is False
    graph = client.get(f"/api/graphs/{accept['graph_id']}").json()
    assert graph["name"] == "Shipping Fees"
    assert graph["test_count"] == len(TESTS)

    versions = client.get(f"/api/graphs/{accept['graph_id']}/versions").json()["versions"]
    assert versions[0]["author"] == "agent"


def test_accept_without_persist_returns_an_unsaved_draft(client):
    """A guest sees the graph on the canvas before deciding to keep it."""
    thread_id = client.post("/api/chat/threads", json={}).json()["id"]
    client.post(
        f"/api/chat/threads/{thread_id}/messages",
        json={"text": "Create a shipping policy.",
              "canvas": {"content": {"nodes": [], "edges": []}}},
    )
    read_events(client, thread_id)
    client.post(
        f"/api/chat/threads/{thread_id}/resume",
        json={"value": "Approve with above understanding & assumptions"},
    )
    read_events(client, thread_id)

    before = len(client.get("/api/graphs").json()["graphs"])
    accept = client.post(
        f"/api/chat/threads/{thread_id}/proposal/accept", json={}
    ).json()

    assert accept["draft"] is True
    assert accept["graph_id"] is None
    assert accept["content"]["nodes"], "the draft carries the graph for the canvas"
    assert accept["tests"]
    assert len(client.get("/api/graphs").json()["graphs"]) == before, (
        "a draft must not create a graph until the user saves it"
    )


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


# --------------------------------------------------------------------------
# Surviving a restart
#
# A run is an in-memory asyncio.Task, but its status is written to SQLite. Nothing
# reconciles the two after a crash, so a row still claiming `running` describes a task
# that cannot exist - and the client believes it, disabling the composer and offering a
# Stop button that will never find anything to cancel.
# --------------------------------------------------------------------------

def _force_status(tmp_path, thread_id: str, status: str) -> None:
    """Leave the row exactly as a process killed mid-turn would have left it."""
    import sqlite3

    conn = sqlite3.connect(tmp_path / "studio.db")
    with conn:
        conn.execute("UPDATE chat_threads SET status = ? WHERE id = ?", (status, thread_id))
    conn.close()


def test_a_conversation_left_running_by_a_crash_is_released_on_restart(app_env, tmp_path):
    with TestClient(app_env) as first:
        thread_id = first.post("/api/chat/threads", json={}).json()["id"]
        cookies = dict(first.cookies)

    _force_status(tmp_path, thread_id, "running")

    with TestClient(app_env) as restarted:
        restarted.cookies.update(cookies)
        state = restarted.get(f"/api/chat/threads/{thread_id}").json()

        assert state["status"] == "idle", (
            "the process that owned this run is gone, so the thread cannot still be running"
        )
        # The status is what the client gates its composer on, so releasing it has to
        # leave the conversation genuinely usable rather than merely relabelled.
        accepted = restarted.post(
            f"/api/chat/threads/{thread_id}/messages",
            json={"text": "Create a shipping policy.",
                  "canvas": {"content": {"nodes": [], "edges": []}}},
        )
        assert accepted.status_code == 202


def test_a_restart_releases_only_the_runs_that_died(app_env, tmp_path):
    """A paused conversation is not a crashed one; only `running` is stale by definition."""
    with TestClient(app_env) as first:
        threads = {
            status: first.post("/api/chat/threads", json={}).json()["id"]
            for status in ("running", "awaiting_input", "error", "idle")
        }
        cookies = dict(first.cookies)
        for status, thread_id in threads.items():
            _force_status(tmp_path, thread_id, status)

    with TestClient(app_env) as restarted:
        restarted.cookies.update(cookies)
        seen = {
            status: restarted.get(f"/api/chat/threads/{tid}").json()["status"]
            for status, tid in threads.items()
        }

    assert seen == {
        "running": "idle",
        "awaiting_input": "awaiting_input",
        "error": "error",
        "idle": "idle",
    }


def test_a_question_interrupted_by_a_crash_can_still_be_answered(app_env, tmp_path):
    """Dying between the checkpoint write and the status write is the awkward case.

    The agent really is waiting - the interrupt is in the checkpoint and survives - but the
    row says `running`. Releasing it to `idle` would render the question with every answer
    refused by `resume`, so the checkpoint has to win.
    """
    with TestClient(app_env) as first:
        thread_id = first.post("/api/chat/threads", json={}).json()["id"]
        first.post(
            f"/api/chat/threads/{thread_id}/messages",
            json={"text": "Create a shipping policy.",
                  "canvas": {"content": {"nodes": [], "edges": []}}},
        )
        read_events(first, thread_id)
        assert first.get(f"/api/chat/threads/{thread_id}").json()["status"] == "awaiting_input"
        cookies = dict(first.cookies)

    _force_status(tmp_path, thread_id, "running")

    with TestClient(app_env) as restarted:
        restarted.cookies.update(cookies)
        state = restarted.get(f"/api/chat/threads/{thread_id}").json()

        assert state["status"] == "awaiting_input"
        assert state["pending_interrupt"]["options"], "the question itself must survive too"

        answered = restarted.post(
            f"/api/chat/threads/{thread_id}/resume",
            json={"value": "Approve with above understanding & assumptions"},
        )
        assert answered.status_code == 202, "the answer must not be refused as NOT_AWAITING_INPUT"
        read_events(restarted, thread_id)


def test_stopping_a_run_that_no_longer_exists_still_resolves_the_conversation(
    app_env, tmp_path
):
    """Pressing Stop on a turn lost with a previous process must not be a no-op.

    `status` is the only thing telling the client a turn is over, so a Stop that reports
    "nothing to cancel" and changes nothing leaves the composer disabled behind a button
    that appears broken - which is what a stuck `running` row used to look like.
    """
    with TestClient(app_env) as client:
        thread_id = client.post("/api/chat/threads", json={}).json()["id"]
        _force_status(tmp_path, thread_id, "running")

        response = client.post(f"/api/chat/threads/{thread_id}/cancel")

        assert response.status_code == 200
        assert response.json()["cancelled"] is False, "there was genuinely no task to stop"
        assert client.get(f"/api/chat/threads/{thread_id}").json()["status"] == "idle"

        # And the stream is told, so a client that is watching settles without a reload.
        events = client.get(f"/api/chat/threads/{thread_id}/events").json()["events"]
        assert events[-1] == {**events[-1], "type": "done", "status": "cancelled"}
