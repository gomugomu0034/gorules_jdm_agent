"""Importing graphs from uploaded files."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile, status

from backend.api.errors import ApiError
from backend.api.graphs import _detail
from backend.db import bootstrap, dao
from backend.models.api import GraphDetail
from backend.models.jdm import blocking_errors, validate_decision

router = APIRouter(prefix="/api/graphs", tags=["graphs"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class ImportResponse(GraphDetail):
    tests_imported: int = 0


def _decode_graph(raw: bytes, label: str) -> dict:
    try:
        content = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("VALIDATION_ERROR", f"{label} is not valid JSON: {exc}", 422) from exc

    errors = blocking_errors(validate_decision(content))
    if errors:
        raise ApiError(
            "VALIDATION_ERROR",
            f"{label} is not a valid decision model.",
            422,
            [e.model_dump() for e in errors],
        )
    return content


def _read_zip(raw: bytes) -> tuple[dict, list, str | None]:
    """Pull `<name>_jdm.json` and an optional `<name>_tests.json` out of a bundle."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ApiError("VALIDATION_ERROR", f"Not a readable zip archive: {exc}", 422) from exc

    names = [n for n in archive.namelist() if not n.startswith("__MACOSX")]
    graph_names = [n for n in names if n.endswith("_jdm.json")] or [
        n for n in names if n.endswith(".json") and not n.endswith("_tests.json")
    ]
    if not graph_names:
        raise ApiError("VALIDATION_ERROR", "The archive contains no graph JSON file.", 422)

    graph_name = graph_names[0]
    content = _decode_graph(archive.read(graph_name), graph_name)

    stem = Path(graph_name).stem.removesuffix("_jdm")
    tests: list = []
    for candidate in (f"{stem}_tests.json", f"{Path(graph_name).parent}/{stem}_tests.json"):
        if candidate in names:
            try:
                loaded = json.loads(archive.read(candidate).decode("utf-8"))
                if isinstance(loaded, list):
                    tests = loaded
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            break

    return content, tests, bootstrap.humanise(Path(graph_name).stem)


@router.post("/import", response_model=ImportResponse, status_code=status.HTTP_201_CREATED)
async def import_graph(
    file: UploadFile = File(...),
    name: str | None = Form(default=None),
) -> ImportResponse:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError("VALIDATION_ERROR", "File exceeds the 10 MB upload limit.", 413)
    if not raw:
        raise ApiError("VALIDATION_ERROR", "The uploaded file is empty.", 422)

    filename = file.filename or "upload.json"
    tests: list = []

    if filename.lower().endswith(".zip"):
        content, tests, derived = _read_zip(raw)
    else:
        content = _decode_graph(raw, filename)
        derived = bootstrap.humanise(Path(filename).stem)

    graph = await dao.create_graph(
        name=name or derived,
        content=content,
        description=f"Imported from {filename}",
        author="import",
        message=f"Imported from {filename}",
    )
    if tests:
        await dao.replace_tests(graph["id"], tests)

    detail = await _detail(await dao.get_graph(graph["id"]))
    return ImportResponse(**detail.model_dump(), tests_imported=len(tests))
