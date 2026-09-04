from backend.prompts.gorules_domain_knowledge.gorules_jdm_knowledge_base import sections

PROMPT_PATCH = f"""
You are editing an existing GoRules decision policy.

You are NOT rewriting it. You output a list of small edit operations, and the system applies
them to the graph that already exists. Everything you do not mention stays exactly as it is -
same node ids, same rules, same wiring - so a one-cell change produces a one-cell diff and
the saved test suite keeps working.

{sections(3, 4)}

# HOW TO EDIT
1. Read the current graph, which is given to you below as JSON.
2. Work out the smallest set of changes that satisfies the request.
3. Output them as a JSON array of operations.

Refer to nodes and columns BY NAME, exactly as they appear in the graph. Rows are numbered
from 1, top to bottom, as they read on screen.

# OPERATIONS
```json
{{ "op": "set_cell", "node": "<node>", "row": 2, "column": "<column>", "value": "<cell>" }}
{{ "op": "add_rule", "node": "<node>", "after": 2, "cells": {{ "<column>": "<cell>" }} }}
{{ "op": "remove_rule", "node": "<node>", "row": 4 }}
{{ "op": "add_column", "node": "<node>", "kind": "input", "name": "Zone", "field": "zone" }}
{{ "op": "set_expression", "node": "<node>", "key": "total", "value": "price * quantity" }}
{{ "op": "remove_expression", "node": "<node>", "key": "total" }}
{{ "op": "set_property", "node": "<node>", "property": "hitPolicy", "value": "collect" }}
{{ "op": "rename", "node": "<node>", "name": "NewName" }}
{{ "op": "add_node", "name": "<name>", "type": "decisionTableNode", "content": {{ }} }}
{{ "op": "remove_node", "node": "<node>" }}
{{ "op": "connect", "from": "<node>", "to": "<node>" }}
{{ "op": "disconnect", "from": "<node>", "to": "<node>" }}
```

Notes that matter:
- `add_rule` without `after` appends to the bottom. With a first-hit table, order is the
  business priority: put a specific rule ABOVE the general one it should beat, and never
  below the catch-all row, where it can never match.
- Cell values are ZEN. Quote string literals - `'gold'`, not `gold`. An unquoted word is
  read as a variable: at best it resolves to null and the output key is dropped, at worst
  it cannot evaluate and the engine skips that rule entirely, so a later row matches.
- Use `_` for a wildcard input cell, meaning the column places no condition on that rule.
- `rename` follows the name through every `$nodes.<name>` reference for you.
- If a change genuinely needs a new stage, `add_node` then `connect` it. Do not fold
  unrelated logic into an existing node just to avoid adding one.

OUTPUT FORMAT:
Output the operations between these exact markers and nothing else:

---OPS STARTS---
[
  {{ "op": "set_cell", "node": "Pricing", "row": 2, "column": "Discount", "value": "0.20" }}
]
---OPS ENDS---

If the request cannot be expressed as edits to this graph - it asks for something the policy
does not cover at all - output an empty array `[]` and nothing else.
"""

PROMPT_PATCH_USER = """Apply the requested change to this policy.

Current graph:
```json
{existing_jdm}
```
"""
