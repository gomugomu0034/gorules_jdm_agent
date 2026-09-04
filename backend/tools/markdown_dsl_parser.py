"""Compile the agent's Markdown DSL into a JDM decision graph.

The parser is deliberately strict. It used to recover from almost anything - dropping edges
whose endpoints did not resolve, inventing nodes for names it did not recognise, defaulting
an unknown `type:` to an input node, and returning a well-formed empty graph for empty
input. None of that raised, so the builder's repair loop never learned there was anything
to repair: the model was handed a graph with no edges and no expressions, and the failure
only surfaced much later as an inscrutable engine error.

Every rejection here carries the offending line and text, because that string is fed
straight back to the model as its repair instruction.
"""

import re
import uuid


class DslError(ValueError):
    """A DSL document that cannot be compiled, with the reasons why.

    Collects every problem rather than stopping at the first, so one repair pass can fix
    the whole document instead of trading one round trip per mistake.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(
            "The graph plan could not be compiled:\n\n"
            + "\n".join(f"  - {p}" for p in problems)
        )


NODE_TYPES = {
    'input': 'inputNode',
    'output': 'outputNode',
    'decisiontable': 'decisionTableNode',
    'expression': 'expressionNode',
    'switch': 'switchNode',
    'function': 'functionNode',
    'decision': 'decisionNode',
}

# One arrow, with an optional |label| hanging off it. Splitting on this keeps the chain in
# `A --> B --> C` intact; the old single `re.match` captured only the first hop and silently
# lost the rest.
_ARROW_RE = re.compile(r'-->\s*(?:\|([^|]*)\|\s*)?')

# `Name` or `id["Display Label"]`
_LABELLED_RE = re.compile(r'^[A-Za-z0-9_]+\["(.*)"\]$')


def _line_of(haystack: str, needle: str) -> int | None:
    """1-based line number of `needle`, for error messages."""
    index = haystack.find(needle)
    return haystack.count("\n", 0, index) + 1 if index != -1 else None


def _at(line: int | None) -> str:
    return f" (line {line})" if line else ""


def _clean_name(raw: str) -> str:
    name = raw.strip()
    match = _LABELLED_RE.match(name)
    return match.group(1).strip() if match else name


def parse_markdown_dsl(md_content: str) -> dict:
    """Parse the Markdown DSL into a JDM dictionary, or raise `DslError`."""
    problems: list[str] = []

    if not md_content or not md_content.strip():
        raise DslError(["The plan is empty - no DSL was produced."])

    nodes_by_name: dict[str, dict] = {}
    edges_raw: list[dict] = []

    sections = re.split(r'^\#\s+', md_content, flags=re.MULTILINE)
    structure_section = ""
    nodes_section = ""

    for section in sections:
        lines = section.strip().split('\n')
        if not lines:
            continue
        title = lines[0].strip().lower()
        content = '\n'.join(lines[1:])
        if 'structure' in title:
            structure_section = content
        elif 'nodes' in title:
            nodes_section = section

    if not structure_section.strip():
        problems.append(
            'No "# Structure" section. It must hold one ```mermaid flowchart LR block whose '
            'arrows define the edges.'
        )
    if not nodes_section.strip():
        problems.append('No "# Nodes" section. Every node named in the flowchart needs a "## <name>" block.')

    # ---------------------------------------------------------------- structure
    mermaid_match = re.search(
        r'```(?:mermaid)?[^\S\n]*\n(.*?)\n```', structure_section, re.DOTALL | re.IGNORECASE
    )
    if structure_section.strip() and not mermaid_match:
        problems.append(
            'The "# Structure" section has no closed ```mermaid block. Open it with three '
            'backticks immediately followed by "mermaid" and close it with three backticks '
            'on their own line.'
        )

    if mermaid_match:
        for offset, line in enumerate(mermaid_match.group(1).split('\n')):
            line = line.strip()
            if not line or 'flowchart' in line or line.startswith('%%'):
                continue
            if '-->' not in line:
                continue
            parts = _ARROW_RE.split(line)
            # split() with one group yields [name, label, name, label, name, ...]
            for i in range(0, len(parts) - 2, 2):
                source, target = _clean_name(parts[i]), _clean_name(parts[i + 2])
                if not source or not target:
                    problems.append(f'Could not read the connection "{line}"{_at(_line_of(md_content, line))}.')
                    continue
                edges_raw.append({
                    "source": source,
                    "target": target,
                    "label": (parts[i + 1] or "").strip() or None,
                })

    # ---------------------------------------------------------------- nodes
    for block in re.split(r'^##\s+', nodes_section, flags=re.MULTILINE)[1:]:
        lines = block.strip().split('\n')
        if not lines or not lines[0].strip():
            continue
        node_name = lines[0].strip().strip('*').strip()
        node_content = '\n'.join(lines[1:])
        at = _at(_line_of(md_content, f"## {node_name}"))

        if node_name in nodes_by_name:
            problems.append(f'Node "{node_name}" is declared more than once{at}.')
            continue

        properties: dict[str, str] = {}
        code_blocks: list[tuple[str, str]] = []

        def stash_code(match, sink=code_blocks):
            sink.append((match.group(1), match.group(2)))
            return f"__CODE_BLOCK_{len(sink) - 1}__"

        cleaned = re.sub(r'```(\w*)[^\S\n]*\n(.*?)\n```', stash_code, node_content, flags=re.DOTALL)

        bullets: list[str] = []
        prop_lines: list[str] = []
        for line in cleaned.split('\n'):
            line = line.strip()
            if not line:
                continue
            if line.startswith('- '):
                bullets.append(line[2:])
            elif '__CODE_BLOCK_' in line or '|' in line:
                prop_lines.append(line)
            else:
                match = re.match(r'^([\w\-]+)\s*:\s*(.*)$', line)
                if match:
                    properties[match.group(1).strip()] = match.group(2).strip()

        declared = properties.get('type', '').strip()
        if not declared:
            problems.append(
                f'Node "{node_name}" has no "type:"{at}. Give it one of: '
                f'{", ".join(sorted(NODE_TYPES))}.'
            )
            continue
        if declared.lower() not in NODE_TYPES:
            problems.append(
                f'Node "{node_name}" has an unknown type "{declared}"{at}. '
                f'Valid types are: {", ".join(sorted(NODE_TYPES))}.'
            )
            continue
        jdm_type = NODE_TYPES[declared.lower()]

        node_data = {
            "id": str(uuid.uuid4()),
            "name": node_name,
            "type": jdm_type,
            "content": {},
        }

        if 'position' in properties:
            pos = re.match(r'(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)', properties['position'])
            if pos:
                node_data['position'] = {"x": float(pos.group(1)), "y": float(pos.group(2))}

        for key in ('hitPolicy', 'passThrough', 'executionMode', 'inputField', 'outputPath', 'kind'):
            if key not in properties:
                continue
            value = properties[key].strip()
            if key == 'passThrough':
                node_data["content"][key] = value.lower() in ('true', 'yes', '1')
            elif key in ('inputField', 'outputPath'):
                # '<root>' and the various spellings of "empty" all mean "not set".
                node_data["content"][key] = "" if value in ('<root>', '""', "''", 'null', 'None') else value
            else:
                node_data["content"][key] = value

        if jdm_type == 'expressionNode':
            expressions = []
            source = next(
                (code for lang, code in code_blocks
                 if lang.lower() in ('expressions', 'expression', 'text', '')), None
            )
            if source is None:
                problems.append(
                    f'Expression node "{node_name}" has no ```expressions block{at}. '
                    'Each line inside it is one assignment, e.g. "total = price * quantity".'
                )
                continue
            for line in source.split('\n'):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                key, sep, value = line.partition('=')
                if not sep:
                    problems.append(
                        f'In expression node "{node_name}", the line "{line}" is not an '
                        'assignment. Write it as "key = expression".'
                    )
                    continue
                expressions.append({"id": str(uuid.uuid4()), "key": key.strip(), "value": value.strip()})
            if not expressions:
                problems.append(f'Expression node "{node_name}" computes nothing{at}.')
                continue
            node_data["content"]["expressions"] = expressions

        elif jdm_type == 'decisionTableNode':
            table_lines = [l for l in prop_lines if l.startswith('|')]
            if len(table_lines) < 3:
                problems.append(
                    f'Decision table "{node_name}" has no rules{at}. It needs a header row of '
                    '"| in <field> [Label] | out <field> |", a "| --- |" separator, and at '
                    'least one rule row.'
                )
                continue

            headers = [c.strip() for c in table_lines[0].split('|')[1:-1]]
            inputs, outputs, col_mappings = [], [], {}
            for idx, header in enumerate(headers):
                col_id = str(uuid.uuid4())
                in_match = re.match(r'^in\s+(.*?)(?:\s*\[(.*?)\])?$', header, re.IGNORECASE)
                out_match = re.match(r'^out\s+(.*?)(?:\s*\[(.*?)\])?$', header, re.IGNORECASE)
                if in_match:
                    field = in_match.group(1).strip()
                    label = in_match.group(2).strip() if in_match.group(2) else field
                    if field.lower() in ('(expression)', 'expression'):
                        field = ""
                    inputs.append({"id": col_id, "name": label, "field": field})
                    col_mappings[idx] = (col_id, 'in')
                elif out_match:
                    field = out_match.group(1).strip()
                    if ':' in field:
                        field = field.split(':', 1)[0].strip()
                    label = out_match.group(2).strip() if out_match.group(2) else field
                    outputs.append({"id": col_id, "name": label, "field": field})
                    col_mappings[idx] = (col_id, 'out')
                else:
                    problems.append(
                        f'In decision table "{node_name}", the column "{header}" is neither an '
                        'input nor an output. Prefix it with "in " or "out ".'
                    )

            if not outputs:
                problems.append(
                    f'Decision table "{node_name}" declares no output column{at}, so it can '
                    'never decide anything. Add at least one "out <field>" column.'
                )
                continue

            node_data["content"]["inputs"] = inputs
            node_data["content"]["outputs"] = outputs

            rules = []
            for r_line in table_lines[2:]:
                cells = [c.strip() for c in r_line.split('|')[1:-1]]
                if not cells:
                    continue
                rule = {"_id": str(uuid.uuid4())}
                for idx, cell in enumerate(cells):
                    if idx in col_mappings:
                        rule[col_mappings[idx][0]] = "" if cell in ('_', '""', "''", '-', 'null') else cell
                rules.append(rule)
            if not rules:
                problems.append(f'Decision table "{node_name}" has a header but no rule rows{at}.')
                continue
            node_data["content"]["rules"] = rules

        elif jdm_type == 'switchNode':
            statements, switch_targets = [], {}
            for bullet in bullets:
                condition, sep, target = bullet.partition('=>')
                if not sep:
                    problems.append(
                        f'In switch "{node_name}", the branch "{bullet}" has no target. '
                        'Write it as "- <condition> => <TargetNode>", and "- _ => <TargetNode>" '
                        'for the catch-all.'
                    )
                    continue
                condition, target = condition.strip(), target.strip()
                stmt_id = str(uuid.uuid4())
                is_default = condition == '_'
                statements.append({
                    "id": stmt_id,
                    "condition": "" if is_default else condition,
                    "isDefault": is_default,
                })
                switch_targets[stmt_id] = target
            if not statements:
                problems.append(
                    f'Switch "{node_name}" has no branches{at}. List them as '
                    '"- <condition> => <TargetNode>" bullets.'
                )
                continue
            node_data["content"]["statements"] = statements
            node_data["_switch_targets"] = switch_targets

        elif jdm_type == 'functionNode':
            source = next(
                (code for lang, code in code_blocks
                 if lang.lower() in ('js', 'javascript', 'ts', 'typescript', '')), None
            )
            if not source or not source.strip():
                problems.append(
                    f'Function node "{node_name}" has no ```js block{at}. It must export a '
                    'handler, e.g. "export const handler = async (input) => ({ ... });".'
                )
                continue
            node_data["content"]["source"] = source.strip()

        elif jdm_type == 'decisionNode':
            # The engine field is `key`; the DSL spells it `calls`.
            key = (properties.get('key') or properties.get('calls') or '').strip()
            if not key:
                problems.append(
                    f'Decision node "{node_name}" does not say which policy it calls{at}. '
                    'Add "calls: <path/to/policy>".'
                )
                continue
            node_data["content"]["key"] = key

        elif jdm_type in ('inputNode', 'outputNode'):
            schema = next((code for lang, code in code_blocks if lang.lower() == 'json'), "")
            node_data["content"]["schema"] = schema.strip()

        nodes_by_name[node_name] = node_data

    # ---------------------------------------------------------------- wiring
    referenced = {e["source"] for e in edges_raw} | {e["target"] for e in edges_raw}
    for name in sorted(referenced - set(nodes_by_name)):
        # Previously invented as a phantom input/output node, which turned a typo into a
        # disconnected graph that only failed much later, inside the engine.
        problems.append(
            f'The flowchart connects "{name}", but there is no "## {name}" block under '
            '"# Nodes". Add it, or correct the name in the flowchart.'
        )

    if not nodes_by_name and not problems:
        problems.append('No nodes were declared. Every graph needs at least an input and an output node.')

    if len(nodes_by_name) > 1 and not edges_raw and not problems:
        problems.append(
            f'{len(nodes_by_name)} nodes were declared but nothing connects them. The '
            '"# Structure" flowchart needs a "-->" line for every connection.'
        )

    if problems:
        raise DslError(problems)

    for i, node in enumerate(nodes_by_name.values()):
        if "position" not in node:
            node["position"] = {"x": 100 + (i * 300), "y": 200}

    edges = []
    for edge_raw in edges_raw:
        source, target = nodes_by_name[edge_raw["source"]], nodes_by_name[edge_raw["target"]]
        edge = {
            "id": str(uuid.uuid4()),
            "sourceId": source["id"],
            "targetId": target["id"],
            "type": "edge",
        }
        if source["type"] == "switchNode" and "_switch_targets" in source:
            stmt_id = next(
                (sid for sid, tgt in source["_switch_targets"].items()
                 if tgt.lower() == edge_raw["target"].lower()),
                None,
            )
            if stmt_id:
                edge["sourceHandle"] = stmt_id
        edges.append(edge)

    for node in nodes_by_name.values():
        node.pop("_switch_targets", None)

    return {
        "contentType": "application/vnd.gorules.decision",
        "nodes": list(nodes_by_name.values()),
        "edges": edges,
    }
