"""What a test suite actually exercises, and what it leaves untouched.

The execution trace records which decision table row matched and which switch branches
fired on every run. Running a suite and collecting that across all of it says exactly which
rules have never been reached - which is a far more useful question than "do the tests
pass", because a suite can pass while never touching half the policy.

Everything here is deterministic: no model is involved in working out coverage, or in
deriving boundary cases from the thresholds a table already contains. That matters on a
rate-limited free tier, and it means the answers do not drift between runs.
"""

from __future__ import annotations

import re
from typing import Any

from backend.tools.zen_evaluator import run_test_suite

TABLE = "decisionTableNode"
SWITCH = "switchNode"


# --------------------------------------------------------------------------- coverage

def coverage(graph: dict, tests: list[dict]) -> dict:
    """Which rules and branches the suite reaches.

    Returns ``{"summary": {...}, "nodes": [...]}``. A node entry names the rows or branches
    that no case reached, so the gap can be closed rather than guessed at.
    """
    nodes = {n.get("id"): n for n in graph.get("nodes", []) if n.get("id")}
    hit_rows: dict[str, set[int]] = {nid: set() for nid in nodes}
    hit_branches: dict[str, set[str]] = {nid: set() for nid in nodes}

    if tests:
        report = run_test_suite(graph, tests, trace=True)
        for result in report["results"]:
            for node_id, step in (result.get("trace") or {}).items():
                data = step.get("traceData")
                if not isinstance(data, dict):
                    continue
                if data.get("index") is not None:
                    hit_rows.setdefault(node_id, set()).add(int(data["index"]))
                for statement in data.get("statements") or []:
                    if statement.get("id"):
                        hit_branches.setdefault(node_id, set()).add(statement["id"])

    entries: list[dict[str, Any]] = []
    total = covered = 0

    for node_id, node in nodes.items():
        content = node.get("content") or {}

        if node.get("type") == TABLE:
            rules = content.get("rules") or []
            if not rules:
                continue
            reached = hit_rows.get(node_id, set())
            missed = [i + 1 for i in range(len(rules)) if i not in reached]
            total += len(rules)
            covered += len(rules) - len(missed)
            entries.append({
                "nodeId": node_id, "nodeName": node.get("name"), "kind": "rules",
                "total": len(rules), "covered": len(rules) - len(missed), "uncovered": missed,
            })

        elif node.get("type") == SWITCH:
            statements = content.get("statements") or []
            if not statements:
                continue
            reached = hit_branches.get(node_id, set())
            missed = [
                (s.get("condition") or "default") for s in statements if s.get("id") not in reached
            ]
            total += len(statements)
            covered += len(statements) - len(missed)
            entries.append({
                "nodeId": node_id, "nodeName": node.get("name"), "kind": "branches",
                "total": len(statements), "covered": len(statements) - len(missed),
                "uncovered": missed,
            })

    return {
        "summary": {
            "total": total,
            "covered": covered,
            "uncovered": total - covered,
            "percent": round(100 * covered / total, 1) if total else 100.0,
            "cases": len(tests),
        },
        "nodes": entries,
    }


# --------------------------------------------------------------------------- boundaries

# The unary forms a decision table cell actually uses, and the numbers in them.
_COMPARISON_RE = re.compile(r'^\s*(>=|<=|>|<|==|!=)\s*(-?\d+(?:\.\d+)?)\s*$')
_RANGE_RE = re.compile(r'^\s*([\[(])\s*(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)\s*([\])])\s*$')
_STRING_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def thresholds(graph: dict) -> list[dict]:
    """Every numeric boundary and string value a table tests, with the field it tests.

    This is where good test cases come from. A model asked to invent them guesses at the
    numbers; the table already contains them exactly.
    """
    found: list[dict] = []
    for node in graph.get("nodes", []):
        if node.get("type") != TABLE:
            continue
        content = node.get("content") or {}
        columns = {c["id"]: c for c in content.get("inputs") or [] if c.get("id")}
        for r, rule in enumerate(content.get("rules") or []):
            for col_id, cell in rule.items():
                column = columns.get(col_id)
                if column is None or not isinstance(cell, str) or not cell.strip():
                    continue
                field = (column.get("field") or "").strip()
                if not field:
                    continue

                comparison = _COMPARISON_RE.match(cell)
                if comparison:
                    operator, number = comparison.group(1), float(comparison.group(2))
                    found.append({"field": field, "node": node.get("name"), "row": r + 1,
                                  "kind": "number", "values": _around(number),
                                  "satisfying": _satisfying(operator, number)})
                    continue

                span = _RANGE_RE.match(cell)
                if span:
                    low, high = float(span.group(2)), float(span.group(3))
                    inclusive_low = span.group(1) == "["
                    found.append({"field": field, "node": node.get("name"), "row": r + 1,
                                  "kind": "number", "values": _around(low) + _around(high),
                                  "satisfying": _clean(low if inclusive_low else low + _step(low))})
                    continue

                literals = [a or b for a, b in _STRING_RE.findall(cell)]
                if literals:
                    found.append({"field": field, "node": node.get("name"), "row": r + 1,
                                  "kind": "string", "values": literals,
                                  "satisfying": literals[0]})
    return found


def _step(value: float) -> float:
    return 1.0 if float(value).is_integer() and abs(value) >= 1 else 0.01


def _around(value: float) -> list[float]:
    """Just below, exactly on, and just above - where off-by-one bugs live."""
    step = _step(value)
    return [_clean(value - step), _clean(value), _clean(value + step)]


def _satisfying(operator: str, value: float) -> float:
    """A value that makes this condition true.

    Not the boundary: `< 0` is not satisfied by 0, and suggesting a case that lands on a
    different row than the one it claims to cover is worse than suggesting nothing.
    """
    step = _step(value)
    return _clean({
        ">=": value, "<=": value, "==": value,
        ">": value + step, "!=": value + step, "<": value - step,
    }[operator])


def _clean(value: float) -> float:
    rounded = round(value, 4)
    return int(rounded) if float(rounded).is_integer() else rounded


def suggest_cases(graph: dict, tests: list[dict]) -> list[dict]:
    """Inputs that would reach the rules the current suite never touches.

    Built from a covered case where possible, so the rest of the payload is realistic, and
    from the thresholds the uncovered rule itself tests.
    """
    report = coverage(graph, tests)
    uncovered = {
        (entry["nodeName"], row)
        for entry in report["nodes"] if entry["kind"] == "rules"
        for row in entry["uncovered"]
    }
    if not uncovered:
        return []

    template = dict(tests[0].get("input") or {}) if tests else {}
    all_thresholds = thresholds(graph)
    by_row = {(t["node"], t["row"]): t for t in all_thresholds}

    suggestions: list[dict] = []
    for node_name, row in sorted(uncovered):
        threshold = by_row.get((node_name, row))
        payload = dict(template)

        if threshold:
            payload[threshold["field"]] = threshold.get(
                "satisfying", threshold["values"][len(threshold["values"]) // 2])
            why = f"No case reaches row {row} of {node_name}. It tests {threshold['field']}."
        else:
            # A catch-all row: it has no condition of its own, so it is reached by falling
            # past every other row. Go beyond the widest numeric threshold in the same
            # table, rather than leaving a payload that would land on an earlier row.
            numeric = [t for t in all_thresholds
                       if t["node"] == node_name and t["kind"] == "number"]
            if numeric:
                field = numeric[-1]["field"]
                ceiling = max(v for t in numeric if t["field"] == field for v in t["values"])
                payload[field] = _clean(ceiling + max(1, abs(ceiling) * 0.1))
                why = (f"Row {row} of {node_name} is the catch-all - nothing falls through "
                       f"to it. This pushes {field} past every earlier rule.")
            else:
                why = (f"No case reaches row {row} of {node_name}, and it has no condition "
                       "to derive an input from - fill this in by hand.")

        suggestions.append({
            "name": f"{node_name} row {row} is never exercised",
            "node": node_name,
            "row": row,
            "input": payload,
            "why": why,
        })
    return suggestions
