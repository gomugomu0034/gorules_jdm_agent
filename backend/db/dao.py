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


async def unique_slug(conn: aiosqlite.Connection, name: str) -> str:
    base = slugify(name)
    candidate, n = base, 2
    while True:
        cur = await conn.execute("SELECT 1 FROM graphs WHERE slug = ?", (candidate,))
        if await cur.fetchone() is None:
            return candidate
        candidate, n = f"{base}-{n}", n + 1


# --------------------------------------------------------------------------
# Graphs
# --------------------------------------------------------------------------

def _graph_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "description": row["description"],
        "current_version": row["current_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
    }


async def list_graphs(q: str | None = None, archived: bool = False) -> list[dict[str, Any]]:
    sql = """
        SELECT g.*,
               (SELECT COUNT(*) FROM test_cases t WHERE t.graph_id = g.id) AS test_count,
               (SELECT v.content FROM graph_versions v
                 WHERE v.graph_id = g.id AND v.version = g.current_version) AS content
          FROM graphs g
         WHERE (g.archived_at IS NULL) = ?
    """
    params: list[Any] = [0 if archived else 1]
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


async def get_graph(graph_id: str, version: int | None = None) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute("SELECT * FROM graphs WHERE id = ?", (graph_id,))
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
    name: str,
    content: dict[str, Any],
    description: str = "",
    author: str = "user",
    message: str = "Initial version",
) -> dict[str, Any]:
    ts = now()
    graph_id = new_id()
    async with connection.write() as conn:
        slug = await unique_slug(conn, name)
        await conn.execute(
            "INSERT INTO graphs (id, name, slug, description, current_version,"
            " created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
            (graph_id, name, slug, description, ts, ts),
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
    async with connection.write() as conn:
        await conn.execute(f"UPDATE graphs SET {', '.join(sets)} WHERE id = ?", params)


async def delete_graph(graph_id: str) -> None:
    async with connection.write() as conn:
        await conn.execute("DELETE FROM graphs WHERE id = ?", (graph_id,))


async def find_graph_by_name(name: str) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute(
            "SELECT id FROM graphs WHERE name = ? OR slug = ?", (name, slugify(name))
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

async def create_thread(graph_id: str | None = None, title: str = "New chat") -> dict[str, Any]:
    ts = now()
    thread_id = new_id()
    async with connection.write() as conn:
        await conn.execute(
            "INSERT INTO chat_threads (id, graph_id, title, status, created_at, updated_at)"
            " VALUES (?,?,?,'idle',?,?)",
            (thread_id, graph_id, title, ts, ts),
        )
    return {"id": thread_id, "graph_id": graph_id, "title": title, "status": "idle",
            "created_at": ts, "updated_at": ts}


async def get_thread(thread_id: str) -> dict[str, Any] | None:
    async with connection.read() as conn:
        cur = await conn.execute("SELECT * FROM chat_threads WHERE id = ?", (thread_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def list_threads(graph_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM chat_threads"
    params: list[Any] = []
    if graph_id:
        sql += " WHERE graph_id = ?"
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
