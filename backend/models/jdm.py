"""JDM decision-model validation.

Pydantic covers structure; ``zen.ZenEngine().create_decision()`` is the real
compiler and is used as the final gate, since it catches everything a schema
cannot (bad expressions, unsupported node config, and so on).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

DECISION_CONTENT_TYPE = "application/vnd.gorules.decision"

NodeType = Literal[
    "inputNode",
    "outputNode",
    "decisionTableNode",
    "functionNode",
    "expressionNode",
    "switchNode",
    "decisionNode",
    "customNode",
]


class Position(BaseModel):
    x: float = 0
    y: float = 0


class DecisionNode(BaseModel):
    id: str
    name: str = ""
    type: NodeType
    position: Position = Field(default_factory=Position)
    content: dict[str, Any] | None = None


class DecisionEdge(BaseModel):
    id: str
    sourceId: str
    targetId: str
    sourceHandle: str | None = None
    targetHandle: str | None = None
    type: str = "edge"


class DecisionModel(BaseModel):
    contentType: str = DECISION_CONTENT_TYPE
    nodes: list[DecisionNode] = Field(default_factory=list)
    edges: list[DecisionEdge] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    path: str
    message: str
    severity: Literal["error", "warning"] = "error"


def _semantic_issues(model: DecisionModel) -> list[ValidationIssue]:
    """Structural errors plus advisory warnings.

    Only ``error`` issues block a save. A half-built graph - a fresh canvas with
    nothing wired up yet, or a node dropped but not yet connected - is a normal
    intermediate state and must remain saveable.
    """
    issues: list[ValidationIssue] = []

    seen: set[str] = set()
    for i, node in enumerate(model.nodes):
        if node.id in seen:
            issues.append(ValidationIssue(path=f"nodes[{i}].id", message=f"Duplicate node id '{node.id}'."))
        seen.add(node.id)

    for i, edge in enumerate(model.edges):
        if edge.sourceId not in seen:
            issues.append(
                ValidationIssue(path=f"edges[{i}].sourceId", message=f"Edge source '{edge.sourceId}' is not a known node.")
            )
        if edge.targetId not in seen:
            issues.append(
                ValidationIssue(path=f"edges[{i}].targetId", message=f"Edge target '{edge.targetId}' is not a known node.")
            )

    types = {n.type for n in model.nodes}
    if model.nodes and "inputNode" not in types:
        issues.append(ValidationIssue(path="nodes", message="Graph has no input node.", severity="warning"))
    if model.nodes and "outputNode" not in types:
        issues.append(ValidationIssue(path="nodes", message="Graph has no output node.", severity="warning"))

    connected = {e.sourceId for e in model.edges} | {e.targetId for e in model.edges}
    for i, node in enumerate(model.nodes):
        if node.id not in connected and len(model.nodes) > 1:
            issues.append(
                ValidationIssue(
                    path=f"nodes[{i}]",
                    message=f"Node '{node.name or node.id}' is not connected to anything.",
                    severity="warning",
                )
            )

    return issues


def validate_decision(content: Any, compile_check: bool = True) -> list[ValidationIssue]:
    """Validate a decision model, returning errors and advisory warnings."""
    try:
        model = DecisionModel.model_validate(content)
    except ValidationError as exc:
        return [
            ValidationIssue(path=".".join(str(p) for p in err["loc"]), message=err["msg"])
            for err in exc.errors()
        ]

    issues = _semantic_issues(model)
    if not compile_check or any(i.severity == "error" for i in issues):
        return issues

    # Final gate: the real Zen compiler, which catches what a schema cannot.
    try:
        import json

        import zen

        zen.ZenEngine().create_decision(json.dumps(content))
    except Exception as exc:  # noqa: BLE001 - surfacing the engine's own message
        issues.append(ValidationIssue(path="$", message=f"Zen could not compile this graph: {exc}"))

    return issues


def blocking_errors(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == "error"]
