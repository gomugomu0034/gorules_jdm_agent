"""Downloadable artifacts: the graph, its test suite, or both as a bundle.

Files are built server-side and sent with Content-Disposition so the browser
saves a real file rather than juggling blob URLs.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response

from backend.api.errors import ApiError
from backend.api.graphs import require_graph
from backend.db import dao

router = APIRouter(prefix="/api/graphs", tags=["graphs"])

ExportFormat = Literal["jdm", "tests", "bundle"]


def _attachment(payload: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def build_bundle(slug: str, name: str, content: dict, tests: list, version: int | None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{slug}_jdm.json", json.dumps(content, indent=2))
        archive.writestr(f"{slug}_tests.json", json.dumps(tests, indent=2))
        archive.writestr(
            "README.md",
            "\n".join(
                [
                    f"# {name}",
                    "",
                    f"- Version: {version if version is not None else 'unsaved'}",
                    f"- Nodes: {len(content.get('nodes', []))}",
                    f"- Test cases: {len(tests)}",
                    f"- Exported: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
                    "",
                    "`" + f"{slug}_jdm.json" + "` is a GoRules JDM decision model and can be",
                    "loaded directly by the Zen engine. `" + f"{slug}_tests.json" + "` is an",
                    "array of `{name, input, expectedOutput}` cases.",
                    "",
                ]
            ),
        )
    return buffer.getvalue()


def export_response(
    fmt: str, slug: str, name: str, content: dict, tests: list, version: int | None
) -> Response:
    if fmt == "jdm":
        return _attachment(
            json.dumps(content, indent=2).encode(), f"{slug}_jdm.json", "application/json"
        )
    if fmt == "tests":
        return _attachment(
            json.dumps(tests, indent=2).encode(), f"{slug}_tests.json", "application/json"
        )
    if fmt == "bundle":
        return _attachment(
            build_bundle(slug, name, content, tests, version), f"{slug}.zip", "application/zip"
        )
    raise ApiError("VALIDATION_ERROR", f"Unknown export format {fmt!r}.", 422)


@router.get("/{graph_id}/export")
async def export_graph(
    graph_id: str,
    format: ExportFormat = Query(default="jdm"),
    version: int | None = Query(default=None),
) -> Response:
    graph = await require_graph(graph_id, version)
    tests = [
        {"name": t["name"], "input": t["input"], "expectedOutput": t["expectedOutput"]}
        for t in await dao.list_tests(graph_id)
    ]
    return export_response(
        format, graph["slug"], graph["name"], graph["content"], tests, graph.get("version")
    )
