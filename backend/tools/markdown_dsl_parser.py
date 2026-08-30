import json
import re
import uuid

def parse_markdown_dsl(md_content: str) -> dict:
    """Parses GoRules Markdown DSL into a JDM Dictionary."""
    try:
        nodes_by_name = {}
        edges_raw = []

        sections = re.split(r'^\#\s+', md_content, flags=re.MULTILINE)
        structure_section = ""
        nodes_section = ""

        for section in sections:
            lines = section.strip().split('\n')
            if not lines: continue

            title = lines[0].strip().lower()
            content = '\n'.join(lines[1:])

            if 'structure' in title:
                structure_section = content
            elif 'nodes' in title:
                nodes_section = section

        # 1. Parse Structure
        mermaid_block_match = re.search(r'```(?:mermaid)?\s*\n(.*?)\n```', structure_section, re.DOTALL | re.IGNORECASE)
        if mermaid_block_match:
            mermaid_content = mermaid_block_match.group(1)
            for line in mermaid_content.split('\n'):
                line = line.strip()
                if not line or 'flowchart' in line: continue
                match = re.match(r'([\w\-\[\]"\'\s]+)\s*-->\s*(?:\|([^|]+)\|\s*)?([\w\-\[\]"\'\s]+)', line)
                if match:
                    src = re.sub(r'^[a-zA-Z0-9_]+\["(.*)"\]$', r'\1', match.group(1).strip())
                    label = match.group(2).strip() if match.group(2) else None
                    dst = re.sub(r'^[a-zA-Z0-9_]+\["(.*)"\]$', r'\1', match.group(3).strip())
                    edges_raw.append(
                        {
                            "source": src,
                            "target": dst,
                            "label": label
                        }
                    )

        # 2. Parse Nodes
        node_blocks = re.split(r'^##\s+', nodes_section, flags=re.MULTILINE)
        for block in node_blocks[1:]:
            lines = block.strip().split('\n')
            if not lines or not lines[0].strip(): continue
            node_name = lines[0].strip().strip('*').strip()
            node_content = '\n'.join(lines[1:])

            properties = {}
            code_blocks = []

            def repl_code(m):
                code_blocks.append(
                    (m.group(1), m.group(2))
                )
                return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

            cleaned_content = re.sub(r'```(\w*)\s*\n(.*?)\n```', repl_code, node_content, flags=re.DOTALL)

            bullets = []
            prop_lines = []
            for line in cleaned_content.split('\n'):
                line = line.strip()
                if not line: continue
                if line.startswith('- '):
                    bullets.append(line[2:])
                elif '__CODE_BLOCK_' in line or '|' in line:
                    prop_lines.append(line)
                else:
                    match = re.match(r'^([\w\-]+)\s*:\s*(.*)$', line)
                    if match:
                        properties[match.group(1).strip()] = match.group(2).strip()

            node_type = properties.get('type', 'inputNode')
            type_mapping = {
                'input': 'inputNode', 'output': 'outputNode',
                'decisiontable': 'decisionTableNode', 'expression': 'expressionNode',
                'switch': 'switchNode', 'function': 'functionNode', 'decision': 'decisionNode'
            }
            jdm_type = type_mapping.get(node_type.lower(), node_type)

            node_data = {
                "id": str(uuid.uuid4()),
                "name": node_name,
                "type": jdm_type,
                "content": {}
            }

            if 'position' in properties:
                pos_match = re.match(r'(\d+)\s*,\s*(\d+)', properties['position'])
                if pos_match:
                    node_data['position'] = {
                        "x": int(pos_match.group(1)),
                        "y": int(pos_match.group(2))
                    }


            for key in ['hitPolicy', 'passThrough', 'executionMode', 'inputField', 'outputPath', 'calls', 'kind']:
                if key in properties:
                    val = properties[key].strip()
                    if key == 'passThrough':
                        node_data["content"][key] = True if properties[key].lower() in ('true', 'yes', '1') else False
                    elif key in ('inputField', 'outputPath'):
                        # Translate '<root>' or literal empty quotes into a true empty string
                        if val in ('<root>', '""', "''", 'null', 'None'):
                            node_data["content"][key] = ""
                        else:
                            node_data["content"][key] = val
                    else:
                        node_data["content"][key] = properties[key]

            if jdm_type == 'expressionNode':
                expressions = []
                expr_code = next(
                    (code for lang, code in code_blocks if lang in ('expressions', 'expression', 'text', '')), ""
                )
                for line in expr_code.split('\n'):
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        expressions.append(
                            {
                                "id": str(uuid.uuid4()),
                                "key": parts[0].strip(),
                                "value": parts[1].strip()
                            }
                        )
                node_data["content"]["expressions"] = expressions

            elif jdm_type == 'decisionTableNode':
                table_lines = [l for l in prop_lines if l.startswith('|')]
                if table_lines:
                    headers = [c.strip() for c in table_lines[0].split('|')[1:-1]]
                    inputs, outputs, col_mappings = [], [], {}

                    for idx, h in enumerate(headers):
                        col_id = str(uuid.uuid4())
                        in_match = re.match(r'^in\s+(.*?)(?:\s*\[(.*?)\])?$', h, re.IGNORECASE)
                        out_match = re.match(r'^out\s+(.*?)(?:\s*\[(.*?)\])?$', h, re.IGNORECASE)

                        if in_match:
                            field_expr = in_match.group(1).strip()
                            label = in_match.group(2).strip() if in_match.group(2) else field_expr
                            if field_expr.lower() in ('(expression)', 'expression'): field_expr = ""
                            inputs.append(
                                {
                                    "id": col_id,
                                    "name": label,
                                    "field": field_expr
                                }
                            )
                            col_mappings[idx] = (col_id, 'in')
                        elif out_match:
                            field_expr = out_match.group(1).strip()
                            if ':' in field_expr: field_expr = field_expr.split(':', 1)[0].strip()
                            label = out_match.group(2).strip() if out_match.group(2) else field_expr
                            outputs.append(
                                {
                                    "id": col_id,
                                    "name": label,
                                    "field": field_expr
                                }
                            )
                            col_mappings[idx] = (col_id, 'out')

                    node_data["content"]["inputs"] = inputs
                    node_data["content"]["outputs"] = outputs

                    rules = []
                    for r_line in table_lines[2:]:
                        cells = [c.strip() for c in r_line.split('|')[1:-1]]
                        if not cells: continue
                        rule_obj = {
                            "_id": str(uuid.uuid4())
                        }

                        for idx, cell_val in enumerate(cells):
                            if idx in col_mappings:
                                # Intercept markdown placeholders and convert to empty string
                                if cell_val in ('_', '""', "''", '-', 'null'):
                                    cell_val = ""

                                rule_obj[col_mappings[idx][0]] = cell_val
                        rules.append(rule_obj)

                    node_data["content"]["rules"] = rules

            elif jdm_type == 'switchNode':
                statements, switch_targets = [], {}
                for bullet in bullets:
                    parts = bullet.split('=>', 1)
                    if len(parts) == 2:
                        cond, target = parts[0].strip(), parts[1].strip()
                        stmt_id = str(uuid.uuid4())
                        is_default = (cond == '_')
                        statements.append(
                            {"id": stmt_id,
                             "condition": "" if is_default else cond,
                             "isDefault": is_default
                             }
                        )
                        switch_targets[stmt_id] = target
                node_data["content"]["statements"] = statements
                node_data["_switch_targets"] = switch_targets

            # ---> NEW: Parse Input/Output Schemas <---
            elif jdm_type in ('inputNode', 'outputNode'):
                schema_json = ""
                for lang, code in code_blocks:
                    if lang == 'json':
                        schema_json = code
                        break

                node_data["content"]["schema"] = schema_json.strip() if schema_json else ""

            nodes_by_name[node_name] = node_data

        # Edges and Layout
        all_node_names = set(e["source"] for e in edges_raw) | set(e["target"] for e in edges_raw)
        for name in all_node_names:
            if name not in nodes_by_name:
                node_type = "inputNode" if not any(e["target"] == name for e in edges_raw) else "outputNode"
                nodes_by_name[name] = {
                    "id": str(uuid.uuid4()),
                    "name": name,
                    "type": node_type,
                    "content": {
                        "schema": ""
                    }
                }

        # Very basic grid positioning to satisfy schema
        # Fallback grid positioning for any nodes where the LLM forgot to provide coordinates
        for i, node in enumerate(nodes_by_name.values()):
            if "position" not in node:
                node["position"] = {"x": 100 + (i * 300), "y": 200}

        edges = []
        for edge_raw in edges_raw:
            src_node, dst_node = nodes_by_name.get(edge_raw["source"]), nodes_by_name.get(edge_raw["target"])
            if not src_node or not dst_node: continue
            edge_obj = {
                "id": str(uuid.uuid4()),
                "sourceId": src_node["id"],
                "targetId": dst_node["id"],
                "type": "edge"
            }

            if src_node["type"] == "switchNode" and "_switch_targets" in src_node:
                stmt_id = next(
                    (sid for sid, tgt in src_node["_switch_targets"].items() if tgt.lower() == edge_raw["target"].lower()), None
                )

                if stmt_id: edge_obj["sourceHandle"] = stmt_id
            edges.append(edge_obj)

        for node in nodes_by_name.values():
            if "_switch_targets" in node: del node["_switch_targets"]

        # ---> NEW: Return the dictionary directly <---
        return {
            "contentType": "application/vnd.gorules.decision",
            "nodes": list(nodes_by_name.values()),
            "edges": edges
        }

    except Exception as e:
        # ---> NEW: Raise the error so the builder_node can catch it <---
        raise ValueError(str(e))