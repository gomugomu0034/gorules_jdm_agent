"""Chat threads, the agent event stream, and proposal review."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage

from backend.api.errors import ApiError, not_found
from backend.api.exports import export_response
from backend.db import dao
from backend.models.api import (
    AcceptProposalRequest,
    CreateThreadRequest,
    PendingInterrupt,
    RejectProposalRequest,
    ResumeRequest,
    RunAcceptedResponse,
    SendMessageRequest,
    ThreadStateResponse,
    ThreadSummary,
)
from backend.services import chat_runner, event_bus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

KEEPALIVE_SECONDS = 15


async def require_thread(thread_id: str) -> dict:
    thread = await dao.get_thread(thread_id)
    if thread is None:
        raise not_found("thread", thread_id)
    return thread


@router.post("/threads", response_model=ThreadSummary, status_code=status.HTTP_201_CREATED)
async def create_thread(body: CreateThreadRequest) -> ThreadSummary:
    thread = await dao.create_thread(body.graph_id, body.title or "New chat")
    return ThreadSummary(**thread)


@router.get("/threads", response_model=list[ThreadSummary])
async def list_threads(graph_id: str | None = Query(default=None)) -> list[ThreadSummary]:
    return [ThreadSummary(**t) for t in await dao.list_threads(graph_id)]


@router.get("/threads/{thread_id}", response_model=ThreadStateResponse)
async def get_thread(thread_id: str) -> ThreadStateResponse:
    """Everything needed to rebuild the panel after a reload."""
    thread = await require_thread(thread_id)

    messages: list[dict] = []
    pending = None
    try:
        from backend.agent_runtime import get_graph

        state = await get_graph().aget_state({"configurable": {"thread_id": thread_id}})
        for m in (state.values or {}).get("messages", []):
            if isinstance(m, HumanMessage):
                messages.append({"role": "user", "content": str(m.content)})
            elif isinstance(m, AIMessage) and str(m.content).strip():
                messages.append({"role": "assistant", "content": str(m.content)})

        payload = chat_runner._pending_interrupt(state)
        if payload is not None:
            options = list(payload.get("options") or [])
            pending = PendingInterrupt(
                prompt=payload.get("prompt", ""),
                options=options,
                kind="choice" if options else "text",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read agent state for %s: %s", thread_id, exc)

    events = await dao.list_events(thread_id)
    return ThreadStateResponse(
        id=thread["id"],
        graph_id=thread["graph_id"],
        status=thread["status"],
        messages=messages,
        pending_interrupt=pending,
        proposal=await dao.get_proposal(thread_id),
        last_seq=events[-1]["seq"] if events else 0,
    )


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: str) -> Response:
    await require_thread(thread_id)
    await dao.delete_thread(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Running the agent
# --------------------------------------------------------------------------

@router.post("/threads/{thread_id}/messages", response_model=RunAcceptedResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def send_message(thread_id: str, body: SendMessageRequest) -> RunAcceptedResponse:
    await require_thread(thread_id)
    try:
        run_id = await chat_runner.start_message(thread_id, body.text, body.canvas)
    except chat_runner.ThreadBusy:
        raise ApiError("THREAD_BUSY", "This conversation is already running.", 409) from None
    return RunAcceptedResponse(run_id=run_id)


@router.post("/threads/{thread_id}/resume", response_model=RunAcceptedResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def resume(thread_id: str, body: ResumeRequest) -> RunAcceptedResponse:
    thread = await require_thread(thread_id)
    if thread["status"] != "awaiting_input":
        raise ApiError(
            "NOT_AWAITING_INPUT",
            "This conversation is not waiting for a reply right now.",
            409,
            {"status": thread["status"]},
        )
    try:
        run_id = await chat_runner.start_resume(thread_id, body.value, body.canvas)
    except chat_runner.ThreadBusy:
        raise ApiError("THREAD_BUSY", "This conversation is already running.", 409) from None
    return RunAcceptedResponse(run_id=run_id)


@router.post("/threads/{thread_id}/cancel")
async def cancel(thread_id: str) -> dict:
    await require_thread(thread_id)
    stopped = await chat_runner.request_cancel(thread_id)
    return {"cancelled": stopped}


@router.get("/threads/{thread_id}/events")
async def list_events(thread_id: str, from_seq: int = Query(default=0)) -> dict:
    """Persisted events as plain JSON.

    The UI uses the SSE stream; this is the non-streaming equivalent, useful for
    debugging and for any client that cannot hold a long-lived connection.
    """
    await require_thread(thread_id)
    return {"events": await dao.list_events(thread_id, from_seq=from_seq)}


@router.get("/threads/{thread_id}/stream")
async def stream(thread_id: str, request: Request, from_seq: int = Query(default=0)):
    """Server-sent events for one conversation.

    `from_seq` replays anything missed while disconnected, so a reload or a
    sleeping laptop does not lose a long build.
    """
    await require_thread(thread_id)
    queue = event_bus.subscribe(thread_id)

    async def generator():
        try:
            for event in await dao.list_events(thread_id, from_seq=from_seq):
                yield _frame(event)

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _frame(event)
        finally:
            event_bus.unsubscribe(thread_id, queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _frame(event: dict) -> str:
    seq = event.get("seq", 0)
    kind = event.get("type", "message")
    return f"id: {seq}\nevent: {kind}\ndata: {json.dumps(event)}\n\n"


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------

@router.post("/threads/{thread_id}/proposal/accept")
async def accept_proposal(thread_id: str, body: AcceptProposalRequest) -> dict:
    await require_thread(thread_id)
    proposal = await dao.get_proposal(thread_id)
    if proposal is None:
        raise ApiError("NO_PROPOSAL", "There is nothing to accept on this thread.", 404)

    graph_id = body.graph_id or proposal["graph_id"]
    name = body.name or proposal["usecase_name"]

    if graph_id and await dao.get_graph(graph_id):
        version = await dao.save_version(
            graph_id, proposal["jdm"],
            message=f"Applied agent changes: {name}",
            author="agent", thread_id=thread_id,
        )
    else:
        created = await dao.create_graph(
            name, proposal["jdm"], author="agent", message="Created by the agent"
        )
        graph_id, version = created["id"], created["current_version"]
        await dao.set_thread_graph(thread_id, graph_id)

    if proposal["tests"]:
        await dao.replace_tests(graph_id, proposal["tests"])

    await dao.clear_proposal(thread_id)
    return {"graph_id": graph_id, "version": version}


@router.post("/threads/{thread_id}/proposal/reject")
async def reject_proposal(thread_id: str, body: RejectProposalRequest) -> dict:
    await require_thread(thread_id)
    if await dao.get_proposal(thread_id) is None:
        raise ApiError("NO_PROPOSAL", "There is nothing to reject on this thread.", 404)
    await dao.clear_proposal(thread_id)
    return {"rejected": True, "reason": body.reason}


@router.get("/threads/{thread_id}/proposal/export")
async def export_proposal(thread_id: str, format: str = Query(default="jdm")) -> Response:
    """Download what the agent produced, before deciding whether to keep it."""
    await require_thread(thread_id)
    proposal = await dao.get_proposal(thread_id)
    if proposal is None:
        raise ApiError("NO_PROPOSAL", "There is nothing to download on this thread.", 404)

    slug = dao.slugify(proposal["usecase_name"])
    return export_response(
        format, slug, proposal["usecase_name"], proposal["jdm"], proposal["tests"], None
    )
