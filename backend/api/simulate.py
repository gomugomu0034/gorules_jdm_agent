"""Single-payload simulation.

The response is shaped exactly like the editor's ``Simulation`` type, so the
frontend can hand it to ``<DecisionGraph simulate={...}>`` untouched.
"""

from __future__ import annotations

import anyio
from fastapi import APIRouter

from backend.api.errors import ApiError
from backend.api.graphs import require_graph
from backend.models.api import (
    SimulateRequest,
    SimulateResponse,
    SimulationError,
    SimulationErrorData,
    SimulationOk,
)
from backend.tools.zen_evaluator import simulate as zen_simulate

router = APIRouter(tags=["simulate"])


async def _run(content: dict, context, trace: bool) -> SimulateResponse:
    try:
        # Zen is a hot Rust call; keep it off the event loop.
        outcome = await anyio.to_thread.run_sync(
            lambda: zen_simulate(content, context, trace)
        )
    except Exception as exc:  # noqa: BLE001 - a failed simulation is a result, not a 500
        return SimulateResponse(
            error=SimulationError(
                title="Simulation failed",
                message=str(exc),
                data=SimulationErrorData(nodeId=_node_id_from_error(str(exc), content)),
            )
        )

    return SimulateResponse(
        result=SimulationOk(
            performance=outcome["performance"],
            result=outcome["result"],
            snapshot=content,
            trace=outcome["trace"],
        )
    )


def _node_id_from_error(message: str, content: dict) -> str | None:
    """Best-effort: point the editor at the node the engine complained about."""
    for node in content.get("nodes", []):
        if node.get("id") and node["id"] in message:
            return node["id"]
        if node.get("name") and node["name"] in message:
            return node["id"]
    return None


@router.post("/api/simulate", response_model=SimulateResponse)
async def simulate_adhoc(body: SimulateRequest) -> SimulateResponse:
    if body.content is None:
        raise ApiError("VALIDATION_ERROR", "A 'content' graph is required.", 422)
    return await _run(body.content, body.context, body.trace)


@router.post("/api/graphs/{graph_id}/simulate", response_model=SimulateResponse)
async def simulate_stored(graph_id: str, body: SimulateRequest) -> SimulateResponse:
    content = body.content
    if content is None:
        graph = await require_graph(graph_id, body.version)
        content = graph["content"]
    return await _run(content, body.context, body.trace)
