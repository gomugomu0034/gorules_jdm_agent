"""Coverage and execution explanations.

Both are derived from the execution trace and involve no model, so they are cheap, exact,
and give the same answer twice.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends

from backend import auth
from backend.api.errors import ApiError
from backend.api.graphs import require_graph
from backend.db import dao
from backend.models.api import (
    CoverageResponse,
    ExplainRequest,
    ExplainResponse,
    LintRequest,
)
from backend.tools.coverage import coverage, suggest_cases
from backend.tools.explain_run import explain_run

router = APIRouter(tags=["insights"])


def _coverage(content: dict, tests: list[dict]) -> CoverageResponse:
    report = coverage(content, tests)
    return CoverageResponse(
        summary=report["summary"],
        nodes=report["nodes"],
        suggestions=suggest_cases(content, tests),
    )


@router.post("/api/graphs/{graph_id}/coverage", response_model=CoverageResponse)
async def graph_coverage(
    graph_id: str, body: LintRequest, owner: str = Depends(auth.get_owner)
) -> CoverageResponse:
    graph = await require_graph(graph_id, body.version, owner=owner)
    content = body.content if body.content is not None else graph["content"]
    tests = [t for t in await dao.list_tests(graph_id) if t.get("enabled", True)]
    return await anyio.to_thread.run_sync(_coverage, content, tests)


@router.post("/api/graphs/{graph_id}/explain", response_model=ExplainResponse)
async def graph_explain(
    graph_id: str, body: ExplainRequest, owner: str = Depends(auth.get_owner)
) -> ExplainResponse:
    graph = await require_graph(graph_id, body.version, owner=owner)
    content = body.content if body.content is not None else graph["content"]
    try:
        result = await anyio.to_thread.run_sync(explain_run, content, body.context)
    except Exception as exc:  # noqa: BLE001 - a failed run is an answer, not a 500
        raise ApiError("SIMULATION_FAILED", str(exc), 422) from exc
    return ExplainResponse(**result)


@router.post("/api/explain", response_model=ExplainResponse)
async def explain(body: ExplainRequest) -> ExplainResponse:
    if body.content is None:
        raise ApiError("VALIDATION_ERROR", "'content' is required.", 422)
    try:
        result = await anyio.to_thread.run_sync(explain_run, body.content, body.context)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("SIMULATION_FAILED", str(exc), 422) from exc
    return ExplainResponse(**result)
