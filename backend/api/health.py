"""Liveness and readiness."""

from __future__ import annotations

import json
import os

from fastapi import APIRouter

from backend.config import settings
from backend.db import connection

router = APIRouter(tags=["health"])

_ZEN_FIXTURE = json.dumps(
    {
        "contentType": "application/vnd.gorules.decision",
        "nodes": [
            {"id": "i", "name": "Input", "type": "inputNode", "position": {"x": 0, "y": 0},
             "content": {"schema": ""}},
            {"id": "o", "name": "Output", "type": "outputNode", "position": {"x": 200, "y": 0},
             "content": {"schema": ""}},
        ],
        "edges": [{"id": "e", "sourceId": "i", "targetId": "o", "type": "edge"}],
    }
)


def _zen_ok() -> bool:
    try:
        import zen

        zen.ZenEngine().create_decision(_ZEN_FIXTURE)
        return True
    except Exception:
        return False


@router.get("/health")
async def health() -> dict:
    db_ok = await connection.healthy()
    zen_ok = _zen_ok()
    return {
        "status": "ok" if db_ok and zen_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "zen": "ok" if zen_ok else "error",
        "llm_provider": os.getenv("LLM_PROVIDER", "huggingface"),
        "version": settings.version,
    }
