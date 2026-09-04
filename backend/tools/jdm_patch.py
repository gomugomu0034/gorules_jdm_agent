"""Targeted edits to an existing decision graph.

Modifying a policy used to mean regenerating it. The planner was told to "not change
existing node IDs" and to "preserve all existing parameters that are not explicitly being
modified" - instructions it could not possibly follow, because the DSL has no syntax for an
id and `parse_markdown_dsl` mints a fresh `uuid4()` for every node and edge on every parse.
So every edit produced a brand new graph in a brand new id space: the canvas jumped, the
diff was total, and any logic the model happened not to re-emit was quietly lost.

A patch says what to change and leaves everything else identical, byte for byte. That makes
a one-cell edit a one-cell diff, keeps node ids stable so `$nodes` references and saved test
suites keep working, and gives the reviewer something they can actually read.

Nodes are addressed by name or id - the model reasons in names, and names are what the
conversation is about. Every failure names the operation and what was actually available,
because that text goes straight back into the repair loop.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

TABLE = "decisionTableNode"


class PatchError(ValueError):
    """One or more operations could not be applied."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            "The edit could not be applied:\n\n" + "\n".join(f"  - {p}" for p in problems)
        )


# --------------------------------------------------------------------------- lookup

def _find_node(graph: dict, ref: str) -> dict | None:
    """By id first, then by exact name, then case-insensitively."""
    if not ref:
        return None
    for node in graph["nodes"]:
        if node.get("id") == ref:
            return node
    for node in graph["nodes"]:
        if node.get("name") == ref:
            return node
    lowered = ref.lower()
    for node in graph["nodes"]:
        if (node.get("name") or "").lower() == lowered:
            return node
    return None


def _node_names(graph: dict) -> str:
    return ", ".join(sorted(f'"{n.get("name")}"' for n in graph["nodes"] if n.get("name")))


def _column(node: dict, ref: str) -> dict | None:
    content = node.get("content") or {}
    for column in (content.get("inputs") or []) + (content.get("outputs") or []):
        if column.get("id") == ref or column.get("name") == ref or column.get("field") == ref:
            return column
    lowered = (ref or "").lower()
    for column in (content.get("inputs") or []) + (content.get("outputs") or []):
        if (column.get("name") or "").lower() == lowered:
            return column
    return None


def _column_names(node: dict) -> str:
    content = node.get("content") or {}
    return ", ".join(
        f'"{c.get("name")}"'
        for c in (content.get("inputs") or []) + (content.get("outputs") or [])
    )


def _rule(node: dict, row: Any) -> dict | None:
    """Rows are addressed the way they read on screen: 1-based."""
    rules = (node.get("content") or {}).get("rules") or []
    try:
        index = int(row) - 1
    except (TypeError, ValueError):
        return None
    return rules[index] if 0 <= index < len(rules) else None


# --------------------------------------------------------------------------- operations

def _op_set_cell(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'set_cell: no node called "{op.get("node")}". The graph has: {_node_names(graph)}.'
    if node.get("type") != TABLE:
        return f'set_cell: "{node.get("name")}" is a {node.get("type")}, not a decision table.'
    rule = _rule(node, op.get("row"))
    if rule is None:
        total = len((node["content"].get("rules") or []))
        return (f'set_cell: "{node.get("name")}" has no row {op.get("row")}; '
                f"it has {total} row(s), numbered from 1.")
    column = _column(node, op.get("column", ""))
    if column is None:
        return (f'set_cell: "{node.get("name")}" has no column "{op.get("column")}". '
                f"Its columns are: {_column_names(node)}.")
    rule[column["id"]] = str(op.get("value", ""))
    return None


def _op_add_rule(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'add_rule: no node called "{op.get("node")}". The graph has: {_node_names(graph)}.'
    if node.get("type") != TABLE:
        return f'add_rule: "{node.get("name")}" is a {node.get("type")}, not a decision table.'

    rules = node["content"].setdefault("rules", [])
    new_rule: dict[str, Any] = {"_id": str(uuid.uuid4())}
    for column in (node["content"].get("inputs") or []) + (node["content"].get("outputs") or []):
        new_rule[column["id"]] = ""

    for name, value in (op.get("cells") or {}).items():
        column = _column(node, name)
        if column is None:
            return (f'add_rule: "{node.get("name")}" has no column "{name}". '
                    f"Its columns are: {_column_names(node)}.")
        new_rule[column["id"]] = str(value)

    after = op.get("after")
    if after is None:
        rules.append(new_rule)
    else:
        try:
            rules.insert(int(after), new_rule)  # after row N == index N
        except (TypeError, ValueError):
            return f'add_rule: "after" must be a row number, got {after!r}.'
    return None


def _op_remove_rule(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'remove_rule: no node called "{op.get("node")}".'
    rule = _rule(node, op.get("row"))
    if rule is None:
        return f'remove_rule: "{node.get("name")}" has no row {op.get("row")}.'
    node["content"]["rules"].remove(rule)
    return None


def _op_add_column(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'add_column: no node called "{op.get("node")}".'
    if node.get("type") != TABLE:
        return f'add_column: "{node.get("name")}" is not a decision table.'
    kind = op.get("kind", "output")
    if kind not in ("input", "output"):
        return f'add_column: "kind" must be "input" or "output", got {kind!r}.'

    column = {
        "id": str(uuid.uuid4()),
        "name": op.get("name") or op.get("field") or "New column",
        "field": op.get("field") or "",
    }
    node["content"].setdefault(kind + "s", []).append(column)
    # Existing rules gain the column as a wildcard, so they keep matching as they did.
    for rule in node["content"].get("rules") or []:
        rule.setdefault(column["id"], "")
    return None


def _op_set_expression(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'set_expression: no node called "{op.get("node")}". The graph has: {_node_names(graph)}.'
    if node.get("type") != "expressionNode":
        return f'set_expression: "{node.get("name")}" is a {node.get("type")}, not an expression node.'

    expressions = node["content"].setdefault("expressions", [])
    key = op.get("key")
    if not key:
        return 'set_expression: "key" is required.'
    for expression in expressions:
        if expression.get("key") == key:
            expression["value"] = str(op.get("value", ""))
            return None
    expressions.append({"id": str(uuid.uuid4()), "key": key, "value": str(op.get("value", ""))})
    return None


def _op_remove_expression(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'remove_expression: no node called "{op.get("node")}".'
    expressions = (node.get("content") or {}).get("expressions") or []
    for expression in expressions:
        if expression.get("key") == op.get("key"):
            expressions.remove(expression)
            return None
    return f'remove_expression: "{node.get("name")}" has no expression called "{op.get("key")}".'


def _op_set_property(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'set_property: no node called "{op.get("node")}".'
    name = op.get("property")
    allowed = {"hitPolicy", "passThrough", "executionMode", "inputField", "outputPath", "key"}
    if name not in allowed:
        return f'set_property: "{name}" is not settable. Choose one of: {", ".join(sorted(allowed))}.'
    node.setdefault("content", {})[name] = op.get("value")
    return None


def _op_rename(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'rename: no node called "{op.get("node")}".'
    new_name = (op.get("name") or "").strip()
    if not new_name:
        return 'rename: "name" is required.'
    old_name = node.get("name")
    node["name"] = new_name
    # Node names are the addressing scheme for $nodes, so a rename has to follow through
    # or every reference to it silently starts resolving to null.
    for other in graph["nodes"]:
        content = other.get("content") or {}
        for expression in content.get("expressions") or []:
            expression["value"] = (expression.get("value") or "").replace(
                f"$nodes.{old_name}", f"$nodes.{new_name}")
        for statement in content.get("statements") or []:
            statement["condition"] = (statement.get("condition") or "").replace(
                f"$nodes.{old_name}", f"$nodes.{new_name}")
        for rule in content.get("rules") or []:
            for key, value in rule.items():
                if key != "_id" and isinstance(value, str):
                    rule[key] = value.replace(f"$nodes.{old_name}", f"$nodes.{new_name}")
    return None


def _op_add_node(graph: dict, op: dict) -> str | None:
    name = (op.get("name") or "").strip()
    if not name:
        return 'add_node: "name" is required.'
    if _find_node(graph, name) is not None:
        return f'add_node: a node called "{name}" already exists.'
    node_type = op.get("type")
    if not node_type or not node_type.endswith("Node"):
        return (f'add_node: "type" must be a JDM node type such as decisionTableNode or '
                f"expressionNode, got {node_type!r}.")

    xs = [n.get("position", {}).get("x", 0) for n in graph["nodes"]] or [0]
    graph["nodes"].append({
        "id": str(uuid.uuid4()),
        "name": name,
        "type": node_type,
        "position": {"x": max(xs) + 300, "y": 200},
        "content": op.get("content") or {},
    })
    return None


def _op_remove_node(graph: dict, op: dict) -> str | None:
    node = _find_node(graph, op.get("node", ""))
    if node is None:
        return f'remove_node: no node called "{op.get("node")}".'
    graph["nodes"].remove(node)
    graph["edges"] = [
        e for e in graph["edges"]
        if e.get("sourceId") != node["id"] and e.get("targetId") != node["id"]
    ]
    return None


def _op_connect(graph: dict, op: dict) -> str | None:
    source = _find_node(graph, op.get("from", ""))
    target = _find_node(graph, op.get("to", ""))
    if source is None:
        return f'connect: no node called "{op.get("from")}". The graph has: {_node_names(graph)}.'
    if target is None:
        return f'connect: no node called "{op.get("to")}". The graph has: {_node_names(graph)}.'
    for edge in graph["edges"]:
        if edge.get("sourceId") == source["id"] and edge.get("targetId") == target["id"]:
            return None  # already connected; nothing to do
    edge = {"id": str(uuid.uuid4()), "sourceId": source["id"],
            "targetId": target["id"], "type": "edge"}
    if op.get("handle"):
        edge["sourceHandle"] = op["handle"]
    graph["edges"].append(edge)
    return None


def _op_disconnect(graph: dict, op: dict) -> str | None:
    source = _find_node(graph, op.get("from", ""))
    target = _find_node(graph, op.get("to", ""))
    if source is None or target is None:
        return f'disconnect: "{op.get("from")}" or "{op.get("to")}" is not a node in this graph.'
    before = len(graph["edges"])
    graph["edges"] = [
        e for e in graph["edges"]
        if not (e.get("sourceId") == source["id"] and e.get("targetId") == target["id"])
    ]
    if len(graph["edges"]) == before:
        return f'disconnect: "{source.get("name")}" is not connected to "{target.get("name")}".'
    return None


OPERATIONS = {
    "set_cell": _op_set_cell,
    "add_rule": _op_add_rule,
    "remove_rule": _op_remove_rule,
    "add_column": _op_add_column,
    "set_expression": _op_set_expression,
    "remove_expression": _op_remove_expression,
    "set_property": _op_set_property,
    "rename": _op_rename,
    "add_node": _op_add_node,
    "remove_node": _op_remove_node,
    "connect": _op_connect,
    "disconnect": _op_disconnect,
}


def apply_patch(graph: dict, operations: list[dict]) -> dict:
    """Apply operations to a copy of `graph`, or raise `PatchError`.

    All or nothing: a patch that fails part way leaves the original untouched, so a bad
    edit can never half-apply and strand the policy in a state nobody asked for.
    """
    if not isinstance(operations, list) or not operations:
        raise PatchError(["No edit operations were produced."])

    working = copy.deepcopy(graph)
    working.setdefault("nodes", [])
    working.setdefault("edges", [])

    problems: list[str] = []
    for i, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            problems.append(f"Operation {i} is not an object.")
            continue
        name = operation.get("op")
        handler = OPERATIONS.get(name)
        if handler is None:
            problems.append(
                f'Operation {i}: "{name}" is not a known edit. '
                f"Available: {', '.join(sorted(OPERATIONS))}."
            )
            continue
        problem = handler(working, operation)
        if problem:
            problems.append(f"Operation {i}: {problem}")

    if problems:
        raise PatchError(problems)
    return working


def describe(operations: list[dict]) -> list[str]:
    """One readable line per operation, for the action log shown to the user."""
    lines = []
    for operation in operations:
        op = operation.get("op")
        if op == "set_cell":
            lines.append(f'Set {operation.get("column")} on row {operation.get("row")} of '
                         f'{operation.get("node")} to {operation.get("value")}')
        elif op == "add_rule":
            lines.append(f'Added a rule to {operation.get("node")}')
        elif op == "remove_rule":
            lines.append(f'Removed row {operation.get("row")} from {operation.get("node")}')
        elif op == "set_expression":
            lines.append(f'Set {operation.get("key")} in {operation.get("node")} to '
                         f'{operation.get("value")}')
        elif op == "rename":
            lines.append(f'Renamed {operation.get("node")} to {operation.get("name")}')
        elif op == "add_node":
            lines.append(f'Added {operation.get("type")} "{operation.get("name")}"')
        elif op == "remove_node":
            lines.append(f'Removed {operation.get("node")}')
        elif op == "connect":
            lines.append(f'Connected {operation.get("from")} to {operation.get("to")}')
        elif op == "disconnect":
            lines.append(f'Disconnected {operation.get("from")} from {operation.get("to")}')
        else:
            lines.append(f'{op} on {operation.get("node", "the graph")}')
    return lines
