"""LLM-backed test-suite generation, shared by the API and the agent."""

from __future__ import annotations

import json
import logging

import anyio
from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)


async def generate_test_suite(content: dict) -> list[dict]:
    """Ask the LLM for a suite of test cases for ``content``.

    Reuses the agent's existing ``PROMPT_TEST`` and marker extraction, so the
    generated shape matches what ``test_node`` has always produced.
    """
    from backend.lang_graph_agent import _extract_bounded_text, call_llm
    from backend.prompts.test_node_prompt import PROMPT_TEST, PROMPT_TEST_USER

    existing_jdm = json.dumps(content)
    user_prompt = PROMPT_TEST_USER.format(existing_jdm=existing_jdm)

    raw = await anyio.to_thread.run_sync(
        lambda: call_llm(PROMPT_TEST, [HumanMessage(content=user_prompt)])
    )
    text = _extract_bounded_text(
        raw, "---TESTS STARTS---", "---TESTS ENDS---", strip_lang="json"
    )
    if not text:
        logger.warning("Test generator returned no delimited block.")
        return []

    try:
        tests = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"The generated test suite was not valid JSON: {exc}") from exc

    if not isinstance(tests, list):
        raise ValueError("The generated test suite was not a JSON array.")

    return [
        {
            "name": t.get("name") or f"Test {i + 1}",
            "input": t.get("input", {}),
            "expectedOutput": t.get("expectedOutput", {}),
            "enabled": True,
            "order": i,
        }
        for i, t in enumerate(tests)
        if isinstance(t, dict)
    ]
