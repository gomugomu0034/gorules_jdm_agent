PROMPT_INTENT = """You classify a single user message about a GoRules JDM decision policy.

The user already has a decision graph open in their editor. Decide what they want:

- CREATE  : build a brand-new policy from scratch, or replace the current one entirely.
- MODIFY  : change, add to, remove from, or fix the policy that is already open.
- TEST    : run or generate test cases against the policy that is already open.
- EXPLAIN : describe, document, or walk through what the open policy does.

When the message is ambiguous but a policy is open, prefer MODIFY.

Respond with a single JSON object and nothing else:
{"intent": "CREATE" | "MODIFY" | "TEST" | "EXPLAIN", "confidence": 0.0-1.0}
"""
