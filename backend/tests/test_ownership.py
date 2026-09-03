"""Isolation between guests, and between guests and the admin.

These are the tests that matter most in this change: everything else is a
convenience, but a leak here would show one visitor another visitor's policies.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery staple")
    # A fixed secret so cookies stay valid for the life of the test.
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-a-real-key")

    from backend.config import get_settings

    get_settings.cache_clear()
    import backend.config as config_module

    config_module.settings = get_settings()
    for name in ("backend.db.connection", "backend.db.bootstrap", "backend.agent_runtime",
                 "backend.services.chat_runner", "backend.auth", "backend.api.auth"):
        module = importlib.import_module(name)
        if hasattr(module, "settings"):
            monkeypatch.setattr(module, "settings", config_module.settings)

    from backend.main import app

    return app


def guest(app) -> TestClient:
    """A client with its own cookie jar, i.e. its own guest session."""
    return TestClient(app)


def make_graph(client, name: str) -> str:
    response = client.post("/api/graphs", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_two_guests_cannot_see_each_others_graphs(app_env):
    with guest(app_env) as alice, guest(app_env) as bob:
        alice_graph = make_graph(alice, "Alice Policy")
        bob_graph = make_graph(bob, "Bob Policy")

        assert [g["name"] for g in alice.get("/api/graphs").json()["graphs"]] == [
            "Alice Policy"
        ]
        assert [g["name"] for g in bob.get("/api/graphs").json()["graphs"]] == [
            "Bob Policy"
        ]

        # A direct hit on someone else's id is a 404, not a 403: a 403 would
        # confirm the id exists.
        assert bob.get(f"/api/graphs/{alice_graph}").status_code == 404
        assert alice.get(f"/api/graphs/{bob_graph}").status_code == 404


def test_another_guest_cannot_modify_or_delete(app_env):
    with guest(app_env) as alice, guest(app_env) as bob:
        gid = make_graph(alice, "Alice Policy")

        assert bob.patch(f"/api/graphs/{gid}", json={"name": "Hijacked"}).status_code == 404
        assert bob.delete(f"/api/graphs/{gid}").status_code == 404
        assert bob.get(f"/api/graphs/{gid}/versions").status_code == 404
        assert bob.post(f"/api/graphs/{gid}/tests/run", json={}).status_code == 404

        assert alice.get(f"/api/graphs/{gid}").json()["name"] == "Alice Policy"


def test_guests_may_reuse_the_same_policy_name(app_env):
    """Slugs are unique per owner, so two guests can both have a Refund Policy."""
    with guest(app_env) as alice, guest(app_env) as bob:
        make_graph(alice, "Refund Policy")
        make_graph(bob, "Refund Policy")

        assert alice.get("/api/graphs").json()["graphs"][0]["slug"] == "refund-policy"
        assert bob.get("/api/graphs").json()["graphs"][0]["slug"] == "refund-policy"


def test_seeded_policies_belong_to_the_admin_not_to_guests(app_env):
    with guest(app_env) as visitor:
        assert visitor.get("/api/graphs").json()["graphs"] == []

        visitor.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": "correct horse battery staple"},
        )
        names = {g["name"] for g in visitor.get("/api/graphs").json()["graphs"]}
        assert {"Refund Policy", "Loan Approval Policy"} <= names


def test_threads_are_scoped_to_their_owner(app_env):
    with guest(app_env) as alice, guest(app_env) as bob:
        thread_id = alice.post("/api/chat/threads", json={}).json()["id"]

        assert bob.get(f"/api/chat/threads/{thread_id}").status_code == 404
        assert bob.get(f"/api/chat/threads/{thread_id}/events").status_code == 404
        assert bob.post(
            f"/api/chat/threads/{thread_id}/messages",
            json={"text": "hi", "canvas": {"content": {"nodes": [], "edges": []}}},
        ).status_code == 404
        assert alice.get(f"/api/chat/threads/{thread_id}").status_code == 200


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def test_session_survives_requests_and_identifies_a_guest(app_env):
    with guest(app_env) as client:
        first = client.get("/api/auth/me").json()
        assert first["mode"] == "guest"
        assert first["login_enabled"] is True

        gid = make_graph(client, "Kept Policy")
        # Same cookie jar -> same owner -> the graph is still there.
        assert client.get(f"/api/graphs/{gid}").status_code == 200


def test_login_logout_round_trip(app_env):
    with guest(app_env) as client:
        bad = client.post(
            "/api/auth/login", json={"email": "admin@example.test", "password": "wrong"}
        )
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] == "INVALID_CREDENTIALS"

        ok = client.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": "correct horse battery staple"},
        )
        assert ok.status_code == 200
        assert ok.json() == {"mode": "admin", "email": "admin@example.test",
                             "login_enabled": True}
        assert client.get("/api/auth/me").json()["mode"] == "admin"

        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").json()["mode"] == "guest"


def test_unknown_email_is_rejected_like_a_bad_password(app_env):
    with guest(app_env) as client:
        response = client.post(
            "/api/auth/login", json={"email": "nobody@example.test", "password": "x"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_a_forged_cookie_is_treated_as_a_new_guest(app_env):
    with guest(app_env) as client:
        gid = make_graph(client, "Private Policy")
        client.cookies.set("jdm_sid", "eyJvd25lciI6InVzZXI6YWRtaW4ifQ.forged")

        assert client.get("/api/auth/me").json()["mode"] == "guest"
        assert client.get(f"/api/graphs/{gid}").status_code == 404


def test_export_all_bundles_every_owned_policy(app_env):
    import io
    import zipfile

    with guest(app_env) as client:
        make_graph(client, "First Policy")
        make_graph(client, "Second Policy")

        response = client.get("/api/graphs/export-all")
        assert response.status_code == 200, response.text
        names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()

        assert "first-policy/first-policy_jdm.json" in names
        assert "second-policy/second-policy_tests.json" in names
        assert "README.md" in names


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def test_migration_backfills_an_existing_database(tmp_path, monkeypatch):
    """A database written before ownership must survive the upgrade intact.

    The `graphs` table is rebuilt (its slug carried a global UNIQUE), so this
    guards the risk that dropping the old table takes its cascading children -
    versions, tests and runs - with it.
    """
    import asyncio
    import json
    import sqlite3

    db = tmp_path / "legacy.db"
    old = sqlite3.connect(db)
    old.executescript(
        """
        CREATE TABLE graphs (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
          description TEXT NOT NULL DEFAULT '', current_version INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
        );
        CREATE TABLE graph_versions (
          graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
          version INTEGER NOT NULL, content TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
          author TEXT NOT NULL DEFAULT 'user', is_autosave INTEGER NOT NULL DEFAULT 0,
          thread_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY (graph_id, version)
        );
        CREATE TABLE test_cases (
          id TEXT PRIMARY KEY,
          graph_id TEXT NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
          name TEXT NOT NULL, input_json TEXT NOT NULL, expected_json TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE chat_threads (
          id TEXT PRIMARY KEY, graph_id TEXT, title TEXT NOT NULL DEFAULT 'New chat',
          status TEXT NOT NULL DEFAULT 'idle',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    content = json.dumps({"nodes": [{"id": "n1"}], "edges": []})
    old.execute(
        "INSERT INTO graphs VALUES ('g1','Legacy Policy','legacy-policy','',2,'t','t',NULL)"
    )
    old.executemany(
        "INSERT INTO graph_versions (graph_id, version, content, created_at)"
        " VALUES (?,?,?,'t')",
        [("g1", 1, content), ("g1", 2, content)],
    )
    old.execute(
        "INSERT INTO test_cases VALUES ('t1','g1','case','{}','{}',1,0,'t','t')"
    )
    old.execute("INSERT INTO chat_threads VALUES ('th1','g1','Chat','idle','t','t')")
    old.commit()
    old.close()

    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    from backend.config import get_settings

    get_settings.cache_clear()
    import backend.config as config_module

    config_module.settings = get_settings()
    for name in ("backend.db.connection", "backend.db.bootstrap"):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "settings", config_module.settings)

    from backend.db import bootstrap, connection, dao

    async def run():
        await connection.connect()
        try:
            await bootstrap.apply_schema()
            await bootstrap.migrate_owners()
            graph = await dao.get_graph("g1", owner="user:admin")
            versions = await dao.list_versions("g1")
            tests = await dao.list_tests("g1")
            thread = await dao.get_thread("th1", owner="user:admin")
            return graph, versions, tests, thread
        finally:
            await connection.disconnect()

    graph, versions, tests, thread = asyncio.run(run())

    assert graph is not None and graph["name"] == "Legacy Policy"
    assert graph["owner_id"] == "user:admin"
    assert len(versions) == 2, "cascading versions must survive the table rebuild"
    assert len(tests) == 1, "cascading test cases must survive the table rebuild"
    assert thread is not None and thread["owner_id"] == "user:admin"


def test_signing_out_returns_to_the_same_guest(app_env):
    """Work made as a guest must still be there after a sign-in round trip.

    Guest policies are keyed to the anonymous session id, so minting a fresh
    guest on sign-out would silently hide them.
    """
    with guest(app_env) as client:
        gid = make_graph(client, "Work In Progress")

        client.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": "correct horse battery staple"},
        )
        assert client.get("/api/auth/me").json()["mode"] == "admin"
        # The admin does not see the guest's draft ...
        assert client.get(f"/api/graphs/{gid}").status_code == 404

        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").json()["mode"] == "guest"
        # ... but it comes back on sign-out.
        assert client.get(f"/api/graphs/{gid}").json()["name"] == "Work In Progress"


def test_each_cold_request_mints_its_own_session(app_env):
    """Documents *why* the client serialises its first call.

    Minting is lazy and per-request, so several requests arriving with no
    cookie each get their own identity. The browser keeps only the last
    Set-Cookie, which would strand anything created under the others - a chat
    thread made that way 404s on the very next call. `ensureSession` in
    frontend/lib/api.ts is what prevents that, and this test pins the server
    behaviour it compensates for.
    """
    sessions = set()
    for _ in range(3):
        with guest(app_env) as cold:  # a fresh cookie jar each time
            cold.get("/api/auth/me")
            sessions.add(cold.cookies.get("jdm_sid"))

    assert len(sessions) == 3, "cold requests are expected to mint distinct guests"


def test_a_thread_is_invisible_to_a_different_guest_session(app_env):
    """The concrete failure the bootstrap gate prevents."""
    with guest(app_env) as first, guest(app_env) as second:
        thread_id = first.post("/api/chat/threads", json={}).json()["id"]
        assert second.get(f"/api/chat/threads/{thread_id}").status_code == 404
        assert second.get(f"/api/chat/threads/{thread_id}").json()["error"]["code"] == (
            "THREAD_NOT_FOUND"
        )
