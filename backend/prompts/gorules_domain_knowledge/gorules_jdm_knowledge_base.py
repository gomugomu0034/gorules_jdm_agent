GORULES_KNOWLEDGE_BASE = """
# GoRules JDM Knowledge Base — Authoring & Evaluation Guide for an Agent.
> Source: official GoRules documentation. This file consolidates the JDM format, node reference, the ZEN expression language, rule-authoring conventions, and testing/evaluation practice into one operational reference. It is written so an agent can go from a plain-English requirement to a valid JDM graph plus a matching set of test cases.

---------------------------------------------------------------------

## 1. What the agent is producing
Two artifacts, always paired:
1. **A JDM graph** (`.json`) — a directed, acyclic graph of nodes (Input → processing nodes → Output) that encodes the business rule.
2. **A test suite** — a set of `{ input, expectedOutput }` pairs (simulator "events") that exercise every rule row, every branch, and the boundary conditions of the graph.
A JDM graph is a **pure function**: same input JSON in → same output JSON out, no side effects, no hidden state between runs. This is what makes it testable with simple fixtures, and it's the property the test suite must verify.


---------------------------------------------------------------------


## 2. JDM file structure
A JDM document has three top-level sections:
```json
{
    "nodes": [ /* processing steps */ ],
    "edges": [ /* connections between nodes */ ],
    "contentType": "application/vnd.gorules.decision"
}
```
### Nodes
```json
{
    "id": "node-1",
    "type": "<nodeType>",
    "name": "<human readable name>",
    "position": { "x": <x coordinate>, "y": <y coordinate> },
    "content": { /* node-type-specific configuration, see §3 */ }
}
```
- `id` — unique, stable identifier. Used by edges.
- `type` — one of the node types in §3.
- `name` — human-readable, and **this is the key other nodes use to reference this node's output via `$nodes.<name>`**. Keep names short, unique, and stable (renaming breaks `$nodes` references elsewhere in the graph).
- `position` - For UI layout.
- `content` — node-specific payload (decision table rules, expression rows, function code, switch conditions, etc.).

### Edges
```json
{
    "id": "edge-1",
    "sourceId": "<input-node-id>",
    "targetId": "<target-node-id>",
    "sourceHandle": "output",
    "targetHandle": "input"
}
```
Data flows left → right along edges, from the graph's single Input node to one or more Output nodes. A node with multiple incoming edges merges the data from all of them. Switch and Decision nodes expose additional named handles (see §3.5–3.6).
### Metadata
Optional: `version`, `author`, `description`, `tags`. Bump `version` and write a meaningful description whenever the rule logic changes — this is what makes JDM diffs in version control meaningful:
```diff
"rules": [
    {
        "inputEntries": [{ "value": "\"gold\"" }],
-       "outputEntries": [{ "value": "0.15" }]
+       "outputEntries": [{ "value": "0.20" }]
    }
]
```

## 3. Node type reference
Every graph starts with exactly one **Input** node and ends in one or more **Output** nodes. Between them, use the processing node types below.

### 3.1 Input node (`inputNode`)
Entry point. All data sent in the evaluation request body becomes available here.
Optionally use schema when the user explicitly asks for a schema graph.

Optionally on user request, attach a JSON Schema to validate incoming structure before any processing runs:
- Cover every field the graph reads, with type, properties, and required for the mandatory ones. No transformation — input flows downstream unchanged.
- Invalid input is rejected before processing, with a clear validation error — build this in whenever the requirement specifies required fields or types.
- A request field with a fixed value set should reference a dictionary instead of a hand-copied enum: { "$dictionary": "productType" } as the property schema ({ "type": "array", "items": { "$dictionary": "productType" } } for lists).
- The dictionary must be reachable through the graph's imports frontmatter. This types the field as the labeled enum, validates the request at evaluation time, and keeps the value set defined in one place.
- An unresolvable $dictionary name is a compile-time diagnostic on the node and a runtime node error. Sibling keys like description are preserved; a type/enum next to $dictionary is overridden.


### 3.2 Output node (`outputNode`)
Terminal node(s). Produces the final result of the evaluation.
A graph can have multiple Output nodes reached via different branches (e.g., one for "approved", one for "rejected" after a Switch node). This is verified to compile, validate and evaluate. Prefer a single Output node when every path ends in the same shape of result; use several only when the branches genuinely terminate differently and naming them makes the graph easier to read.


### 3.3 Decision Table node (`decisionTableNode`)
The core rule-authoring construct: a spreadsheet of conditions → outcomes.
**Raw content shape:**
```json
{
    "hitPolicy": "first" or "collect",
    "inputs": [
        { "id": "in1", "name": "Customer Tier", "field": "customer.tier" },
        { "id": "in2", "name": "Order Total", "field": "order.total" }
    ],
    "outputs": [
        { "id": "out1", "name": "Discount", "field": "discount" }
    ],
    "rules": [
    {
        "in1": "'gold'",
        "in2": "> 100",
        "out1": "0.20"
    },
    {
        "in1": "'silver'",
        "in2": "",
        "out1": "0.10"
    },
    {
        "in1": "",
        "in2": "",
        "out1": "0"
    }
    ],
    "passThrough": true or false,
    "inputField": null,
    "outputPath": null,
    "executionMode": "single" or "loop"
}
```
(Editor exports may represent rules as `inputEntries`/`outputEntries` arrays keyed by column id instead of the flattened form above — both are valid JDM; the agent should follow whichever shape the target GoRules SDK/editor version expects, but the **semantics below are constant**.)
**Authoring semantics:**
- Input columns operate in one of three modes: 1) Field mode (in customer.age): unary test with $ bound to that field — >= 18, "US", ["US","CA"], [1..10], contains($, "x"), not in ["a","b"]. 2) Computed-field mode (in <expression>): the header is itself a ZEN expression, evaluated per row, with each cell a unary test against the result ($ bound to the computed value). The header expression reads input fields and $nodes.<ancestor> directly, but not $.. 3) Whole- expression mode (literal header in (expression)): no bound field; each cell is a full boolean ZEN expression.
- Output columns (out rate) always name a field; the cell is a ZEN expression evaluated on match. Full output header grammar: out <field>[: <type>] [<Label>] — type and label coexist and are independent. The type is string, number, boolean, date, or a dictionary name (optionally [] for a list); it validates literal cells and narrows the field's type downstream. Dictionary names resolve through the graph's imports frontmatter; an unresolved name is a TYPE_MISMATCH on the header. Label position differs from policies: graph headers put [Label] AFTER the field, policy headers put "Label" before it.
- **Wildcard cells — two layers, two spellings.** In the **JDM JSON** a wildcard is the empty string `""`. In the **Markdown DSL you author**, write `_`; the parser normalises `_`, `-`, `""` and `''` to `""`. Use `_` in the DSL, because a genuinely blank cell in a markdown table is invisible and its column position is easy to miscount. Both mean the same thing: this column places no condition on this rule.
- An **empty cell** in an input column is used to skip condition. Keep the cell blank if you don't want to use the input column in the condition.
- An empty output cell drops its key entirely (no null) — this is what makes splitting an object-literal output column into per-field dotted columns shape- preserving.
- Cell value conventions (each cell is a string):
| Want | Write |
|---|---|
| Wildcard | "" |
| String match | "Zone 1" |
| String list | ["US","CA"] |
| Number | 5 |
| Boolean | true |
| Comparison | > 50 |
| Range | [0..10] |
| Date comparison | >= d("2020-01-01") |
| Date range (inclusive) | [d("2010-01-01")..d("2025-12-31")] |
| String output |"approved" |
| Expression output | amount * 0.1 |
Add an optional trailing # column for row descriptions (not evaluated).

**Evaluation semantics:**
- Rows are evaluated top to bottom.
- Each **input column is ANDed** left to right within a row.
- If a cell errors, that row is skipped (not the whole table).

- **Hit policy** decides what happens across rows:
    - `first` (default) — stop at the first fully-matching row; result is one object shaped by the output columns; if nothing matches, result is `null`/`undefined` (empty object `{}` in the newer editor semantics — treat both as "no match" and always test for it).
    - `collect` — evaluate every row; result is an **array** of one object per matching row (empty array `[]` if none match). Use when multiple rules can legitimately apply at once (e.g., stacking fees, gathering all rejection reasons, all applicable promotions).
**Column types:**
- **Targeted field (unary)** — column has a `field` path (e.g., `customer.revenue`). Cells contain short unary tests: `> 100`, `[18..65]`, `'US', 'CA'`, empty for wildcard. See §4.3 for full unary syntax.
- **Generic field (standard expression)** — column `field` is unset (`-`). Cells contain full ZEN expressions, e.g. `customer.revenue > 6000 and customer.status == 'active'`, or expressions referencing other nodes: `$nodes.CreditCheck.rating == 'excellent'`.
**Output columns** may contain literals (`100`, `"approved"`, `true`), expressions (`input.amount * 0.1`), or references to earlier nodes (`$nodes.BaseRates.premium`). Output field names with a dot (e.g. `resolution.responsibleParty`) create nested objects automatically.
**Loop execution mode** — when the incoming data is an array of items that each need independent evaluation:

| Property | Purpose |
|---|---|
| `executionMode: "loop"` | Iterate over an array instead of evaluating once |
| `inputField` | Path to the array to iterate, e.g. `testResults` |
| `outputPath` | Where the resulting array is placed in the output |

Always set `outputPath` in loop mode — otherwise the array lands unnamed at the root.
Default output replaces input; set passThrough: true if you want the upstream fields to remain alongside.
Decision tables order rules specific → general, catch-all last.


### 3.4 Expression node (`expressionNode`)
Transforms/reshapes data using one ZEN expression per output row. Rows are evaluated top to bottom; a row can reference a previous row's result in the *same node* with `$.previousKey`:
```json
{
    "expressions": [
        { "key": "subtotal", "value": "sum(map(items, #.price * #.quantity))" },
        { "key": "tax", "value": "$.subtotal * 0.08" },
        { "key": "shipping", "value": "$.subtotal > 100 ? 0 : 9.99" },
        { "key": "total", "value": "$.subtotal + $.tax + $.shipping" }
    ]
}
```
**Authoring semantics:**
- Row expressions are evaluated top to bottom.
- After or bottom expressions can reference earlier keys (derived in the earlier or top expressions) in the same node via $.keyName (always include the dot — $keyName is invalid). example below :
``` subtotal = sum(map(order.items as item, item.price * item.qty))
    tax = $.subtotal * taxRate
    total = $.subtotal + $.tax
```

**Evaluation semantics:**
- Row expressions are evaluated top to bottom.
- Any error inside an Expression node halts the graph, so guard against `null`s (`??`) when fields may be absent.
- One key = value line per expression in the ```expressions fence. Evaluated top- to-bottom;
- After expressions can reference earlier keys in the same node via $.keyName (always include the dot — $keyName is invalid). example below :
``` subtotal = sum(map(order.items as item, item.price * item.qty))
    tax = $.subtotal * taxRate
    total = $.subtotal + $.tax
```
**Loop execution mode** — when the incoming data is an array of items that each need independent evaluation:
| Property | Purpose |
|---|---|
| `executionMode: "loop"` | Iterate over an array instead of evaluating once |
| `inputField` | Path to the array to iterate, e.g. `testResults` |
| `outputPath` | Where the resulting array is placed in the output |

**Transform paths — two layers, two spellings.** In the **JDM JSON**, omit `inputField`/`outputPath` entirely unless the node needs them; an unused key must never carry the literal string `"<root>"`. In the **Markdown DSL you author**, write `<root>` to mean "not set" — the parser turns it into `""`.
Always set `outputPath` in loop mode — otherwise the array lands unnamed at the root.
Default output replaces input; set passThrough: true if you want the upstream fields to remain alongside.



### 3.5 Function node (`functionNode`)
A JavaScript snippet for logic that's awkward to express declaratively (complex parsing, external HTTP calls, iterative algorithms). Runs on a QuickJS isolate embedded in the engine; execution is time-limited (engine-enforced timeout).
```javascript
import zen from 'zen';
import dayjs from 'dayjs';

export const handler = async (input) => {
    const creditRating = input.$nodes.CreditScore.rating;
    const incomeLevel = input.$nodes.IncomeCheck.level;
    
    return {
        eligible: creditRating === "good" && incomeLevel === "sufficient"
    };
};
```

- This block is **JavaScript**, and it is the only place in a graph where JavaScript syntax is valid. `===`, `!==`, `&&` and `||` belong here and nowhere else: in a ZEN expression or a decision table cell they fail to lex. ZEN uses `==`, `!=`, `and`, `or`, `not`.
- Inputs arrive as the function's single `input` argument (includes `$nodes` for upstream node outputs).
- Supports `async`/`await`; importable modules include `zen` (invoke other ZEN decisions from JS), `dayjs`, `big.js`, `http`.
- Prefer Decision Tables / Expression nodes for anything a business user should be able to read and modify. Reach for Function nodes only when expressions genuinely can't do the job — they are the least transparent, least auditable node  type.


### 3.6 Switch node (`switchNode`)
Branches the graph based on conditions, without altering the data — it forwards the full incoming context unchanged down whichever branch(es) match.
```
                    ┌─── approved ───→ [Generate Approval]
[Evaluate] → [Switch]
                    └─── rejected ───→ [Generate Rejection]
```
Content shape (conceptually):
```json
{
    "hitPolicy": "first" or "collect",
    "statements": [
        { "id": "cond-1", "condition": "evaluation.isApproved == true" }
    ]
}
```
Each statement maps to a named output handle used by outgoing edges (plus an implicit default handle for "no condition matched").
- `first` — branch to the first matching condition only (like first-hit decision tables).
- `collect` — branch to **every** matching condition simultaneously; downstream results from all taken branches are merged. If multiple edges leave the same condition, there's no guaranteed execution order among them.
Use switch nodes for workflow branching (approve/reject paths) — see the validation pattern in §5.5.
A branch condition appears twice in the projection (mermaid |label| for display, the bullet as authoritative) — anchor edits on the bullet.


### 3.7 Decision node (`decisionNode`)
Invokes another JDM graph (by key/path) as a reusable sub-decision, passing it the current context and receiving its output. Use this to modularize shared logic (e.g., a "CreditScoreLookup" sub-decision reused across a loan-approval graph and a credit-limit-increase graph) instead of duplicating a decision table in multiple places.


---------------------------------------------------------------------



## 4. ZEN Expression Language
ZEN is the expression language used throughout decision tables, expression nodes, switch conditions, and loop/output-path fields. It's designed to read like Excel formulas, so business users can maintain it.

### 4.1 Two evaluation modes
| Mode | Used in | Example |
|---|---|---|
| **Standard** | Expression nodes, output columns, generic (`-`) decision-table columns | `price * quantity * (1 - discount)` |
| **Unary test** | Decision-table input columns with a `field` set | `>= 100`, `[1..10]`, `'US', 'CA'` |

Inside a unary column, prefixing with `$` (or calling a function) flips evaluation into full expression mode, e.g. `len($) > 5`, `contains($, 'urgent')` — `$` stands for the column's own value.

### 4.2 Literals and core syntax
```
// Numbers
42  3.14  -17  1e6

// Strings
"double quotes"  'single quotes'  `template ${withVars}`

// Booleans / null
true  false  null

// Arrays / Objects
[1, 2, 3]
{ name: "John", age: 30 }
{ [dynamicKey]: value }

// Ternary
score >= 70 ? "pass" : "fail"
age >= 18 ? "adult" : age >= 13 ? "teen" : "child"

// Null coalescing (first non-null)
user.nickname ?? user.name ?? "Anonymous"

// Property access
customer.address.city
items[0].price

// String slicing
str[0:5]   // first 5 chars
str[7:]    // from index 7 to end
str[:6]    // first 6 chars
```

### 4.3 Operators

**Arithmetic:** `+  -  *  /  %  ^` (power)
**Comparison:** `==  !=  >  <  >=  <=`
**Logical:** `and  or  not`

**Ranges (standard and unary):**
```
x in [1..10]      // inclusive both ends
x in (0..100)     // exclusive both ends
x in [0..100)     // inclusive low, exclusive high
x not in [1..10]
```

**Unary-mode shorthand (decision table cells):**
```
> 100                       // comparison
[18..65]  (0..100)          // ranges
'US', 'CA', 'GB'             // list — comma = OR
> 0 and < 100               // combined
// empty = wildcard, always matches
```

### 4.4 Closures and iteration (`#` / `as`)

```
map(items, #.price * #.quantity)
filter([1,2,3,4,5], # > 3)
some(items, #.outOfStock)
all(items, #.verified)

// Named alias — clearer for complex bodies
map(cart.items as item, item.price * item.quantity)
filter(users as user, user.isActive and user.age >= 18)
```

### 4.5 Self-reference within a node: `$` and `$root`

```
subtotal = sum(map(items, #.price * #.quantity))
tax      = $.subtotal * 0.08
shipping = $.subtotal > 100 ? 0 : 9.99
total    = $.subtotal + $.tax + $.shipping
```

Assignment building blocks:

```
a = 5                                     // {"a": 5}
user.name = 'Alice'                       // nested paths auto-create objects
a = 1; b = 2                              // multiple statements, semicolon-separated
a = 5; b = 10; a + b                      // last expression is the return value; here, 15
config.debug = true; $root                // $root returns the whole accumulated context
```

### 4.6 Referencing other nodes in the graph: `$nodes`

```
$nodes.CreditScore.rating
$nodes.RiskAssessment.score
$nodes["Credit Score"].field     // use bracket syntax if the node name has a space
```

Works in expression nodes, function-node `input.$nodes`, decision-table input conditions (generic columns) and output columns, and switch conditions. Node names are case-sensitive and must match the `name` field on the source node exactly.

### 4.7 Built-in functions (complete reference)

**Math:** `abs(x)`, `floor(x)`, `ceil(x)`, `round(x, precision?)`, `trunc(x)`, `min(arr)`, `max(arr)`, `sum(arr)`, `avg(arr)`, `median(arr)`, `mode(arr)`, `rand(max)`

**String:** `len(s)`, `upper(s)`, `lower(s)`, `trim(s)`, `contains(s, sub)` (also works on arrays), `startsWith(s, prefix)`, `endsWith(s, suffix)`, `matches(s, regex)`, `extract(s, regex)` (capture groups), `split(s, delim)`, `fuzzyMatch(a, b)` (similarity 0–1)

**Array:** `map(arr, expr)`, `filter(arr, expr)`, `some(arr, expr)`, `all(arr, expr)`, `one(arr, expr)` (exactly one matches), `none(arr, expr)`, `count(arr, expr)`, `flatMap(arr, expr)`, `keys(objOrArr)`, `values(obj)`, `merge(arrOfArraysOrObjects)`, `mergeDeep(arrOfObjects)` (recursive merge, arrays concatenated)

**Date/time:** `d(str, tz?)` → date object, `date(str)` → unix timestamp seconds (accepts `'now'`, `'yesterday'`, ISO strings), `duration(str)` → seconds from a duration string (`"1h 30m"`, `"7d"`); date objects also support extracting month/day-of-week/start-of-unit etc. — see the dates reference if the requirement needs deep date arithmetic.

**Type:** `string(x)`, `number(x)`, `bool(x)`, `type(x)` → `"string" | "number" | "bool" | "array" | "object" | "null"`, `isNumeric(x)`


---------------------------------------------------------------------

## 5. Rule-authoring guidelines

### 5.1 Choose the right node for the job

| If the requirement is... | Use |
|---|---|
| A table of conditions → outcomes (tiers, eligibility, pricing) | Decision Table |
| A calculation / reshape of data, step by step | Expression node |
| Non-declarative logic: parsing, external calls, loops with side effects | Function node |
| "Do X, otherwise do Y" workflow branching | Switch node |
| Shared logic reused by multiple decisions | Decision node (sub-graph) |

Prefer Decision Table > Expression > Function, in that order, for auditability. Business users can read and safely edit tables and expressions; they generally cannot review JS.

### 5.2 Data flow control

- **`passThrough`** (default: on) — each node forwards prior data plus its own output. Turn it **off** on a node when you want the graph (or that branch) to return only that node's computed fields — typical for a final calculation node feeding directly into Output.
- **`outputPath`** — nest a node's outputs under a named key instead of merging at the root, to keep the final result organized (e.g. `outputPath: "resolution"` groups `responsibleParty`/`refundApproved` under `resolution.*`). Always required when using `loop` or `collect` (both output arrays, and an unnamed array at the root is unusable).

### 5.3 Hit-policy selection

- Use **`first`** when rules are mutually exclusive (e.g., a customer has exactly one tier). Order rows **most-specific → most-general**, and end with a catch-all row (all input cells empty) so the table never silently returns "no match."
- Use **`collect`** when multiple rules can legitimately apply at once (stacking discounts, gathering all validation errors, all matching promotions). Sum/reduce the resulting array downstream with an expression node (`sum(map(result, #.field))`).

### 5.4 Multi-stage decisions

Chain decision tables so each one enriches the context for the next, referencing earlier results via `$nodes`:

```
[Input] → [Credit Score Table] → [Income Table] → [Calculate DTI] → [Interest Rate Table] → [Output]
```

Each intermediate table has `passThrough: true` so downstream nodes can see all accumulated fields.

### 5.5 Validation pattern

1. **Validation table** — a `collect`-hit-policy decision table where each row detects one invalid condition and emits an error + `isValid: false`; a catch-all row emits `isValid: true`.
2. **Switch node** — routes `valid` → continue processing; `invalid`/`len(errors) > 0` → go straight to an error Output node.

This avoids wasted computation on bad input and gives specific, traceable error messages. Include this pattern when the requirements state validation rules or "must be rejected if..." conditions — those are business rules and belong in the graph. Do not add it other than that: schema-shape checking is not the graph's job, so never invent validation the requirements did not ask for.

### 5.6 Array / loop patterns

When the requirement says "for each item in the order," "evaluate every lab result," etc.:
- Set the decision table (or expression node) to `executionMode: loop`, `inputField` = path to the array, `outputPath` = where results go.
- The node evaluates each array element independently and returns an array of the same length in the same order.

### 5.7 Naming and structure conventions

- Node `name` values: short, PascalCase or Title Case, stable (don't rename after other nodes reference them via `$nodes`).
- Keep single decision tables under ~20–30 rows; beyond that, split by concern or introduce a switch node.
- Column labels should read like the business requirement's own vocabulary — labels are what a non-technical reviewer sees.
- Write a `metadata.description` and bump `metadata.version` whenever logic changes.

---------------------------------------------------------------------

## 6. Evaluation & testing guidelines

The GoRules **simulator** is the reference tool for validating a graph before deployment: run an input JSON, see the final output, and trace execution node- by-node (input/output per node, which decision-table rows were evaluated vs. matched, timing).

### 6.1 Test design checklist

For every graph the agent produces, build a test suite that includes:

1. **One test per decision-table row** — a distinct input that is designed to match each row, confirming every rule path is reachable and correct. For `first`- hit tables, also test that row ordering doesn't let a general row shadow a specific one.
2. **Boundary/edge-case tests around every numeric/range condition** — for a condition like `>= 100`, test `99.99`, `100`, `100.01`. For `[18..65]`, test `17`, `18`, `65`, `66`.
3. **No-match tests** — an input that hits no row, to confirm the catch-all behaves as intended (or that "no match" is the deliberately expected behavior).
4. **`collect` cardinality tests** — inputs that match zero, one, and multiple rows, confirming the array shape (`[]`, `[x]`, `[x,y,...]`) and that `outputPath` places it correctly.
5. **Cross-node reference tests** — when a node uses `$nodes.X.field`, test that changing upstream node X's output correctly changes downstream behavior.
6. **Validation/error tests** — malformed input (missing required fields, null values, wrong types, empty arrays) to confirm the validation pattern (§5.5) or input JSON Schema rejects it with a sensible message.
7. **Switch-branch coverage** — one test per outgoing branch of every switch node, including the default/fallback branch.
8. **Loop tests** — an array input with a mix of elements that should and shouldn't match table rows inside the loop, confirming per-element independence and output ordering.

### 6.2 Test case format

Each test case should be expressed as an "event" the simulator (or an SDK-level test harness) can run directly:

```json
{
    "name": "Gold tier, order over 100 -> 20% discount",
    "input": {
        "customer": { "tier": "gold" },
        "order": { "total": 150 }
    },
    "expectedOutput": {
        "customer": { "tier": "gold" },
        "order": { "total": 150 },
        "discount": 0.20
    }
}
```

Include `expectedOutput` as the **full** result object if `passThrough` is on (since input fields are echoed back), or just the computed fields if `passThrough` is off — match whatever the graph actually returns.

### 6.3 Reading the trace when a test fails

- **No output at all** → a node is disconnected; data can't reach Output. Check edges.
- **Wrong row matched** → with `first` hit policy, check row order — an earlier, more general row is shadowing a later, more specific one.
- **Unexpected `null`** → trace forward from Input to find exactly which node introduced the null; check for field-name typos (ZEN silently returns `null` for a missing path rather than erroring, in most contexts).
- **Expression error, graph halts** → the trace shows the exact failing expression and message; usually a `null`/missing field feeding an operation that needs a value — add `??` fallbacks or a validation/switch guard upstream.

### 6.4 Regression suite

Save the full test suite (not just ad hoc runs) as reusable events, and re-run the entire suite whenever the graph changes. Treat it exactly like a unit-test suite for code: every rule addition/removal should come with at least one new test case demonstrating the new/changed behavior, and no existing test case's expected output should change unintentionally.

---------------------------------------------------------------------

## 7. Worked example

**Requirement (plain English):** *"Give customers a discount based on loyalty tier and order size. Gold customers get 20% off orders over $100, otherwise 10%. Silver customers get 10% off orders over $100, otherwise 5%. Everyone else gets no discount. Reject the order if the total is zero or negative."*

### 7.1 Graph shape

```
[Input] → [Validate Order] → [Switch: valid?] ─ valid ──→ [Discount Table] → [Output: Success]
                                              └─ invalid ─────────────────→ [Output: Error]
```

### 7.2 JDM (abridged, illustrating the key node contents)

```json
{
    "nodes": [
        {
            "id": "input-1",
            "type": "inputNode",
            "name": "Request",
            "content": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "customer": {
                            "type": "object",
                            "properties": { "tier": { "type": "string" } },
                            "required": ["tier"]
                        },
                        "order": {
                            "type": "object",
                            "properties": { "total": { "type": "number" } },
                            "required": ["total"]
                        }
                    },
                    "required": ["customer", "order"]
                }
            }
        },
        {
            "id": "table-validate",
            "type": "decisionTableNode",
            "name": "ValidateOrder",
            "content": {
                "hitPolicy": "first",
                "inputs": [
                    { "id": "i1", "name": "Order Total", "field": "order.total" }
                ],
                "outputs": [
                    { "id": "o1", "name": "isValid", "field": "isValid" },
                    { "id": "o2", "name": "error", "field": "error" }
                ],
                "rules": [
                    { "i1": "<= 0", "o1": "false", "o2": "'Order total must be positive'" },
                    { "i1": "", "o1": "true", "o2": "null" }
                ],
                "passThrough": true
            }
        },
        {
            "id": "switch-1",
            "type": "switchNode",
            "name": "RouteOnValidity",
            "content": {
                "hitPolicy": "first",
                "statements": [
                    { "id": "s1", "condition": "isValid == true" }
                ]
            }
        },
        {
            "id": "table-discount",
            "type": "decisionTableNode",
            "name": "DiscountTable",
            "content": {
                "hitPolicy": "first",
                "inputs": [
                    { "id": "i1", "name": "Customer Tier", "field": "customer.tier" },
                    { "id": "i2", "name": "Order Total", "field": "order.total" }
                ],
                "outputs": [
                    { "id": "o1", "name": "discount", "field": "discount" }
                ],
                "rules": [
                    { "i1": "'gold'",   "i2": "> 100", "o1": "0.20" },
                    { "i1": "'gold'",   "i2": "",      "o1": "0.10" },
                    { "i1": "'silver'", "i2": "> 100", "o1": "0.10" },
                    { "i1": "'silver'", "i2": "",      "o1": "0.05" },
                    { "i1": "",         "i2": "",      "o1": "0" }
                ],
                "passThrough": true
            }
        },
        { "id": "output-success", "type": "outputNode", "name": "Success", "content": {} },
        { "id": "output-error",   "type": "outputNode", "name": "Error",   "content": {} }
    ],
    "edges": [
        { "id": "e1", "sourceId": "input-1",        "targetId": "table-validate" },
        { "id": "e2", "sourceId": "table-validate",  "targetId": "switch-1" },
        { "id": "e3", "sourceId": "switch-1",        "targetId": "table-discount", "sourceHandle": "s1" },
        { "id": "e4", "sourceId": "switch-1",        "targetId": "output-error", "sourceHandle": "default" },
        { "id": "e5", "sourceId": "table-discount",  "targetId": "output-success" }
    ],
    "metadata": {
        "version": "1.0.0",
        "description": "Loyalty-tier order discount with validation",
        "tags": ["pricing", "discounts"]
    }
}
```

### 7.3 Matching test suite

| # | Purpose | Input | Expected `discount` / outcome |
|---|---|---|---|
| 1 | Gold, above boundary | `{customer:{tier:"gold"}, order:{total:150}}` | `0.20` |
| 2 | Gold, at boundary (not `> 100`) | `{customer:{tier:"gold"}, order: {total:100}}` | `0.10` |
| 3 | Gold, just above boundary | `{customer:{tier:"gold"}, order:{total:100.01}}` | `0.20` |
| 4 | Gold, small order | `{customer:{tier:"gold"}, order:{total:50}}` | `0.10` |
| 5 | Silver, above boundary | `{customer:{tier:"silver"}, order:{total:150}}` | `0.10` |
| 6 | Silver, small order | `{customer:{tier:"silver"}, order:{total:50}}` | `0.05` |
| 7 | Unknown tier (catch-all row) | `{customer:{tier:"bronze"}, order: {total:200}}` | `0` |
| 8 | Invalid: zero total | `{customer:{tier:"gold"}, order:{total:0}}` | routed to Error, `error:"Order total must be positive"` |
| 9 | Invalid: negative total | `{customer:{tier:"gold"}, order:{total:-10}}` | routed to Error |
| 10 | Missing required field | `{customer:{tier:"gold"}, order:{}}` | rejected by input schema |

This table covers: every decision-table row (1,2,5,6,7 for discount table; row-level coverage of ValidateOrder), the numeric boundary at 100 (2 vs 3), both switch branches (1–7 vs 8–9), and schema validation (10).

---------------------------------------------------------------------


## 8. Workflow checklist

When turning a new requirement into a JDM graph + tests, work through this order:
1. **Extract entities and fields** the rule depends on → shape the Input node's schema.
2. **Identify mutually exclusive vs. stackable rules** → choose `first` vs `collect` hit policy per table.
3. **Draft the decision table(s)** from the requirement's own conditions, most specific row first, always ending in a catch-all.
4. **Identify validation/error conditions stated or implied** ("must be", "cannot be", "only if") → build the validation-table + switch pattern (§5.5).
5. **Identify any per-item / "for each" language** → mark that node `loop` with correct `inputField`/`outputPath`.
6. **Wire the graph**: Input → validation → branching → business tables/expressions → Output(s). Decide `passThrough`/`outputPath` per node based on desired final shape.
7. **Write the ZEN expressions** for anything not representable as a flat unary condition, using `$nodes` to bridge across nodes.
8. **Generate the test suite** per §6.1 (one per row, boundaries, no-match, collect cardinality, cross-node refs, validation, switch branches, loop cases).
9. **Trace-check** each test mentally against the table/graph logic before delivering, and flag any row that can never be reached (dead rule) or condition that's always/never true.
10. **Document**: fill `metadata.description`/`version`, and label columns/nodes in the requirement's own business vocabulary.

"""



ADDITIONAL_DOMAIN_KNOWLEDGE_BASE = """
A decision graph is a directed acyclic set of typed nodes wired together by edges.
The Zen engine walks the graph topologically: the request payload enters the single inputNode, flows along outgoing edges, and each downstream node receives the merged output of its incoming edges, runs, and emits a result.
An outputNode (if present) terminates evaluation when reached.

## Markdown Format Rules
# Structure holds one mermaid flowchart LR — the plain arrows ARE the edges. Every referenced name must have a ## <name> section under # Nodes. 
Arrows leaving a switch node carry the branch condition as a |label| for readability only — the switch section's bullets are authoritative for branch edges.
Each ## <name> section starts with type: (input, output, decisionTable, expression, switch, function, decision, custom).
Transform settings (passThrough, executionMode, inputField, outputPath) are always written explicitly on decisionTable/expression/decision nodes; <root> means "not set". hitPolicy appears on decisionTable/switch.

## Vocabulary & Node Types
- inputNode: Entry point;
- outputNode: Terminator;
- decisionTableNode: Spreadsheet of input/output rules
- expressionNode: Key-value assignments, evaluated top-to-bottom
- switchNode: Conditional routing — each statement carries a ZEN condition
- functionNode: Async JavaScript handler
- decisionNode: Calls another graph file as a sub-decision

## Transform Attributes & Loop Semantics
Three node types (expressionNode, decisionTableNode, decisionNode) share a transform-attributes block that controls how the node's input/output behaves:
- passThrough: boolean — the authoring tools default this to true (and always write it explicitly; the engine's own default is false). When true, the node's output is merged on top of its input rather than replacing it, so upstream fields remain visible to downstream nodes. Set false only when you explicitly want to drop everything except this node's output.
- outputPath: string — nests the node's output under that key. outputPath: "pricing" turns { total: 50 } into { pricing: { total: 50 } }; the path may be dotted to nest deeper (outputPath: "pricing.discounts" → { pricing: { discounts: { … } } }). Path nesting is applied before any passThrough merge. outputPath is not loop-only: on a single (non-loop) node it nests that node's one output at the key, silently overwriting whatever was there — an outputPath: "results" left on a node after you drop executionMode: loop replaces an upstream results array with the scalar output (verified). Pair the path with the loop; clear it when you remove the loop.
- inputField: string + executionMode: "loop" — iterates over the array at inputField. Each element becomes the node's input for one iteration. Always set outputPath in loop mode — without it the node emits a raw array as its entire output, which replaces the merged upstream object (so sibling fields like carried vanish and any downstream node reading them breaks). With outputPath: "results" the collected array nests under that key and upstream context survives alongside it: { results: [...] }.

Loop mode is the idiomatic way to do per-element work on collections. It's available on expressionNode, decisionTableNode, and decisionNode.
Loop merge semantics (passThrough, per-iteration, outputPath)
Three things interact, and passThrough governs both merges at once (verified):

- passThrough: true (default) — each result element keeps its source element's fields plus the node's computed output ({ ...element, ...computed }), and the upstream context is preserved at the top level alongside the outputPath array. So items: [{v:1}] + carried:"C" through a loop computing doubled yields { carried:"C", items:[...], results:[{ v:1, doubled:2 }] }.
- passThrough: false — each element contains only the node's computed output ({ doubled }, no v), and the top level is just { results: [...] } — upstream context dropped.
- That's the lever for "keep the display fields but drop the carried scaffolding": choose passThrough per what each element should retain, and always pair loop mode with outputPath.

Reaching another node from loop scope — $nodes
Inside loop mode the iteration scope is just the current element; sibling upstream fields are not directly visible. $nodes is the clean answer — it exposes every executed ancestor's output. It is keyed by node name, not id (verified against the engine):

identifier-safe name → dot access: $nodes.riskScore.value
name with spaces/punctuation → bracket access: $nodes['Credit history'].score
read-side dotted paths are null-safe and nest normally: $nodes.financials.totals.netMargin
Works in expression nodes (loop and non-loop), decision-table output cells (loop and non-loop), and decision-table input cells in expression mode. The target must be an executed ancestor of the reading node.

This is the default way to read cross-node state from loop scope, not a last resort. The alternative — carrying an aggregate onto every element upstream — is a workaround for when the value isn't a clean ancestor output.

Prefer a loop-mode expressionNode over a giant inline map
A map(arr as e, { ...huge object literal... }) that enumerates pass-through fields and repeats sub-expressions is hard to read and verify. A loop-mode expressionNode is the preferred shape for per-element computation:

passThrough: true carries unchanged fields — never re-list name: e.name, ….
One expression per derived field, $. chaining earlier lines: allocPct = units / $nodes.totals.unitsTotal, then allocITD = $nodes.pools.poolITD * $.allocPct — compute once, reuse.
$nodes.<name> reaches upstream aggregates from loop scope, so you don't need a single-mode map just to see them.
A downstream node aggregates a field an upstream loop node computed, directly: sum(map(items as r, r.lineTotal)) reads lineTotal once the loop has written it onto each element — no need to carry it onto a separate object first.
Positional / cross-element logic — running totals, "first/last is special", a holistic array rewrite — is where graphs beat policies: loop mode can't do it (it processes elements independently, with no index), so use a single non-loop expressionNode with an index map, map([0..len(items)-1] as i, merge([items[i], { ... }])), which can see neighbours and know which element is last.
Reserve a map object literal for a genuine small reshape (2-3 derived fields, no chaining, no pass-through enumeration).


## Reaching another node from loop scope — $nodes
Inside loop mode, the iteration scope is just the current element. $nodes exposes every executed ancestor's output. It is keyed by node name.
- identifier-safe name → dot access: $nodes.riskScore.value
- name with spaces/punctuation → bracket access: $nodes['Credit history'].score

## Diagnostic / evaluation errors

| Code | Meaning | Fix |
|---|---|---|
| invalidInputCount | Graph lacks exactly one inputNode | Add or dedupe |
| cyclicGraph | Edges form a cycle | Remove the back-edge |
| missingNode | Edge references a nonexistent node | Fix target or add the node |
| NodeError | A node threw at runtime | Inspect trace; fix expression/function/target |
| LoaderError | A decisionNode.key doesn't resolve | Fix the path |
| DepthLimitExceeded | Sub-decision recursion exceeded max depth | Break recursion; lift shared logic |
| Validation | Input/output failed a schema | Adjust payload or schema |

The validation codes surface nested under a top-level InvalidGraph error (source.type for the specific code). Runtime node failures report Node "<name>" failed: <reason>.

"""

CRITICAL_CONSISTENCY_RULES = """
CRITICAL CONSISTENCY RULES:
1. ONE RESPONSIBILITY PER NODE: Give each node a single job, and name it after that job. Split the policy along the seams the business already has - deriving values, validating, scoring, pricing, routing - so a reader can point at the node that owns a rule, and a failing test names the node that broke.
    - A decision table answers ONE question. If a table's outputs serve two different decisions, or a column exists only to carry a value through, that is two nodes.
    - Split a table before it passes ~25 rows.
    - Compute derived values in an `expression` node ahead of the table that uses them, rather than repeating the arithmetic in every cell.
    - Do not add nodes that carry no logic. Decomposition means separating responsibilities, not inserting pass-through steps: a policy that genuinely has one rule is one table, and that is correct.
2. GRAPH LAYOUT & POSITIONING: To ensure the visual graph is readable, you MUST assign a `position: <x>, <y>` property to every node defined in the `# Nodes` section.
    - Anchor: Always place the initial `type: input` node at `position: 100, 200`.
    - Sequential Nodes: Add exactly 300 to the X-axis for each subsequent step (e.g., if Node 1 is at `100, 200`, the next is `400, 200`).
    - Parallel Branches: When a switch node routes to multiple target nodes, place all target nodes at the same X-axis (+300 from the switch), but separate their Y- axis by 400 units (e.g., Branch A at `700, 200`, Branch B at `700, 600`, Branch C at `700, 1000`).
3. VALIDATE ONLY WHAT WAS ASKED FOR: Assume the input matches the schema - never add nodes that re-check its shape or types. But when the requirements state validation rules or "must be rejected if..." conditions, those are business rules: build them with the validation pattern in section 5.5.
4. PREFER ONE OUTPUT NODE: Route terminal paths to a single output node when they all produce the same shape of result. Multiple output nodes are legal and evaluate correctly, so use a separate one only when a branch genuinely ends differently and naming it makes the graph clearer.
5. DEFAULT HIT POLICY: Always use `hitPolicy: first` for decision tables unless the specific requirements explicitly demand multiple rules triggering (e.g., `collect`).
6. NO EXTERNAL GRAPHS: Do NOT use `type: decision` (which is used to call a separate external graph) unless the user specifically asks to call another graph.
7. SWITCH NODE MATCHING: If a node routes traffic to multiple different nodes in your Mermaid diagram (e.g., it has conditional arrows like `RoutingNode - ->|condition| TargetA`), that node MUST be defined with `type: switch` in the `# Nodes` section.
    - NEVER define a routing node as `type: expression`.
    - Expression nodes CANNOT have multiple outgoing conditional edges.
8. SWITCH BULLETS: If you define a `type: switch` node, you MUST provide the routing bullets (e.g., `- condition => TargetA`) and they must perfectly match the targets in your Mermaid diagram.
9. EMPTY CELLS: You are writing the Markdown DSL, not raw JDM JSON, so use `_` for empty decision table cells and `<root>` for empty transform paths. The parser converts both to the empty string the engine expects. (Sections 3.3 and 3.4 describe the JSON side, where the same things are written `""`.)
10. EXPRESSION VARIABLE REFERENCING: Inside a `type: expression` node, expressions are evaluated sequentially within a single expressionNode. If a lower expression of an expressionNode references a key calculated by a higher expression within the *same node*, you MUST use the `$.` prefix (e.g., if line 1 is `fee = 50`, line 2 must be `total = amount + $.fee`). This is not applicable across different expressionNodes.
"""


CRITICAL_DEBUGGING_KNOWLEDGE = """
# GORULES ZEN ENGINE - CRITICAL DEBUGGING KNOWLEDGE
Use this guide to diagnose and fix JSON structure, logic, and evaluation errors in the Zen Engine.

## 1. Core Data Flow & The `passThrough` Concept (Crucial)
Most "missing field" or "undefined" errors occur because data was dropped between nodes.
- **`passThrough: true`**: The node merges its output with the incoming data. Upstream variables remain available to downstream nodes.
- **`passThrough: false`**: The node OUTPUTS ONLY what it explicitly creates. All upstream data is wiped from the current payload.
- **Fixing Data Loss**: If a node complains about a missing parameter, check the node immediately preceding it. If the preceding node has `passThrough: false`, it dropped the data. Change it to `true`.

## 2. Reading the Trace & General Symptoms
- **No output at all / Graph stops prematurely**: A node is disconnected, or a `switchNode` hit a condition that had no outgoing edge.
- **Unexpected `null` values**: The Zen Engine expression language (Unary+) is null-safe and silently returns `null` for missing paths rather than crashing. Trace backward to find exactly which node dropped or misspelled the field. Use the `??` operator (e.g., `amount ?? 0`) for fallbacks.
- **Wrong row matched in Decision Table**: If using `hitPolicy: first`, check the row order. An earlier, broader condition (like `_`) is shadowing a later, more specific condition. Move specific rules to the top.

## 3. Diagnostic Engine Errors & Resolutions

| Error Code | Meaning | How to Fix |
|---|---|---|
| `invalidInputCount` | Graph lacks exactly one `inputNode`. | Ensure there is exactly 1 `type: input` node. |
| `cyclicGraph` | Edges form an infinite loop. | Zen Engine is a Directed Acyclic Graph (DAG). Remove back-edges. |
| `missingNode` | An edge references a `targetId` that does not exist. | Check for typos in your Mermaid diagram edges vs Node names. |
| `NodeError` | A node threw a runtime exception during evaluation. | Inspect the specific node for missing variables, division by zero, or bad syntax. |
| `Validation` | Input or Output failed JSON schema validation. | 1. Ensure the generated test case exactly matches the `inputNode` schema.<br>2. Ensure the graph's final data matches the `outputNode` schema. |
| `ParseError` / `Invalid JSON` | Rust engine failed to parse the JDM string. | DO NOT stringify JSON inside JSON. Ensure `schema` is a valid string, and numbers/booleans are correctly typed. |

## 4. Node-Specific Debugging

### Expression Node (`type: expression`)
- **Variable Referencing Rules**:
    - **Incoming Data**: Parameters passed *into* the node from previous steps are accessed by their raw name (e.g., `order_amount`). DO NOT prefix incoming variables with `$.`.
    - **Locally Derived Data**: Keys created in the *earlier lines of the exact same expression node* MUST be referenced with the `$.` prefix (e.g., `total = order_amount + $.tax`).
    - **Absolute Node Data**: If data was lost, you can bypass the local payload and access a previous node's exact output using `$nodes.<NodeName>. <FieldName>`.
- **NodeError here** usually means a mathematical operation was attempted on a `null` or missing value. Add null-checks or fix upstream `passThrough` settings.

### Decision Table Node (`type: decisionTable`)
- **Type Mismatches**: A frequent cause of `NodeError`. If the input field is a string, the table cell must be quoted (e.g., `"gold"`). If it is a number, it must be unquoted (e.g., `100`, `< 50`).
- **Missing Columns**: If an input column places no condition on a rule, write `_` in that cell of the Markdown DSL - not a blank, and not empty quotes (`""`). The parser converts `_` to the empty string the engine expects, and an explicit `_` keeps the column positions countable by eye.
- **Hit Policy outputs**: Remember that `hitPolicy: collect` outputs an ARRAY of results, whereas `first` outputs a SINGLE object. Downstream nodes must be prepared for arrays if `collect` is used.

### Switch Node (`type: switch`)
- **The "Graph Did Not Halt / No Target" Error**: A switch node evaluates conditions top-to-bottom. If NO condition matches, and there is no default fallback, execution dies silently.
- **The Golden Rule of Switches**: ALWAYS include a default catch-all statement (`- _ => TargetNode`) at the bottom of the switch node to prevent edge-case dead ends.
- **Redundancy**: Each statement should point to a distinct target. If multiple conditions route to the exact same node, combine them into one statement with the `or` keyword (e.g. `tier == 'gold' or tier == 'platinum' => Premium`). ZEN has **no** `||` operator - it fails to lex, with `{"type":"lexerError","source":"Unmatched symbol: |"}`. The logical operators are `and`, `or`, `not`. For a list of alternatives, `tier in ['gold', 'platinum']` reads better than a chain of `or`.

### Function Node (`type: function`)
- **Syntax**: GoRules executes JavaScript in these nodes. The trace will provide the exact line number of the failure.
- **Context Access**: To access incoming data inside the JS function, you must use the `input` object (e.g., `input.order_amount`). If `input.X` is undefined, verify the upstream node's `passThrough` configuration.
"""