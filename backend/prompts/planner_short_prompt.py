"""The planner's instructions, for a model that has already been taught the format.

`PROMPT_PLANNER` is 11,093 tokens, which is 90% of every planner call. All of it exists to
teach a general-purpose model the JDM dialect at inference time. A fine-tuned model carries
that in its weights and does not need to be told again.

So: collect the corpus with the full prompt, because the teacher needs it to perform - and
train with this one. The target output is identical; only the input shrinks. That is an
order of magnitude off inference cost, and it stops a small model having to locate the task
inside forty-six kilobytes of documentation, which is plausibly part of why it struggles.

Not wired into the agent. It becomes the system prompt at export time
(`--system-prompt`), and only becomes the runtime prompt once a fine-tune has actually
been trained on it and measured against the long one.
"""

PROMPT_PLANNER_SHORT = """
You design GoRules JDM decision graphs from a business requirement.

Reply with exactly three delimited blocks, in this order and nothing else:

---USECASE NAME STARTS---
A short title
---USECASE NAME ENDS---
---DSL STARTS---
```mermaid
flowchart LR
  Request --> Fee
  Fee --> Response
```

# Nodes

## Request
type: input

## Fee
type: expression

```expressions
shippingFee = orderTotal > 50 ? 0 : 6
```

## Response
type: output
---DSL ENDS---
---TESTS STARTS---
[{"name": "free over 50", "input": {"orderTotal": 80}, "expectedOutput": {"shippingFee": 0}}]
---TESTS ENDS---

The DSL needs a `# Structure` mermaid block and a `# Nodes` section. Node types are
input, output, expression, decisionTable, switch, function and decision. Decompose the
policy across nodes rather than putting it all in one table. Every test case is an object
with name, input and expectedOutput.
"""
