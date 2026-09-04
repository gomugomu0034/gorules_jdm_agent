"""Static analysis of decision graphs.

Two routes, mirroring the test-run pair: one addressed at a saved graph, one taking raw
content so the unsaved canvas can be checked as it is edited.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends

from backend import auth
from backend.api.errors import ApiError
from backend.api.graphs import require_graph
from backend.models.api import LintRequest, LintResponse
from backend.tools.jdm_linter import lint

router = APIRouter(tags=["lint"])


def _report(content: dict) -> LintResponse:
    findings = [d.as_dict() for d in lint(content)]
    counts = {"error": 0, "warning": 0, "hint": 0}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return LintResponse(summary=counts, findings=findings)


@router.post("/api/graphs/{graph_id}/lint", response_model=LintResponse)
async def lint_graph(
    graph_id: str, body: LintRequest, owner: str = Depends(auth.get_owner)
) -> LintResponse:
    graph = await require_graph(graph_id, body.version, owner=owner)
    # As with running tests, `content` lets the caller check the unsaved canvas.
    content = body.content if body.content is not None else graph["content"]
    # Linting compiles the graph, which is CPU-bound enough to keep off the event loop.
    return await anyio.to_thread.run_sync(_report, content)


@router.post("/api/lint", response_model=LintResponse)
async def lint_content(body: LintRequest) -> LintResponse:
    if body.content is None:
        raise ApiError("VALIDATION_ERROR", "'content' is required.", 422)
    return await anyio.to_thread.run_sync(_report, body.content)
