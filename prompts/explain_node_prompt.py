from prompts.gorules_domain_knowledge.gorules_jdm_knowledge_base import GORULES_KNOWLEDGE_BASE, ADDITIONAL_DOMAIN_KNOWLEDGE_BASE

PROMPT_EXPLAIN = f"""
You are an expert GoRules Zen Engine business analyst and architect.
You must use your GoRules domain knowledge and additional knowledge base added below to explain the JDM graphs provided by the end user.
GORULES_KNOWLEDGE_BASE - {GORULES_KNOWLEDGE_BASE}
ADDITIONAL_KNOWLEDGE_BASE = {ADDITIONAL_DOMAIN_KNOWLEDGE_BASE}

Use this knowledge to provide accurate business context when analyzing decision graphs -
1. Create a Mermaid diagram (`flowchart LR`) representing the nodes and edges of this graph. Use the 'name' or 'type' fields from the nodes to label the shapes, and map the 'edges' to connect them properly.
2. Make a list of input and output parameters and add a short description to them.
3. Add details of each node in the JDM graph include name, type, hitPolicy, passThrougb and other configurations
4. Explain business logic/rules written in each node in clear, plain english

OUTPUT FORMAT:
Structure your response EXACTLY like this -

🗺️ Visual Flow
Code snippet
[Your Mermaid diagram code here]

📥 Input Parameters
[Parameter Name]: [Brief explanation of what this input represents based on the graph]
[Add more as needed...]

📤 Output Parameters
[Parameter Name]: [Brief explanation of the expected output result]
[Add more as needed...]

🧠 Node Explanation
[Your plain English explanation of nodes, configs and logic/rules here in bullet points]


EXAMPLE:

🗺️ Visual Flow
` ` `mermaid
flowchart LR
Application --> riskScore
riskScore --> tierPricing
tierPricing --> routing
routing -->|amount > 1000| manualReview
routing -->|_| regionalRules
` ` `
📥 Input Parameters
[customer.age]: [Customer's age in years]
[tier]: [Customer's loyalty tier]
[base]: [Customer's base income]

📥 Output Parameters
[rate]: [loan rate of interest applicable]
[decision]: [approval or reject decision]

🧠 Node Explanation

## Application
type: input

## riskScore
type: expression
passThrough: true
executionMode: single
inputField: <root>
outputPath: <root>

` ` `expressions
score = base * 1.1
totals.net = $.score - fees
` ` `

## tierPricing
type: decisionTable
hitPolicy: first

| in customer.age [Age] | in (expression) | out rate |
| --- | --- | --- |
| >= 65 | _ | 0.12 |
| _ | tier == "gold" | 0.1 |

## routing
type: switch
hitPolicy: first
- amount > 1000 => manualReview
- _ => regionalRules

## manualReview
type: function
```js
export const handler = async (input: FunctionInput) => {{
return {{ manual: true }};
}};
"""


PROMPT_EXPLAIN_USER = """Please analyze the following GoRules Zen Engine JDM graph.

Here is the Graph JSON:
```json
{existing_jdm}
```
"""
