"""SQLite access.

Writes go through a single shared connection guarded by a lock; reads use
short-lived connections. WAL mode is what makes this safe alongside LangGraph's
``AsyncSqliteSaver``, which holds its own connection to the same file.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from backend.config import settings

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

_write_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


async def _apply_pragmas(conn: aiosqlite.Connection) -> None:
    for pragma in _PRAGMAS:
        await conn.execute(pragma)


async def connect() -> None:
    """Open the shared write connection. Called from the FastAPI lifespan."""
    global _write_conn
    if _write_conn is not None:
        return
    db_file = Path(settings.db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    _write_conn = await aiosqlite.connect(db_file, isolation_level=None)
    _write_conn.row_factory = aiosqlite.Row
    await _apply_pragmas(_write_conn)


async def disconnect() -> None:
    global _write_conn
    if _write_conn is not None:
        await _write_conn.close()
        _write_conn = None


@asynccontextmanager
async def write() -> AsyncIterator[aiosqlite.Connection]:
    """Serialized write transaction. Commits on success, rolls back on error."""
    if _write_conn is None:
        raise RuntimeError("Database is not connected; call connect() first.")
    async with _write_lock:
        await _write_conn.execute("BEGIN IMMEDIATE")
        try:
            yield _write_conn
        except Exception:
            await _write_conn.execute("ROLLBACK")
            raise
        else:
            await _write_conn.execute("COMMIT")


async def script(sql: str) -> None:
    """Run a multi-statement script (e.g. schema.sql).

    ``executescript`` issues its own COMMIT, so it must not run inside the
    explicit transaction that :func:`write` opens.
    """
    if _write_conn is None:
        raise RuntimeError("Database is not connected; call connect() first.")
    async with _write_lock:
        await _write_conn.executescript(sql)


@asynccontextmanager
async def read() -> AsyncIterator[aiosqlite.Connection]:
    """Short-lived read connection; concurrent with writers thanks to WAL."""
    conn = await aiosqlite.connect(Path(settings.db_path))
    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA busy_timeout=5000")
        yield conn
    finally:
        await conn.close()


async def healthy() -> bool:
    try:
        async with read() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False
