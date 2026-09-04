"""Typed diagnostics for generated decision graphs.

Every failure used to reach the model as one undifferentiated string:

    SYSTEM ERROR:
    {str(exception)}
    Please fix the logic and output the corrected DSL and Test array.

A parse error, a dangling edge, a malformed expression and a wrong business answer all
arrived looking identical, with no pointer to the node at fault - so the model could not
tell "your syntax is wrong" from "your logic is wrong", and routinely rewrote a working
graph to chase a one-cell mistake.

This module turns each of those into a `Diagnostic` that names its kind, its node and the
change to make. Three engine capabilities do the work, none of which the codebase used:

* `ZenDecision.validate()` catches structural faults that `create_decision()` misses -
  `create_decision` only deserializes, so a dangling edge or a missing input node compiles
  cleanly and only explodes at evaluation time, once per test case.
* the evaluate-time errors are already JSON carrying a `nodeId`, rather than opaque text.
* the execution trace records what every node received and produced, and which decision
  table row matched - which is what lets a failing assertion be attributed to one node.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import zen

Kind = Literal["dsl_parse", "structure", "expression", "engine", "assertion", "lint"]

# `error` blocks a build; `warning` is probably a bug but the graph still runs; `hint` is a
# quality suggestion. Only errors gate the repair loop - a loop that refused to finish over
# a style hint would never converge.
Severity = Literal["error", "warning", "hint"]

# Repair order, most fundamental first. There is no point telling a model its business
# logic is wrong while the graph does not compile - it will "fix" the logic and the same
# structural error will come back.
KIND_ORDER: list[Kind] = ["dsl_parse", "structure", "expression", "engine", "assertion"]

KIND_HEADINGS: dict[str, str] = {
    "dsl_parse": "THE PLAN COULD NOT BE COMPILED",
    "structure": "THE GRAPH IS NOT WIRED CORRECTLY",
    "expression": "AN EXPRESSION IS NOT VALID ZEN",
    "engine": "THE ENGINE FAILED WHILE RUNNING THE GRAPH",
    "assertion": "THE GRAPH RUNS, BUT DECIDES THE WRONG THING",
}

# Probing a unary cell with several `$` values separates a syntax error, which raises for
# every value, from a type mismatch, which raises only for some. `validate_unary_expression`
# is not usable here: it rejects `>= 100`, `'US','CA'` and `> 10 and < 50`, all of which the
# evaluator accepts, so linting with it would flag most valid tables.
_UNARY_PROBES: tuple[Any, ...] = (1, "x", True, [1], None)


@dataclass
class Diagnostic:
    """One thing wrong with a graph, and what to do about it."""

    kind: Kind
    code: str
    message: str
    node_id: str | None = None
    node_name: str | None = None
    path: str | None = None
    line: int | None = None
    fix_hint: str = ""
    severity: Severity = "error"

    def as_dict(self) -> dict[str, Any]:
        """Wire form. `TestRunResponse.results` is untyped, so this needs no model change."""
        return {
            "kind": self.kind,
            "code": self.code,
            "message": self.message,
            "nodeId": self.node_id,
            "nodeName": self.node_name,
            "path": self.path,
            "line": self.line,
            "fix": self.fix_hint,
            "severity": self.severity,
        }

    def render(self) -> str:
        where = f' in node "{self.node_name}"' if self.node_name else ""
        if self.path:
            where += f" at {self.path}"
        if self.line:
            where += f" (line {self.line})"
        out = f"[{self.code}]{where}: {self.message}"
        if self.fix_hint:
            out += f"\n    Fix: {self.fix_hint}"
        return out


# --------------------------------------------------------------------------- helpers

def _nodes_by_id(graph: dict) -> dict[str, dict]:
    return {n.get("id"): n for n in graph.get("nodes", []) if n.get("id")}


def _name_of(graph: dict, node_id: str | None) -> str | None:
    if not node_id:
        return None
    node = _nodes_by_id(graph).get(node_id)
    return node.get("name") if node else None


def _as_json(text: str) -> dict | None:
    """Zen's evaluate-time errors are JSON documents wearing an exception's clothes."""
    text = (text or "").strip()
    if not text.startswith("{"):
        match = re.search(r'\{.*}', text, re.DOTALL)
        text = match.group(0) if match else ""
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- engine errors

_INVALID_GRAPH_HINTS = {
    "invalidInputCount": "The graph needs exactly one input node that every path starts from.",
    "missingNode": "An edge points at a node id that does not exist. Check the flowchart "
                   "names against the '## <name>' blocks.",
    "cyclicGraph": "The nodes form a loop. Data must flow one way, from input to output.",
    "depthLimitExceeded": "The graph nests too deeply - flatten it or split it up.",
}


def _structure_message(inner_type: str, inner: dict, fallback: str) -> str:
    """A sentence, rather than the engine's JSON blob echoed at the model."""
    if inner_type == "missingNode":
        return f'An edge points at node id "{inner.get("nodeId")}", which no node has.'
    if inner_type == "invalidInputCount":
        count = inner.get("nodeCount")
        return (f"The graph has {count} input node(s); it needs exactly one."
                if count is not None else "The graph does not have exactly one input node.")
    if inner_type == "cyclicGraph":
        return "The nodes form a cycle, so evaluation can never terminate."
    return fallback.strip()


def parse_engine_error(error: BaseException | str, graph: dict | None = None) -> Diagnostic:
    """Turn a zen exception into a diagnostic, keeping the node id it already carries."""
    text = str(error)
    payload = _as_json(text)
    graph = graph or {}

    if payload:
        outer = payload.get("type", "")
        source = payload.get("source")

        # {"type": "InvalidGraph", "source": {"type": "missingNode", "nodeId": "..."}}
        # `validate()` raises the inner object on its own, so handle both shapes.
        inner = source if isinstance(source, dict) else payload
        inner_type = inner.get("type", outer) if isinstance(inner, dict) else outer
        node_id = (inner.get("nodeId") if isinstance(inner, dict) else None) or payload.get("nodeId")

        if outer == "NodeError" or (inner_type == "NodeError"):
            detail = source if isinstance(source, str) else json.dumps(source)
            return Diagnostic(
                kind="engine",
                code="NodeError",
                message=str(detail),
                node_id=node_id,
                node_name=_name_of(graph, node_id),
                fix_hint="Rewrite this node's expression or cell. A field it reads is "
                         "probably absent - check the node upstream actually produces it, "
                         "and that passThrough is on if it needs to travel further.",
            )

        return Diagnostic(
            kind="structure",
            code=str(inner_type or outer or "InvalidGraph"),
            message=_structure_message(str(inner_type), inner if isinstance(inner, dict) else {}, text),
            node_id=node_id,
            node_name=_name_of(graph, node_id),
            fix_hint=_INVALID_GRAPH_HINTS.get(str(inner_type), "Correct the graph's nodes and edges."),
        )

    # `create_decision` deserialization failures are plain text, e.g.
    # "Invalid JSON\n\nCaused by:\n    nodes[0]: unknown variant `weirdNode`, expected ..."
    variant = re.search(r'unknown variant `([^`]+)`', text)
    if variant:
        return Diagnostic(
            kind="structure",
            code="UNKNOWN_NODE_TYPE",
            message=f'"{variant.group(1)}" is not a JDM node type.',
            fix_hint="Use one of: input, output, decisionTable, expression, switch, "
                     "function, decision.",
        )

    return Diagnostic(kind="engine", code="EngineError", message=text.strip())


# --------------------------------------------------------------------------- structure

def check_structure(graph: dict) -> list[Diagnostic]:
    """Compile and validate, so structural faults surface once instead of per test case."""
    found: list[Diagnostic] = []
    try:
        decision = zen.ZenEngine().create_decision(json.dumps(graph))
    except Exception as exc:  # noqa: BLE001 - the engine's own message is the diagnostic
        return [parse_engine_error(exc, graph)]

    try:
        # The check `create_decision` does not do: dangling edges, missing input node.
        decision.validate()
    except Exception as exc:  # noqa: BLE001
        found.append(parse_engine_error(exc, graph))

    return found


# --------------------------------------------------------------------------- expressions

def _unary_cell_error(cell: str) -> str | None:
    """The error a unary cell raises for *every* probe value, i.e. a real syntax error."""
    first: str | None = None
    for probe in _UNARY_PROBES:
        try:
            zen.evaluate_unary_expression(cell, {"$": probe})
            return None  # it parsed for at least one value, so the syntax is fine
        except Exception as exc:  # noqa: BLE001
            first = first or str(exc)
    return first


def _describe(error: str | dict | None) -> str:
    payload = _as_json(error) if isinstance(error, str) else error
    if isinstance(payload, dict):
        return f"{payload.get('type', 'error')}: {payload.get('source', '')}"
    return str(error)


def check_expressions(graph: dict) -> list[Diagnostic]:
    """Validate every expression and table cell before the graph is ever evaluated."""
    found: list[Diagnostic] = []

    for node in graph.get("nodes", []):
        name, node_id = node.get("name"), node.get("id")
        content = node.get("content") or {}

        if node.get("type") == "expressionNode":
            for i, expression in enumerate(content.get("expressions") or []):
                value = (expression.get("value") or "").strip()
                if not value:
                    found.append(Diagnostic(
                        kind="expression", code="EMPTY_EXPRESSION",
                        message=f'"{expression.get("key")}" has no value.',
                        node_id=node_id, node_name=name,
                        path=f"expressions[{i}].{expression.get('key')}",
                        fix_hint="Give it a ZEN expression, or remove the line.",
                    ))
                    continue
                error = zen.validate_expression(value)
                if error:
                    found.append(Diagnostic(
                        kind="expression", code="PARSE_ERROR",
                        message=f'"{value}" is not valid ZEN - {_describe(error)}',
                        node_id=node_id, node_name=name,
                        path=f"expressions[{i}].{expression.get('key')}",
                        fix_hint="ZEN uses ==, !=, and, or, not. There is no && or ||, and "
                                 "no === or !==; those are JavaScript.",
                    ))

        elif node.get("type") == "decisionTableNode":
            inputs = {c["id"]: c for c in content.get("inputs") or [] if c.get("id")}
            outputs = {c["id"]: c for c in content.get("outputs") or [] if c.get("id")}
            for r, rule in enumerate(content.get("rules") or []):
                for col_id, cell in rule.items():
                    if col_id == "_id" or not isinstance(cell, str) or not cell.strip():
                        continue
                    column = inputs.get(col_id) or outputs.get(col_id)
                    if column is None:
                        continue
                    is_input = col_id in inputs
                    # An input column with a field bound reads its cell as a unary test;
                    # a generic column (field "") and every output cell are full expressions.
                    unary = is_input and bool((column.get("field") or "").strip())
                    error = _unary_cell_error(cell) if unary else zen.validate_expression(cell)
                    if error:
                        found.append(Diagnostic(
                            kind="expression", code="PARSE_ERROR",
                            message=f'row {r + 1}, column "{column.get("name")}": '
                                    f'"{cell}" is not valid - {_describe(error)}',
                            node_id=node_id, node_name=name,
                            path=f"rules[{r}].{column.get('name')}",
                            fix_hint="An input cell is a unary test against its column's "
                                     "field (>= 100, 'US','CA', [1..10]); an output cell is "
                                     "a full expression. Quote string literals.",
                        ))

        elif node.get("type") == "switchNode":
            for s, statement in enumerate(content.get("statements") or []):
                condition = (statement.get("condition") or "").strip()
                if not condition or statement.get("isDefault"):
                    continue
                error = zen.validate_expression(condition)
                if error:
                    found.append(Diagnostic(
                        kind="expression", code="PARSE_ERROR",
                        message=f'branch {s + 1}: "{condition}" is not valid ZEN - {_describe(error)}',
                        node_id=node_id, node_name=name,
                        path=f"statements[{s}].condition",
                        fix_hint="Combine conditions with `or`, not `||`. A list of "
                                 "alternatives reads better as `field in ['a', 'b']`.",
                    ))

    return found


# --------------------------------------------------------------------------- assertions

def _walk(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a result into dotted paths, so a mismatch path can be matched against it."""
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            flat[path] = child
            flat.update(_walk(child, path))
    return flat


def attribute_failure(result: dict, graph: dict) -> list[Diagnostic]:
    """Blame a failing assertion on the node responsible for it, using the trace.

    The trace records every node's input and output plus, for a decision table, the index
    of the row that matched. That is enough to say either "node X produced the wrong value"
    or "nothing produced this field at all", which is a different bug with a different fix.
    """
    trace = result.get("trace") or {}
    steps = sorted(trace.values(), key=lambda t: t.get("order", 0))
    found: list[Diagnostic] = []

    for mismatch in result.get("mismatches") or []:
        path = mismatch.get("path", "")
        leaf = path.split(".")[-1]

        producer = None
        for step in steps:  # last writer wins
            if leaf in _walk(step.get("output") or {}):
                producer = step

        if producer is None:
            # The field never appeared. Two very different bugs: either no node declares it
            # at all, or one declares it and simply did not fire. Say which.
            owner = _declaring_node(graph, leaf)
            step = next((s for s in steps if s.get("id") == (owner or {}).get("id")), None)
            detail = _row_detail(step, graph) if step else ""
            last = steps[-2] if len(steps) > 1 else (steps[-1] if steps else None)

            if owner is None:
                node_id, node_name = (last or {}).get("id"), (last or {}).get("name")
                hint = (f'No node declares "{leaf}". Add it as an output column or an '
                        "expression key on the node that should decide it.")
            else:
                node_id, node_name = owner.get("id"), owner.get("name")
                hint = (f'"{leaf}" is declared on "{owner.get("name")}" but nothing filled '
                        "it in for this input. Either no rule matched, or the rule that "
                        "did left that output cell empty - an empty output cell drops the "
                        "key entirely.")

            found.append(Diagnostic(
                kind="assertion", code="FIELD_NEVER_PRODUCED",
                message=f'test "{result.get("name")}" expected '
                        f'{json.dumps(mismatch.get("expected"))} at "{path}", but nothing '
                        f'produced that field. ' + detail,
                node_id=node_id, node_name=node_name, path=path, fix_hint=hint,
            ))
            continue

        found.append(Diagnostic(
            kind="assertion", code="WRONG_VALUE",
            message=f'test "{result.get("name")}" expected '
                    f'{json.dumps(mismatch.get("expected"))} at "{path}" but got '
                    f'{json.dumps(mismatch.get("actual"))}. '
                    + _row_detail(producer, graph),
            node_id=producer.get("id"), node_name=producer.get("name"),
            path=path,
            fix_hint=f'"{leaf}" is set by "{producer.get("name")}". Change the rule there '
                     "that handles this input - do not change the test.",
        ))

    return found


def _declaring_node(graph: dict, leaf: str) -> dict | None:
    """The node that claims to produce `leaf`, whether or not it actually did."""
    for node in graph.get("nodes", []):
        content = node.get("content") or {}
        for column in content.get("outputs") or []:
            if (column.get("field") or "").split(".")[-1] == leaf:
                return node
        for expression in content.get("expressions") or []:
            if (expression.get("key") or "").split(".")[-1] == leaf:
                return node
    return None


def _row_detail(step: dict, graph: dict) -> str:
    """For a decision table, say which row matched - or that none did."""
    node = _nodes_by_id(graph).get(step.get("id"))
    if not node or node.get("type") != "decisionTableNode":
        return ""
    data = step.get("traceData")
    if isinstance(data, dict) and data.get("index") is not None:
        return (f'In "{step.get("name")}", row {data["index"] + 1} matched on '
                f'{json.dumps(data.get("reference_map"))}.')
    hit = (node.get("content") or {}).get("hitPolicy", "first")
    if hit == "first":
        return (f'In "{step.get("name")}", no row matched, so it produced nothing. '
                "Add a catch-all row with every input cell empty as the last row.")
    return ""


# --------------------------------------------------------------------------- reporting

def format_for_llm(diagnostics: list[Diagnostic]) -> str:
    """Render the most fundamental group of problems as a repair instruction.

    Only one kind is reported at a time, deliberately. Downstream failures are usually
    consequences of the upstream one, and a model shown five kinds at once tends to rewrite
    the whole graph instead of making the one change that matters.
    """
    if not diagnostics:
        return ""

    by_kind: dict[str, list[Diagnostic]] = {}
    for diagnostic in diagnostics:
        by_kind.setdefault(diagnostic.kind, []).append(diagnostic)

    kind = next(k for k in KIND_ORDER if k in by_kind)
    group = by_kind[kind]

    lines = [KIND_HEADINGS[kind], ""]
    lines += [d.render() for d in group]

    deferred = sum(len(v) for k, v in by_kind.items() if k != kind)
    if deferred:
        lines += ["", f"({deferred} further problem(s) will be reported once these are fixed - "
                      "they are most likely consequences of them.)"]

    lines += ["", "Fix only what is listed above, then output the complete corrected DSL and "
                  "test cases again."]
    return "\n".join(lines)


def diagnose(graph: dict, report: dict | None = None) -> list[Diagnostic]:
    """Every problem with a graph: structure, expressions, then failing assertions."""
    found = check_structure(graph)
    found += check_expressions(graph)

    for result in (report or {}).get("results", []):
        if result.get("status") == "errored" and result.get("error"):
            found.append(parse_engine_error(result["error"], graph))
        elif result.get("status") == "failed":
            found += attribute_failure(result, graph)

    return found
