"""Synchronous writer for the training corpus.

Deliberately plain `sqlite3` rather than the app's aiosqlite pool. Model calls happen in
worker threads (LangGraph's executor, `anyio.to_thread.run_sync`), so a synchronous writer
records from the thread that already has the data, with no bridge back to the event loop
and no ordering hazard. Writing to a *separate* database file is what makes that safe:
there is no contention with the connections the app holds on `studio.db`.

Every public function is wrapped by `_never_fails`. Capture sits directly in the path of
every model call the app makes, and instrumentation that can take down the thing it
instruments is worse than no instrumentation. A corrupt file, a full disk, a schema that
does not match: all of it is logged and swallowed, and after a few consecutive failures
capture switches itself off for the rest of the process rather than logging on every call.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"

# One connection, shared across threads under a lock. Writes are tiny and arrive at most
# once per model call - a call that takes tens of seconds - so contention is not a real
# concern, and a single connection keeps WAL bookkeeping simple.
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

# Set once capture has given up, so a broken corpus file does not produce a warning per
# model call for the rest of the process's life.
_disabled_reason: str | None = None
_failures = 0
_MAX_FAILURES = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enabled() -> bool:
    return settings.corpus_enabled and _disabled_reason is None


def _degrade(exc: BaseException) -> None:
    """Give up after repeated failures rather than logging on every call."""
    global _failures, _disabled_reason
    _failures += 1
    if _failures >= _MAX_FAILURES:
        _disabled_reason = str(exc) or exc.__class__.__name__
        logger.error(
            "Corpus capture failed %s times and is now off for this process (%s). "
            "The agent is unaffected; no further training data will be recorded.",
            _failures, _disabled_reason,
        )


def _never_fails(fn):
    """Capture failures never reach the caller."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not enabled():
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Corpus capture failed in %s(); continuing.", fn.__name__,
                           exc_info=True)
            _degrade(exc)
            return None

    return wrapper


# Columns added after an earlier version of the schema was already in use. `schema.sql`
# creates tables with IF NOT EXISTS, which does nothing at all to a table that already
# exists - so a corpus written by an earlier build keeps its old shape unless the missing
# columns are added explicitly. Kept as data rather than a list of ALTER statements so
# later phases extend it by adding a line.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "samples": {
        "upstream_provider": "TEXT",
        "cost": "REAL",
    },
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                logger.info("Corpus: added %s.%s", table, name)


def _connect() -> sqlite3.Connection:
    """Open the corpus database, creating the file and schema on first use."""
    global _conn
    if _conn is not None:
        return _conn
    path = settings.corpus_db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    # `check_same_thread=False` because writes arrive from whichever worker thread ran the
    # node; `_lock` is what actually serialises them.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _ensure_columns(conn)
    _conn = conn
    logger.info("Training corpus at %s", path)
    return conn


_git_sha: str | None = None


def _sha_from_git_dir(root: Path) -> str:
    """Read HEAD straight out of `.git`, without needing the git binary.

    The container is where this matters: the image has no git installed, so shelling out
    returns nothing and every Docker-recorded run would carry a blank `git_sha` - which is
    the whole point of the column. The repository is bind-mounted, `.git` with it, so the
    commit is right there to be read.
    """
    git = root / ".git"
    if git.is_file():
        # A worktree or submodule: `.git` is a pointer, not a directory.
        pointer = git.read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir:"):
            return ""
        git = Path(pointer.split(":", 1)[1].strip())
        if not git.is_absolute():
            git = (root / git).resolve()
    if not git.is_dir():
        return ""

    head = (git / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref:"):
        return head[:8]  # detached HEAD holds the sha directly
    ref = head.split(":", 1)[1].strip()
    target = git / ref
    if target.is_file():
        return target.read_text(encoding="utf-8").strip()[:8]
    # The ref has been packed away; `packed-refs` is a flat "<sha> <ref>" table.
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:8]
    return ""


def git_sha() -> str:
    """The commit the app is running, so a sample can be tied to the code that made it.

    Resolved once, through three sources in order of reliability: an explicit `GIT_SHA`
    (how it reaches an image built with no repository in it), the `.git` directory read
    directly, then the git binary for anything the reader does not understand.
    """
    global _git_sha
    if _git_sha is not None:
        return _git_sha

    root = Path(__file__).resolve().parents[2]
    _git_sha = os.getenv("GIT_SHA", "").strip()

    if not _git_sha:
        try:
            _git_sha = _sha_from_git_dir(root)
        except Exception:  # noqa: BLE001
            _git_sha = ""

    if not _git_sha:
        try:
            _git_sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=3, check=True,
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            _git_sha = ""
    return _git_sha


# ---------------------------------------------------------------------------- runs

@_never_fails
def start_run(run_id: str, *, thread_id: str = "", graph_id: str | None = None,
              owner_hash: str = "") -> None:
    with _lock:
        _connect().execute(
            "INSERT OR IGNORE INTO runs"
            " (run_id, thread_id, graph_id, owner_hash, app_version, git_sha, started_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (run_id, thread_id, graph_id, owner_hash, settings.version, git_sha(), now()),
        )


@_never_fails
def finish_run(run_id: str, *, outcome: str, intent: str = "", mode: str = "",
               final_jdm: str | None = None, graph_id: str | None = None,
               owner_hash: str = "") -> None:
    """Close a run and stamp what it turned out to be.

    Almost nothing here is knowable when the run opens. `intent` and `mode` are settled by
    `intent_router_node`; the graph may be attached part-way through a turn that started on
    a blank canvas. So the row is opened with an id and a timestamp and completed here.
    CASE/COALESCE keep whatever was already written when this call has nothing better, so a
    cancelled turn still records what it was trying to do.
    """
    final_hash = content_hash(final_jdm) if final_jdm else None
    with _lock:
        _connect().execute(
            "UPDATE runs SET outcome = ?,"
            "  intent   = CASE WHEN ? <> '' THEN ? ELSE intent END,"
            "  mode     = CASE WHEN ? <> '' THEN ? ELSE mode END,"
            "  owner_hash = CASE WHEN ? <> '' THEN ? ELSE owner_hash END,"
            "  graph_id = COALESCE(?, graph_id),"
            "  final_jdm_hash = COALESCE(?, final_jdm_hash),"
            "  ended_at = ?"
            " WHERE run_id = ?",
            (outcome, intent, intent, mode, mode, owner_hash, owner_hash,
             graph_id, final_hash, now(), run_id),
        )


# ---------------------------------------------------------------------------- prompts

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _remember_prompt(conn: sqlite3.Connection, text: str, kind: str) -> str:
    """Store a system prompt once, keyed by its own hash.

    The hash is also the prompt's version. Editing `PROMPT_PLANNER` mints a new row, which
    is what lets a training set be filtered to a single instruction set instead of quietly
    mixing several generations of one.
    """
    digest = content_hash(text)
    stamp = now()
    conn.execute(
        "INSERT INTO prompts (hash, kind, text, chars, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(hash) DO UPDATE SET last_seen_at = excluded.last_seen_at",
        (digest, kind, text, len(text), stamp, stamp),
    )
    return digest


# ---------------------------------------------------------------------------- samples

@_never_fails
def record_sample(
    *,
    node: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    completion: str | None = None,
    reasoning: Any = None,
    provider: str = "",
    model_requested: str = "",
    temperature: float | None = None,
    reasoning_enabled: bool = False,
    latency_ms: int | None = None,
    error: str | None = None,
    attempt: int = 1,
    purpose: str = "",
    model_served: str = "",
    generation_id: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    upstream_provider: str = "",
    cost: float | None = None,
    run_id: str | None = None,
) -> str | None:
    """Record one model call. Returns the sample id, or None if capture is off."""
    sample_id = str(uuid.uuid4())
    # Built from a mapping rather than parallel lists of columns and `?` placeholders.
    # Phases 3 to 5 keep adding to this table, and hand-counting placeholders is how a
    # column and its value silently drift apart - which SQLite reports as
    # "N values for N+1 columns" only if you are lucky enough to be off by one.
    values: dict[str, Any] = {
        "sample_id": sample_id,
        "run_id": run_id,
        "node": node,
        "attempt": attempt,
        "purpose": purpose,
        "messages_json": json.dumps(messages, ensure_ascii=False, default=str),
        "completion": completion,
        "reasoning_json": (
            json.dumps(reasoning, ensure_ascii=False, default=str)
            if reasoning is not None else None
        ),
        "provider": provider,
        "upstream_provider": upstream_provider or None,
        "model_requested": model_requested,
        "model_served": model_served or None,
        "temperature": temperature,
        "reasoning_enabled": int(reasoning_enabled),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "generation_id": generation_id or None,
        "cost": cost,
        "latency_ms": latency_ms,
        "error": error,
        "created_at": now(),
    }

    with _lock:
        conn = _connect()
        values["prompt_hash"] = _remember_prompt(conn, system_prompt, _prompt_kind(node))
        columns = ", ".join(values)
        placeholders = ", ".join("?" * len(values))
        conn.execute(
            f"INSERT INTO samples ({columns}, seq) VALUES ({placeholders},"
            # Assigned inside the INSERT so two writers cannot pick the same number.
            # With run_id NULL the subquery matches nothing and yields 1.
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM samples WHERE run_id = ?))",
            (*values.values(), run_id),
        )
    return sample_id


@_never_fails
def record_tool_result(
    *,
    tool: str,
    node: str,
    ok: bool,
    attempt: int = 1,
    diagnostics: list | None = None,
    output: Any = None,
    error: str | None = None,
    duration_ms: int | None = None,
    run_id: str | None = None,
    sample_id: str | None = None,
) -> str | None:
    """Record what a deterministic tool made of a model's output.

    The objective half of the corpus. A sample the linter rejected and a sample it passed
    are different training data, and knowing which is which needs no human.
    """
    values: dict[str, Any] = {
        "tool_result_id": str(uuid.uuid4()),
        "run_id": run_id,
        "sample_id": sample_id,
        "node": node,
        "attempt": attempt,
        "tool": tool,
        "ok": int(ok),
        "diagnostics_json": (
            json.dumps(diagnostics, ensure_ascii=False, default=str) if diagnostics else None
        ),
        "output_json": (
            json.dumps(output, ensure_ascii=False, default=str) if output is not None else None
        ),
        "error": error,
        "duration_ms": duration_ms,
        "created_at": now(),
    }
    with _lock:
        columns = ", ".join(values)
        placeholders = ", ".join("?" * len(values))
        _connect().execute(
            f"INSERT INTO tool_results ({columns}, seq) VALUES ({placeholders},"
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM tool_results WHERE run_id = ?))",
            (*values.values(), run_id),
        )
    return values["tool_result_id"]


@_never_fails
def record_interaction(
    *,
    kind: str,
    thread_id: str = "",
    graph_id: str | None = None,
    prompt: str | None = None,
    options: list | None = None,
    response: str | None = None,
    detail: Any = None,
    run_id: str | None = None,
    sample_id: str | None = None,
    answered: bool = True,
) -> str | None:
    """Record something a person did.

    `answered=False` opens a question and leaves `answered_at` NULL, so the reply that
    arrives a whole turn later can be matched to it by `answer_open_question`.
    """
    stamp = now()
    values: dict[str, Any] = {
        "interaction_id": str(uuid.uuid4()),
        "run_id": run_id,
        "thread_id": thread_id,
        "graph_id": graph_id,
        "kind": kind,
        "prompt": prompt,
        "options_json": json.dumps(options, ensure_ascii=False, default=str) if options else None,
        "response": response,
        "responds_to_sample": sample_id,
        "detail_json": json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None,
        "asked_at": stamp,
        "answered_at": stamp if answered else None,
    }
    with _lock:
        columns = ", ".join(values)
        placeholders = ", ".join("?" * len(values))
        _connect().execute(
            f"INSERT INTO interactions ({columns}, seq) VALUES ({placeholders},"
            " (SELECT COALESCE(MAX(seq), 0) + 1 FROM interactions WHERE run_id = ?))",
            (*values.values(), run_id),
        )
    return values["interaction_id"]


@_never_fails
def forget_correction(graph_id: str, from_version: int) -> int:
    """Drop any correction already recorded for this agent version.

    A person's edit is not one event. Consecutive autosaves coalesce into a single version
    row whose content keeps changing, so the correction is re-recorded as it settles and
    the earlier, partial snapshot has to go - otherwise the corpus holds a half-finished
    edit alongside the finished one and cannot tell which is which.
    """
    with _lock:
        cur = _connect().execute(
            "DELETE FROM interactions WHERE kind = 'correction' AND graph_id = ?"
            " AND json_extract(detail_json, '$.from_version') = ?",
            (graph_id, from_version),
        )
        return cur.rowcount or 0


@_never_fails
def answer_open_question(thread_id: str, response: str, *, run_id: str | None = None) -> bool:
    """Attach a reply to the question this thread is waiting on.

    The question and its answer arrive as two events a whole turn apart, with the studio
    holding nothing that connects them. Matching on "the newest unanswered question for
    this thread" survives a restart, which an in-memory pairing would not - and a paused
    conversation outliving the process is exactly what the checkpointer is for.
    """
    with _lock:
        cur = _connect().execute(
            "UPDATE interactions SET response = ?, answered_at = ?"
            " WHERE interaction_id = ("
            "   SELECT interaction_id FROM interactions"
            "   WHERE thread_id = ? AND kind = 'clarification' AND answered_at IS NULL"
            "   ORDER BY asked_at DESC LIMIT 1)",
            (response, now(), thread_id),
        )
        return bool(cur.rowcount)


def _prompt_kind(node: str) -> str:
    """`planner_node` -> `planner`. Just a readable label on the prompts table."""
    return node.removesuffix("_node") or node


# ---------------------------------------------------------------------------- lifecycle

@_never_fails
def open_now() -> None:
    """Create the database and schema up front.

    Called from the app lifespan so an unwritable path is reported at boot, rather than
    silently costing the first few samples of the session before `_degrade` notices.
    """
    _connect()


def close() -> None:
    """Release the connection. Called from the FastAPI lifespan and by tests."""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None


def reset_for_tests() -> None:
    """Drop the cached connection and re-enable capture after an induced failure."""
    global _disabled_reason, _failures
    close()
    _disabled_reason = None
    _failures = 0
