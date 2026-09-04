"""Static analysis for JDM decision graphs.

Rule codes follow GoRules' own static analysis reference, so the vocabulary matches the
product these graphs are eventually authored in:
https://docs.gorules.io/brms/quality/static-analysis

Three severities. `error` means the graph will misbehave or fail to evaluate, and blocks a
build. `warning` means it runs but is probably not what was meant. `hint` is a quality
suggestion - most of the "a graph should be readable, and not one giant node" rules live
here, and they never block anything.

A linter that fires on a correct graph is worse than no linter: in the build loop it sends
the model off to "fix" working logic, and in the editor it teaches people to ignore the
panel. So every rule here is deliberately conservative, and anything needing type inference
to decide - nullability, input shadowing, whether a field is genuinely undefined - is left
out rather than guessed at. The same reasoning is why decision table cells are validated by
probing the evaluator rather than with `zen.validate_unary_expression`, which rejects
`>= 100` and `'US','CA'`; see `diagnostics.check_expressions`.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from backend.tools.diagnostics import Diagnostic, check_expressions, check_structure

# A first-hit table beyond this is hard to reason about and usually wants splitting.
# GoRules' own authoring guidance says 20-30.
MAX_TABLE_ROWS = 25

# One logic node carrying at least this much is the monolith pattern.
MONOLITH_RULES = 5
MONOLITH_OUTPUTS = 4

LOGIC_TYPES = {"decisionTableNode", "expressionNode", "switchNode", "functionNode", "decisionNode"}

_GENERIC_NAME_RE = re.compile(
    r'^(node|table|expr|expression|switch|function|decision|rule|step|output|input|'
    r'untitled|new\s?node|dt|fn)[\s_-]*\d*$',
    re.IGNORECASE,
)

# A bare word in a cell: a variable reference, almost always a forgotten quote.
_BARE_WORD_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_CELL_KEYWORDS = {"true", "false", "null"}

_NODES_REF_RE = re.compile(r'\$nodes\.([A-Za-z_][A-Za-z0-9_]*)|\$nodes\[[\'"]([^\'"]+)[\'"]]')


def _content(node: dict) -> dict:
    return node.get("content") or {}


def _label(node: dict) -> str:
    return node.get("name") or node.get("id") or "?"


# --------------------------------------------------------------------------- errors

def _structural_errors(graph: dict) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    nodes = graph.get("nodes", [])

    seen: dict[str, int] = Counter(n.get("id") for n in nodes if n.get("id"))
    for node_id, count in seen.items():
        if count > 1:
            found.append(Diagnostic(
                kind="lint", severity="error", code="DUPLICATE_NODE_ID",
                message=f'Node id "{node_id}" is used by {count} nodes.',
                node_id=node_id,
                fix_hint="Every node needs its own id; edges address nodes by it.",
            ))

    if not any(n.get("type") == "inputNode" for n in nodes):
        found.append(Diagnostic(
            kind="lint", severity="error", code="MISSING_INPUT_NODE",
            message="The graph has no input node, so nothing can enter it.",
            fix_hint="Add one input node and connect it to the first step.",
        ))

    for node in nodes:
        content, node_type = _content(node), node.get("type")
        empty = (
            (node_type == "decisionTableNode" and not content.get("rules"))
            or (node_type == "expressionNode" and not content.get("expressions"))
            or (node_type == "switchNode" and not content.get("statements"))
            or (node_type == "functionNode" and not (content.get("source") or "").strip())
        )
        if empty:
            found.append(Diagnostic(
                kind="lint", severity="error", code="EMPTY_BLOCK",
                message=f'"{_label(node)}" has no content, so it decides nothing.',
                node_id=node.get("id"), node_name=node.get("name"),
                fix_hint="Give it rules, expressions or branches - or remove the node.",
            ))

    found += _unreachable(graph)
    return found


def _unreachable(graph: dict) -> list[Diagnostic]:
    """Nodes no path from an input can reach.

    A real traversal, not the degree-zero orphan check the save-path validator does: a
    connected pair of nodes hanging off the side of the graph has edges, and is still dead.
    """
    nodes = {n.get("id"): n for n in graph.get("nodes", []) if n.get("id")}
    if not nodes:
        return []

    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("sourceId") and edge.get("targetId"):
            outgoing[edge["sourceId"]].append(edge["targetId"])

    reached: set[str] = set()
    queue = [nid for nid, n in nodes.items() if n.get("type") == "inputNode"]
    reached.update(queue)
    while queue:
        for target in outgoing.get(queue.pop(), []):
            if target in nodes and target not in reached:
                reached.add(target)
                queue.append(target)

    if not any(n.get("type") == "inputNode" for n in nodes.values()):
        return []  # MISSING_INPUT_NODE already covers this; everything would be unreachable

    return [
        Diagnostic(
            kind="lint", severity="error", code="UNREACHABLE_NODE",
            message=f'"{_label(node)}" cannot be reached from the input node, '
                    "so it never runs.",
            node_id=node_id, node_name=node.get("name"),
            fix_hint="Connect it to the flow, or delete it.",
        )
        for node_id, node in nodes.items() if node_id not in reached
    ]


# --------------------------------------------------------------------------- warnings

def _warnings(graph: dict) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    nodes = graph.get("nodes", [])
    known_names = {n.get("name") for n in nodes if n.get("name")}

    if not any(n.get("type") == "outputNode" for n in nodes):
        found.append(Diagnostic(
            kind="lint", severity="warning", code="MISSING_OUTPUT_NODE",
            message="The graph has no output node.",
            fix_hint="Results come from whichever nodes end a path; an explicit output "
                     "node makes the contract obvious.",
        ))

    for node in nodes:
        content = _content(node)

        if node.get("type") == "inputNode" and not (content.get("schema") or "").strip():
            found.append(Diagnostic(
                kind="lint", severity="warning", code="MISSING_INPUT_SCHEMA",
                message=f'"{_label(node)}" declares no schema, so nothing downstream can '
                        "be type-checked.",
                node_id=node.get("id"), node_name=node.get("name"),
                fix_hint="Add a JSON Schema describing the fields the policy expects.",
            ))

        if node.get("type") == "switchNode":
            statements = content.get("statements") or []
            if statements and not any(s.get("isDefault") for s in statements):
                found.append(Diagnostic(
                    kind="lint", severity="warning", code="MISSING_DEFAULT_BRANCH",
                    message=f'Switch "{_label(node)}" has no catch-all branch, so an input '
                            "matching none of its conditions stops here.",
                    node_id=node.get("id"), node_name=node.get("name"),
                    fix_hint="Add a default branch (`- _ => SomeNode`) as the last one.",
                ))

        if node.get("type") == "decisionTableNode":
            found += _table_warnings(node, content, _known_identifiers(graph))

        # `$nodes.Something` where Something is not a node in this graph. Precise, because
        # it needs no type inference - either the name exists or it does not.
        for text in _expression_strings(node, content):
            for match in _NODES_REF_RE.finditer(text):
                referenced = match.group(1) or match.group(2)
                if referenced and referenced not in known_names:
                    found.append(Diagnostic(
                        kind="lint", severity="warning", code="UNKNOWN_NODE_REFERENCE",
                        message=f'"{_label(node)}" reads $nodes.{referenced}, but no node '
                                f'is called "{referenced}".',
                        node_id=node.get("id"), node_name=node.get("name"),
                        fix_hint="Node names are case-sensitive and must match exactly. "
                                 'Use $nodes["Name with spaces"] when a name has spaces.',
                    ))

    return found


def _known_identifiers(graph: dict) -> set[str]:
    """Every name something in this graph could plausibly resolve to.

    Used to tell a bare word that is a real field reference from one that is a forgotten
    quote. Both are valid ZEN, so the parser cannot separate them - but only the second
    silently resolves to null and drops the output.
    """
    names: set[str] = set()
    for node in graph.get("nodes", []):
        content = _content(node)
        for column in (content.get("inputs") or []) + (content.get("outputs") or []):
            field = (column.get("field") or "").strip()
            if field:
                names.update(field.split("."))
        for expression in content.get("expressions") or []:
            key = (expression.get("key") or "").strip()
            if key:
                names.update(key.split("."))
        schema = content.get("schema")
        if isinstance(schema, str) and schema.strip():
            names.update(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', schema))
    return names


def _table_warnings(node: dict, content: dict, known: set[str]) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    inputs = content.get("inputs") or []
    rules = content.get("rules") or []
    hit_policy = content.get("hitPolicy", "first")

    if hit_policy == "first" and rules:
        last = rules[-1]
        if any((last.get(column["id"]) or "").strip() for column in inputs if column.get("id")):
            found.append(Diagnostic(
                kind="lint", severity="warning", code="MISSING_CATCH_ALL_ROW",
                message=f'"{_label(node)}" is first-hit but its last row still has '
                        "conditions, so an input matching no row produces nothing.",
                node_id=node.get("id"), node_name=node.get("name"),
                fix_hint="Add a final row with every input cell empty to give a default.",
            ))

    for column in inputs:
        if not (column.get("field") or "").strip():
            continue  # generic column: cells are full expressions, bare words are legitimate
        for r, rule in enumerate(rules):
            cell = (rule.get(column.get("id")) or "").strip()
            if (
                cell
                and _BARE_WORD_RE.match(cell)
                and cell.lower() not in _CELL_KEYWORDS
            ):
                found.append(Diagnostic(
                    kind="lint", severity="warning", code="UNQUOTED_STRING_CELL",
                    message=f'"{_label(node)}" row {r + 1}, column "{column.get("name")}": '
                            f'{cell} is read as a variable, not the text "{cell}".',
                    node_id=node.get("id"), node_name=node.get("name"),
                    path=f"rules[{r}].{column.get('name')}",
                    fix_hint=f"Quote it: '{cell}'. Unquoted, it resolves to null and the "
                             "row never matches.",
                ))

    # The same mistake on the output side is what makes a graph "run" while producing
    # nothing: an unquoted label is a variable reference, resolves to null, and the engine
    # drops the key entirely rather than raising. Only flag words nothing in the graph
    # could actually produce, so a genuine field reference is left alone.
    for column in content.get("outputs") or []:
        for r, rule in enumerate(rules):
            cell = (rule.get(column.get("id")) or "").strip()
            if (
                cell
                and _BARE_WORD_RE.match(cell)
                and cell.lower() not in _CELL_KEYWORDS
                and cell not in known
            ):
                found.append(Diagnostic(
                    kind="lint", severity="warning", code="UNQUOTED_STRING_CELL",
                    message=f'"{_label(node)}" row {r + 1}, output "{column.get("name")}": '
                            f'{cell} is read as a variable, not the text "{cell}".',
                    node_id=node.get("id"), node_name=node.get("name"),
                    path=f"rules[{r}].{column.get('name')}",
                    fix_hint=f"Quote it: '{cell}'. Unquoted it resolves to null, and an "
                             "empty output drops the key - which is why the field goes "
                             "missing without any error.",
                ))

    return found


# --------------------------------------------------------------------------- hints

def _hints(graph: dict) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    nodes = graph.get("nodes", [])
    logic = [n for n in nodes if n.get("type") in LOGIC_TYPES]

    if len(logic) == 1:
        only = logic[0]
        content = _content(only)
        rules, outputs = content.get("rules") or [], content.get("outputs") or []
        if len(rules) >= MONOLITH_RULES or len(outputs) >= MONOLITH_OUTPUTS:
            found.append(Diagnostic(
                kind="lint", severity="hint", code="MONOLITHIC_GRAPH",
                message=f'The whole policy lives in one node, "{_label(only)}" '
                        f"({len(rules)} rules, {len(outputs)} outputs).",
                node_id=only.get("id"), node_name=only.get("name"),
                fix_hint="Split it along the decisions it makes - derive values in an "
                         "expression node, then decide in a table, then route - so each "
                         "node answers one question and a failure names the node at fault.",
            ))

    for node in nodes:
        content = _content(node)

        name = (node.get("name") or "").strip()
        if not name or _GENERIC_NAME_RE.match(name):
            found.append(Diagnostic(
                kind="lint", severity="hint", code="UNDESCRIPTIVE_NAME",
                message=f'"{name or node.get("id")}" does not say what the node does.',
                node_id=node.get("id"), node_name=node.get("name"),
                fix_hint="Name it after the decision it makes - CreditTier, ShippingBand - "
                         "since other nodes reference it by name via $nodes.",
            ))

        if node.get("type") != "decisionTableNode":
            continue

        rules, inputs = content.get("rules") or [], content.get("inputs") or []
        if len(rules) > MAX_TABLE_ROWS:
            found.append(Diagnostic(
                kind="lint", severity="hint", code="TABLE_TOO_LARGE",
                message=f'"{_label(node)}" has {len(rules)} rows.',
                node_id=node.get("id"), node_name=node.get("name"),
                fix_hint=f"Beyond about {MAX_TABLE_ROWS} rows a table is hard to reason "
                         "about. Split it into tables that each decide one thing.",
            ))

        for column in inputs:
            label, field = (column.get("name") or "").strip(), (column.get("field") or "").strip()
            if field and label == field and "." in field:
                found.append(Diagnostic(
                    kind="lint", severity="hint", code="MISSING_COLUMN_LABEL",
                    message=f'"{_label(node)}" column "{label}" is labelled with its field path.',
                    node_id=node.get("id"), node_name=node.get("name"),
                    fix_hint="Give the column a business label so the table reads as rules.",
                ))
            if field and rules and not any((r.get(column.get("id")) or "").strip() for r in rules):
                found.append(Diagnostic(
                    kind="lint", severity="hint", code="NON_DISCRIMINATING_COLUMN",
                    message=f'"{_label(node)}" column "{label or field}" is empty in every '
                            "row, so it never affects which row matches.",
                    node_id=node.get("id"), node_name=node.get("name"),
                    fix_hint="Use it in at least one rule, or remove the column.",
                ))

        found += _redundant_rows(node, inputs, rules)

    found += _repeated_derivations(nodes)
    return found


def _redundant_rows(node: dict, inputs: list, rules: list) -> list[Diagnostic]:
    """Under first-hit, a row whose conditions repeat an earlier row can never fire."""
    if _content(node).get("hitPolicy", "first") != "first":
        return []

    found, seen = [], {}
    for r, rule in enumerate(rules):
        key = tuple((rule.get(c.get("id")) or "").strip() for c in inputs if c.get("id"))
        if key in seen:
            found.append(Diagnostic(
                kind="lint", severity="hint", code="REDUNDANT_TABLE_ROW",
                message=f'"{_label(node)}" row {r + 1} tests exactly what row {seen[key] + 1} '
                        "tests, so it can never match.",
                node_id=node.get("id"), node_name=node.get("name"),
                path=f"rules[{r}]",
                fix_hint="Remove it, or make its conditions distinct.",
            ))
        else:
            seen[key] = r
    return found


def _repeated_derivations(nodes: list) -> list[Diagnostic]:
    """The same sub-expression computed in several places: compute once, reference it."""
    where: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for expression in _content(node).get("expressions") or []:
            value = (expression.get("value") or "").strip()
            # Only non-trivial expressions; `amount` repeated is not worth a hint.
            if len(value) > 12 and any(op in value for op in "+-*/?"):
                where[value].append(_label(node))

    return [
        Diagnostic(
            kind="lint", severity="hint", code="REPEATED_DERIVATION",
            message=f'"{value}" is computed in {len(places)} places ({", ".join(places)}).',
            fix_hint="Compute it once in an upstream node and read it with $nodes, so the "
                     "rule has one place to change.",
        )
        for value, places in where.items() if len(places) > 1
    ]


def _expression_strings(node: dict, content: dict) -> list[str]:
    """Every piece of ZEN text in a node, for reference scanning."""
    texts = [e.get("value") or "" for e in content.get("expressions") or []]
    texts += [s.get("condition") or "" for s in content.get("statements") or []]
    texts += [
        value for rule in content.get("rules") or []
        for key, value in rule.items() if key != "_id" and isinstance(value, str)
    ]
    if node.get("type") == "functionNode":
        texts.append(content.get("source") or "")
    return [t for t in texts if t]


# --------------------------------------------------------------------------- entry point

_SEVERITY_RANK = {"error": 0, "warning": 1, "hint": 2}


def lint(graph: dict) -> list[Diagnostic]:
    """Every static finding for a graph, most severe first."""
    found = _structural_errors(graph)

    # The engine's own structural check and the expression parser, reused rather than
    # reimplemented. They already return errors, which is the right severity.
    already_reported_missing_input = any(d.code == "MISSING_INPUT_NODE" for d in found)
    for diagnostic in check_structure(graph):
        # The engine reports a missing input node as `invalidInputCount`; saying it twice
        # in two vocabularies helps nobody.
        if diagnostic.code == "invalidInputCount" and already_reported_missing_input:
            continue
        found.append(diagnostic)

    found += check_expressions(graph)

    found += _warnings(graph)
    found += _hints(graph)

    return sorted(found, key=lambda d: _SEVERITY_RANK.get(d.severity, 3))


def blocking(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Only errors gate a build. Warnings and hints travel with the result instead."""
    return [d for d in diagnostics if d.severity == "error"]
