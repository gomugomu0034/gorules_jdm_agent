"""Schema creation and one-time import of the legacy on-disk graphs."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from backend.config import settings
from backend.db import connection, dao

logger = logging.getLogger(__name__)

BOOTSTRAP_KEY = "bootstrap_v1"
SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


def humanise(stem: str) -> str:
    """'RefundPolicy_jdm' -> 'Refund Policy'."""
    name = re.sub(r"_jdm$", "", stem)
    name = name.replace("_", " ")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return re.sub(r"\s+", " ", name).strip() or stem


async def apply_schema() -> None:
    await connection.script(SCHEMA_FILE.read_text(encoding="utf-8"))


async def import_legacy_graphs() -> int:
    """Import backend/jdm_graphs/*.json and their matching test suites."""
    graphs_dir = Path(settings.legacy_graphs_dir)
    tests_dir = Path(settings.legacy_tests_dir)
    if not graphs_dir.is_dir():
        return 0

    imported = 0
    for path in sorted(graphs_dir.glob("*_jdm.json")):
        name = humanise(path.stem)
        if await dao.find_graph_by_name(name):
            continue

        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping %s: %s", path.name, exc)
            continue

        graph = await dao.create_graph(
            name=name,
            content=content,
            description=f"Imported from {path.name}",
            author="import",
            message=f"Imported from {path.name}",
        )
        imported += 1

        test_file = tests_dir / f"{path.stem.removesuffix('_jdm')}_tests.json"
        tests: list = []
        if test_file.is_file():
            try:
                tests = json.loads(test_file.read_text(encoding="utf-8"))
                await dao.replace_tests(graph["id"], tests)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not import tests for %s: %s", name, exc)

        _warn_if_failing(name, content, tests)

    return imported


def _warn_if_failing(name: str, content: dict, tests: list) -> None:
    """Log up front when an imported graph does not satisfy its own suite.

    Both graphs shipped in this repo fail every assertion. Surfacing that at
    import time is deliberate: it is pre-existing breakage that the old
    evaluator hid, not a regression in the test runner.
    """
    if not tests:
        return
    try:
        from backend.tools.zen_evaluator import run_test_suite

        summary = run_test_suite(content, tests, trace=False)["summary"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Imported %r but could not evaluate it: %s", name, exc)
        return

    if summary["failed"] or summary["errored"]:
        logger.warning(
            "Imported %r with failing tests: %s/%s passed (%s failed, %s errored). "
            "These graphs do not produce their declared output fields.",
            name, summary["passed"], summary["total"],
            summary["failed"], summary["errored"],
        )


async def bootstrap() -> None:
    await apply_schema()
    if await dao.get_meta(BOOTSTRAP_KEY):
        return
    count = await import_legacy_graphs()
    await dao.set_meta(BOOTSTRAP_KEY, "done")
    logger.info("Bootstrap complete; imported %s legacy graph(s).", count)
