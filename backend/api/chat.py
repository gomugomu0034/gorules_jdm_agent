"""Chat threads, the agent event stream, and proposal review."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage

from backend import auth, corpus
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


async def require_thread(thread_id: str, owner: str | None = None) -> dict:
    """Fetch a thread the caller owns; another owner's reads as missing."""
    thread = await dao.get_thread(thread_id, owner=owner)
    if thread is None:
        raise not_found("thread", thread_id)
    return thread


@router.post("/threads", response_model=ThreadSummary, status_code=status.HTTP_201_CREATED)
async def create_thread(
    body: CreateThreadRequest, owner: str = Depends(auth.get_owner)
) -> ThreadSummary:
    thread = await dao.create_thread(owner, body.graph_id, body.title or "New chat")
    return ThreadSummary(**thread)


@router.get("/threads", response_model=list[ThreadSummary])
async def list_threads(
    graph_id: str | None = Query(default=None), owner: str = Depends(auth.get_owner)
) -> list[ThreadSummary]:
    return [ThreadSummary(**t) for t in await dao.list_threads(owner, graph_id)]


@router.get("/threads/{thread_id}", response_model=ThreadStateResponse)
async def get_thread(thread_id: str, owner: str = Depends(auth.get_owner)) -> ThreadStateResponse:
    """Everything needed to rebuild the panel after a reload."""
    thread = await require_thread(thread_id, owner)

    messages: list[dict] = []
    pending = None
    try:
        from backend.agent_runtime import get_graph

        state = await get_graph().aget_state({"configurable": {"thread_id": thread_id}})
        for m in (state.values or {}).get("messages", []):
            if isinstance(m, HumanMessage):
                messages.append({"role": "user", "content": str(m.content)})
            elif isinstance(m, AIMessage) and str(m.content).strip():
                # Same filter as the live stream, so a reload shows the same
                # conversation rather than the builder's internal DSL dumps.
                if chat_runner._is_internal(m):
                    continue
                messages.append({"role": "assistant", "content": str(m.content)})

        payload = chat_runner._pending_interrupt(state)
        if payload is not None:
            options = list(payload.get("options") or [])
            pending = PendingInterrupt(
                prompt=payload.get("prompt", ""),
                options=options,
                kind="choice" if options else "text",
            )

        # The checkpoint is the authority on whether the agent is waiting. A process that
        # died between writing it and recording `awaiting_input` leaves a thread whose
        # stored status disagrees - and `resume` trusts the stored status, so the pending
        # question would render with every answer refused. Reconcile here, which is the
        # one place both are already in hand, and the moment it matters: page load.
        stale = pending is not None and thread["status"] != "awaiting_input"
        if stale and not chat_runner.is_running(thread_id):
            await dao.set_thread_status(thread_id, "awaiting_input")
            thread = dict(thread, status="awaiting_input")
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
async def delete_thread(thread_id: str, owner: str = Depends(auth.get_owner)) -> Response:
    await require_thread(thread_id, owner)
    await dao.delete_thread(thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Running the agent
# --------------------------------------------------------------------------

@router.post("/threads/{thread_id}/messages", response_model=RunAcceptedResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    thread_id: str, body: SendMessageRequest, owner: str = Depends(auth.get_owner)
) -> RunAcceptedResponse:
    await require_thread(thread_id, owner)
    try:
        run_id = await chat_runner.start_message(thread_id, body.text, body.canvas)
    except chat_runner.ThreadBusy:
        raise ApiError("THREAD_BUSY", "This conversation is already running.", 409) from None
    except chat_runner.WorkspaceBusy as busy:
        raise ApiError(
            "WORKSPACE_BUSY",
            f"Something else is already using the model for this policy ({busy}). "
            "Wait for it to finish and ask again.",
            409,
        ) from None
    return RunAcceptedResponse(run_id=run_id)


@router.post("/threads/{thread_id}/resume", response_model=RunAcceptedResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def resume(
    thread_id: str, body: ResumeRequest, owner: str = Depends(auth.get_owner)
) -> RunAcceptedResponse:
    thread = await require_thread(thread_id, owner)
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
async def cancel(thread_id: str, owner: str = Depends(auth.get_owner)) -> dict:
    await require_thread(thread_id, owner)
    stopped = await chat_runner.request_cancel(thread_id)
    return {"cancelled": stopped}


@router.get("/threads/{thread_id}/events")
async def list_events(
    thread_id: str, from_seq: int = Query(default=0), owner: str = Depends(auth.get_owner)
) -> dict:
    """Persisted events as plain JSON.

    The UI uses the SSE stream; this is the non-streaming equivalent, useful for
    debugging and for any client that cannot hold a long-lived connection.
    """
    await require_thread(thread_id, owner)
    return {"events": await dao.list_events(thread_id, from_seq=from_seq)}


@router.get("/threads/{thread_id}/stream")
async def stream(
    thread_id: str, request: Request, from_seq: int = Query(default=0), owner: str = Depends(auth.get_owner)
):
    """Server-sent events for one conversation.

    `from_seq` replays anything missed while disconnected, so a reload or a
    sleeping laptop does not lose a long build.
    """
    await require_thread(thread_id, owner)
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
            # Nobody is watching this run any more. That may mean the tab was closed, or
            # only that it was reloaded, so this starts a grace period rather than
            # stopping anything: see `watch_disconnect`.
            chat_runner.watch_disconnect(thread_id)

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
async def accept_proposal(
    thread_id: str, body: AcceptProposalRequest, owner: str = Depends(auth.get_owner)
) -> dict:
    """Take the agent's proposal onto the canvas.

    With `persist=False` (the default for an unsaved draft) the content is
    handed back without touching the database, so a visitor can look at the
    graph and run its tests before deciding to keep it. Accepting into an
    existing graph, or explicitly asking to persist, writes a version as before.
    """
    await require_thread(thread_id, owner)
    proposal = await dao.get_proposal(thread_id)
    if proposal is None:
        raise ApiError("NO_PROPOSAL", "There is nothing to accept on this thread.", 404)

    graph_id = body.graph_id or proposal["graph_id"]
    name = body.name or proposal["usecase_name"]
    target = await dao.get_graph(graph_id, owner=owner) if graph_id else None

    if target is not None:
        version = await dao.save_version(
            graph_id, proposal["jdm"],
            message=f"Applied agent changes: {name}",
            author="agent", thread_id=thread_id,
        )
    elif body.persist:
        created = await dao.create_graph(
            owner, name, proposal["jdm"],
            author="agent", message="Created by the agent",
        )
        graph_id, version = created["id"], created["current_version"]
        await dao.set_thread_graph(thread_id, graph_id)
    else:
        # Draft: nothing is stored yet. The client holds the content and saves
        # it later through POST /api/graphs once the user has named it.
        corpus.record_interaction(
            kind="approval", thread_id=thread_id, graph_id=None,
            response="accepted", detail={"name": name, "draft": True},
        )
        await dao.clear_proposal(thread_id)
        return {
            "graph_id": None,
            "version": None,
            "draft": True,
            "name": name,
            "content": proposal["jdm"],
            "tests": proposal["tests"],
        }

    if proposal["tests"]:
        await dao.replace_tests(graph_id, proposal["tests"])

    corpus.record_interaction(
        kind="approval", thread_id=thread_id, graph_id=graph_id,
        response="accepted", detail={"version": version, "name": name, "draft": False},
    )
    await dao.clear_proposal(thread_id)
    return {"graph_id": graph_id, "version": version, "draft": False}


@router.post("/threads/{thread_id}/proposal/reject")
async def reject_proposal(
    thread_id: str, body: RejectProposalRequest, owner: str = Depends(auth.get_owner)
) -> dict:
    thread = await require_thread(thread_id, owner)
    proposal = await dao.get_proposal(thread_id)
    if proposal is None:
        raise ApiError("NO_PROPOSAL", "There is nothing to reject on this thread.", 404)
    # The reason was echoed back to the client and stored nowhere. A rejected proposal with
    # the reason attached is the negative half of a preference pair; without the reason it
    # is only a shrug.
    corpus.record_interaction(
        kind="rejection", thread_id=thread_id, graph_id=thread.get("graph_id"),
        response=body.reason or "rejected",
        detail={"reason": body.reason, "usecase_name": proposal["usecase_name"],
                "jdm": proposal["jdm"]},
    )
    await dao.clear_proposal(thread_id)
    return {"rejected": True, "reason": body.reason}


@router.get("/threads/{thread_id}/proposal/export")
async def export_proposal(
    thread_id: str, format: str = Query(default="jdm"), owner: str = Depends(auth.get_owner)
) -> Response:
    """Download what the agent produced, before deciding whether to keep it."""
    await require_thread(thread_id, owner)
    proposal = await dao.get_proposal(thread_id)
    if proposal is None:
        raise ApiError("NO_PROPOSAL", "There is nothing to download on this thread.", 404)

    slug = dao.slugify(proposal["usecase_name"])
    return export_response(
        format, slug, proposal["usecase_name"], proposal["jdm"], proposal["tests"], None
    )
