"""Request and response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# --------------------------------------------------------------------------
# Graphs
# --------------------------------------------------------------------------

class GraphSummary(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    current_version: int
    node_count: int
    test_count: int
    created_at: str
    updated_at: str
    archived_at: str | None = None


class GraphDetail(GraphSummary):
    content: dict[str, Any]
    version: int


class GraphListResponse(BaseModel):
    graphs: list[GraphSummary]


class CreateGraphRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    content: dict[str, Any] | None = None


class UpdateGraphRequest(BaseModel):
    content: dict[str, Any]
    name: str | None = None
    description: str | None = None
    message: str = ""
    base_version: int | None = None
    autosave: bool = False


class PatchGraphRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    archived: bool | None = None


class SaveGraphResponse(BaseModel):
    graph: GraphDetail
    version: int


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

class VersionSummary(BaseModel):
    version: int
    message: str
    author: str
    is_autosave: bool
    thread_id: str | None = None
    created_at: str
    node_count: int


class VersionListResponse(BaseModel):
    versions: list[VersionSummary]


class VersionDetail(BaseModel):
    version: int
    content: dict[str, Any]
    message: str
    author: str
    created_at: str


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

class ValidateRequest(BaseModel):
    content: dict[str, Any]


class ValidateResponse(BaseModel):
    valid: bool
    errors: list[dict[str, str]] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

class SimulateRequest(BaseModel):
    content: dict[str, Any] | None = None
    context: Any = Field(default_factory=dict)
    trace: bool = True
    version: int | None = None


class SimulationOk(BaseModel):
    performance: str | None = None
    result: Any = None
    snapshot: dict[str, Any]
    trace: dict[str, Any] = Field(default_factory=dict)


class SimulationErrorData(BaseModel):
    nodeId: str | None = None


class SimulationError(BaseModel):
    title: str | None = None
    message: str | None = None
    data: SimulationErrorData = Field(default_factory=SimulationErrorData)


class SimulateResponse(BaseModel):
    result: SimulationOk | None = None
    error: SimulationError | None = None


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class TestCase(BaseModel):
    id: str | None = None
    name: str = ""
    input: Any = Field(default_factory=dict)
    expectedOutput: Any = Field(default_factory=dict)
    enabled: bool = True
    order: int = 0


class TestListResponse(BaseModel):
    tests: list[TestCase]


class ReplaceTestsRequest(BaseModel):
    tests: list[TestCase]


class PatchTestRequest(BaseModel):
    name: str | None = None
    input: Any = None
    expectedOutput: Any = None
    enabled: bool | None = None
    order: int | None = None


class RunTestsRequest(BaseModel):
    content: dict[str, Any] | None = None
    test_ids: list[str] | None = None
    tests: list[TestCase] | None = None
    strict: bool = False
    version: int | None = None


class TestRunResponse(BaseModel):
    summary: dict[str, Any]
    results: list[dict[str, Any]]


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------

class CanvasPayload(BaseModel):
    """The graph as it stands in the editor, including unsaved edits."""

    content: dict[str, Any] = Field(default_factory=lambda: {"nodes": [], "edges": []})
    graph_id: str | None = None
    name: str | None = None


class CreateThreadRequest(BaseModel):
    graph_id: str | None = None
    title: str | None = None


class ThreadSummary(BaseModel):
    id: str
    graph_id: str | None = None
    title: str
    status: str
    created_at: str
    updated_at: str


class SendMessageRequest(BaseModel):
    text: str = Field(min_length=1)
    canvas: CanvasPayload = Field(default_factory=CanvasPayload)


class ResumeRequest(BaseModel):
    value: str
    canvas: CanvasPayload | None = None


class RunAcceptedResponse(BaseModel):
    run_id: str
    status: Literal["accepted"] = "accepted"


class PendingInterrupt(BaseModel):
    prompt: str
    options: list[str]
    kind: Literal["choice", "text"]


class ThreadStateResponse(BaseModel):
    id: str
    graph_id: str | None = None
    status: str
    messages: list[dict[str, Any]]
    pending_interrupt: PendingInterrupt | None = None
    proposal: dict[str, Any] | None = None
    last_seq: int = 0


class AcceptProposalRequest(BaseModel):
    graph_id: str | None = None
    name: str | None = None
    # When there is no target graph yet, False returns the content as an
    # unsaved draft and True commits it as a new graph.
    persist: bool = False


class RejectProposalRequest(BaseModel):
    reason: str = ""


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class SessionResponse(BaseModel):
    mode: Literal["guest", "admin"]
    email: str | None = None
    # False when no ADMIN_PASSWORD is configured, so the UI can hide sign-in
    # instead of offering a form that cannot succeed.
    login_enabled: bool = False
