"""Zen engine execution and test evaluation.

The important behaviour here is that a test only passes when the engine's actual
output matches the test's ``expectedOutput``. The previous implementation
returned success for any evaluation that did not raise, which let graphs that
produced none of their declared outputs be saved as working.
"""

from __future__ import annotations

import json
import math
import time
from decimal import Decimal
from typing import Any

import zen


def check_jdm_format(jdm_content: str, tests_content: str) -> tuple[bool, any]:
    """
    Validates the syntax and structure of the JDM graph and Test Suite.
    Returns:
    (True, (parsed_jdm, parsed_tests)) if successful.
    (False, error_message) if validation fails.
    """
    try:
        parsed_jdm = json.loads(jdm_content)
        parsed_tests = json.loads(tests_content)

        if "nodes" not in parsed_jdm or "edges" not in parsed_jdm:
            return False, "JDM missing 'nodes' or 'edges'."

        if not isinstance(parsed_tests, list):
            return False, "Test suite must be a JSON array."

        return True, (parsed_jdm, parsed_tests)

    except Exception as e:
        return False, str(e)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _as_json_value(value: Any) -> Any:
    """Normalise engine output into plain JSON-comparable Python values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _as_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json_value(v) for v in value]
    return value


def simulate(jdm_content: str | dict, context: Any, trace: bool = True) -> dict:
    """Run one payload through the graph.

    Returns ``{"performance", "result", "trace"}``. Raises on compile or
    evaluation failure so callers can distinguish an engine error from a
    mismatched result.
    """
    if isinstance(jdm_content, dict):
        jdm_content = json.dumps(jdm_content)

    decision = zen.ZenEngine().create_decision(jdm_content)
    response = decision.evaluate(context, {"trace": trace})
    return {
        "performance": response.get("performance"),
        "result": _as_json_value(response.get("result")),
        "trace": _as_json_value(response.get("trace")) or {},
    }


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

_MISSING = object()


def compare_output(expected: Any, actual: Any, subset: bool = True) -> list[dict]:
    """Compare expected against actual, returning a list of mismatches.

    ``subset`` (the default) requires every key present in ``expected`` to match
    but tolerates extra keys in ``actual``. That is the right default for Zen:
    graphs are pass-through by design, so the result carries the whole input
    alongside the outputs.
    """
    mismatches: list[dict] = []

    def walk(exp: Any, act: Any, path: str) -> None:
        if isinstance(exp, dict):
            if not isinstance(act, dict):
                mismatches.append({"path": path or "$", "expected": exp, "actual": act})
                return
            for key, exp_value in exp.items():
                child = f"{path}.{key}" if path else key
                act_value = act.get(key, _MISSING)
                if act_value is _MISSING:
                    mismatches.append({"path": child, "expected": exp_value, "actual": None})
                else:
                    walk(exp_value, act_value, child)
            if not subset:
                for key in act:
                    if key not in exp:
                        child = f"{path}.{key}" if path else key
                        mismatches.append({"path": child, "expected": None, "actual": act[key]})
            return

        if isinstance(exp, list):
            if not isinstance(act, list) or len(exp) != len(act):
                mismatches.append({"path": path or "$", "expected": exp, "actual": act})
                return
            for i, (e, a) in enumerate(zip(exp, act)):
                walk(e, a, f"{path}[{i}]")
            return

        if isinstance(exp, bool) or isinstance(act, bool):
            # bool is a subclass of int, so `True == 1`. Require both sides to be
            # booleans before comparing, otherwise 1 would satisfy an expected True.
            if isinstance(exp, bool) != isinstance(act, bool) or exp != act:
                mismatches.append({"path": path or "$", "expected": exp, "actual": act})
            return

        if isinstance(exp, (int, float)) and isinstance(act, (int, float)):
            if not math.isclose(float(exp), float(act), rel_tol=1e-9, abs_tol=1e-12):
                mismatches.append({"path": path or "$", "expected": exp, "actual": act})
            return

        if exp != act:
            mismatches.append({"path": path or "$", "expected": exp, "actual": act})

    walk(expected, _as_json_value(actual), "")
    return mismatches


# --------------------------------------------------------------------------
# Test suites
# --------------------------------------------------------------------------

def run_test_suite(
    jdm_content: str | dict,
    tests: list[dict],
    trace: bool = True,
    subset: bool = True,
) -> dict:
    """Run a whole suite and return ``{"summary", "results"}``.

    A test with no ``expectedOutput`` is reported as ``skipped`` rather than
    failed - it still exercises the engine, but there is nothing to assert.
    """
    if isinstance(jdm_content, dict):
        jdm_content = json.dumps(jdm_content)

    started = time.perf_counter()
    results: list[dict] = []

    try:
        decision = zen.ZenEngine().create_decision(jdm_content)
    except Exception as exc:  # noqa: BLE001 - the graph itself does not compile
        return {
            "summary": {
                "total": len(tests),
                "passed": 0,
                "failed": 0,
                "errored": len(tests),
                "skipped": 0,
                "duration_ms": 0,
                "compile_error": str(exc),
            },
            "results": [
                {
                    "test_id": t.get("id"),
                    "name": t.get("name") or f"Test {i + 1}",
                    "status": "errored",
                    "input": t.get("input", t),
                    "expected": t.get("expectedOutput"),
                    "actual": None,
                    "mismatches": [],
                    "performance": None,
                    "trace": {},
                    "error": f"Graph failed to compile: {exc}",
                }
                for i, t in enumerate(tests)
            ],
        }

    for i, test_case in enumerate(tests):
        payload = test_case.get("input", test_case) if isinstance(test_case, dict) else test_case
        expected = test_case.get("expectedOutput") if isinstance(test_case, dict) else None
        entry: dict[str, Any] = {
            "test_id": test_case.get("id") if isinstance(test_case, dict) else None,
            "name": (test_case.get("name") if isinstance(test_case, dict) else None)
            or f"Test {i + 1}",
            "input": payload,
            "expected": expected,
            "actual": None,
            "mismatches": [],
            "performance": None,
            "trace": {},
            "error": None,
        }

        try:
            response = decision.evaluate(payload, {"trace": trace})
        except Exception as exc:  # noqa: BLE001 - one bad test must not stop the suite
            entry["status"] = "errored"
            entry["error"] = str(exc)
            results.append(entry)
            continue

        entry["actual"] = _as_json_value(response.get("result"))
        entry["performance"] = response.get("performance")
        entry["trace"] = _as_json_value(response.get("trace")) or {}

        if expected in (None, {}):
            entry["status"] = "skipped"
        else:
            mismatches = compare_output(expected, entry["actual"], subset=subset)
            entry["mismatches"] = mismatches
            entry["status"] = "passed" if not mismatches else "failed"

        results.append(entry)

    counts = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    for r in results:
        counts[r["status"]] += 1

    return {
        "summary": {
            "total": len(results),
            **counts,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        },
        "results": results,
    }


# --------------------------------------------------------------------------
# Agent-facing feedback
# --------------------------------------------------------------------------

def _format_failures_for_llm(report: dict) -> str:
    """Turn a failing report into corrective feedback for the builder loop."""
    summary = report["summary"]
    lines = [
        f"TEST RESULTS: {summary['passed']}/{summary['total']} passed, "
        f"{summary['failed']} failed, {summary['errored']} errored.",
        "",
    ]

    if summary.get("compile_error"):
        lines.append(f"The graph does not compile: {summary['compile_error']}")
        return "\n".join(lines)

    for r in report["results"]:
        if r["status"] == "passed" or r["status"] == "skipped":
            continue
        lines.append(f"[{r['status'].upper()}] {r['name']}")
        lines.append(f"  Input:    {json.dumps(r['input'])}")
        if r["error"]:
            lines.append(f"  Error:    {r['error']}")
        else:
            lines.append(f"  Expected: {json.dumps(r['expected'])}")
            lines.append(f"  Actual:   {json.dumps(r['actual'])}")
            for m in r["mismatches"][:8]:
                lines.append(
                    f"    - '{m['path']}': expected {json.dumps(m['expected'])}, "
                    f"got {json.dumps(m['actual'])}"
                )
        lines.append("")

    lines.append(
        "A field that came back missing usually means the responsible node never "
        "produced it: check that the decision table's output columns are populated "
        "for the matching rule, and that the output node is wired to receive them."
    )
    return "\n".join(lines)


def _format_success(report: dict) -> str:
    lines = [
        f"All {report['summary']['passed']} assertions passed "
        f"({report['summary']['skipped']} skipped)."
    ]
    for r in report["results"]:
        lines.append(f"{r['name']}: {json.dumps(r['actual'])}")
    return "\n".join(lines)


def evaluate_against_zen(jdm_content: str, parsed_tests: list) -> tuple[bool, str]:
    """Compile the graph and run it against the test suite.

    Signature is unchanged so ``builder_node`` keeps working as written: it
    raises with this message and feeds it back to the LLM. The difference is
    that failing assertions now count as failure, so the self-healing loop
    repairs wrong logic rather than only crashes.
    """
    try:
        report = run_test_suite(jdm_content, parsed_tests)
    except Exception as e:  # noqa: BLE001
        return False, str(e)

    summary = report["summary"]
    if summary["failed"] or summary["errored"]:
        return False, _format_failures_for_llm(report)
    return True, _format_success(report)
