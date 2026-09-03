"""What the assistant says in chat, as opposed to what it puts on the canvas.

The chat must carry prose. The graph travels separately as a `graph_proposed`
event, and the builder's raw retry conversation is for the model only.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from backend.lang_graph_agent import output_node
from backend.services.chat_runner import _is_internal, _new_messages

GRAPH = {
    "nodes": [
        {"id": "i", "name": "Request", "type": "inputNode"},
        {"id": "d", "name": "Discount Rules", "type": "decisionTableNode"},
        {"id": "o", "name": "Response", "type": "outputNode"},
    ]
}


def text_of(result: dict) -> str:
    return str(result["messages"][0].content)


def test_success_summary_carries_no_json():
    body = text_of(
        output_node(
            {
                "usecase_name": "Ticket Discount Policy",
                "build_status": "SUCCESS",
                "jdm_json": json.dumps(GRAPH),
                "test_suite_json": json.dumps([{"name": "a"}, {"name": "b"}]),
            }
        )
    )
    assert "Ticket Discount Policy" in body
    assert "3 nodes" in body and "2 tests" in body
    assert "Decision Rules" not in body
    # The whole point: no serialised graph in the conversation.
    assert "```json" not in body
    assert '"nodes"' not in body
    assert "<details>" not in body


def test_a_failed_build_is_reported_as_a_failure():
    """Eight exhausted attempts must not be announced as success.

    The canvas stays empty in that case, so a cheerful summary would be a
    straight contradiction of what the user is looking at.
    """
    result = output_node(
        {
            "usecase_name": "Ticket Discount Policy",
            "build_status": "ERROR",
            "jdm_json": "{}",
            "test_suite_json": json.dumps([{"name": "a"}] * 8),
            "evaluation_feedback": "expected 20 but got null",
        }
    )
    body = text_of(result)

    assert result["build_failed"] is True
    assert "could not build" in body.lower()
    assert "all passing" not in body.lower()
    assert "expected 20 but got null" in body


def test_a_cancelled_build_says_nothing_changed():
    result = output_node({"build_status": "CANCELLED", "jdm_json": "{}"})
    assert result["build_failed"] is True
    assert "Nothing was changed" in text_of(result)


def test_success_with_an_empty_graph_is_still_a_failure():
    """A SUCCESS status with no nodes cannot be shown as a built graph."""
    result = output_node(
        {"build_status": "SUCCESS", "jdm_json": json.dumps({"nodes": []})}
    )
    assert result["build_failed"] is True


def test_internal_flag_hides_a_reply_whatever_its_shape():
    """The flag is the reliable signal; content markers are only a fallback.

    The model does not always wrap its answer in the markers it was asked for,
    so filtering on shape alone let a "## Markdown DSL" dump through.
    """
    from backend.lang_graph_agent import _assistant_message_from_llm

    tagged = _assistant_message_from_llm(
        "## Some heading\n\nprose that looks perfectly ordinary", internal=True
    )
    assert _is_internal(tagged)

    delta = {"messages": [tagged, AIMessage(content="Real answer.", id="2")]}
    assert [m["content"] for m in _new_messages(delta, set())] == ["Real answer."]


def test_builder_retry_dumps_never_reach_the_chat():
    raw = (
        "---USECASE NAME STARTS---\nTicket Discount Policy\n---USECASE NAME ENDS---\n"
        "---DSL STARTS---\n```json\n{\"nodes\": [{\"id\": \"input-1\"}]}\n```\n---DSL ENDS---"
    )
    assert _is_internal(raw)

    delta = {
        "messages": [
            AIMessage(content=raw, id="1"),
            AIMessage(content="Built **Ticket Discount Policy** - 3 nodes.", id="2"),
        ]
    }
    emitted = _new_messages(delta, set())

    assert [m["content"] for m in emitted] == [
        "Built **Ticket Discount Policy** - 3 nodes."
    ]


def test_ordinary_prose_is_not_mistaken_for_internal_output():
    assert not _is_internal("Here is what this policy does, in plain English.")
    assert not _is_internal("I could not build a working graph for **X**.")
