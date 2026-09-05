"""What each provider says about a call, and whether any of it survives.

All four paths used to return `response.choices[0].message.content` (or `response.text`)
and discard everything around it: how many tokens it cost, which model actually answered,
and the id the provider needs to look the generation up again. The information was always
in the response - nothing here asks the provider for anything new.

These build real SDK objects (`ChatCompletion`, `GenerateContentResponse`) rather than
mocks, so the extractors are checked against the schemas they will actually meet. A mock
would happily return whatever attribute name I guessed.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from google.genai import types
from langchain_core.messages import HumanMessage
from openai.types.chat import ChatCompletion

from backend import lang_graph_agent as agent
from backend.corpus import store


def rows(table: str = "samples") -> list[dict]:
    conn = sqlite3.connect(store.settings.corpus_db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


def chat_completion(**overrides) -> ChatCompletion:
    """A response in the shape LiteLLM and Hugging Face's router both return."""
    body = {
        "id": "gen-abc123",
        "object": "chat.completion",
        "created": 1,
        "model": "served/model-v2",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "a design"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33,
                  "completion_tokens_details": {"reasoning_tokens": 7}},
    }
    body.update(overrides)
    return ChatCompletion.model_validate(body)


class FakeCompletions:
    def __init__(self, response):
        self._response = response
        self.seen: dict = {}

    def create(self, **kwargs):
        self.seen = kwargs
        return self._response


class FakeOpenAIClient:
    def __init__(self, response):
        self.chat = type("chat", (), {"completions": FakeCompletions(response)})()


# ----------------------------------------------------------------- openrouter

class FakeHTTPResponse:
    def __init__(self, body: dict, status: int = 200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")


@pytest.fixture
def openrouter(monkeypatch):
    """Configure the OpenRouter path and stub the single HTTP call it makes."""
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", "openrouter")
    monkeypatch.setattr(agent, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(agent, "OPENROUTER_MODEL_NAME", "asked-for/model:free")
    monkeypatch.setattr(agent, "LLM_INIT_ERROR", None)

    def install(body: dict):
        captured: dict = {}

        def post(*args, **kwargs):
            captured.update(kwargs)
            return FakeHTTPResponse(body)

        monkeypatch.setattr(agent.requests, "post", post)
        return captured

    return install


OPENROUTER_BODY = {
    "id": "gen-9f2b",
    # Not the model that was asked for: OpenRouter routes, and on the free tier the
    # substitution can differ between two calls in the same turn.
    "model": "actually-served/model-v3",
    "choices": [{"message": {"role": "assistant", "content": "a design",
                             "reasoning_details": [{"type": "reasoning.text",
                                                    "text": "two bands, so a switch"}]}}],
    # Which upstream actually ran it, behind the gateway.
    "provider": "DeepInfra",
    "usage": {"prompt_tokens": 12000, "completion_tokens": 540, "total_tokens": 12540,
              "cost": 0.00214,
              "completion_tokens_details": {"reasoning_tokens": 310,
                                            "image_tokens": 0, "audio_tokens": 0}},
}


def test_openrouter_records_the_model_that_actually_served(openrouter):
    openrouter(OPENROUTER_BODY)

    result = agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert result.meta.model == "actually-served/model-v3"
    sample, = rows()
    assert sample["model_requested"] == "asked-for/model:free"
    assert sample["model_served"] == "actually-served/model-v3", (
        "attributing a sample to the model that was asked for rather than the one that "
        "answered is how a fine-tune ends up trained on another model's output"
    )


def test_openrouter_usage_and_generation_id_are_kept(openrouter):
    """All of it arrived in every response and all of it was discarded by
    `raise_for_status` and a `.get("content")`."""
    openrouter(OPENROUTER_BODY)

    agent.call_llm("SYS", [HumanMessage(content="x")], node="builder_node")

    sample, = rows()
    assert sample["prompt_tokens"] == 12000
    assert sample["completion_tokens"] == 540
    assert sample["reasoning_tokens"] == 310
    assert sample["generation_id"] == "gen-9f2b"


def test_openrouter_still_carries_its_reasoning(openrouter):
    openrouter(OPENROUTER_BODY)

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert json.loads(rows()[0]["reasoning_json"])[0]["text"] == "two bands, so a switch"


def test_a_null_completion_is_empty_not_the_string_None(openrouter):
    """A reply that is all reasoning and no text answers with a null content.
    `str.__new__(cls, None)` would hand the four characters "None" downstream to be
    parsed as a design.
    """
    openrouter({**OPENROUTER_BODY,
                "choices": [{"message": {"role": "assistant", "content": None}}]})

    result = agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert str(result) == ""


def test_the_api_key_never_reaches_the_corpus(openrouter):
    """Capture records the payload, never the headers - and the OpenRouter path builds an
    `Authorization: Bearer` header right beside the payload it sends."""
    captured = openrouter(OPENROUTER_BODY)

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert "test-key" in json.dumps(captured.get("headers")), "the call really was signed"
    everything = json.dumps(rows()) + json.dumps(rows("runs")) + json.dumps(rows("prompts"))
    assert "test-key" not in everything
    assert "Authorization" not in everything


# ------------------------------------------------------- openai-compatible paths

@pytest.mark.parametrize("provider,attribute,configure", [
    ("litellm", "litellm_client", {"LITELLM_MODEL_NAME": "asked-for/model"}),
    ("huggingface", "huggingface_client", {"HUGGINGFACE_MODEL_NAME": "asked-for/model"}),
])
def test_an_openai_compatible_provider_reports_its_usage(
    provider, attribute, configure, monkeypatch
):
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", provider)
    monkeypatch.setattr(agent, "LLM_INIT_ERROR", None)
    for name, value in configure.items():
        monkeypatch.setattr(agent, name, value)
    monkeypatch.setattr(agent, attribute, FakeOpenAIClient(chat_completion()))

    result = agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert str(result) == "a design"
    sample, = rows()
    assert sample["provider"] == provider
    assert sample["model_served"] == "served/model-v2"
    assert sample["generation_id"] == "gen-abc123"
    assert (sample["prompt_tokens"], sample["completion_tokens"]) == (11, 22)
    assert sample["reasoning_tokens"] == 7


@pytest.mark.parametrize("key", ["reasoning_content", "reasoning", "reasoning_details"])
def test_reasoning_is_found_under_whichever_name_the_provider_chose(key, monkeypatch):
    """Not part of the OpenAI schema, so every provider picked its own key and the SDK
    parks all of them in `model_extra`."""
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", "litellm")
    monkeypatch.setattr(agent, "LLM_INIT_ERROR", None)
    monkeypatch.setattr(agent, "LITELLM_MODEL_NAME", "m")
    response = chat_completion(choices=[{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "a design", key: "thinking out loud"},
    }])
    monkeypatch.setattr(agent, "litellm_client", FakeOpenAIClient(response))

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert json.loads(rows()[0]["reasoning_json"]) == "thinking out loud"


def test_a_provider_that_omits_usage_records_unknown_rather_than_crashing(monkeypatch):
    """`usage` is optional in the OpenAI schema and several endpoints behind the Hugging
    Face router leave it out. A missing block must mean "unknown", never an exception on
    the path of every model call."""
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", "huggingface")
    monkeypatch.setattr(agent, "LLM_INIT_ERROR", None)
    monkeypatch.setattr(agent, "HUGGINGFACE_MODEL_NAME", "m")
    monkeypatch.setattr(agent, "huggingface_client",
                        FakeOpenAIClient(chat_completion(usage=None)))

    result = agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert str(result) == "a design"
    sample, = rows()
    assert sample["prompt_tokens"] is None
    assert sample["completion_tokens"] is None
    assert sample["model_served"] == "served/model-v2", "what was there is still kept"


# ----------------------------------------------------------------------- gemini

class FakeGeminiModels:
    def __init__(self, response):
        self._response = response

    def generate_content(self, **kwargs):
        return self._response


def gemini_response(**overrides) -> types.GenerateContentResponse:
    body = {
        "model_version": "gemini-2.5-flash-002",
        "response_id": "resp-xyz",
        "candidates": [{"content": {"role": "model", "parts": [
            {"text": "Two bands, so a switch.", "thought": True},
            {"text": "# Structure"},
        ]}}],
        "usage_metadata": {"prompt_token_count": 100, "candidates_token_count": 40,
                           "thoughts_token_count": 15, "total_token_count": 155},
    }
    body.update(overrides)
    return types.GenerateContentResponse.model_validate(body)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", "gemini")
    monkeypatch.setattr(agent, "LLM_INIT_ERROR", None)
    monkeypatch.setattr(agent, "GOOGLE_MODEL", "gemini-flash-latest")

    def install(response):
        monkeypatch.setattr(agent, "gemini_client",
                            type("c", (), {"models": FakeGeminiModels(response)})())

    return install


def test_gemini_reports_its_usage(gemini):
    gemini(gemini_response())

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    sample, = rows()
    assert sample["prompt_tokens"] == 100
    assert sample["completion_tokens"] == 40
    # `thoughts_token_count` records that thinking happened even when the text is not
    # returned, which is the usual case since this code does not request thought parts.
    assert sample["reasoning_tokens"] == 15
    assert sample["generation_id"] == "resp-xyz"


def test_gemini_records_the_version_an_alias_resolved_to(gemini):
    """`gemini-flash-latest` is a moving target. Which dated model answered is the whole
    difference between a reproducible sample and an undated one."""
    gemini(gemini_response())

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    sample, = rows()
    assert sample["model_requested"] == "gemini-flash-latest"
    assert sample["model_served"] == "gemini-2.5-flash-002"


def test_gemini_thinking_is_captured_when_the_model_returns_it(gemini):
    gemini(gemini_response())

    result = agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert json.loads(rows()[0]["reasoning_json"]) == [
        {"type": "reasoning.text", "text": "Two bands, so a switch."}
    ]
    assert str(result) == "# Structure", "a thought part is not part of the answer"


def test_gemini_without_thoughts_records_none(gemini):
    gemini(gemini_response(candidates=[
        {"content": {"role": "model", "parts": [{"text": "# Structure"}]}}
    ]))

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert rows()[0]["reasoning_json"] is None


def test_what_a_call_cost_is_recorded(openrouter):
    """Checked against a live response: OpenRouter puts the cost in the body, so knowing
    what a run cost needs no second call to the /generation endpoint. That matters
    directly for budgeting an evaluation on a paid tier."""
    openrouter(OPENROUTER_BODY)

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    assert rows()[0]["cost"] == pytest.approx(0.00214)


def test_the_upstream_that_ran_the_model_is_recorded(openrouter):
    """`provider` says `openrouter`, which is only the gateway. Two samples from one model
    name are not necessarily from the same deployment of it."""
    openrouter(OPENROUTER_BODY)

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    sample, = rows()
    assert sample["provider"] == "openrouter"
    assert sample["upstream_provider"] == "DeepInfra"


def test_a_provider_that_reports_no_cost_leaves_it_unknown(monkeypatch):
    """Only OpenRouter reports cost. A zero here would read as a free call rather than an
    unmeasured one."""
    monkeypatch.setattr(agent, "ACTIVE_PROVIDER", "litellm")
    monkeypatch.setattr(agent, "LLM_INIT_ERROR", None)
    monkeypatch.setattr(agent, "LITELLM_MODEL_NAME", "m")
    monkeypatch.setattr(agent, "litellm_client", FakeOpenAIClient(chat_completion()))

    agent.call_llm("SYS", [HumanMessage(content="x")], node="planner_node")

    sample, = rows()
    assert sample["cost"] is None
    assert sample["upstream_provider"] is None


def test_a_corpus_from_an_earlier_build_gains_the_new_columns(tmp_path, monkeypatch):
    """`schema.sql` creates tables with IF NOT EXISTS, which does nothing to a table that
    already exists - so without an explicit migration a corpus started before these columns
    existed would keep its old shape and every insert into it would fail.

    The "old" schema is the real one with the added columns stripped out, rather than a
    hand-written imitation, so this keeps testing the actual upgrade path as later phases
    extend the table.
    """
    added = store._ADDED_COLUMNS["samples"]
    previous = "\n".join(
        line for line in store.SCHEMA_FILE.read_text(encoding="utf-8").splitlines()
        if not any(line.strip().startswith(name + " ") for name in added)
    )

    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(previous)
    assert {r[1] for r in conn.execute("PRAGMA table_info(samples)")}.isdisjoint(added), (
        "the fixture must actually be missing the columns, or this proves nothing"
    )
    conn.execute(
        "INSERT INTO samples (sample_id, node, messages_json, created_at)"
        " VALUES ('s1', 'planner_node', '[]', 'then')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(store, "settings",
                        store.settings.model_copy(update={"corpus_db_path": str(old_db)}))
    store.reset_for_tests()
    store._connect()

    conn = sqlite3.connect(old_db)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(samples)")}
    kept = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()

    assert set(added) <= columns
    assert kept == 1, "the migration must not disturb what is already recorded"


def test_the_upgraded_corpus_actually_accepts_a_write(tmp_path, monkeypatch):
    """The columns existing is not the same as an insert working, and an insert that names
    a column the table lacks is exactly the failure the migration exists to prevent."""
    added = store._ADDED_COLUMNS["samples"]
    previous = "\n".join(
        line for line in store.SCHEMA_FILE.read_text(encoding="utf-8").splitlines()
        if not any(line.strip().startswith(name + " ") for name in added)
    )
    old_db = tmp_path / "old.db"
    conn = sqlite3.connect(old_db)
    conn.executescript(previous)
    conn.close()

    monkeypatch.setattr(store, "settings",
                        store.settings.model_copy(update={"corpus_db_path": str(old_db)}))
    store.reset_for_tests()

    sample_id = store.record_sample(
        node="planner_node", system_prompt="SYS", messages=[],
        completion="a design", cost=0.5, upstream_provider="DeepInfra",
    )

    assert sample_id is not None, "the write was swallowed by _never_fails"
    conn = sqlite3.connect(old_db)
    row = conn.execute("SELECT cost, upstream_provider FROM samples").fetchone()
    conn.close()
    assert row == (0.5, "DeepInfra")
