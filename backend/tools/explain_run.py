"""Why one input produced one result.

"The tests fail" and "the answer is wrong" are not the same question as "what did the policy
actually do". The trace already records what every node received, what it produced, and
which decision table row matched - this reads it back as a sequence a person can follow.

Deterministic, like coverage: the model is not asked to narrate an execution it cannot see.
That keeps the account honest, and it costs no LLM call.
"""

from __future__ import annotations

import json
from typing import Any

from backend.tools.zen_evaluator import simulate


def explain_run(graph: dict, payload: Any) -> dict:
    """Evaluate `payload` and describe how the result was reached.

    Returns ``{"result", "steps"}`` where each step names a node and says what it did in
    business terms - which row matched and on what, which branch was taken, what was
    computed - rather than dumping the raw trace at the reader.
    """
    outcome = simulate(graph, payload, trace=True)
    nodes = {n.get("id"): n for n in graph.get("nodes", []) if n.get("id")}

    steps = []
    for entry in sorted((outcome.get("trace") or {}).values(), key=lambda t: t.get("order", 0)):
        node = nodes.get(entry.get("id")) or {}
        steps.append({
            "order": entry.get("order"),
            "node": entry.get("name"),
            "nodeId": entry.get("id"),
            "type": node.get("type"),
            "summary": _summarise(node, entry),
            "output": entry.get("output"),
        })

    return {"result": outcome.get("result"), "steps": steps}


def _summarise(node: dict, entry: dict) -> str:
    node_type = node.get("type")
    content = node.get("content") or {}
    data = entry.get("traceData") if isinstance(entry.get("traceData"), dict) else {}
    # Nodes pass data through by default, so a node's raw output repeats everything
    # upstream produced. What matters is what *this* node added or changed.
    output = _added(entry.get("input"), entry.get("output"))

    if node_type == "inputNode":
        return f"The request arrived with {_fields(entry.get('output'))}."

    if node_type == "outputNode":
        return f"Returned the final result: {_fields(entry.get('input'))}."

    if node_type == "decisionTableNode":
        index = data.get("index")
        if index is None:
            hit = content.get("hitPolicy", "first")
            if hit == "first":
                return ("No row matched, so this table produced nothing. Anything downstream "
                        "that expected its outputs will be missing them.")
            return "No row matched."
        rule = content.get("rules") or []
        conditions = []
        if index < len(rule):
            for column in content.get("inputs") or []:
                cell = (rule[index].get(column["id"]) or "").strip()
                if cell:
                    conditions.append(f'{column.get("name")} {cell}')
        because = f" because {' and '.join(conditions)}" if conditions else " (the catch-all row)"
        produced = _fields(output) or "nothing"
        return f"Row {index + 1} matched{because}, producing {produced}."

    if node_type == "switchNode":
        fired = {s.get("id") for s in data.get("statements") or []}
        for i, statement in enumerate(content.get("statements") or [], start=1):
            if statement.get("id") in fired:
                condition = statement.get("condition") or "the default branch"
                return f"Took branch {i}: {condition}."
        return "No branch matched, so nothing downstream ran."

    if node_type == "expressionNode":
        computed = [f'{k} = {json.dumps(v)}' for k, v in output.items()]
        return "Computed " + (", ".join(computed) if computed else "nothing") + "."

    if node_type == "functionNode":
        return f"Ran its function and returned {_fields(output) or 'nothing'}."

    if node_type == "decisionNode":
        return f'Called the policy "{content.get("key")}" and returned {_fields(output)}.'

    return f"Produced {_fields(output) or 'nothing'}."


def _added(before: Any, after: Any) -> dict:
    """The fields a node introduced or changed, ignoring what it merely passed through."""
    if not isinstance(after, dict):
        return {}
    if not isinstance(before, dict):
        return dict(after)
    return {k: v for k, v in after.items() if k not in before or before[k] != v}


def _fields(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    parts = [f"{k}={json.dumps(v)}" for k, v in list(value.items())[:6]]
    if len(value) > 6:
        parts.append(f"...and {len(value) - 6} more")
    return ", ".join(parts)


def as_markdown(explanation: dict) -> str:
    """The same walk-through, for the chat."""
    lines = ["Here is what the policy did with that input:", ""]
    for step in explanation["steps"]:
        lines.append(f"{step['order'] + 1}. **{step['node']}** - {step['summary']}")
    lines += ["", f"Result: `{json.dumps(explanation['result'])}`"]
    return "\n".join(lines)
