"""What the explanation prompt asks for, and what it no longer does.

The prompt is the deliverable here, so most of this checks the prompt rather than a
generated reply - a stubbed model tells you nothing about whether prose reads well, and a
live one is not something a test suite should depend on. The live checks were run by hand
against both shipped policies while this was written; what is pinned here is the contract
that produced them.

Three things changed. The mermaid diagram is gone, because the reader is looking at the
graph on a canvas while they read the explanation and redrawing it in ASCII pushed the
actual answer below the fold. Configuration key names are gone, because `hitPolicy: first`
is implementation trivia to someone asking what their policy does - the behaviour it causes
stays, in words. And it now opens by saying what the policy is *for*, rather than listing
field names to a reader who does not yet know what they are looking at.
"""

from __future__ import annotations

import json

import pytest

from backend.prompts.explain_node_prompt import PROMPT_EXPLAIN, PROMPT_EXPLAIN_USER

# The prompt injects knowledge-base sections that legitimately teach `hitPolicy`,
# `passThrough` and mermaid - they are how the format works. What is under test is the
# instructions written on top of them, so every check below reads only that part.
INSTRUCTIONS = PROMPT_EXPLAIN.split("WHAT TO COVER", 1)[1]


def flat(text: str) -> str:
    """Line wrapping is not meaningful here; a phrase split across two lines is the same
    phrase."""
    return " ".join(text.split())


# ------------------------------------------------------------------ what it asks for

def test_it_does_not_ask_for_a_diagram():
    """The canvas is already showing one."""
    assert "mermaid" not in INSTRUCTIONS.lower()
    assert "Visual Flow" not in INSTRUCTIONS
    assert "flowchart" not in INSTRUCTIONS


@pytest.mark.parametrize("key", ["hitPolicy", "passThrough", "outputPath", "executionMode"])
def test_configuration_keys_are_only_ever_forbidden_never_requested(key):
    """The old point 3 asked for these by name and the model obliged, which is what made
    the explanation read like a dump of the file. They may still appear in the prompt -
    forbidding `hitPolicy: first` means writing it down - but never in the part that says
    what to produce.
    """
    wanted = INSTRUCTIONS.split("OUTPUT FORMAT", 1)[1]
    assert key not in wanted, "not in the format or the worked example"

    for line in INSTRUCTIONS.splitlines():
        if key in line:
            assert "not " in line or "Do not" in line, (
                f"{key} appears outside a prohibition: {line.strip()!r}"
            )


def test_it_says_what_to_write_instead_of_configuration():
    """Removing the keys is only half of it - the behaviour they cause is the part a policy
    owner actually needs, so the prompt has to ask for that in words."""
    assert "first matching row wins" in flat(INSTRUCTIONS)
    assert "still available further down" in flat(INSTRUCTIONS)


def test_the_format_opens_by_saying_what_the_policy_is_for():
    headings = [line.strip() for line in INSTRUCTIONS.splitlines()
                if line.startswith(("📋", "📥", "📤", "⚙"))]

    # Each heading appears twice: once in the format, once in the worked example - and the
    # example has to agree with the format, or the model follows the example.
    assert headings[:4] == headings[4:8]
    assert headings[:4] == [
        "📋 What this policy decides",
        "📥 What it needs",
        "📤 What it returns",
        "⚙️ How it decides",
    ], "purpose first, then the contract, then the rules"


def test_it_still_forbids_pasting_the_file_back():
    lowered = flat(INSTRUCTIONS).lower()
    assert "never paste jdm json" in lowered
    assert "never emit the authoring dsl" in lowered


def test_it_asks_for_the_real_values_not_a_generic_description():
    """An explanation that could describe any refund policy describes none."""
    assert "thresholds" in INSTRUCTIONS
    assert "could describe any refund policy describes none" in flat(INSTRUCTIONS)


def test_the_worked_example_obeys_its_own_rules():
    """The example is the strongest instruction in any prompt. When it contradicted the
    rules, the model followed the example - so it has to be clean."""
    example = INSTRUCTIONS.split("EXAMPLE:", 1)[1]

    assert "```" not in example, "no fenced blocks, which is where a diagram would go"
    for key in ("hitPolicy", "passThrough"):
        assert key not in example
    assert "first match wins" in flat(example), "the behaviour, in words"


def test_the_user_template_still_carries_the_graph():
    assert "{existing_jdm}" in PROMPT_EXPLAIN_USER


# ------------------------------------------------------------------- the plumbing

def test_the_node_does_not_paste_the_file_back_around_the_answer(monkeypatch):
    """The prompt was only half of it.

    `explain_node` wrapped every reply in a Streamlit-era shell: a `<details>` block
    holding the whole JDM file, a "Logic Explanation:" label, and the lot indented four
    spaces - which markdown reads as a code block, so the explanation rendered as
    preformatted text and the prose escaped only by accident. Fixing the prompt while that
    stood would have changed nothing anyone could see.
    """
    from backend import lang_graph_agent as agent

    monkeypatch.setattr(agent, "_dispatch",
                        lambda *_a, **_k: "📋 What this policy decides\nIt decides refunds.")
    graph = json.dumps({"contentType": "application/vnd.gorules.decision",
                        "nodes": [{"id": "i", "name": "Request", "type": "inputNode",
                                   "content": {"schema": ""}}],
                        "edges": []})

    body = agent.explain_node(
        {"existing_jdm_json": graph, "selected_file": "Refund Policy"}
    )["messages"][0].content

    assert "```json" not in body, "the file is not pasted back"
    assert "<details>" not in body and "Raw JDM" not in body
    assert "Logic Explanation" not in body
    assert not any(line.startswith("    ") for line in body.splitlines()), (
        "four-space indentation turns the whole reply into a code block"
    )
    assert body.startswith("### Refund Policy"), "named, then answered"
    assert "It decides refunds." in body


def test_the_node_still_runs_and_returns_a_message(monkeypatch):
    """Format aside, `explain_node` has to keep working - it injects the graph into the
    user template and wraps the reply as a chat message."""
    from backend import lang_graph_agent as agent

    seen = {}

    def dispatch(sys_prompt, messages):
        seen["system"] = sys_prompt
        seen["user"] = messages[-1].content
        return "📋 What this policy decides\nIt decides things."

    monkeypatch.setattr(agent, "_dispatch", dispatch)

    graph = json.dumps({"contentType": "application/vnd.gorules.decision",
                        "nodes": [{"id": "i", "name": "Request", "type": "inputNode",
                                   "content": {"schema": ""}}],
                        "edges": []})
    result = agent.explain_node({"existing_jdm_json": graph, "canvas_jdm_json": graph})

    assert seen["system"] is PROMPT_EXPLAIN
    assert "Request" in seen["user"], "the graph reached the model"
    assert "It decides things." in result["messages"][0].content
