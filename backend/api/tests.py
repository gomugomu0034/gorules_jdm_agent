"""Test suite CRUD and execution."""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, Request, Response, status

from backend import auth, corpus
from backend.api.errors import ApiError
from backend.api.graphs import require_graph
from backend.db import dao
from backend.models.api import (
    CheckTestRequest,
    CheckTestResponse,
    PatchTestRequest,
    ReplaceTestsRequest,
    RunTestsRequest,
    TestCase,
    TestListResponse,
    TestRunResponse,
)
from backend.tools.zen_evaluator import run_test_suite

router = APIRouter(tags=["tests"])


@router.get("/api/graphs/{graph_id}/tests", response_model=TestListResponse)
async def list_tests(graph_id: str, owner: str = Depends(auth.get_owner)) -> TestListResponse:
    await require_graph(graph_id, owner=owner)
    return TestListResponse(tests=await dao.list_tests(graph_id))


@router.put("/api/graphs/{graph_id}/tests", response_model=TestListResponse)
async def replace_tests(
    graph_id: str, body: ReplaceTestsRequest, owner: str = Depends(auth.get_owner)
) -> TestListResponse:
    await require_graph(graph_id, owner=owner)
    tests = await dao.replace_tests(graph_id, [t.model_dump() for t in body.tests])
    return TestListResponse(tests=tests)


@router.post("/api/graphs/{graph_id}/tests", response_model=TestCase,
             status_code=status.HTTP_201_CREATED)
async def add_test(graph_id: str, body: TestCase, owner: str = Depends(auth.get_owner)) -> TestCase:
    await require_graph(graph_id, owner=owner)
    return TestCase(**await dao.add_test(graph_id, body.model_dump()))


@router.patch("/api/graphs/{graph_id}/tests/{test_id}", response_model=TestListResponse)
async def patch_test(
    graph_id: str, test_id: str, body: PatchTestRequest, owner: str = Depends(auth.get_owner)
) -> TestListResponse:
    await require_graph(graph_id, owner=owner)
    await dao.update_test(graph_id, test_id, body.model_dump(exclude_unset=True))
    return TestListResponse(tests=await dao.list_tests(graph_id))


@router.delete("/api/graphs/{graph_id}/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_test(graph_id: str, test_id: str, owner: str = Depends(auth.get_owner)) -> Response:
    await require_graph(graph_id, owner=owner)
    await dao.delete_test(graph_id, test_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

async def _execute(content: dict, tests: list[dict], strict: bool) -> dict:
    return await anyio.to_thread.run_sync(
        lambda: run_test_suite(content, tests, trace=True, subset=not strict)
    )


@router.post("/api/graphs/{graph_id}/tests/run", response_model=TestRunResponse)
async def run_graph_tests(
    graph_id: str, body: RunTestsRequest, owner: str = Depends(auth.get_owner)
) -> TestRunResponse:
    graph = await require_graph(graph_id, body.version, owner=owner)
    # `content` lets the caller test the unsaved canvas rather than what's stored.
    content = body.content if body.content is not None else graph["content"]

    tests = await dao.list_tests(graph_id)
    if body.test_ids is not None:
        wanted = set(body.test_ids)
        tests = [t for t in tests if t["id"] in wanted]
    tests = [t for t in tests if t.get("enabled", True)]

    if not tests:
        return TestRunResponse(
            summary={"total": 0, "passed": 0, "failed": 0, "errored": 0,
                     "skipped": 0, "duration_ms": 0},
            results=[],
        )

    report = await _execute(content, tests, body.strict)
    await dao.record_test_run(
        graph_id, graph.get("version"), report["summary"], report["results"]
    )
    return TestRunResponse(**report)


@router.post("/api/tests/run", response_model=TestRunResponse)
async def run_adhoc_tests(body: RunTestsRequest) -> TestRunResponse:
    if body.content is None or body.tests is None:
        raise ApiError("VALIDATION_ERROR", "Both 'content' and 'tests' are required.", 422)
    report = await _execute(body.content, [t.model_dump() for t in body.tests], body.strict)
    return TestRunResponse(**report)


@router.post("/api/tests/check", response_model=CheckTestResponse)
async def check_test(body: CheckTestRequest) -> CheckTestResponse:
    """Put one draft case to the live canvas and report what happened.

    Written for someone typing a test by hand, where the useful question is not "did it
    pass" but "which of us is wrong - the policy or my expectation". So it answers three
    things separately: whether the graph would run the input at all, whether the fields
    being asserted are ones the graph can even produce, and where the values differ.

    Stateless and unsaved, like `POST /api/tests/run` beside it. Nothing here writes.
    """
    from backend.tools.jdm_linter import interface_of

    accepts, produces = interface_of(body.content)

    # A field the graph never writes cannot be asserted about. Caught before the run,
    # because "expected 5, got null" for a misspelt name reads as a policy defect rather
    # than a typo - and that is the mistake a person typing JSON by hand actually makes.
    expected = body.expectedOutput if isinstance(body.expectedOutput, dict) else {}
    unknown = [field for field in expected if field.split(".")[0] not in produces]

    case = {"name": "draft", "input": body.input, "expectedOutput": body.expectedOutput}
    report = await _execute(body.content, [case], strict=False)
    result = report["results"][0]

    if result["status"] == "errored" or report["summary"].get("compile_error"):
        return CheckTestResponse(
            ran=False,
            error=report["summary"].get("compile_error") or result.get("error")
                  or "The graph could not run this input.",
            unknown_fields=unknown, produces=produces, accepts=accepts,
        )

    return CheckTestResponse(
        ran=True,
        actual=result.get("actual"),
        # A case with nothing asserted is reported as skipped rather than passed, and
        # calling that a match would let an empty expectation look like a green tick.
        matches=result["status"] == "passed",
        mismatches=result.get("mismatches") or [],
        unknown_fields=unknown,
        produces=produces,
        accepts=accepts,
    )


@router.post("/api/graphs/{graph_id}/tests/generate", response_model=TestListResponse)
async def generate_tests(
    graph_id: str, request: Request, owner: str = Depends(auth.get_owner)
) -> TestListResponse:
    """Generate a suite with the LLM. Returns without saving.

    This is the only model call outside the agent's own serialisation, so it is the only
    place two conversations could claim the same quota at once. Refused rather than
    queued: a queue would hide the contention behind a spinner, where a 409 names it.
    """
    graph = await require_graph(graph_id, owner=owner)
    from backend.lang_graph_agent import RateLimited
    from backend.services import chat_runner
    from backend.services.test_service import generate_test_suite

    if await chat_runner.running_thread_for_graph(owner, graph_id):
        raise ApiError(
            "WORKSPACE_BUSY",
            "The assistant is working on this policy. Wait for it to finish, or stop it, "
            "then generate the tests.",
            409,
        )
    if chat_runner.is_generating(graph_id):
        raise ApiError(
            "WORKSPACE_BUSY", "Tests are already being generated for this policy.", 409
        )

    try:
        # Its own corpus run: reached straight from the API, so there is no agent turn to
        # attribute the call to, and a sample with no run at all loses the graph it was
        # generated for.
        async with chat_runner.generating(graph_id):
            with corpus.run_scope(corpus.new_run_id(), graph_id=graph_id, owner=owner):
                corpus.observe(intent="TEST", mode="EXISTING")
                try:
                    tests = await generate_test_suite(graph["content"])
                    corpus.observe(outcome="completed")
                except BaseException:
                    corpus.observe(outcome="error")
                    raise
    except RateLimited as exc:
        raise ApiError("LLM_RATE_LIMITED", f"The model provider is rate limiting: {exc}", 429) from exc
    except Exception as exc:  # noqa: BLE001
        raise ApiError("LLM_ERROR", f"Could not generate tests: {exc}", 502) from exc
    return TestListResponse(tests=[TestCase(**t) for t in tests])
