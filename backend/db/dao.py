"""Data access for graphs, versions, test cases, chat threads and proposals.

Every function is a thin, explicit SQL wrapper. JSON columns are encoded and
decoded here so callers deal in plain Python structures.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from backend.db import connection

AUTOSAVE_COALESCE_WINDOW = timedelta(seconds=60)

# Compared against when no user matches, so a bad email costs the same time as
# a bad password and cannot be distinguished by an attacker.
_DUMMY_HASH = "$2b$12$" + "." * 53


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "graph"


async def unique_slug(conn: aiosqlite.Connection, owner: str, name: str) -> str:
    """Unique within one owner: two guests may each have a `refund-policy`."""
    base = slugify(name)
    candidate, n = base, 2
    while True:
        cur = await conn.execute(
            "SELECT 1 FROM graphs WHERE owner_id = ? AND slug = ?", (owner, candidate)
        )
        if await cur.fetchone() is None:
            return candidate
        candidate, n = f"{base}-{n}", n + 1


# --------------------------------------------------------------------------
# Graphs
# --------------------------------------------------------------------------

def _graph_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "current_version": row["current_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
    }


async def list_graphs(
    owner: str, q: str | None = None, archived: bool = False
) -> list[dict[str, Any]]:
    sql = """
        SELECT g.*,
               (SELECT COUNT(*) FROM test_cases t WHERE t.graph_id = g.id) AS test_count,
               (SELECT v.content FROM graph_versions v
                 WHERE v.graph_id = g.id AND v.version = g.current_version) AS content
          FROM graphs g
         WHERE g.owner_id = ? AND (g.archived_at IS NULL) = ?
    """
    params: list[Any] = [owner, 0 if archived else 1]
    if q:
        sql += " AND (g.name LIKE ? OR g.description LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY g.updated_at DESC"

    async with connection.read() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()

    out = []
    for row in rows:
        item = _graph_row(row)
        item["test_count"] = row["test_count"]
        try:
            item["node_count"] = len(json.loads(row["content"] or "{}").get("nodes", []))
        except (json.JSONDecodeError, AttributeError):
            item["node_count"] = 0
        out.append(item)
    return out


async def get_graph(
    graph_id: str, version: int | None = None, owner: str | None = None
) -> dict[str, Any] | None:
    """Fetch a graph. With `owner`, another owner's graph reads as missing."""
    async with connection.read() as conn:
        if owner is None:
            cur = await conn.execute("SELECT * FROM graphs WHERE id = ?", (graph_id,))
        else:
            cur = await conn.execute(
                "SELECT * FROM graphs WHERE id = ? AND owner_id = ?", (graph_id, owner)
            )
        row = await cur.fetchone()
        if row is None:
            return None
        graph = _graph_row(row)
        target = version if version is not None else row["current_version"]
        cur = await conn.execute(
            "SELECT content FROM graph_versions WHERE graph_id = ? AND version = ?",
            (graph_id, target),
        )
        vrow = await cur.fetchone()

    if vrow is None:
        graph["content"] = {"nodes": [], "edges": []}
        graph["version"] = 0
    else:
        graph["content"] = json.loads(vrow["content"])
        graph["version"] = target
    return graph


async def create_graph(
    owner: str,
    name: str,
    content: dict[str, Any],
    description: str = "",
    author: str = "user",
    message: str = "Initial version",
) -> dict[str, Any]:
    ts = now()
    graph_id = new_id()
    async with connection.write() as conn:
        slug = await unique_slug(conn, owner, name)
        await conn.execute(
            "INSERT INTO graphs (id, owner_id, name, slug, description,"
            " current_version, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (graph_id, owner, name, slug, description, ts, ts),
        )
        await conn.execute(
            "INSERT INTO graph_versions (graph_id, version, content, message, author,"
            " is_autosave, created_at) VALUES (?,1,?,?,?,0,?)",
            (graph_id, json.dumps(content), message, author, ts),
        )
    return await get_graph(graph_id)  # type: ignore[return-value]


async def save_version(
    graph_id: str,
    content: dict[str, Any],
    message: str = "",
    author: str = "user",
    is_autosave: bool = False,
    thread_id: str | None = None,
) -> int:
    """Write a new version and return its number.

    Consecutive autosaves by the same author inside a 60s window overwrite the
    latest row instead of piling up near-identical versions.
    """
    ts = now()
    async with connection.write() as conn:
        cur = await conn.execute(
            "SELECT current_version FROM graphs WHERE id = ?", (graph_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise KeyError(graph_id)
        current = row["current_version"]

        if is_autosave and current > 0:
            cur = await conn.execute(
                "SELECT author, is_autosave, created_at FROM graph_versions"
                " WHERE graph_id = ? AND version = ?",
                (graph_id, current),
            )
            prev = await cur.fetchone()
            if (
                prev
                and prev["is_autosave"]
                and prev["author"] == author
                and _parse_ts(ts) - _parse_ts(prev["created_at"]) < AUTOSAVE_COALESCE_WINDOW
            ):
                await conn.execute(
                    "UPDATE graph_versions SET content = ?, created_at = ?"
                    " WHERE graph_id = ? AND version = ?",
                    (json.dumps(content), ts, graph_id, current),
                )
                await conn.execute(
                    "UPDATE graphs SET updated_at = ? WHERE id = ?", (ts, graph_id)
                )
                return current

        version = current + 1
        await conn.execute(
            "INSERT INTO graph_versions (graph_id, version, content, message, author,"
            " is_autosave, thread_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                graph_id,
                version,
                json.dumps(content),
                message,
                author,
                1 if is_autosave else 0,
                thread_id,
                ts,
            ),
        )
        await conn.execute(
            "UPDATE graphs SET current_version = ?, updated_at = ? WHERE id = ?",
            (version, ts, graph_id),
        )
    return version


async def update_graph_meta(
    graph_id: str,
    name: str | None = None,
    description: str | None = None,
    archived: bool | None = None,
    owner: str | None = None,
) -> None:
    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if archived is not None:
        sets.append("archived_at = ?")
        params.append(now() if archived else None)
    if not sets:
        return
    sets.append("updated_at = ?")
    params += [now(), graph_id]
    where = "id = ?"
    if owner is not None:
        where += " AND owner_id = ?"
        params.append(owner)
    async with connection.write() as conn:
        await conn.execute(f"UPDATE graphs SET {', '.join(sets)} WHERE {where}", params)


async def delete_graph(graph_id: str, owner: str | None = None) -> None:
    async with connection.write() as conn:
        if owner is None:
            await conn.execute("DELETE FROM graphs WHERE id = ?", (graph_id,))
        else:
            await conn.execute(
                "DELETE FROM graphs WHERE id = ? AND owner_id = ?", (graph_id, owner)
            )


async def find_graph_by_name(name: str, owner: str) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT id FROM graphs WHERE owner_id = ? AND (name = ? OR slug = ?)",
            (owner, name, slugify(name)),
        )
        row = await cur.fetchone()
    return await get_graph(row["id"]) if row else None


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

async def list_versions(graph_id: str) -> list[dict[str, Any]]:
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT version, message, author, is_autosave, thread_id, created_at,"
            " content FROM graph_versions WHERE graph_id = ? ORDER BY version DESC",
            (graph_id,),
        )
        rows = await cur.fetchall()
    out = []
    for row in rows:
        try:
            node_count = len(json.loads(row["content"]).get("nodes", []))
        except json.JSONDecodeError:
            node_count = 0
        out.append(
            {
                "version": row["version"],
                "message": row["message"],
                "author": row["author"],
                "is_autosave": bool(row["is_autosave"]),
                "thread_id": row["thread_id"],
                "created_at": row["created_at"],
                "node_count": node_count,
            }
        )
    return out


async def latest_agent_version(graph_id: str) -> dict[str, Any] | None:
    """The most recent version the agent wrote, if any.

    Used to pair an agent's output with the edits a person then made to it. Looking for the
    latest agent version rather than simply the previous one matters because consecutive
    user autosaves coalesce into a single row: the human's edit keeps changing under a
    version number that was already created, and only the agent version stays put.
    """
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT version, content, thread_id, created_at FROM graph_versions"
            " WHERE graph_id = ? AND author = 'agent' ORDER BY version DESC LIMIT 1",
            (graph_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
    except json.JSONDecodeError:
        return None
    return {
        "version": row["version"],
        "content": content,
        "thread_id": row["thread_id"],
        "created_at": row["created_at"],
    }


async def get_version(graph_id: str, version: int) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT * FROM graph_versions WHERE graph_id = ? AND version = ?",
            (graph_id, version),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "version": row["version"],
        "content": json.loads(row["content"]),
        "message": row["message"],
        "author": row["author"],
        "created_at": row["created_at"],
    }


# --------------------------------------------------------------------------
# Test cases
# --------------------------------------------------------------------------

def _test_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "input": json.loads(row["input_json"]),
        "expectedOutput": json.loads(row["expected_json"]),
        "enabled": bool(row["enabled"]),
        "order": row["sort_order"],
    }


async def list_tests(graph_id: str) -> list[dict[str, Any]]:
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT * FROM test_cases WHERE graph_id = ? ORDER BY sort_order, created_at",
            (graph_id,),
        )
        rows = await cur.fetchall()
    return [_test_row(r) for r in rows]


async def replace_tests(graph_id: str, tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ts = now()
    async with connection.write() as conn:
        await conn.execute("DELETE FROM test_cases WHERE graph_id = ?", (graph_id,))
        for i, t in enumerate(tests):
            await conn.execute(
                "INSERT INTO test_cases (id, graph_id, name, input_json, expected_json,"
                " enabled, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    t.get("id") or new_id(),
                    graph_id,
                    t.get("name") or f"Test {i + 1}",
                    json.dumps(t.get("input", {})),
                    json.dumps(t.get("expectedOutput", {})),
                    0 if t.get("enabled") is False else 1,
                    t.get("order", i),
                    ts,
                    ts,
                ),
            )
    return await list_tests(graph_id)


async def add_test(graph_id: str, test: dict[str, Any]) -> dict[str, Any]:
    ts = now()
    test_id = new_id()
    async with connection.write() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM test_cases WHERE graph_id = ?",
            (graph_id,),
        )
        order = (await cur.fetchone())["n"]
        await conn.execute(
            "INSERT INTO test_cases (id, graph_id, name, input_json, expected_json,"
            " enabled, sort_order, created_at, updated_at) VALUES (?,?,?,?,?,1,?,?,?)",
            (
                test_id,
                graph_id,
                test.get("name") or "Untitled test",
                json.dumps(test.get("input", {})),
                json.dumps(test.get("expectedOutput", {})),
                order,
                ts,
                ts,
            ),
        )
    tests = await list_tests(graph_id)
    return next(t for t in tests if t["id"] == test_id)


async def update_test(graph_id: str, test_id: str, patch: dict[str, Any]) -> None:
    mapping = {
        "name": ("name", lambda v: v),
        "input": ("input_json", json.dumps),
        "expectedOutput": ("expected_json", json.dumps),
        "enabled": ("enabled", lambda v: 1 if v else 0),
        "order": ("sort_order", lambda v: v),
    }
    sets, params = [], []
    for key, (column, encode) in mapping.items():
        if key in patch:
            sets.append(f"{column} = ?")
            params.append(encode(patch[key]))
    if not sets:
        return
    sets.append("updated_at = ?")
    params += [now(), graph_id, test_id]
    async with connection.write() as conn:
        await conn.execute(
            f"UPDATE test_cases SET {', '.join(sets)} WHERE graph_id = ? AND id = ?",
            params,
        )


async def delete_test(graph_id: str, test_id: str) -> None:
    async with connection.write() as conn:
        await conn.execute(
            "DELETE FROM test_cases WHERE graph_id = ? AND id = ?", (graph_id, test_id)
        )


async def record_test_run(
    graph_id: str, version: int | None, summary: dict, results: list
) -> str:
    run_id = new_id()
    async with connection.write() as conn:
        await conn.execute(
            "INSERT INTO test_runs (id, graph_id, version, summary_json, results_json,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (run_id, graph_id, version, json.dumps(summary), json.dumps(results), now()),
        )
    return run_id


# --------------------------------------------------------------------------
# Chat threads, events and proposals
# --------------------------------------------------------------------------

async def create_thread(
    owner: str, graph_id: str | None = None, title: str = "New chat"
) -> dict[str, Any]:
    ts = now()
    thread_id = new_id()
    async with connection.write() as conn:
        await conn.execute(
            "INSERT INTO chat_threads (id, owner_id, graph_id, title, status,"
            " created_at, updated_at) VALUES (?,?,?,?,'idle',?,?)",
            (thread_id, owner, graph_id, title, ts, ts),
        )
    return {"id": thread_id, "owner_id": owner, "graph_id": graph_id, "title": title,
            "status": "idle", "created_at": ts, "updated_at": ts}


async def get_thread(thread_id: str, owner: str | None = None) -> dict[str, Any] | None:
    """With `owner`, another owner's thread reads as missing."""
    async with connection.read() as conn:
        if owner is None:
            cur = await conn.execute(
                "SELECT * FROM chat_threads WHERE id = ?", (thread_id,)
            )
        else:
            cur = await conn.execute(
                "SELECT * FROM chat_threads WHERE id = ? AND owner_id = ?",
                (thread_id, owner),
            )
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_threads(owner: str, graph_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM chat_threads WHERE owner_id = ?"
    params: list[Any] = [owner]
    if graph_id:
        sql += " AND graph_id = ?"
        params.append(graph_id)
    sql += " ORDER BY updated_at DESC"
    async with connection.read() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def set_thread_status(thread_id: str, status: str) -> None:
    async with connection.write() as conn:
        await conn.execute(
            "UPDATE chat_threads SET status = ?, updated_at = ? WHERE id = ?",
            (status, now(), thread_id),
        )


async def reset_running_threads() -> int:
    """Clear the `running` status left behind by a process that did not stop cleanly.

    A run lives in an in-memory `asyncio.Task`, but its status is written to the database.
    Nothing survives a crash or a `docker compose down` mid-run, so a row still claiming
    `running` after a restart is describing a task that no longer exists - and the client
    believes it, disabling the composer and showing a Stop button that can never resolve.

    `updated_at` is deliberately left alone: nothing about the conversation changed, and
    bumping it on every boot would reorder the thread list.
    """
    async with connection.write() as conn:
        cur = await conn.execute("UPDATE chat_threads SET status = 'idle' WHERE status = 'running'")
        return cur.rowcount or 0


async def set_thread_graph(thread_id: str, graph_id: str | None) -> None:
    async with connection.write() as conn:
        await conn.execute(
            "UPDATE chat_threads SET graph_id = ?, updated_at = ? WHERE id = ?",
            (graph_id, now(), thread_id),
        )


async def delete_thread(thread_id: str) -> None:
    async with connection.write() as conn:
        await conn.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))


async def append_event(thread_id: str, run_id: str, event: dict[str, Any]) -> int:
    """Persist one SSE event and return its sequence number."""
    async with connection.write() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM chat_events WHERE thread_id = ?",
            (thread_id,),
        )
        seq = (await cur.fetchone())["n"]
        await conn.execute(
            "INSERT INTO chat_events (thread_id, seq, run_id, type, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (thread_id, seq, run_id, event.get("type", "message"), json.dumps(event), now()),
        )
    return seq


async def list_events(thread_id: str, from_seq: int = 0) -> list[dict[str, Any]]:
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT seq, payload FROM chat_events WHERE thread_id = ? AND seq > ?"
            " ORDER BY seq",
            (thread_id, from_seq),
        )
        rows = await cur.fetchall()
    return [{"seq": r["seq"], **json.loads(r["payload"])} for r in rows]


async def save_proposal(
    thread_id: str,
    jdm: dict[str, Any],
    tests: list[dict[str, Any]],
    usecase_name: str,
    graph_id: str | None = None,
    base_version: int | None = None,
    report: dict | None = None,
) -> None:
    async with connection.write() as conn:
        await conn.execute(
            "INSERT INTO proposals (thread_id, graph_id, jdm_json, tests_json,"
            " usecase_name, base_version, report_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(thread_id) DO UPDATE SET graph_id=excluded.graph_id,"
            " jdm_json=excluded.jdm_json, tests_json=excluded.tests_json,"
            " usecase_name=excluded.usecase_name, base_version=excluded.base_version,"
            " report_json=excluded.report_json, created_at=excluded.created_at",
            (
                thread_id,
                graph_id,
                json.dumps(jdm),
                json.dumps(tests),
                usecase_name,
                base_version,
                json.dumps(report) if report is not None else None,
                now(),
            ),
        )


async def get_proposal(thread_id: str) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute("SELECT * FROM proposals WHERE thread_id = ?", (thread_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "thread_id": row["thread_id"],
        "graph_id": row["graph_id"],
        "jdm": json.loads(row["jdm_json"]),
        "tests": json.loads(row["tests_json"]),
        "usecase_name": row["usecase_name"],
        "base_version": row["base_version"],
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
        "created_at": row["created_at"],
    }


async def clear_proposal(thread_id: str) -> None:
    async with connection.write() as conn:
        await conn.execute("DELETE FROM proposals WHERE thread_id = ?", (thread_id,))


# --------------------------------------------------------------------------
# Schema metadata
# --------------------------------------------------------------------------

async def get_meta(key: str) -> str | None:
    async with connection.read() as conn:
        cur = await conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,))
        row = await cur.fetchone()
    return row["value"] if row else None


async def set_meta(key: str, value: str) -> None:
    async with connection.write() as conn:
        await conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------
# Users and guest housekeeping
# --------------------------------------------------------------------------

async def upsert_user(user_id: str, email: str, password: str) -> None:
    """Create or update a user, storing only a bcrypt hash of the password."""
    import bcrypt

    digest = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    ts = now()
    async with connection.write() as conn:
        await conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET email=excluded.email,"
            " password_hash=excluded.password_hash, updated_at=excluded.updated_at",
            (user_id, email, digest, ts, ts),
        )


async def verify_user(email: str, password: str) -> dict[str, Any] | None:
    """Return the user when the password matches, else None.

    The hash comparison runs even when no such user exists so that a wrong
    email and a wrong password take the same time to reject.
    """
    import bcrypt

    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        )
        row = await cur.fetchone()

    digest = row["password_hash"] if row else _DUMMY_HASH
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), digest.encode("ascii"))
    except ValueError:
        return None
    return {"id": row["id"], "email": row["email"]} if (ok and row) else None


async def get_user(user_id: str) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute("SELECT id, email FROM users WHERE id = ?", (user_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def delete_stale_guests(ttl_days: int) -> int:
    """Delete guest graphs and threads idle for longer than `ttl_days`.

    Versions, tests, runs, chat events and proposals all cascade from these two
    tables, so no other cleanup is needed.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat(
        timespec="seconds"
    )
    async with connection.write() as conn:
        cur = await conn.execute(
            "DELETE FROM graphs WHERE owner_id LIKE 'guest:%' AND updated_at < ?",
            (cutoff,),
        )
        removed = cur.rowcount or 0
        await conn.execute(
            "DELETE FROM chat_threads WHERE owner_id LIKE 'guest:%' AND updated_at < ?",
            (cutoff,),
        )
    return removed
