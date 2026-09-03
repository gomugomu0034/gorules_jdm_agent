"""Graph CRUD, validation and version history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from backend import auth
from backend.api.errors import ApiError, not_found
from backend.db import dao
from backend.models.api import (
    CreateGraphRequest,
    GraphDetail,
    GraphListResponse,
    PatchGraphRequest,
    SaveGraphResponse,
    UpdateGraphRequest,
    ValidateRequest,
    ValidateResponse,
    VersionDetail,
    VersionListResponse,
)
from backend.models.jdm import DECISION_CONTENT_TYPE, blocking_errors, validate_decision

router = APIRouter(prefix="/api/graphs", tags=["graphs"])

EMPTY_GRAPH = {
    "contentType": DECISION_CONTENT_TYPE,
    "nodes": [
        {
            "id": "input-node",
            "name": "Request",
            "type": "inputNode",
            "position": {"x": 120, "y": 200},
            "content": {"schema": ""},
        },
        {
            "id": "output-node",
            "name": "Response",
            "type": "outputNode",
            "position": {"x": 620, "y": 200},
            "content": {"schema": ""},
        },
    ],
    "edges": [],
}


async def require_graph(
    graph_id: str, version: int | None = None, owner: str | None = None
) -> dict:
    """Fetch a graph the caller owns.

    A graph owned by someone else is reported as missing rather than
    forbidden - a 403 would confirm that the id exists.
    """
    graph = await dao.get_graph(graph_id, version, owner=owner)
    if graph is None:
        raise not_found("graph", graph_id)
    if version is not None and graph.get("version") != version:
        raise ApiError("VERSION_NOT_FOUND", f"Graph has no version {version}.", 404)
    return graph


@router.get("", response_model=GraphListResponse)
async def list_graphs(
    q: str | None = Query(default=None),
    archived: bool = Query(default=False),
    owner: str = Depends(auth.get_owner),
) -> GraphListResponse:
    return GraphListResponse(graphs=await dao.list_graphs(owner, q=q, archived=archived))


@router.post("", response_model=GraphDetail, status_code=status.HTTP_201_CREATED)
async def create_graph(
    body: CreateGraphRequest, owner: str = Depends(auth.get_owner)
) -> GraphDetail:
    content = body.content if body.content is not None else EMPTY_GRAPH
    errors = blocking_errors(validate_decision(content))
    if errors:
        raise ApiError(
            "VALIDATION_ERROR",
            "The graph is not a valid decision model.",
            422,
            [i.model_dump() for i in errors],
        )
    graph = await dao.create_graph(owner, body.name, content, body.description)
    return await _detail(graph)


@router.post("/validate", response_model=ValidateResponse)
async def validate(body: ValidateRequest) -> ValidateResponse:
    issues = validate_decision(body.content)
    return ValidateResponse(
        valid=not blocking_errors(issues), errors=[i.model_dump() for i in issues]
    )


@router.get("/{graph_id}", response_model=GraphDetail)
async def get_graph(
    graph_id: str,
    version: int | None = Query(default=None),
    owner: str = Depends(auth.get_owner),
) -> GraphDetail:
    return await _detail(await require_graph(graph_id, version, owner=owner))


@router.put("/{graph_id}", response_model=SaveGraphResponse)
async def update_graph(
    graph_id: str, body: UpdateGraphRequest, owner: str = Depends(auth.get_owner)
) -> SaveGraphResponse:
    graph = await require_graph(graph_id, owner=owner)

    if body.base_version is not None and body.base_version != graph["current_version"]:
        raise ApiError(
            "STALE_VERSION",
            "This graph changed since you loaded it; reload before saving.",
            409,
            {"your_version": body.base_version, "current_version": graph["current_version"]},
        )

    errors = blocking_errors(validate_decision(body.content, compile_check=False))
    if errors:
        raise ApiError(
            "VALIDATION_ERROR",
            "The graph is not a valid decision model.",
            422,
            [i.model_dump() for i in errors],
        )

    version = await dao.save_version(
        graph_id,
        body.content,
        message=body.message or ("autosave" if body.autosave else ""),
        author="user",
        is_autosave=body.autosave,
    )
    if body.name is not None or body.description is not None:
        await dao.update_graph_meta(
            graph_id, name=body.name, description=body.description, owner=owner
        )

    return SaveGraphResponse(
        graph=await _detail(await require_graph(graph_id, owner=owner)), version=version
    )


@router.patch("/{graph_id}", response_model=GraphDetail)
async def patch_graph(
    graph_id: str, body: PatchGraphRequest, owner: str = Depends(auth.get_owner)
) -> GraphDetail:
    await require_graph(graph_id, owner=owner)
    await dao.update_graph_meta(
        graph_id, name=body.name, description=body.description,
        archived=body.archived, owner=owner,
    )
    return await _detail(await require_graph(graph_id, owner=owner))


@router.delete("/{graph_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_graph(
    graph_id: str, hard: bool = Query(default=False), owner: str = Depends(auth.get_owner)
) -> Response:
    await require_graph(graph_id, owner=owner)
    if hard:
        await dao.delete_graph(graph_id, owner=owner)
    else:
        await dao.update_graph_meta(graph_id, archived=True, owner=owner)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

@router.get("/{graph_id}/versions", response_model=VersionListResponse)
async def list_versions(graph_id: str, owner: str = Depends(auth.get_owner)) -> VersionListResponse:
    await require_graph(graph_id, owner=owner)
    return VersionListResponse(versions=await dao.list_versions(graph_id))


@router.get("/{graph_id}/versions/{version}", response_model=VersionDetail)
async def get_version(graph_id: str, version: int, owner: str = Depends(auth.get_owner)) -> VersionDetail:
    await require_graph(graph_id, owner=owner)
    found = await dao.get_version(graph_id, version)
    if found is None:
        raise ApiError("VERSION_NOT_FOUND", f"Graph has no version {version}.", 404)
    return VersionDetail(**found)


@router.post("/{graph_id}/versions/{version}/restore", response_model=SaveGraphResponse)
async def restore_version(
    graph_id: str, version: int, owner: str = Depends(auth.get_owner)
) -> SaveGraphResponse:
    await require_graph(graph_id, owner=owner)
    found = await dao.get_version(graph_id, version)
    if found is None:
        raise ApiError("VERSION_NOT_FOUND", f"Graph has no version {version}.", 404)
    new_version = await dao.save_version(
        graph_id, found["content"], message=f"Restored from version {version}", author="user"
    )
    return SaveGraphResponse(
        graph=await _detail(await require_graph(graph_id, owner=owner)),
        version=new_version
    )


@router.get("/{graph_id}/versions/{a}/diff/{b}")
async def diff_versions(graph_id: str, a: int, b: int, owner: str = Depends(auth.get_owner)) -> dict:
    await require_graph(graph_id, owner=owner)
    left, right = await dao.get_version(graph_id, a), await dao.get_version(graph_id, b)
    if left is None or right is None:
        raise ApiError("VERSION_NOT_FOUND", "One of the versions does not exist.", 404)

    def by_id(content: dict) -> dict:
        return {n["id"]: n for n in content.get("nodes", [])}

    old, new = by_id(left["content"]), by_id(right["content"])
    return {
        "from": a,
        "to": b,
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "modified": sorted(k for k in set(old) & set(new) if old[k] != new[k]),
    }


async def _detail(graph: dict) -> GraphDetail:
    tests = await dao.list_tests(graph["id"])
    return GraphDetail(
        **{k: graph[k] for k in ("id", "name", "slug", "description", "current_version",
                                 "created_at", "updated_at", "archived_at")},
        content=graph["content"],
        version=graph.get("version", graph["current_version"]),
        node_count=len(graph["content"].get("nodes", [])),
        test_count=len(tests),
    )
