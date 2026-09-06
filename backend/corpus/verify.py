"""Judge a graph against an exam its author did not write.

Every objective label in this corpus rested on `tests_passed`, and the suite behind it came
out of the same reply as the graph. A model that misread the requirement wrote tests that
misread it identically and scored a clean pass. This closes that.

What the author is given is the requirement and the graph's **interface** - the field names
it reads and writes - and nothing else. Sharing the interface is deliberate: without it the
author invents plausible names and every assertion misses on spelling, which fails correct
graphs for a reason unrelated to their logic. Sharing the rules would defeat the point, so
it does not.

Suites are cached on the requirement and the interface, so one is authored per requirement
and reused across every model that attempts it - which is also what makes a bake-off's
scores comparable rather than each model being marked by its own examiner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from backend.corpus import store
from backend.tools.jdm_linter import interface_of

logger = logging.getLogger(__name__)


def _content(node: dict) -> dict:
    content = node.get("content")
    return content if isinstance(content, dict) else {}


def suite_id(requirement: str, reads: list[str], writes: list[str]) -> str:
    payload = json.dumps([requirement.strip(), reads, writes], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def cached_suite(sid: str) -> list[dict] | None:
    if not store.enabled():
        return None
    try:
        row = store._connect().execute(
            "SELECT cases_json FROM suites WHERE suite_id = ?", (sid,)
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    try:
        return json.loads(row["cases_json"])
    except json.JSONDecodeError:
        return None


@store._never_fails
def remember_suite(sid: str, requirement: str, reads: list[str], writes: list[str],
                   cases: list[dict], authored_by: str) -> None:
    with store._lock:
        store._connect().execute(
            "INSERT OR REPLACE INTO suites (suite_id, requirement, inputs_json,"
            " outputs_json, cases_json, authored_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, requirement, json.dumps(reads), json.dumps(writes),
             json.dumps(cases, ensure_ascii=False), authored_by, store.now()),
        )


def author_suite(requirement: str, graph: dict) -> list[dict]:
    """Write acceptance tests from the requirement. Cached per requirement and interface."""
    from backend.lang_graph_agent import _extract_bounded_text, call_llm
    from backend.prompts.test_author_prompt import (
        PROMPT_TEST_AUTHOR, PROMPT_TEST_AUTHOR_USER,
    )
    from langchain_core.messages import HumanMessage

    reads, writes = interface_of(graph)
    if not writes:
        # Nothing to assert against. A graph that produces no named field cannot be
        # checked this way, and guessing would be worse than declining.
        return []

    sid = suite_id(requirement, reads, writes)
    cached = cached_suite(sid)
    if cached is not None:
        return cached

    user = PROMPT_TEST_AUTHOR_USER.format(
        requirement=requirement.strip(),
        inputs="\n".join(f"  - {f}" for f in reads) or "  (none declared)",
        outputs="\n".join(f"  - {f}" for f in writes),
    )
    raw = call_llm(PROMPT_TEST_AUTHOR, [HumanMessage(content=user)],
                   node="test_author", purpose="independent_suite")
    text = _extract_bounded_text(raw, "---TESTS STARTS---", "---TESTS ENDS---",
                                 strip_lang="json")
    try:
        cases = json.loads(text) if text else []
    except json.JSONDecodeError:
        logger.warning("The test author's reply was not valid JSON; no suite for this run.")
        cases = []

    cases = [c for c in cases if isinstance(c, dict) and c.get("expectedOutput")]
    if cases:
        from backend.lang_graph_agent import _active_model
        remember_suite(sid, requirement, reads, writes, cases, _active_model())
    return cases


def verify(graph: dict, requirement: str, *, node: str = "builder_node",
           attempt: int = 1) -> dict | None:
    """Run the independent suite and record the verdict. Returns its summary.

    Recorded as its own tool, `independent_tests`, rather than overwriting `run_tests`.
    Keeping both is what lets you measure how often the model's own exam and a fair one
    disagree - which is the number that says whether any of this was necessary.
    """
    from backend.tools.zen_evaluator import run_test_suite

    cases = author_suite(requirement, graph)
    if not cases:
        return None

    from backend.lang_graph_agent import _tool_run

    try:
        with _tool_run("independent_tests", node=node, attempt=attempt) as run:
            report = run_test_suite(json.dumps(graph), cases, trace=False)
            summary = report["summary"]
            run.output = summary
            run.diagnostics = [
                {"kind": "assertion", "code": "INDEPENDENT_FAIL",
                 "message": f"{r['name']}: expected {r.get('expected')}, got {r.get('actual')}"}
                for r in report["results"] if r["status"] in ("failed", "errored")
            ][:12]
            if summary["failed"] or summary["errored"] or not summary["passed"]:
                raise AssertionError(
                    f"{summary['passed']}/{summary['total']} independent cases passed"
                )
    except AssertionError:
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("Independent verification could not run: %s", exc)
        return None
    return summary
