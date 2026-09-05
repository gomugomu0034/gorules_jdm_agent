import time
import json
import re
import os
import tempfile
from pathlib import Path
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TypedDict, Annotated
from dotenv import load_dotenv
import requests

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt
from langchain_core.messages import HumanMessage, AIMessage

try:
    from langgraph.config import get_stream_writer
except ImportError:  # pragma: no cover - older langgraph
    get_stream_writer = None

# Provider SDKs
from google import genai
from google.genai import types
import openai

# Imports for your tools/prompts
from backend.prompts.triage_node_prompt import PROMPT_TRIAGE
from backend.prompts.planner_node_prompt import PROMPT_PLANNER
from backend.prompts.builder_node_prompt import PROMPT_BUILDER
from backend.prompts.modify_triage_node_prompt import PROMPT_MODIFY_TRIAGE
from backend.prompts.explain_node_prompt import PROMPT_EXPLAIN, PROMPT_EXPLAIN_USER
from backend.prompts.test_node_prompt import PROMPT_TEST, PROMPT_TEST_USER, PROMPT_TEST_REPORT
from backend.prompts.intent_router_prompt import PROMPT_INTENT
from backend.prompts.patch_node_prompt import PROMPT_PATCH, PROMPT_PATCH_USER

from backend import corpus
from backend.tools.markdown_dsl_parser import DslError, parse_markdown_dsl
from backend.tools.diagnostics import (
    KIND_HEADINGS,
    Diagnostic,
    format_for_llm,
    parse_engine_error,
)
from backend.tools.jdm_linter import blocking, lint
from backend.tools.jdm_patch import PatchError, apply_patch, describe as describe_patch
from backend.tools.zen_evaluator import check_jdm_format, evaluate, run_test_suite





# ==========================================
# 1. CONFIGURATION
# ==========================================
# Load backend/.env explicitly. A bare load_dotenv() searches upward from the
# process cwd, which misses it whenever the app is started from the repo root.
load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv()  # allow a repo-root .env to override for local overrides

# Select the active provider: "gemini", "litellm", "huggingface", or "openrouter"
ACTIVE_PROVIDER = os.getenv("LLM_PROVIDER", "huggingface").lower()

# Every provider gets the same ceiling; a hung call would otherwise stall a run
# until the agent-level wall-clock budget fires.
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT", "120"))

# Generating a delimited DSL is a format-following task, not a creative one, and the repair
# loop depends on the same input producing the same output. Only Gemini pinned this before;
# the other three ran at whatever the provider defaults to.
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0"))

# The whole-run budget enforced by `asyncio.wait_for` in chat_runner. Mirrors
# `settings.agent_run_timeout`; the builder needs it to size its repair loop.
AGENT_RUN_TIMEOUT_SECONDS = int(os.getenv("AGENT_RUN_TIMEOUT", "900"))


# How many times the planner may be asked for a design before the turn gives up. A plan
# that never arrives cannot be repaired by the builder, so retrying is the planner's job -
# but only briefly: at temperature 0 the model is deterministic, so a retry is worth making
# exactly as long as the input to it keeps changing.
MAX_PLAN_ATTEMPTS = 2


def _max_build_attempts(planner_calls: int = 1) -> int:
    """How many builder passes fit inside the run's wall clock.

    Attempt 0 spends no LLM call, so N attempts cost N-1 calls. The old fixed ceiling of 8
    could spend 960s against a 900s run budget - and when that budget fires, `asyncio.wait_for`
    discards the turn while this node has checkpointed nothing, losing every repair it made.
    Leave room for the planner calls that precede the loop and the reporting that follows -
    `planner_calls` is however many the turn actually spent, since a re-plan costs one more.
    """
    usable = AGENT_RUN_TIMEOUT_SECONDS - max(1, planner_calls) * LLM_TIMEOUT_SECONDS
    return max(2, min(8, 1 + int(usable * 0.8) // max(LLM_TIMEOUT_SECONDS, 1)))

# Gemini Config
GOOGLE_API_KEY = os.getenv("GOOGLE_AI_API_KEY")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL_NAME")

# LiteLLM Config
LITELLM_BASE_URL = os.getenv("LITELLM_BASE_URL")
LITELLM_API_KEY = os.getenv("LITELLM_API_KEY")
LITELLM_MODEL_NAME = os.getenv("LITELLM_MODEL_NAME")

# Hugging Face Inference Providers config. Together is used through Hugging Face
# Routing, so this must be a Hugging Face token (not a Together API key).
HUGGINGFACE_API_KEY = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_API_KEY")
HUGGINGFACE_MODEL_NAME = os.getenv("HF_MODEL_NAME") or os.getenv("HUGGINGFACE_MODEL_NAME")
HUGGINGFACE_INFERENCE_PROVIDER = os.getenv("HF_INFERENCE_PROVIDER", "together")
HUGGINGFACE_BASE_URL = (
    os.getenv("HF_BASE_URL")
    or os.getenv("HUGGINGFACE_BASE_URL")
    or "https://router.huggingface.co/v1"
)

# OpenRouter config
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL_NAME = os.getenv("OPENROUTER_MODEL_NAME") or os.getenv("OPENROUTER_MODEL")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_REASONING_ENABLED = os.getenv("OPENROUTER_REASONING_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL")
OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME")

# Initialize Clients Conditionally (so it doesn't crash if one key is missing)
gemini_client = None
litellm_client = None
huggingface_client = None

LLM_INIT_ERROR = None

try:
    if ACTIVE_PROVIDER == "gemini":
        gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    elif ACTIVE_PROVIDER == "litellm":
        litellm_client = openai.OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)
    elif ACTIVE_PROVIDER == "huggingface":
        huggingface_client = openai.OpenAI(
            base_url=HUGGINGFACE_BASE_URL,
            api_key=HUGGINGFACE_API_KEY,
        )
    elif ACTIVE_PROVIDER == "openrouter":
        pass
    else:
        raise ValueError(f"Unsupported LLM_PROVIDER: {ACTIVE_PROVIDER}")
except Exception as _exc:  # noqa: BLE001
    # A missing key must not take down the whole API. Everything that does not
    # need the LLM - the editor, simulation, tests, import/export - keeps working,
    # and call_llm raises with this message when the agent is actually used.
    LLM_INIT_ERROR = f"LLM provider {ACTIVE_PROVIDER!r} is not configured: {_exc}"
    print(f"[Config] {LLM_INIT_ERROR}")


# ==========================================
# 2. UNIFIED LLM WRAPPER
# ==========================================


@dataclass(frozen=True)
class CallMetadata:
    """What the provider said about a call, beyond the text it returned.

    Every provider already sends this and every provider path used to throw it away. It is
    what makes a corpus row answerable: which model actually produced this, what did it
    cost, and can I look the generation up with the provider later.

    `model` is the model that *served* the request, which is not always the one asked for -
    OpenRouter routes, and Gemini resolves an alias like `gemini-flash-latest` to a dated
    version. Recording only the request would attribute a sample to the wrong model.
    """

    model: str = ""
    generation_id: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    # Only OpenRouter reports these: which upstream actually ran the model, and what the
    # call cost. The others leave both unset rather than have them guessed at.
    upstream_provider: str = ""
    cost: float | None = None


class LLMResponse(str):
    """String-like LLM output that can carry provider metadata alongside content."""

    def __new__(cls, content: str | None, reasoning_details=None,
                meta: "CallMetadata | None" = None):
        # `content or ""` because a provider may answer with a null content - a pure
        # refusal, or a reply that was all reasoning and no text. `str.__new__(cls, None)`
        # would quietly produce the four-character string "None" and hand it downstream to
        # be parsed as a design.
        obj = str.__new__(cls, content or "")
        obj.reasoning_details = reasoning_details
        obj.meta = meta or CallMetadata()
        return obj


def _int(value) -> int | None:
    """Token counts arrive as ints, as None, and occasionally as strings."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _openai_metadata(response) -> CallMetadata:
    """Metadata from an OpenAI-shaped response: LiteLLM and Hugging Face's router.

    Every field is read defensively. `usage` is optional in the OpenAI schema and several
    community endpoints behind the HF router omit it, so a missing block has to mean
    "unknown", never an exception on the path of every model call.
    """
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return CallMetadata(
        model=getattr(response, "model", "") or "",
        generation_id=getattr(response, "id", "") or "",
        prompt_tokens=_int(getattr(usage, "prompt_tokens", None)),
        completion_tokens=_int(getattr(usage, "completion_tokens", None)),
        reasoning_tokens=_int(getattr(details, "reasoning_tokens", None)),
    )


def _openai_reasoning(message):
    """The thinking trace, under whichever name the provider chose for it.

    Not part of the OpenAI schema, so the SDK parks it in `model_extra`. Providers have
    each picked a different key: `reasoning_content` (DeepSeek), `reasoning` (several
    others), `reasoning_details` (OpenRouter's shape, which some proxies mirror).
    """
    extra = getattr(message, "model_extra", None) or {}
    for key in ("reasoning_details", "reasoning_content", "reasoning"):
        value = extra.get(key)
        if value:
            return value
    return None


def _gemini_metadata(response) -> CallMetadata:
    usage = getattr(response, "usage_metadata", None)
    return CallMetadata(
        model=getattr(response, "model_version", "") or "",
        generation_id=getattr(response, "response_id", "") or "",
        prompt_tokens=_int(getattr(usage, "prompt_token_count", None)),
        completion_tokens=_int(getattr(usage, "candidates_token_count", None)),
        reasoning_tokens=_int(getattr(usage, "thoughts_token_count", None)),
    )


def _gemini_reasoning(response):
    """Gemini returns thinking as ordinary parts flagged `thought=True`.

    Only present when the model is asked for them, which this code deliberately does not
    do: `thinking_config` is rejected outright by models that do not support thinking, and
    silently breaking the Gemini path to collect telemetry is the wrong trade. The
    `thoughts_token_count` in usage still records that thinking happened either way.
    """
    thoughts = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "thought", False) and getattr(part, "text", None):
                thoughts.append({"type": "reasoning.text", "text": part.text})
    return thoughts or None


def _assistant_message_from_llm(
    response: str, content: str | None = None, internal: bool = False
) -> AIMessage:
    """Wrap an LLM reply as a state message.

    `internal=True` marks a reply that exists only as retry context for the
    model - the builder's DSL dumps - so the chat can drop it without guessing
    from its content, which is unreliable because the model does not always
    emit the same markers.
    """
    extra: dict = {}
    reasoning_details = getattr(response, "reasoning_details", None)
    if reasoning_details is not None:
        extra["reasoning_details"] = reasoning_details
    if internal:
        extra["internal"] = True
    message_content = str(response) if content is None else content
    return AIMessage(content=message_content, additional_kwargs=extra) if extra \
        else AIMessage(content=message_content)


def _format_chat_messages(sys_prompt: str, messages: list, include_reasoning_details: bool = False) -> list:
    formatted = [{"role": "system", "content": sys_prompt}]
    for msg in messages:
        if isinstance(msg, HumanMessage):
            formatted.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            assistant_message = {"role": "assistant", "content": msg.content}
            if include_reasoning_details:
                reasoning_details = getattr(msg, "additional_kwargs", {}).get("reasoning_details")
                if reasoning_details is not None:
                    assistant_message["reasoning_details"] = reasoning_details
            formatted.append(assistant_message)
    return formatted

def _call_gemini(sys_prompt: str, messages: list) -> str:
    gemini_messages = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            gemini_messages.append(types.Content(role="user", parts=[types.Part.from_text(text=msg.content)]))
        elif isinstance(msg, AIMessage):
            gemini_messages.append(types.Content(role="model", parts=[types.Part.from_text(text=msg.content)]))

    config = types.GenerateContentConfig(
        system_instruction=sys_prompt,
        temperature=LLM_TEMPERATURE,
        http_options=types.HttpOptions(timeout=LLM_TIMEOUT_SECONDS * 1000),
    )
    response = gemini_client.models.generate_content(
        model=GOOGLE_MODEL, contents=gemini_messages, config=config
    )
    return LLMResponse(
        response.text,
        reasoning_details=_gemini_reasoning(response),
        meta=_gemini_metadata(response),
    )


def _call_litellm(sys_prompt: str, messages: list) -> str:
    formatted = _format_chat_messages(sys_prompt, messages)
    response = litellm_client.chat.completions.create(
        model=LITELLM_MODEL_NAME, messages=formatted,
        timeout=LLM_TIMEOUT_SECONDS,
        temperature=LLM_TEMPERATURE,
    )
    message = response.choices[0].message
    return LLMResponse(
        message.content,
        reasoning_details=_openai_reasoning(message),
        meta=_openai_metadata(response),
    )


def _call_huggingface(sys_prompt: str, messages: list) -> str:
    """Call Hugging Face Inference Providers through its OpenAI-compatible router."""
    if not HUGGINGFACE_MODEL_NAME:
        raise ValueError(
            "HF_MODEL_NAME (or HUGGINGFACE_MODEL_NAME) must be set when "
            "LLM_PROVIDER=huggingface."
        )

    formatted = _format_chat_messages(sys_prompt, messages)

    # Hugging Face selects a specific inference provider with the ':provider'
    # suffix. Preserve it if the configured model already includes one.
    model = HUGGINGFACE_MODEL_NAME
    if ":" not in model:
        model = f"{model}:{HUGGINGFACE_INFERENCE_PROVIDER}"

    response = huggingface_client.chat.completions.create(
        model=model,
        messages=formatted,
        timeout=LLM_TIMEOUT_SECONDS,
        temperature=LLM_TEMPERATURE,
    )
    message = response.choices[0].message
    return LLMResponse(
        message.content,
        reasoning_details=_openai_reasoning(message),
        meta=_openai_metadata(response),
    )


def _call_openrouter(sys_prompt: str, messages: list) -> LLMResponse:
    """Call OpenRouter using its chat completions API."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY must be set when LLM_PROVIDER=openrouter.")
    if not OPENROUTER_MODEL_NAME:
        raise ValueError(
            "OPENROUTER_MODEL_NAME (or OPENROUTER_MODEL) must be set when "
            "LLM_PROVIDER=openrouter."
        )

    formatted = _format_chat_messages(sys_prompt, messages, include_reasoning_details=True)
    payload = {
        "model": OPENROUTER_MODEL_NAME,
        "messages": formatted,
        "temperature": LLM_TEMPERATURE,
    }
    if OPENROUTER_REASONING_ENABLED:
        payload["reasoning"] = {"enabled": True}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = OPENROUTER_APP_NAME

    response = requests.post(
        url=f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
        headers=headers,
        data=json.dumps(payload),
        timeout=LLM_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        raise RateLimited(_provider_message(response) or "The model provider is rate limiting us.")
    response.raise_for_status()

    response_json = response.json()
    message = response_json["choices"][0]["message"]
    usage = response_json.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return LLMResponse(
        message.get("content"),
        reasoning_details=message.get("reasoning_details"),
        meta=CallMetadata(
            # OpenRouter routes: the model that answered is often not the one asked for,
            # and on the free tier it can change between two calls in the same turn.
            model=str(response_json.get("model") or ""),
            generation_id=str(response_json.get("id") or ""),
            prompt_tokens=_int(usage.get("prompt_tokens")),
            completion_tokens=_int(usage.get("completion_tokens")),
            reasoning_tokens=_int(details.get("reasoning_tokens")),
            # Which upstream ran it - DeepInfra, Together, and so on. Two samples from the
            # same model name are not necessarily from the same deployment of it.
            upstream_provider=str(response_json.get("provider") or ""),
            # Confirmed against a live response: the cost is in the body, so knowing what a
            # run cost needs no second call to the /generation endpoint. Free-tier models
            # report 0.
            cost=_float(usage.get("cost")),
        ),
    )


class RateLimited(RuntimeError):
    """The model provider refused the call because a quota is exhausted.

    Worth its own type because it is the single most likely failure on a free tier and the
    only one the user can act on - by waiting, or by pointing the app at a paid model. As a
    generic error it reached the chat as "AGENT_ERROR: 429 Client Error: Too Many Requests",
    with the provider's own explanation of *which* limit and *when it resets* thrown away
    in `raise_for_status`.
    """


def _provider_message(response) -> str:
    """The provider's own account of a refusal, if it gave one."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "").strip()[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        # OpenRouter names the exhausted limit here, e.g. "free-models-per-day".
        metadata = error.get("metadata")
        if isinstance(metadata, dict) and metadata.get("headers"):
            reset = metadata["headers"].get("X-RateLimit-Reset")
            if reset:
                message = f"{message} (resets at {reset})".strip()
        return message[:300]
    return ""


def _dispatch(sys_prompt: str, messages: list):
    if ACTIVE_PROVIDER == "gemini":
        return _call_gemini(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "litellm":
        return _call_litellm(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "huggingface":
        return _call_huggingface(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "openrouter":
        return _call_openrouter(sys_prompt, messages)
    raise ValueError(f"Unsupported LLM_PROVIDER: {ACTIVE_PROVIDER}")


def _active_model() -> str:
    """The model this provider was asked for.

    Only ever a *request*: OpenRouter may route to something else, and recording what
    actually served the call is Phase 2's job.
    """
    return {
        "gemini": GOOGLE_MODEL,
        "litellm": LITELLM_MODEL_NAME,
        "huggingface": HUGGINGFACE_MODEL_NAME,
        "openrouter": OPENROUTER_MODEL_NAME,
    }.get(ACTIVE_PROVIDER) or ""


def _messages_for_corpus(messages: list) -> list[dict]:
    """The request in canonical role/content form, system prompt excluded.

    Not `_format_chat_messages`: that renders per provider and inlines the system prompt,
    which the corpus stores once by hash rather than on every row. The `internal` flag is
    kept because it marks the builder's own retry context - the difference between a first
    attempt and a repair is exactly what makes these samples worth having.
    """
    out: list[dict] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            out.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            entry = {"role": "assistant", "content": msg.content}
            extra = getattr(msg, "additional_kwargs", None) or {}
            if extra.get("internal"):
                entry["internal"] = True
            if extra.get("reasoning_details") is not None:
                entry["reasoning_details"] = extra["reasoning_details"]
            out.append(entry)
    return out


def call_llm(sys_prompt: str, messages: list, *, node: str = "unknown",
             attempt: int = 1, purpose: str = "") -> str:
    """Routes the request to the active LLM provider and returns the string response.

    Also the one place every model call in the codebase is recorded for fine-tuning.
    `node` is the attribution that made that possible: without it the planner, builder,
    triage and patch calls are indistinguishable at the point of the call, and a corpus
    you cannot split by task is one you cannot train a planner on.

    Capture never affects the result. `backend.corpus` swallows its own failures, and the
    provider's return value is passed back untouched - `_call_openrouter` returns an
    `LLMResponse` carrying reasoning, and stringifying it here would silently drop the
    model's thinking from every downstream turn.
    """
    if LLM_INIT_ERROR:
        raise RuntimeError(LLM_INIT_ERROR)

    started = time.monotonic()
    common = {
        "node": node,
        "attempt": attempt,
        "purpose": purpose,
        "system_prompt": sys_prompt,
        "messages": _messages_for_corpus(messages),
        "provider": ACTIVE_PROVIDER,
        "model_requested": _active_model(),
        "temperature": LLM_TEMPERATURE,
        "reasoning_enabled": ACTIVE_PROVIDER == "openrouter" and OPENROUTER_REASONING_ENABLED,
    }

    try:
        response = _dispatch(sys_prompt, messages)
    except Exception as exc:
        # A refusal is a sample too: a rate limit, a timeout and a malformed request are
        # all things worth being able to count later.
        corpus.record_llm_call(
            **common,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    # `getattr` rather than `response.meta`: a provider path could return a bare string,
    # and so does every test fake that stubs one out.
    meta = getattr(response, "meta", None) or CallMetadata()
    corpus.record_llm_call(
        **common,
        completion=str(response),
        reasoning=getattr(response, "reasoning_details", None),
        latency_ms=int((time.monotonic() - started) * 1000),
        model_served=meta.model,
        generation_id=meta.generation_id,
        prompt_tokens=meta.prompt_tokens,
        completion_tokens=meta.completion_tokens,
        reasoning_tokens=meta.reasoning_tokens,
        upstream_provider=meta.upstream_provider,
        cost=meta.cost,
    )
    return response


# ==========================================
# 3. STATE & HELPERS
# ==========================================
def _latest(_current, incoming):
    """Reducer: when a step carries more than one write, the newest one wins.

    Resuming a paused run sends the live canvas along with the reply. A node
    that pauses twice in a row - the clarification chip, then the text box -
    is resumed twice against the *same* checkpoint, and LangGraph accumulates
    the writes of every resume into that single step. A plain last-value
    channel rejects two values in one step (INVALID_CONCURRENT_GRAPH_UPDATE),
    so the channels the resume carries reduce instead of colliding.
    """
    return incoming


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    triage_status: str
    triage_message: str
    triage_options: list
    graph_plan_dsl: str
    plan_status: str  # "OK" once the planner produced a design; "EMPTY" when it produced none
    plan_attempts_used: int  # Planner calls spent this turn; also sizes the builder's budget
    jdm_json: str
    test_suite_json: str
    evaluation_feedback: str
    build_status: str
    build_failed: bool
    build_attempts_used: int  # Repair budget spent so far, across re-entries to the builder
    # Travels with every resume, so it needs `_latest` for the same reason the canvas
    # keys do: two resumes against one checkpoint would otherwise collide.
    thread_id: Annotated[str, _latest]  # Lets long-running nodes see a stop request
    lint_findings: list  # Non-blocking lint warnings and hints on the built graph
    patch_log: list  # One line per edit applied, for the action log
    test_regressions: list  # Saved cases that disagree with an edit's new behaviour
    final_approval_status: str
    usecase_name: str
    mode: str  # "NEW" or "EXISTING"
    selected_file: str  # Display name of the policy being worked on
    existing_jdm_json: str  # The raw JSON of the policy under discussion
    action_type: str  # "EXPLAIN", "MODIFY", or "TEST"
    # --- Web studio fields ---
    # These four travel with every resume, so they take the `_latest` reducer:
    # see the note there for why a plain channel is not enough.
    canvas_jdm_json: Annotated[str, _latest]  # The graph on the canvas, unsaved edits included
    canvas_graph_id: Annotated[str, _latest]  # Database id, "" for a scratch canvas
    canvas_graph_name: Annotated[str, _latest]
    intent: str  # "CREATE" | "MODIFY" | "TEST" | "EXPLAIN" | "LINT"
    intent_confidence: float
    cancel_requested: Annotated[bool, _latest]


REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo_path(*parts: str) -> Path:
    """Absolute path inside the repo, independent of the process cwd."""
    return REPO_ROOT.joinpath(*parts)


def _emit(event: dict) -> None:
    """Push a custom progress event to whoever is streaming this run.

    Safe to call when the node runs outside a graph stream (tests, scripts).
    """
    if get_stream_writer is None:
        return
    try:
        writer = get_stream_writer()
    except Exception:
        return
    if writer is None:
        return
    try:
        writer(event)
    except Exception:
        pass


@contextmanager
def _tool_run(tool: str, *, node: str, attempt: int = 1):
    """Time a deterministic tool and record its verdict, whether or not it raised.

    Every stage of a build is a check some model output either passes or fails, and until
    now only the last one survived - `evaluation_feedback` held one string and each attempt
    overwrote the one before. Recorded per attempt, a failing attempt and the passing one
    that follows it are a preference pair with a machine-checkable reason attached, which
    is the single most valuable thing this pipeline produces and was being thrown away.

    Yields a handle whose `diagnostics` and `output` the caller fills in. An exception
    marks the run failed and is re-raised untouched: the repair loop is driven by these
    exceptions, so swallowing one here would change what the agent does.
    """
    handle = SimpleNamespace(diagnostics=None, output=None)
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        yield handle
    except Exception as exc:
        corpus.record_tool_result(
            tool=tool, node=node, attempt=attempt, ok=False, duration_ms=elapsed(),
            diagnostics=handle.diagnostics or _diagnostics_from_error(exc),
            output=handle.output,
            error=f"{type(exc).__name__}: {exc}"[:4000],
        )
        raise
    corpus.record_tool_result(
        tool=tool, node=node, attempt=attempt, ok=True, duration_ms=elapsed(),
        diagnostics=handle.diagnostics, output=handle.output,
    )


def _diagnostics_from_error(exc: BaseException) -> list[dict] | None:
    """Structure out of the exceptions the tools raise, where they carry any.

    `DslError` collects every problem in the document rather than stopping at the first,
    so it is already a list; the rest carry only a message, and inventing structure for
    those would be worse than recording none.
    """
    if isinstance(exc, DslError):
        return [{"kind": "dsl_parse", "code": "DSL_ERROR", "message": problem}
                for problem in exc.problems]
    if isinstance(exc, PatchError):
        return [{"kind": "patch", "code": "PATCH_ERROR", "message": str(exc)}]
    return None


def _debug_write(dsl_content: str) -> None:
    """Persist the DSL under inspection. Never fatal."""
    target = Path(os.getenv("DEBUG_DIR") or tempfile.gettempdir()) / "debug_graph.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dsl_content, encoding="utf-8")
    except OSError as e:
        print(f"  --> [Debug]: could not write {target}: {e}")


def _inject_jdm(template: str, existing_jdm: str) -> str:
    """Substitute the {existing_jdm} placeholder without str.format().

    These prompts show the model literal JSON examples, so `.format()` treats
    every `{` in them as a field and raises KeyError.
    """
    return template.replace("{existing_jdm}", existing_jdm)


def _extract_single_json(text: str) -> str:
    blocks = re.findall(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if blocks: return blocks[0].strip()
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1: return text[start:end + 1].strip()
    return text.strip()


# A fence that opens on the first line and closes on the last: the only shape that can be
# a *wrapper* rather than part of the payload.
_WRAPPING_FENCE_RE = re.compile(r'^```([^\s`]*)[^\S\n]*\n(.*)\n```$', re.DOTALL)


def _unwrap_fence(text: str, expect_lang: str = "") -> str:
    """Remove a code fence, but only one that encloses the whole of `text`.

    The DSL legitimately *contains* fences - ```mermaid for the flowchart, ```expressions,
    ```js - so a stripper that removes any leading backticks eats the mermaid fence and
    leaves the bare word "mermaid" behind. That parses to a graph with no edges and raises
    nothing, which is exactly the failure `backend/debug_graph.md` was left behind by.

    A fence is treated as a wrapper only when it opens at the very start, closes at the very
    end, and its language tag is either absent or the one we asked for.
    """
    match = _WRAPPING_FENCE_RE.match(text.strip())
    if not match:
        return text.strip()
    lang = match.group(1).lower()
    if lang and expect_lang and lang != expect_lang.lower():
        # e.g. ```mermaid where we expected ```markdown: this is content, not a wrapper.
        return text.strip()
    return match.group(2).strip()


def _extract_bounded_text(content: str, start_marker: str, end_marker: str, strip_lang: str = "") -> str:
    """Extract the text between two literal markers, unwrapping an enclosing fence."""
    pattern = rf'{re.escape(start_marker)}\s*(.*?)\s*{re.escape(end_marker)}'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return ""
    return _unwrap_fence(match.group(1), strip_lang)


def _fenced_block(content: str, lang: str) -> str:
    """First fenced block tagged `lang`, matched to *its own* closing fence.

    Depth matters here: the DSL nests ```mermaid and ```js inside, so a non-greedy regex
    stops at the first inner close and silently truncates the block. A line of bare
    backticks closes; a line of backticks with a language tag opens.
    """
    lines = content.split("\n")
    opener = re.compile(rf'^\s*```{re.escape(lang)}\s*$', re.IGNORECASE)
    start = next((i for i, line in enumerate(lines) if opener.match(line)), None)
    if start is None:
        return ""

    depth = 0
    for i in range(start, len(lines)):
        fence = lines[i].strip()
        if not fence.startswith("```"):
            continue
        if fence == "```":
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start + 1:i]).strip()
        else:
            depth += 1
    return ""


def _extract_plan_blocks(content: str) -> tuple[str, str, str]:
    """Pull (dsl, tests, usecase_name) out of a planner or builder reply.

    The markers are the contract, but a small model follows a long format imperfectly, and
    an unparseable reply costs a whole attempt. So each block falls back to the shape the
    model most plausibly reached for instead: a fenced block, or - for the DSL, which has an
    unmistakable "# Structure" / "# Nodes" skeleton - the raw text itself.
    """
    dsl = _extract_bounded_text(content, "---DSL STARTS---", "---DSL ENDS---", strip_lang="markdown")
    tests = _extract_bounded_text(content, "---TESTS STARTS---", "---TESTS ENDS---", strip_lang="json")
    usecase = _extract_bounded_text(content, "---USECASE NAME STARTS---", "---USECASE NAME ENDS---")

    if not dsl:
        dsl = _fenced_block(content, "markdown")
    if not dsl and "# Structure" in content and "# Nodes" in content:
        # Unfenced and unmarked, but structurally unmistakable. Take from the heading to
        # wherever the tests begin, so a trailing JSON array is not swept into the DSL.
        body = content[content.index("# Structure"):]
        for boundary in ("---TESTS STARTS---", "---USECASE NAME STARTS---"):
            if boundary in body:
                body = body[: body.index(boundary)]
        # An unmarked tests array may simply trail the DSL; do not swallow it.
        body = re.sub(r'\n\s*\[\s*\{.*}\s*]\s*$', '', body, flags=re.DOTALL)
        dsl = body.strip()

    if not tests:
        tests = _fenced_block(content, "json")
    if not tests:
        # A bare array anywhere in the reply.
        match = re.search(r'\[\s*\{.*}\s*]', content, re.DOTALL)
        tests = match.group(0).strip() if match else ""

    return dsl, tests, usecase

# ==========================================
# 3. WORKFLOW NODES
# ==========================================

# Step 1 : Intent Router (entry point)
#
# The web UI owns policy selection, so there is no welcome chip list and no
# separate "what would you like to do" prompt. The user's own message decides
# where the run goes, and the canvas travels in on state.

_EXPLAIN_RE = re.compile(
    r"\b(explain|describe|walk me through|what does (this|it) do|how does (this|it) work|document|summari[sz]e)\b",
    re.IGNORECASE,
)
_TEST_RE = re.compile(
    r"\b(test|tests|testing|test case|test cases|run the suite|verify|validate|check that)\b",
    re.IGNORECASE,
)
# Matched before EXPLAIN and TEST: "check my policy" is a request to review it, not to
# run its suite. Rules cover the common phrasings so this never has to reach the LLM -
# which also matters on a rate-limited free tier.
_LINT_RE = re.compile(
    r"\b(lint|critique|code review|best practices?|"
    r"well[ -]?built|well[ -]?structured|look(s)? (ok|okay|good|right|fine)|"
    r"(any|what) (problems?|issues?|mistakes?)|anything wrong|what.s wrong|"
    r"review (this|the|my)|(check|assess|audit) (the |this |my )?(graph|policy|quality|structure)|"
    r"quality (check|control)|improve (this|the|my) (graph|policy))\b",
    re.IGNORECASE,
)
_MODIFY_RE = re.compile(
    r"\b(add|change|modify|remove|delete|update|rename|fix|adjust|instead|also|tweak|extend|edit)\b",
    re.IGNORECASE,
)
_CREATE_RE = re.compile(
    r"\b(create|new policy|build me|from scratch|start over|generate a|make a)\b",
    re.IGNORECASE,
)


def _is_non_empty_graph(raw: str) -> bool:
    """True when the canvas holds more than an untouched input/output skeleton."""
    if not raw or not raw.strip():
        return False
    try:
        graph = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False

    nodes = graph.get("nodes") or []
    if not nodes:
        return False
    # A fresh canvas is just an input and an output node with nothing between.
    substantive = [n for n in nodes if n.get("type") not in ("inputNode", "outputNode")]
    return bool(substantive) or bool(graph.get("edges"))


def _last_human_text(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, HumanMessage):
            return str(msg.content).strip()
    return ""


def _classify_intent(text: str, has_graph: bool) -> tuple[str, float]:
    """Rules first, LLM only when the rules cannot decide."""
    if not has_graph:
        return "CREATE", 1.0
    if not text:
        return "MODIFY", 0.3

    if _LINT_RE.search(text):
        return "LINT", 0.9
    if _EXPLAIN_RE.search(text):
        return "EXPLAIN", 0.9
    if _TEST_RE.search(text):
        return "TEST", 0.9
    if _CREATE_RE.search(text) and re.search(r"\b(new|scratch|over)\b", text, re.IGNORECASE):
        return "CREATE", 0.85
    if _MODIFY_RE.search(text):
        return "MODIFY", 0.85

    try:
        raw = call_llm(PROMPT_INTENT, [HumanMessage(content=text)], node="intent_router_node")
        parsed = json.loads(_extract_single_json(raw))
        intent = str(parsed.get("intent", "")).upper()
        if intent in ("CREATE", "MODIFY", "TEST", "EXPLAIN", "LINT"):
            return intent, float(parsed.get("confidence", 0.5))
    except Exception as e:
        print(f"  --> [Intent]: LLM classification failed ({e}); defaulting.")

    return ("MODIFY" if has_graph else "CREATE"), 0.3


def intent_router_node(state: AgentState):
    """Entry node. Never interrupts, so every run starts with real work."""
    canvas = state.get("canvas_jdm_json", "") or ""
    has_graph = _is_non_empty_graph(canvas)
    text = _last_human_text(state.get("messages", []))

    intent, confidence = _classify_intent(text, has_graph)
    print(f"\n[Intent Router]: {intent} (confidence {confidence:.2f}, canvas={'yes' if has_graph else 'empty'})")

    # Downstream nodes and every prompt read `existing_jdm_json`; keeping it
    # populated from the canvas is what lets them stay unchanged.
    return {
        "intent": intent,
        "intent_confidence": confidence,
        "mode": "EXISTING" if intent in ("MODIFY", "TEST", "EXPLAIN") else "NEW",
        "existing_jdm_json": canvas if has_graph else "",
        "selected_file": state.get("canvas_graph_name") or "the current graph",
        "action_type": intent if intent in ("MODIFY", "TEST", "EXPLAIN") else "",
    }


# Step 2 : Explain Node
def explain_node(state: AgentState):
    filename = state.get("selected_file", "this policy")
    existing_jdm = state.get("existing_jdm_json", "")

    print(f"\n[Explain Node]: Generating compulsory explanation for {filename}...")

    # 1. Compact the JSON
    try:
        jdm_compact = json.dumps(json.loads(existing_jdm))
    except Exception:
        jdm_compact = existing_jdm.replace('\n', '').replace('\r', '')

    # 2. Ask LLM for the explanation
    user_prompt = _inject_jdm(PROMPT_EXPLAIN_USER, existing_jdm)

    messages = [HumanMessage(content=user_prompt)]
    explanation = call_llm(PROMPT_EXPLAIN, messages, node="explain_node")

    # 4. Format the final UI Message
    # Notice the mandatory empty lines inside the <details> tags to ensure Streamlit parses the markdown correctly!
    ui_message = f"""### 📖 Policy Analysis: `{filename}`
    <details>
    <summary><b>📜 Click to view Raw JDM Logic</b></summary>
    
    ```json
    {jdm_compact}
    ```
    </details>
    
    Logic Explanation:
    {explanation}
    """
    # Save the explanation to the chat history so the user can read it
    # right before the action chips appear.
    return {
        "messages": [_assistant_message_from_llm(ui_message)]
    }


# Step 3 : Modify Triage Node
def modify_triage_node(state: AgentState):
    print("\n[Modify Triage]: Evaluating requested changes against existing logic...")

    existing_jdm = state.get("existing_jdm_json", "")

    # 1. Dynamically inject the existing JDM into the prompt template
    prompt = _inject_jdm(PROMPT_MODIFY_TRIAGE, existing_jdm)
    # Call the LLM with the custom modification prompt + the conversation history
    # Note: Replace call_llm with however your function is named
    response_text = call_llm(prompt, state["messages"], node="modify_triage_node")
    clean_json = _extract_single_json(response_text)
    try:
        parsed = json.loads(clean_json)
        status = parsed.get("status", "NEEDS_INFO")
        triage_msg = parsed.get("message", "Please clarify how this change affects other rules.")

        if status in ["READY_FOR_APPROVAL", "REQUEST_FOR_APPROVAL"]:
            options = ["Approve with above understanding & assumptions", "Custom clarification"]
        else:
            options = parsed.get("options", ["Use standard defaults"])
            if "Custom clarification" not in options:
                options.append("Custom clarification")

        return {
            "triage_status": status,
            "triage_message": triage_msg,
            "triage_options": options,
            # Save the AI's triage question to the chat history
            "messages": [_assistant_message_from_llm(response_text, triage_msg)]
        }
    except json.JSONDecodeError:
        error_msg = "Could you clarify how these changes should be applied?"
        return {
            "triage_status": "NEEDS_INFO",
            "triage_message": error_msg,
            "triage_options": ["Custom clarification"],
            "messages": [_assistant_message_from_llm(error_msg)]
        }


# Step 5 : Test Node
def test_node(state: AgentState):
    print("\n[Test Node]: Running standalone test execution...")

    existing_jdm = state.get("existing_jdm_json", "") or state.get("canvas_jdm_json", "")
    policy_name = state.get("selected_file", "the current graph")

    # The suite is supplied on state by the caller (loaded from the database).
    # Only generate one when the policy genuinely has no tests yet.
    test_suite_json = state.get("test_suite_json", "") or ""
    generated = False

    if not test_suite_json.strip() or test_suite_json.strip() == "[]":
        print("  -> No existing tests found. Generating new ones...")
        _emit({"type": "progress", "node": "test_node", "attempt": 1, "max_attempts": 1,
               "phase": "llm", "message": "Writing test cases for this policy"})
        user_prompt = _inject_jdm(PROMPT_TEST_USER, existing_jdm)
        content = call_llm(PROMPT_TEST, [HumanMessage(content=user_prompt)],
                           node="test_node", purpose="generate_suite")
        test_suite_json = _extract_bounded_text(
            content, "---TESTS STARTS---", "---TESTS ENDS---", strip_lang="json"
        ) or "[]"
        generated = True
        source_msg = f"✨ **Generated a new test suite for** `{policy_name}`."
    else:
        source_msg = f"🧪 **Ran the saved test suite for** `{policy_name}`."

    try:
        parsed_tests = json.loads(test_suite_json)
    except json.JSONDecodeError as e:
        return {
            "test_suite_json": test_suite_json,
            "messages": [_assistant_message_from_llm(
                f"❌ The test suite could not be parsed as JSON: `{e}`"
            )],
        }

    _emit({"type": "progress", "node": "test_node", "attempt": 1, "max_attempts": 1,
           "phase": "evaluate", "message": f"Running {len(parsed_tests)} test case(s)"})

    try:
        with _tool_run("run_tests", node="test_node") as run:
            report = run_test_suite(existing_jdm, parsed_tests)
            run.output = report.get("summary")
    except Exception as e:
        return {
            "test_suite_json": test_suite_json,
            "messages": [_assistant_message_from_llm(
                f"❌ **The engine could not run this graph:**\n```\n{e}\n```"
            )],
        }

    _emit({"type": "test_report", "report": report, "generated": generated})

    ui_message = f"{source_msg}\n\n{_format_test_report_markdown(report)}"

    return {
        "test_suite_json": test_suite_json,
        "evaluation_feedback": json.dumps(report["summary"]),
        "messages": [_assistant_message_from_llm(ui_message)],
    }


def _format_test_report_markdown(report: dict) -> str:
    """Deterministic pass/fail table. The verdict comes from the engine, not an LLM."""
    summary = report["summary"]
    icon = "✅" if not (summary["failed"] or summary["errored"]) else "❌"
    lines = [
        f"### {icon} {summary['passed']}/{summary['total']} tests passed",
        "",
        f"*{summary['failed']} failed · {summary['errored']} errored · "
        f"{summary['skipped']} skipped · {summary['duration_ms']}ms*",
        "",
    ]

    if summary.get("compile_error"):
        lines.append(f"The graph does not compile: `{summary['compile_error']}`")
        return "\n".join(lines)

    lines += ["| | Test | Details |", "|---|---|---|"]
    marks = {"passed": "✅", "failed": "❌", "errored": "⚠️", "skipped": "➖"}
    for r in report["results"]:
        if r["status"] == "passed":
            detail = "—"
        elif r["status"] == "skipped":
            detail = "no expected output"
        elif r["error"]:
            detail = f"`{r['error'][:120]}`"
        else:
            detail = "; ".join(
                f"`{m['path']}`: expected `{json.dumps(m['expected'])}`, got `{json.dumps(m['actual'])}`"
                for m in r["mismatches"][:3]
            )
            if len(r["mismatches"]) > 3:
                detail += f" (+{len(r['mismatches']) - 3} more)"
        lines.append(f"| {marks[r['status']]} | {r['name']} | {detail} |")

    return "\n".join(lines)



# Step 1: Evaluate Requirement

# Step 0c: Lint (static analysis of the open graph)
_SEVERITY_TITLES = {
    "error": "Must fix",
    "warning": "Worth checking",
    "hint": "Could be better",
}


def lint_node(state: AgentState):
    """Report the graph's static findings. Deterministic - no LLM call.

    The same checks that gate a build, run on demand against whatever is on the canvas.
    """
    print("\n[Lint]: Checking the open graph...")

    raw = state.get("existing_jdm_json") or state.get("canvas_jdm_json") or ""
    try:
        graph = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        graph = None

    if not graph or not graph.get("nodes"):
        return {"messages": [_assistant_message_from_llm(
            "There is no policy open to check. Build or open one first."
        )]}

    _emit({"type": "progress", "node": "lint_node", "attempt": 1, "max_attempts": 1,
           "phase": "lint", "message": "Checking the graph against the quality rules"})

    with _tool_run("lint", node="lint_node") as run:
        findings = lint(graph)
        run.diagnostics = [d.as_dict() for d in findings]
        run.output = {f"{s}s": sum(1 for d in findings if d.severity == s)
                      for s in ("error", "warning", "hint")}
    name = state.get("selected_file") or state.get("canvas_graph_name") or "this policy"

    if not findings:
        return {"messages": [_assistant_message_from_llm(
            f"**{name}** passes every check - structure, expressions, and quality."
        )]}

    counts = {s: sum(1 for d in findings if d.severity == s) for s in ("error", "warning", "hint")}
    headline = ", ".join(f"{n} {s}{'s' if n != 1 else ''}"
                         for s, n in counts.items() if n)
    body = [f"Checked **{name}**: {headline}.", ""]

    for severity in ("error", "warning", "hint"):
        group = [d for d in findings if d.severity == severity]
        if not group:
            continue
        body.append(f"### {_SEVERITY_TITLES[severity]}")
        for diagnostic in group:
            where = f" *({diagnostic.node_name})*" if diagnostic.node_name else ""
            body.append(f"- **{diagnostic.code}**{where} - {diagnostic.message}")
            if diagnostic.fix_hint:
                body.append(f"  - {diagnostic.fix_hint}")
        body.append("")

    if counts["error"]:
        body.append("Ask me to fix any of these and I will update the policy.")

    return {"messages": [_assistant_message_from_llm("\n".join(body).strip())]}


def triage_node(state: AgentState):
    print(f"\n[Step 1: Triage]: Evaluating requirements using {ACTIVE_PROVIDER.upper()}...")

    response_text = call_llm(PROMPT_TRIAGE, state["messages"], node="triage_node")
    clean_json = _extract_single_json(response_text)

    try:
        parsed = json.loads(clean_json)
        status = parsed.get("status", "NEEDS_INFO")
        triage_msg = parsed.get("message", "Please clarify your requirements.")

        # --- CHIP GENERATION LOGIC ---
        if status in ["READY_FOR_APPROVAL", "REQUEST_FOR_APPROVAL"]:
            options = ["Approve with above understanding & assumptions", "Custom clarification"]
        else:
            options = parsed.get("options", ["Use standard defaults"])
            if "Custom clarification" not in options:
                options.append("Custom clarification")

        return {
            "triage_status": status,
            "triage_message": triage_msg,
            "triage_options": options,
            # CRITICAL FIX: Save the AI's question to the permanent chat history!
            "messages": [_assistant_message_from_llm(response_text, triage_msg)]
        }
    except json.JSONDecodeError:
        error_msg = "Could you clarify the logic rules?"
        return {
            "triage_status": "NEEDS_INFO",
            "triage_message": error_msg,
            "triage_options": ["Custom clarification"],
            # CRITICAL FIX: Save the AI's error to the permanent chat history!
            "messages": [_assistant_message_from_llm(error_msg)]
        }


# Step 1b: Human-in-the-Loop Review
def human_triage_review_node(state: AgentState):
    print(f"\n[System Paused]: {state['triage_status']}")

    options = state.get("triage_options", [])
    if "Custom clarification" not in options:
        options.append("Custom clarification")

    # --- FIRST PAUSE: Show the generated chips ---
    payload_1 = {
        # The AI's actual message is already in chat history,
        # so this is just a temporary UI instruction above the chips.
        "prompt": "👉 **Please select an option to proceed:**",
        "options": options
    }

    user_response = interrupt(payload_1)
    response_text = str(user_response).strip()

    # --- SECOND PAUSE: Show text box ONLY if they requested custom clarification ---
    if response_text == "Custom clarification":
        payload_2 = {
            "prompt": "📝 **Please type your custom clarification or edge case details:**",
            "options": []  # Empty options tells Streamlit to render the text input box
        }
        user_response = interrupt(payload_2)
        response_text = str(user_response).strip()

    # --- ROUTING LOGIC ---
    if response_text == "Approve with above understanding & assumptions" or response_text.upper() == "APPROVE":
        return {
        "triage_status": "APPROVED",
        "messages": [HumanMessage(content="I approve the assumptions. Please proceed to build the plan.")]
        }
    else:
        return {
            "triage_status": "NEEDS_INFO",
            "messages": [HumanMessage(content=response_text)]
        }

# Step 2: Planner (Expert Analyst)
def _is_a_plan(dsl: str) -> bool:
    """Does this reply contain a design at all, as opposed to prose or nothing?

    The distinction decides who repairs it. A plan with mistakes in it - a dangling edge, a
    bad cell - belongs to the builder, whose repair loop exists for exactly that and which
    is better placed to fix one line than the planner is to start over. A reply with no DSL
    skeleton is a different thing entirely: there is nothing to repair, and handing it on
    spends the whole repair budget discovering that before reporting a builder failure for
    what was really a planning one.
    """
    return bool(dsl.strip()) and "# Structure" in dsl and "# Nodes" in dsl


# Given to the planner when its previous reply carried no design. At temperature 0 an
# unchanged prompt returns an unchanged answer, so a bare retry is guaranteed to reproduce
# the same non-answer - the second attempt only means anything because this is added to it.
# Rendered through the same diagnostic channel the builder's repair loop uses, so a failure
# reads the same way wherever in the pipeline it happened.
_REPLAN_INSTRUCTION = format_for_llm([
    Diagnostic(
        kind="dsl_parse",
        code="NO_PLAN",
        message=(
            "Your previous reply contained no graph plan - neither a `# Structure` section "
            "nor a `# Nodes` section - so there was nothing to build from."
        ),
        fix_hint=(
            "Emit the plan in the format described above: one `# Structure` section holding "
            "a mermaid flowchart whose arrows define the edges, and one `# Nodes` section "
            "with a `## <name>` block per node named in that flowchart. If the requirement "
            "is too large to express at once, design only the main decision path - a request "
            "node, one or two decision tables, a response node - and leave the rest out. A "
            "partial plan can be extended; no plan cannot."
        ),
    )
])


def _no_plan_feedback(reply: str) -> str:
    """What to show the user when the planner produced no design.

    Its actual words matter here: a model that declines usually says why ("this needs data
    the graph cannot reach"), and that is more useful than any summary of mine.
    """
    said = " ".join((reply or "").split())
    if not said:
        return "The planner returned an empty reply."
    return f"The planner replied without a design:\n\n{said[:600]}"


def planner_node(state: AgentState):
    print("\n[Step 2: Planner]: Generating DSL Implementation Plan...")

    # Only *consecutive* failures count. Re-entering the planner after a design that worked
    # - which is what a rejected final approval does - is a fresh start, not a third try.
    retrying = state.get("plan_status") == "EMPTY"
    spent = int(state.get("plan_attempts_used") or 0) if retrying else 0

    # If we are modifying an existing graph, inject it as a hidden system message
    # so the Planner knows not to start from scratch!
    messages_for_planner = state["messages"].copy()
    if state.get("mode") == "EXISTING":
        existing_jdm = state.get("existing_jdm_json", "")
        injection = f"""SYSTEM NOTE: You are updating an EXISTING JDM graph based on the approved changes in the chat history.
        CURRENT JDM JSON:
        ```json
        {existing_jdm}
        ```
        Please generate the updated DSL and updated Test Cases based on the approved modifications.
        """

        messages_for_planner.append(HumanMessage(content=injection))

    if retrying:
        messages_for_planner.append(HumanMessage(content=_REPLAN_INSTRUCTION))

    # Call your LLM using messages_for_planner instead of state["messages"]
    content = call_llm(PROMPT_PLANNER, messages_for_planner, node="planner_node",
                       attempt=spent + 1, purpose="replan" if retrying else "plan")
    print(f"planner node content: {content}")

    # Use the robust extraction
    dsl_content, test_suite_json, usecase_name = _extract_plan_blocks(content)

    # Fallback to empty array if tests weren't found
    if not test_suite_json:
        test_suite_json = "[]"

    # The planner's own verdict on its reply. "The model answered with prose instead of a
    # design" is a distinct and common failure, and one a corpus should be able to count.
    corpus.record_tool_result(
        tool="extract_plan", node="planner_node", attempt=spent + 1,
        ok=_is_a_plan(dsl_content),
        output={"dsl_chars": len(dsl_content or ""),
                "tests": test_suite_json != "[]",
                "named": bool(usecase_name)},
        error=None if _is_a_plan(dsl_content) else "the reply carried no DSL skeleton",
    )

    if not _is_a_plan(dsl_content):
        print("  --> [Planner]: no design in the reply; the builder is not entered.")
        return {
            "graph_plan_dsl": dsl_content,
            "plan_status": "EMPTY",
            "plan_attempts_used": spent + 1,
            # A failed re-plan must not leave the previous turn's graph in state: the
            # reporter reads `jdm_json` and would otherwise announce a stale policy as
            # though this turn had produced it.
            "jdm_json": "",
            "evaluation_feedback": _no_plan_feedback(content),
            "messages": [_assistant_message_from_llm(content, internal=True)],
        }

    return {
        "graph_plan_dsl": dsl_content,
        "plan_status": "OK",
        "plan_attempts_used": spent + 1,
        "test_suite_json": test_suite_json,
        "usecase_name": usecase_name,
        # The builder repairs what the planner wrote, so the plan has to be in the history
        # it reads. Without this the first repair attempt is asked to fix a graph it was
        # never shown and has to reconstruct it from the triage conversation. Tagged
        # internal so `_is_internal` keeps the raw DSL out of the chat, exactly as the
        # builder's own retry dumps already are.
        "messages": [_assistant_message_from_llm(content, internal=True)],
    }


def _user_cancelled(state: AgentState) -> bool:
    """Has the user asked to stop this thread?

    Imported lazily: the agent must not depend on the web layer at import time, since it
    is also driven directly from tests and scripts.
    """
    thread_id = state.get("thread_id")
    if not thread_id:
        return False
    try:
        from backend.services.chat_runner import is_cancelled
    except ImportError:  # pragma: no cover - agent used without the API
        return False
    return is_cancelled(thread_id)


def _repair_instruction(error: Exception) -> str:
    """Turn whatever went wrong into one instruction the model can act on.

    Everything used to arrive as `SYSTEM ERROR: {str(e)}`, so a parse error, a dangling
    edge, a bad expression and a wrong business answer were indistinguishable - and none
    of them said which node to look at.
    """
    if isinstance(error, (DslError, PatchError)):
        return format_for_llm([
            Diagnostic(kind="dsl_parse", code="EDIT_ERROR", message=problem)
            for problem in error.problems
        ])

    text = str(error)
    if any(text.startswith(heading) for heading in KIND_HEADINGS.values()):
        return text  # already a rendered diagnostic, raised from inside the loop

    return format_for_llm([parse_engine_error(error)])



# Step 2b: Patch (edit an existing graph in place)
def patch_node(state: AgentState):
    """Apply the requested change as targeted edits, rather than regenerating the policy.

    The planner path rebuilds a graph from a plan, which mints new ids for every node and
    edge - so an edit lost the canvas layout, broke `$nodes` references and produced a
    total diff. Here the existing graph is the base and only the named parts move.
    """
    print("\n[Step 2b: Patch]: Editing the existing graph...")

    # The canvas is re-sent on every resume, but `existing_jdm_json` is only computed once
    # in the intent router - so an edit made on the canvas mid-conversation never reached
    # the agent. Prefer the live canvas whenever there is one.
    raw = state.get("canvas_jdm_json") or state.get("existing_jdm_json") or ""
    try:
        graph = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        graph = None
    if not graph or not graph.get("nodes"):
        return {
            "build_status": "ERROR",
            "evaluation_feedback": "There is no policy open to edit.",
            "build_failed": True,
        }

    tests = _load_tests(state)
    context = list(state["messages"]) + [
        HumanMessage(content=_inject_jdm(PROMPT_PATCH_USER, json.dumps(graph, indent=2)))
    ]

    spent_before = int(state.get("build_attempts_used") or 0)
    attempts = max(1, _max_build_attempts() - spent_before)
    new_messages: list = []
    last_feedback = ""

    for attempt in range(attempts):
        # The same cooperative stop the builder honours. Without it an edit could only ever
        # be killed mid-call, which discards the turn instead of reporting it.
        if state.get("cancel_requested") or _user_cancelled(state):
            print("  --> [Patch]: Cancellation requested; stopping.")
            return {
                "build_status": "CANCELLED",
                "evaluation_feedback": "The edit was cancelled before it was applied.",
            }

        _emit({"type": "progress", "node": "patch_node", "attempt": attempt + 1,
               "max_attempts": attempts, "phase": "llm",
               "message": "Working out the smallest change" if attempt == 0
                          else f"Revising the edit after attempt {attempt}"})

        content = call_llm(PROMPT_PATCH, context, node="patch_node",
                           attempt=attempt + 1,
                           purpose="patch" if attempt == 0 else "repair")
        new_messages.append(_assistant_message_from_llm(content, internal=True))
        context.append(_assistant_message_from_llm(content))

        block = _extract_bounded_text(content, "---OPS STARTS---", "---OPS ENDS---", strip_lang="json")
        if not block:
            block = _fenced_block(content, "json")
        # Recorded directly rather than through `_tool_run`: this failure continues the
        # loop instead of raising, so there is no exception for the helper to catch.
        try:
            operations = json.loads(block) if block.strip() else []
        except json.JSONDecodeError as exc:
            corpus.record_tool_result(
                tool="extract_ops", node="patch_node", attempt=attempt + 1, ok=False,
                error=f"JSONDecodeError: {exc}",
                diagnostics=[{"kind": "dsl_parse", "code": "OPS_NOT_JSON",
                              "message": str(exc)}],
            )
            last_feedback = (f"The edit operations were not valid JSON ({exc}). Output a JSON "
                             "array between ---OPS STARTS--- and ---OPS ENDS---.")
            context.append(HumanMessage(content=last_feedback))
            continue
        corpus.record_tool_result(
            tool="extract_ops", node="patch_node", attempt=attempt + 1,
            ok=bool(operations), output={"operations": len(operations)},
        )

        if not operations:
            return {
                "build_status": "ERROR",
                "build_failed": True,
                "evaluation_feedback": "That change cannot be made by editing this policy.",
                "messages": new_messages + [_assistant_message_from_llm(
                    "I could not express that as a change to this policy. Could you say which "
                    "node or rule it should affect?"
                )],
                "build_attempts_used": spent_before + attempt + 1,
            }

        try:
            _emit({"type": "progress", "node": "patch_node", "attempt": attempt + 1,
                   "max_attempts": attempts, "phase": "parse",
                   "message": f"Applying {len(operations)} change(s)"})
            with _tool_run("apply_patch", node="patch_node", attempt=attempt + 1) as run:
                patched = apply_patch(graph, operations)
                # The ops themselves, which are the edit the model proposed. This is the
                # only record of what it wanted to change if the patch is then rejected.
                run.output = {"operations": operations,
                              "applied": describe_patch(operations)}

            with _tool_run("lint", node="patch_node", attempt=attempt + 1) as run:
                findings = lint(patched)
                run.diagnostics = [d.as_dict() for d in findings]
                blockers = blocking(findings)
                if blockers:
                    raise RuntimeError(format_for_llm(blockers))

            _emit({"type": "progress", "node": "patch_node", "attempt": attempt + 1,
                   "max_attempts": attempts, "phase": "evaluate",
                   "message": f"Re-running {len(tests)} test case(s)"})

            # A failing suite after a clean edit is not necessarily a mistake. The user
            # asked for a behaviour change, so the cases that pinned the old behaviour are
            # *supposed* to disagree - retrying would have the agent fight the request it
            # was given. Report which ones moved and let the approval gate decide.
            with _tool_run("run_tests", node="patch_node", attempt=attempt + 1) as run:
                report = run_test_suite(json.dumps(patched), tests, trace=True) if tests else None
                regressions = [
                    r["name"] for r in (report or {}).get("results", [])
                    if r["status"] in ("failed", "errored")
                ]
                run.output = {"summary": (report or {}).get("summary"),
                              "regressions": regressions}
                # Deliberately not raised on a regression, so this records as ok. The edit
                # applied cleanly; a behaviour change is *supposed* to move the cases that
                # pinned the old behaviour, and scoring that as a tool failure would teach
                # the corpus that doing what the user asked is a mistake. Which cases moved
                # is in `output`, for a scorer to weigh later.
            result = "" if report is None else json.dumps(report["summary"])

            return {
                "test_regressions": regressions,
                "jdm_json": json.dumps(patched),
                "test_suite_json": json.dumps(tests),
                "evaluation_feedback": result,
                "usecase_name": state.get("canvas_graph_name") or state.get("selected_file") or "Policy",
                "build_status": "SUCCESS",
                "patch_log": describe_patch(operations),
                "lint_findings": [d.as_dict() for d in lint(patched)],
                "build_attempts_used": spent_before + attempt + 1,
                "messages": new_messages,
            }

        except Exception as exc:  # noqa: BLE001
            last_feedback = _repair_instruction(exc)
            print(f"  --> [Patch error]: {str(exc)[:100]}...")
            context.append(HumanMessage(content=last_feedback))
            continue

    return {
        "build_status": "ERROR",
        "evaluation_feedback": last_feedback or "The edit could not be applied.",
        "build_attempts_used": spent_before + attempts,
        "messages": new_messages,
    }


def _load_tests(state: AgentState) -> list:
    """The saved suite, so an edit is checked against what the policy already promised."""
    try:
        tests = json.loads(state.get("test_suite_json") or "[]")
        return tests if isinstance(tests, list) else []
    except json.JSONDecodeError:
        return []


# Step 3: Builder (Generate JDM and Tests)
def builder_node(state: AgentState):
    print("\n[Step 3: Builder/Evaluator]: Compiling & Testing Graph...")

    context = list(state["messages"])

    # Load the initial drafts provided by the Planner
    dsl_content = state.get("graph_plan_dsl", "")
    test_suite_json = state.get("test_suite_json", "[]")
    usecase_name = state.get("usecase_name", "Untitled")

    new_messages = []  # Track LLM responses during the loop to append later

    # Internal loop: attempt 0 validates the Planner's output directly; later attempts ask
    # the LLM to repair it. The budget lives in state, so re-entering the builder through
    # the final-approval loop cannot silently restart it and overrun the run's wall clock.
    spent_before = int(state.get("build_attempts_used") or 0)
    # A re-plan costs a whole LLM call out of the same wall clock, so the repair budget has
    # to shrink by it; sizing the loop as though planning were always one call is how a run
    # comes to be killed mid-repair with nothing checkpointed.
    MAX_ATTEMPTS = max(1, _max_build_attempts(int(state.get("plan_attempts_used") or 1))
                       - spent_before)
    started = time.monotonic()
    attempts_used = 0
    last_feedback = ""
    advisories: list = []

    for attempt in range(MAX_ATTEMPTS):
        attempts_used = attempt + 1

        # Stop while there is still time to report; being killed by the run budget
        # mid-repair would discard everything, since this node checkpoints only on return.
        if attempt and time.monotonic() - started > AGENT_RUN_TIMEOUT_SECONDS * 0.7:
            print("  --> [Builder]: wall-clock budget nearly spent; stopping early.")
            break

        if state.get("cancel_requested") or _user_cancelled(state):
            print("  --> [Builder]: Cancellation requested; stopping.")
            return {
                "build_status": "CANCELLED",
                "evaluation_feedback": "The build was cancelled before it completed.",
            }

        def progress(phase: str, message: str) -> None:
            _emit({
                "type": "progress",
                "node": "builder_node",
                "attempt": attempt + 1,
                "max_attempts": MAX_ATTEMPTS,
                "phase": phase,
                "message": message,
            })

        # --- LLM FIXER (Only runs if Attempt 0 failed) ---
        if attempt > 0:
            print(f"  --> [Attempt {attempt}]: Calling LLM to fix errors...")
            progress("llm", f"Revising the graph after attempt {attempt}")

            # `attempt + 1` matches the number the progress event shows the user, so a
            # corpus row can be lined up against what they actually saw happen.
            content = call_llm(PROMPT_BUILDER, context, node="builder_node",
                               attempt=attempt + 1, purpose="repair")
            new_messages.append(_assistant_message_from_llm(content, internal=True))
            context.append(_assistant_message_from_llm(content))

            # 1. Extract, accepting the near-misses a small model tends to produce
            new_dsl, new_tests, new_name = _extract_plan_blocks(content)

            # 2. Fallbacks if LLM skipped something
            if not new_dsl:
                # Nothing usable came back. This is the only shape worth spending an
                # attempt on, because there is no graph to compile.
                context.append(HumanMessage(
                    content="FORMAT ERROR: Could not find ---DSL STARTS--- and ---DSL ENDS--- markers. Please output the DSL within these boundaries."))
                continue
            dsl_content = new_dsl

            if not new_tests or new_tests == "[]":
                # Fallback to the history if the LLM was lazy
                print("  --> [Info]: Retained test suite from history.")
            else:
                test_suite_json = new_tests

            # A missing name is cosmetic - `usecase_name` already defaults, and the save
            # path handles it - so it must never cost an attempt or discard a working DSL.
            if new_name:
                usecase_name = new_name


        # Save to a scratch file for debugging
        _debug_write(dsl_content)

        # --- COMPILATION & TESTING ---
        # Each stage is recorded per attempt. `stage` numbers them as the user saw them,
        # so a corpus row lines up with the progress events for the same attempt.
        stage = attempt + 1
        try:
            print("  --> [Tool Call]: Compiling Markdown DSL to JSON")
            progress("parse", "Compiling the plan into a decision graph")
            with _tool_run("parse_dsl", node="builder_node", attempt=stage) as run:
                jdm_dict = parse_markdown_dsl(dsl_content)
                jdm_json = json.dumps(jdm_dict)
                run.output = {"nodes": len(jdm_dict.get("nodes", [])),
                              "edges": len(jdm_dict.get("edges", []))}

            # Now Evaluate against Zen Engine
            progress("compile", "Checking the graph and test suite structure")
            with _tool_run("check_format", node="builder_node", attempt=stage):
                is_valid, format_result = check_jdm_format(jdm_json, test_suite_json)
                if not is_valid:
                    raise ValueError(f"Test JSON Format Error: {format_result}")
                parsed_jdm, parsed_tests = format_result

            # Lint before behaviour. `create_decision` only deserializes, so a dangling
            # edge, a missing input node or a malformed expression compiles cleanly here
            # and only surfaces during evaluation - once per test case, as an opaque blob
            # with no node attached. Only errors block: a loop that refused to finish over
            # a style hint would never converge, so warnings and hints travel with the
            # result for the user to judge.
            with _tool_run("lint", node="builder_node", attempt=stage) as run:
                findings = lint(parsed_jdm)
                advisories = [d.as_dict() for d in findings]
                # Every finding, not only the blocking ones: a graph that merely warns is
                # worse training data than one that lints clean, and the difference is
                # invisible if only errors are kept.
                run.diagnostics = advisories
                run.output = {"errors": sum(1 for d in findings if d.severity == "error"),
                              "warnings": sum(1 for d in findings if d.severity == "warning"),
                              "hints": sum(1 for d in findings if d.severity == "hint")}
                blockers = blocking(findings)
                if blockers:
                    raise RuntimeError(format_for_llm(blockers))

            progress("evaluate", f"Running {len(parsed_tests)} test case(s) through the engine")
            with _tool_run("run_tests", node="builder_node", attempt=stage) as run:
                evaluation = evaluate(jdm_json, parsed_tests)
                eval_result = evaluation.feedback
                run.diagnostics = evaluation.as_diagnostic_dicts()
                run.output = evaluation.summary or None
                if not evaluation.ok:
                    raise RuntimeError(eval_result)

            # SUCCESS! Break the loop and return
            print("  --> Tests Passed!")
            return {
                "jdm_json": jdm_json,
                "test_suite_json": test_suite_json,
                "evaluation_feedback": eval_result,
                "usecase_name": usecase_name,
                "build_status": "SUCCESS",
                "lint_findings": advisories,
                "build_attempts_used": spent_before + attempts_used,
                "messages": new_messages  # Append debugging conversation to state
            }

        except Exception as e:
            last_feedback = _repair_instruction(e)
            print(f"  --> [Error Caught]: {str(e)[:100]}...")
            context.append(HumanMessage(content=last_feedback))
            continue  # Loop back and let the LLM try to fix it

    return {
        "build_status": "ERROR",
        # Carry the real reason, not just a count. This is the only thing the user sees on
        # a failed build, and the only thing the planner sees if the approval loop sends
        # the turn back around - a bare "failed after N attempts" told neither anything.
        "evaluation_feedback": last_feedback or (
            f"Failed to compile and test the Markdown DSL after {attempts_used} attempts."
        ),
        "build_attempts_used": spent_before + attempts_used,
        "messages": new_messages,
    }

# Step 4: Output Success
def output_node(state: AgentState):
    status = state.get("build_status", "SUCCESS")
    print(f"\n[Step 5: Output]: build_status={status}")

    # The graph itself reaches the canvas as a `graph_proposed` event, so the
    # chat carries a readable summary rather than a wall of JSON.
    try:
        jdm = json.loads(state.get("jdm_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        jdm = {}
    try:
        tests = json.loads(state.get("test_suite_json", "[]"))
    except (json.JSONDecodeError, TypeError):
        tests = []

    nodes = jdm.get("nodes", []) if isinstance(jdm, dict) else []
    name = state.get("usecase_name") or "the policy"

    # A failed build must say so. Reporting "all passing" over an empty graph
    # would be worse than useless: the canvas would stay blank while the chat
    # claimed success.
    no_plan = state.get("plan_status") == "EMPTY"
    if no_plan or status != "SUCCESS" or not nodes:
        feedback = (state.get("evaluation_feedback") or "").strip()
        label = "Last error:"
        if status == "CANCELLED":
            body = ["Stopped before the graph was finished. Nothing was changed."]
        elif no_plan:
            # Naming the builder here would be a lie: it was never entered. The advice has
            # to match the step that actually failed, which is the design, not the code.
            body = [
                f"I could not work out a graph structure for **{name}**.",
                "",
                "The planning step returned no design, so nothing was built or put on "
                "the canvas.",
                "",
                "This usually means the requirement is carrying too much at once. Try "
                "describing one decision at a time - what goes in, what comes out, and "
                "the rule connecting them - and I can extend it from there.",
            ]
            label = "What the planner returned:"
        else:
            body = [
                f"I could not build a working graph for **{name}**.",
                "",
                "Every attempt either failed to compile or did not satisfy its own "
                "test cases, so there is nothing to put on the canvas.",
                "",
                "Try narrowing the rules, or describe the inputs and the expected "
                "decision for one concrete example.",
            ]
        if feedback and status != "CANCELLED":
            body += ["", label, "", "```", feedback[:600], "```"]
        return {
            "messages": [_assistant_message_from_llm("\n".join(body))],
            "build_failed": True,
        }

    def _describe(node: dict) -> str:
        kind = {
            "decisionTableNode": "decision table",
            "functionNode": "function",
            "expressionNode": "expression",
            "switchNode": "switch",
            "inputNode": "input",
            "outputNode": "output",
        }.get(node.get("type", ""), node.get("type", "node"))
        return f"**{node.get('name', 'Unnamed')}** ({kind})"

    # An edit reports what it changed; a fresh build reports what it made. Listing every
    # node after a one-cell change buries the one line the reviewer actually needs.
    patch_log = state.get("patch_log") or []
    if patch_log:
        # Say plainly whether the edit was verified. "0 tests still passing" reads like a
        # result; it is the absence of one.
        if tests:
            headline = (f"Updated **{name}** - {len(patch_log)} "
                        f"{'change' if len(patch_log) == 1 else 'changes'}, "
                        f"{len(tests)} {'test' if len(tests) == 1 else 'tests'} still passing.")
        else:
            headline = (f"Updated **{name}** - {len(patch_log)} "
                        f"{'change' if len(patch_log) == 1 else 'changes'}. This policy has "
                        "no saved tests, so nothing checked the change.")
        lines = [headline, "", "What changed:", ""]
        lines += [f"- {entry}" for entry in patch_log]

        regressions = state.get("test_regressions") or []
        if regressions:
            # Deliberately not framed as a failure: the change was requested, so the cases
            # that pinned the old behaviour are meant to disagree. The user decides whether
            # the policy moved or the tests are now out of date.
            lines[0] = (f"Updated **{name}** - {len(patch_log)} "
                        f"{'change' if len(patch_log) == 1 else 'changes'}. "
                        f"{len(regressions)} saved "
                        f"{'test' if len(regressions) == 1 else 'tests'} now expect the old "
                        "behaviour:")
            lines += ["", "No longer matching:", ""]
            lines += [f"- {case}" for case in regressions[:8]]
            if len(regressions) > 8:
                lines.append(f"- ...and {len(regressions) - 8} more")
            lines += ["", "Approve if the policy is right and the tests need updating, or "
                          "tell me what to change instead."]
        elif tests:
            lines += ["", "Everything else is untouched. Review it on the canvas, then approve."]
        else:
            lines += ["", "Everything else is untouched. Ask me to write a test suite if you "
                          "want the change pinned down."]
    else:
        lines = [
            f"Built **{name}** - {len(nodes)} "
            f"{'node' if len(nodes) == 1 else 'nodes'}, "
            f"{len(tests)} {'test' if len(tests) == 1 else 'tests'}, all passing.",
            "",
            "The graph is on the canvas. Review it there, then approve to keep it.",
        ]
        if nodes:
            lines += ["", "Flow:", ""]
            lines += [f"- {_describe(n)}" for n in nodes]

    return {
        "messages": [_assistant_message_from_llm("\n".join(lines))],
        "build_failed": False,
    }


# Step 5: Human Final Approval
def human_final_approval_node(state: AgentState):
    print("\n[System Paused for Final Approval]")

    # --- FIRST PAUSE: Show only the chips ---
    payload_1 = {
        "prompt": "Please review the generated JDM and Test Cases shown above.",
        "options": ["Approve & Save", "Needs Change"]
    }

    user_response = interrupt(payload_1)
    response_text = str(user_response).strip()

    # --- SECOND PAUSE: Ask for text if they requested changes ---
    if response_text == "Needs Change":
        payload_2 = {
        "prompt": "📝 Please type the specific changes you need for the logic or test cases:",
        "options": []  # Empty options tells the UI to show a text box instead of chips
        }
        user_response = interrupt(payload_2)
        response_text = str(user_response).strip()

    # --- PROCESS THE FINAL DECISION ---
    if response_text == "Approve & Save" or response_text.upper() == "APPROVE":
        return {"final_approval_status": "APPROVED"}
    else:
        # Whatever they typed in the second pause becomes the feedback for the Planner
        return {
            "final_approval_status": "NEEDS_CHANGES",
            "messages": [HumanMessage(
                content=f"The user requested these specific changes:\n\n'{response_text}'\n\nPlease update the implementation plan, DSL, and test cases accordingly.")]
        }


# Step 6: Saves Files after Final Approval
def save_files_node(state: AgentState):
    print("\n[Step 6: Save]: Saving files to disk as JSON...")

    jdm_content = state.get("jdm_json", "{}")
    test_suite_content = state.get("test_suite_json", "[]")
    usecase_name = state.get("usecase_name", "Untitled")
    if not usecase_name or usecase_name == "Untitled":
        usecase_name = "Untitled"

    # Make the name safe for file systems
    safe_name = usecase_name.replace(' ', '_')
    jdm_filename = f"{safe_name}_jdm.json"
    test_filename = f"{safe_name}_tests.json"

    # Anchor on the repository, not the process cwd. These used to be relative
    # strings, so what was written and what was later read never lined up.
    graphs_dir = _repo_path("backend", "jdm_graphs")
    tests_dir = _repo_path("backend", "jdm_tests")

    try:
        # 1. Create the directories if they don't already exist
        os.makedirs(graphs_dir, exist_ok=True)
        os.makedirs(tests_dir, exist_ok=True)

        # 2. Construct the full file paths
        jdm_filepath = os.path.join(graphs_dir, jdm_filename)
        test_filepath = os.path.join(tests_dir, test_filename)

        # 3. Parse and Save
        jdm_dict = json.loads(jdm_content)
        test_suite_list = json.loads(test_suite_content)

        with open(jdm_filepath, "w", encoding="utf-8") as f:
            json.dump(jdm_dict, f, indent=2)

        with open(test_filepath, "w", encoding="utf-8") as f:
            json.dump(test_suite_list, f, indent=2)

        # Update the success message to show the correct paths
        final_message = f"✅ Files successfully saved as '{jdm_filepath}' and '{test_filepath}'."

    except Exception as e:
        final_message = f"❌ Error saving files: {str(e)}"

    print(final_message)
    return {"messages": [_assistant_message_from_llm(final_message)]}


# ==========================================
# 4. ROUTING & GRAPH COMPILATION
# ==========================================
def route_after_intent(state: AgentState):
    return {
        "CREATE": "triage_node",
        "MODIFY": "modify_triage_node",
        "TEST": "test_node",
        "EXPLAIN": "explain_node",
        "LINT": "lint_node",
    }.get(state.get("intent", "CREATE"), "triage_node")


def route_after_human_review(state: AgentState):
    if state.get("triage_status") == "APPROVED":
        # Editing an existing policy is a patch, not a rebuild: regenerating it from a plan
        # would mint new ids for every node and edge and lose whatever the model did not
        # happen to re-emit.
        if state.get("mode") == "EXISTING" and _is_non_empty_graph(
            state.get("existing_jdm_json") or state.get("canvas_jdm_json") or ""
        ):
            return "patch_node"
        return "planner_node"
    else:
        # If the human review resulted in NEEDS_INFO, loop back to the correct triage AI!
        if state.get("mode") == "EXISTING":
            return "modify_triage_node"
        return "triage_node"


def route_after_planner(state: AgentState):
    """A design the builder cannot use must never reach it.

    The edge here used to be unconditional, so a planner reply with no plan in it went
    straight on and the builder spent its entire repair budget asking the model to fix a
    document that was never written - then reported a build failure for what was a planning
    one. Re-planning is bounded because the retry only differs by the instruction added to
    it; once that has been tried, more attempts are just the same call again.
    """
    if state.get("plan_status") != "EMPTY":
        return "builder_node"
    if int(state.get("plan_attempts_used") or 0) < MAX_PLAN_ATTEMPTS:
        return "planner_node"
    return "output_node"


def route_after_final_approval(state: AgentState):
    if state.get("final_approval_status") == "APPROVED":
        return "save_files_node"
    return "planner_node"





# ==========================================
# 5. GRAPH DEFINITION
# ==========================================
# WORKFLOW DIAGRAM
# [ START ]
#              │
#              ▼
#      ┌───────────────┐
#      │ welcome_node  │
#      └───────┬───────┘
#              │
#              ▼
#    ◇ route_after_welcome ◇ ───────────▶ (Dynamic Targets)
#
#
# ======================================================================
#     EXISTING POLICY & MODIFICATION CLUSTER
# ======================================================================
#
#      ┌──────────────┐
#      │ explain_node │
#      └──────┬───────┘
#             │
#             ▼
# ┌───────────────────────┐
# │ action_selection_node │
# └───────────┬───────────┘
#             │
#             ▼
# ◇ route_after_action_selection ◇ ─────▶ (Dynamic Targets)
#
#
#      ┌───────────────────┐
#      │ modify_input_node │
#      └─────────┬─────────┘
#                │
#                ▼
#      ┌────────────────────┐
#      │ modify_triage_node │
#      └─────────┬──────────┘
#                │
#                ▼
# ┌──────────────────────────┐
# │ human_triage_review_node │
# └────────────┬─────────────┘
#              │
#              ▼
#  ◇ route_after_human_review ◇ ────────▶ (Dynamic Targets)
#
#
#      ┌───────────┐
#      │ test_node │────────────────────▶ [ END ]
#      └───────────┘
#
#
# ======================================================================
#     NEW POLICY CLUSTER
# ======================================================================
#
#      ┌─────────────┐
#      │ triage_node │
#      └──────┬──────┘
#             │
#             ▼
#   ◇ route_after_triage ◇ ─────────────▶ (Dynamic Targets)
#
#
# ======================================================================
#     GENERATION & OUTPUT CLUSTER
# ======================================================================
#
#      ┌──────────────┐
#      │ planner_node │
#      └──────┬───────┘
#             │
#             ▼
#      ┌──────────────┐
#      │ builder_node │
#      └──────┬───────┘
#             │
#             ▼
#   ◇ route_after_builder ◇ ────────────▶ (Dynamic Targets)
#
#
#      ┌─────────────┐
#      │ output_node │
#      └──────┬──────┘
#             │
#             ▼
# ┌───────────────────────────┐
# │ human_final_approval_node │
# └────────────┬──────────────┘
#              │
#              ▼
#  ◇ route_after_final_approval ◇ ──────▶ (Dynamic Targets)
#
#
#      ┌─────────────────┐
#      │ save_files_node │──────────────▶ [ END ]
#      └─────────────────┘

workflow = StateGraph(AgentState)

workflow.add_node("intent_router_node", intent_router_node)
workflow.add_node("explain_node", explain_node)
workflow.add_node("test_node", test_node)
workflow.add_node("lint_node", lint_node)
workflow.add_node("triage_node", triage_node)
workflow.add_node("modify_triage_node", modify_triage_node)
workflow.add_node("human_triage_review_node", human_triage_review_node)
workflow.add_node("planner_node", planner_node)
workflow.add_node("patch_node", patch_node)
workflow.add_node("builder_node", builder_node)
workflow.add_node("output_node", output_node)
workflow.add_node("human_final_approval_node", human_final_approval_node)
workflow.add_node("save_files_node", save_files_node)


# Define edges

# The UI selects the policy, so the run enters on the user's own message and is
# routed by inferred intent rather than by a chip-driven wizard.
workflow.add_edge(START, "intent_router_node")
workflow.add_conditional_edges(
    "intent_router_node",
    route_after_intent,
    {
        "triage_node": "triage_node",              # CREATE
        "modify_triage_node": "modify_triage_node",  # MODIFY
        "test_node": "test_node",                  # TEST
        "explain_node": "explain_node",            # EXPLAIN
    }
)

# Read-only intents finish in one pass.
workflow.add_edge("explain_node", END)
workflow.add_edge("test_node", END)
workflow.add_edge("lint_node", END)

workflow.add_edge("modify_triage_node", "human_triage_review_node")
workflow.add_edge("triage_node", "human_triage_review_node")

# --- 4. The Human Review Router (The source of the bug) ---
workflow.add_conditional_edges(
    "human_triage_review_node",
    route_after_human_review,
    # CRITICAL FIX: Explicit path map prevents silent termination!
    {
        "planner_node": "planner_node",
        "patch_node": "patch_node",
        "modify_triage_node": "modify_triage_node",
        "triage_node": "triage_node"
    }
)

# generation for new and existing policy
# The builder is only reachable with a design in hand; without one the planner is asked
# again, and if it still produces nothing the turn reports that rather than pretending
# the failure happened downstream.
workflow.add_conditional_edges(
    "planner_node",
    route_after_planner,
    {
        "builder_node": "builder_node",
        "planner_node": "planner_node",
        "output_node": "output_node",
    },
)
# The patch is applied, linted and tested inside the node, so it reports directly.
workflow.add_edge("patch_node", "output_node")
# The builder reports failure through build_status; either way the user sees the
# result, so this is a plain edge rather than a router with one destination.
workflow.add_edge("builder_node", "output_node")
# Nothing was produced, so there is nothing to approve: end the turn and let
# the user reply instead of offering "Approve & Save" over an empty canvas.
workflow.add_conditional_edges(
    "output_node",
    lambda state: END if state.get("build_failed") else "human_final_approval_node",
    {END: END, "human_final_approval_node": "human_final_approval_node"},
)
workflow.add_conditional_edges(
    "human_final_approval_node",
    route_after_final_approval,
    {
        "save_files_node": "save_files_node",
        "planner_node": "planner_node",
    }
)
workflow.add_edge("save_files_node", END)


# ==========================================
# 5. COMPILATION
# ==========================================
def build_graph(checkpointer=None):
    """Compile the workflow against a caller-supplied checkpointer.

    The web app injects an AsyncSqliteSaver so threads survive a restart;
    scripts and tests can pass nothing and get in-memory persistence.
    """
    return workflow.compile(checkpointer=checkpointer or MemorySaver())


# Convenience singleton for scripts and tests.
graph = build_graph()
