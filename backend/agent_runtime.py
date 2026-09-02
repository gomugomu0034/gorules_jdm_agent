"""Compiled-agent singleton backed by a SQLite checkpointer.

Threads live in the same database file as the studio's own tables, so a paused
conversation survives a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.config import settings

logger = logging.getLogger(__name__)

_graph = None
_saver_cm = None


async def startup() -> None:
    global _graph, _saver_cm

    from backend.lang_graph_agent import build_graph

    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        _saver_cm = AsyncSqliteSaver.from_conn_string(settings.db_path)
        saver = await _saver_cm.__aenter__()
        _graph = build_graph(saver)
        logger.info("Agent checkpoints persisted to %s", settings.db_path)
    except Exception as exc:  # noqa: BLE001
        # Falling back keeps the app usable; the cost is that paused threads do
        # not survive a restart, which we say out loud rather than hide.
        logger.warning(
            "Could not open the SQLite checkpointer (%s); falling back to "
            "in-memory checkpoints. Conversations will not survive a restart.",
            exc,
        )
        _saver_cm = None
        _graph = build_graph()


async def shutdown() -> None:
    global _graph, _saver_cm
    if _saver_cm is not None:
        try:
            await _saver_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        _saver_cm = None
    _graph = None


def get_graph():
    if _graph is None:
        raise RuntimeError("The agent runtime has not been started.")
    return _graph
