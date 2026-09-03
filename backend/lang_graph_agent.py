import json
import re
import os
import tempfile
from pathlib import Path
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

from backend.tools.markdown_dsl_parser import parse_markdown_dsl
from backend.tools.zen_evaluator import check_jdm_format, evaluate_against_zen, run_test_suite





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


class LLMResponse(str):
    """String-like LLM output that can carry provider metadata alongside content."""

    def __new__(cls, content: str, reasoning_details=None):
        obj = str.__new__(cls, content)
        obj.reasoning_details = reasoning_details
        return obj


def _assistant_message_from_llm(response: str, content: str | None = None) -> AIMessage:
    reasoning_details = getattr(response, "reasoning_details", None)
    message_content = str(response) if content is None else content
    if reasoning_details is not None:
        return AIMessage(
            content=message_content,
            additional_kwargs={"reasoning_details": reasoning_details},
        )
    return AIMessage(content=message_content)


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
        temperature=0.0,
        http_options=types.HttpOptions(timeout=LLM_TIMEOUT_SECONDS * 1000),
    )
    response = gemini_client.models.generate_content(
        model=GOOGLE_MODEL, contents=gemini_messages, config=config
    )
    return response.text


def _call_litellm(sys_prompt: str, messages: list) -> str:
    formatted = _format_chat_messages(sys_prompt, messages)
    response = litellm_client.chat.completions.create(
        model=LITELLM_MODEL_NAME, messages=formatted,
        timeout=LLM_TIMEOUT_SECONDS,
        # temperature=0.0
    )
    return response.choices[0].message.content


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
    )
    return response.choices[0].message.content


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
    response.raise_for_status()

    response_json = response.json()
    message = response_json["choices"][0]["message"]
    return LLMResponse(
        message.get("content", ""),
        reasoning_details=message.get("reasoning_details"),
    )


def call_llm(sys_prompt: str, messages: list) -> str:
    """Routes the request to the active LLM provider and returns the string response."""
    if LLM_INIT_ERROR:
        raise RuntimeError(LLM_INIT_ERROR)
    if ACTIVE_PROVIDER == "gemini":
        return _call_gemini(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "litellm":
        return _call_litellm(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "huggingface":
        return _call_huggingface(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "openrouter":
        return _call_openrouter(sys_prompt, messages)
    raise ValueError(f"Unsupported LLM_PROVIDER: {ACTIVE_PROVIDER}")


# ==========================================
# 3. STATE & HELPERS
# ==========================================
class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    triage_status: str
    triage_message: str
    triage_options: list
    graph_plan_dsl: str
    jdm_json: str
    test_suite_json: str
    evaluation_feedback: str
    build_status: str
    final_approval_status: str
    usecase_name: str
    mode: str  # "NEW" or "EXISTING"
    selected_file: str  # Display name of the policy being worked on
    existing_jdm_json: str  # The raw JSON of the policy under discussion
    action_type: str  # "EXPLAIN", "MODIFY", or "TEST"
    # --- Web studio fields ---
    canvas_jdm_json: str  # The graph currently on the canvas, unsaved edits included
    canvas_graph_id: str  # Database id, "" for a scratch canvas
    canvas_graph_name: str
    intent: str  # "CREATE" | "MODIFY" | "TEST" | "EXPLAIN"
    intent_confidence: float
    cancel_requested: bool


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


def _extract_bounded_text(content: str, start_marker: str, end_marker: str, strip_lang: str = "") -> str:
    """Extracts text between custom markers and strips any rogue markdown backticks."""
    # Find text between markers
    pattern = rf'{start_marker}\s*(.*?)\s*{end_marker}'
    match = re.search(pattern, content, re.DOTALL)

    if not match:
        return ""
    text = match.group(1).strip()
    # Strip rogue opening backticks (e.g., ```markdown or ```json)
    if text.startswith('```'):
        text = re.sub(rf'^```(?:{strip_lang})?\s*', '', text, flags=re.IGNORECASE)
    # Strip rogue closing backticks
    if text.endswith('```'):
        text = re.sub(r'```$', '', text).strip()

    return text.strip()

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

    if _EXPLAIN_RE.search(text):
        return "EXPLAIN", 0.9
    if _TEST_RE.search(text):
        return "TEST", 0.9
    if _CREATE_RE.search(text) and re.search(r"\b(new|scratch|over)\b", text, re.IGNORECASE):
        return "CREATE", 0.85
    if _MODIFY_RE.search(text):
        return "MODIFY", 0.85

    try:
        raw = call_llm(PROMPT_INTENT, [HumanMessage(content=text)])
        parsed = json.loads(_extract_single_json(raw))
        intent = str(parsed.get("intent", "")).upper()
        if intent in ("CREATE", "MODIFY", "TEST", "EXPLAIN"):
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
    explanation = call_llm(PROMPT_EXPLAIN, messages)

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
    response_text = call_llm(prompt, state["messages"])
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
        content = call_llm(PROMPT_TEST, [HumanMessage(content=user_prompt)])
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
        report = run_test_suite(existing_jdm, parsed_tests)
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
def triage_node(state: AgentState):
    print(f"\n[Step 1: Triage]: Evaluating requirements using {ACTIVE_PROVIDER.upper()}...")

    response_text = call_llm(PROMPT_TRIAGE, state["messages"])
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
def planner_node(state: AgentState):
    print("\n[Step 2: Planner]: Generating DSL Implementation Plan...")

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

    # Call your LLM using messages_for_planner instead of state["messages"]
    content = call_llm(PROMPT_PLANNER, messages_for_planner)
    print(f"planner node content: {content}")

    # Use the robust extraction
    dsl_content = _extract_bounded_text(content, "---DSL STARTS---", "---DSL ENDS---", strip_lang="markdown")
    test_suite_json = _extract_bounded_text(content, "---TESTS STARTS---", "---TESTS ENDS---", strip_lang="json")
    usecase_name = _extract_bounded_text(content, "---USECASE NAME STARTS---", "---USECASE NAME ENDS---")

    # Fallback to empty array if tests weren't found
    if not test_suite_json:
        test_suite_json = "[]"

    return {
        "graph_plan_dsl": dsl_content,
        "test_suite_json": test_suite_json,
        "usecase_name": usecase_name,
        # "messages": [AIMessage(content=content)]
    }


# Step 3: Builder (Generate JDM and Tests)
def builder_node(state: AgentState):
    print("\n[Step 3: Builder/Evaluator]: Compiling & Testing Graph...")

    context = list(state["messages"])

    # Load the initial drafts provided by the Planner
    dsl_content = state.get("graph_plan_dsl", "")
    test_suite_json = state.get("test_suite_json", "[]")
    usecase_name = state.get("usecase_name", "Untitled")

    new_messages = []  # Track LLM responses during the loop to append later

    # Internal loop: Attempt 0 uses the Planner's output directly. Attempt 1-4 uses the LLM.
    MAX_ATTEMPTS = 8
    for attempt in range(MAX_ATTEMPTS):

        if state.get("cancel_requested"):
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

            content = call_llm(PROMPT_BUILDER, context)
            new_messages.append(_assistant_message_from_llm(content))
            context.append(_assistant_message_from_llm(content))

            # 1. Extract using the robust markers
            dsl_content = _extract_bounded_text(content, "---DSL STARTS---", "---DSL ENDS---", strip_lang="markdown")
            test_suite_json = _extract_bounded_text(content, "---TESTS STARTS---", "---TESTS ENDS---", strip_lang="json")
            usecase_name = _extract_bounded_text(content, "---USECASE NAME STARTS---", "---USECASE NAME ENDS---")

            # 2. Fallbacks if LLM skipped something
            if not dsl_content:
                # If it completely failed to output DSL, prompt it
                context.append(HumanMessage(
                    content="FORMAT ERROR: Could not find ---DSL STARTS--- and ---DSL ENDS--- markers. Please output the DSL within these boundaries."))
                continue

            if not test_suite_json or test_suite_json == "[]":
                # Fallback to the history if the LLM was lazy
                test_suite_json = state.get("test_suite_json", "[]")
                print("  --> [Info]: Retained test suite from history.")

            if not usecase_name:
                # If it completely failed to output DSL, prompt it
                context.append(HumanMessage(
                    content="FORMAT ERROR: Could not find ---USECASE NAME STARTS--- and ---USECASE NAME ENDS--- markers. Please output the Usecase name within these boundaries."))
                continue


        # Save to a scratch file for debugging
        _debug_write(dsl_content)

        # --- COMPILATION & TESTING ---
        try:
            print("  --> [Tool Call]: Compiling Markdown DSL to JSON")
            progress("parse", "Compiling the plan into a decision graph")
            jdm_dict = parse_markdown_dsl(dsl_content)
            jdm_json = json.dumps(jdm_dict)


            # Now Evaluate against Zen Engine
            progress("compile", "Checking the graph and test suite structure")
            is_valid, format_result = check_jdm_format(jdm_json, test_suite_json)
            if not is_valid:
                raise ValueError(f"Test JSON Format Error: {format_result}")

            parsed_jdm, parsed_tests = format_result
            progress("evaluate", f"Running {len(parsed_tests)} test case(s) through the engine")
            is_success, eval_result = evaluate_against_zen(jdm_json, parsed_tests)

            if not is_success:
                raise RuntimeError(eval_result)

            # SUCCESS! Break the loop and return
            print("  --> Tests Passed!")
            return {
                "jdm_json": jdm_json,
                "test_suite_json": test_suite_json,
                "evaluation_feedback": eval_result,
                "usecase_name": usecase_name,
                "build_status": "SUCCESS",
                "messages": new_messages  # Append debugging conversation to state
            }

        except Exception as e:
            error_msg = f"SYSTEM ERROR:\n{str(e)}\n\nPlease fix the logic and output the corrected DSL and Test array."
            print(f"  --> [Error Caught]: {str(e)[:100]}...")
            context.append(HumanMessage(content=error_msg))
            continue  # Loop back and let the LLM try to fix it

    return {
        "build_status": "ERROR",
        "evaluation_feedback": f"Failed to compile and test the Markdown DSL after {MAX_ATTEMPTS} attempts.",
        "messages": new_messages,
    }

# Step 4: Output Success
def output_node(state: AgentState):
    print("\n[Step 5: Output]: Compilation and Testing Complete.")

    jdm_json = state.get("jdm_json", "{}")
    test_suite_json = state.get("test_suite_json", "[]")

    # Dump without 'indent' to force a single-line compact JSON string
    try:
        jdm_compact = json.dumps(json.loads(jdm_json))
    except Exception:
        # Fallback: manually strip newlines if parsing fails
        jdm_compact = jdm_json.replace('\n', '').replace('\r', '')

    try:
        tests_compact = json.dumps(json.loads(test_suite_json))
    except Exception:
        tests_compact = test_suite_json.replace('\n', '').replace('\r', '')

    final_output = f"""✅ **GoRules Zen Graph Successfully Generated and Tested!**
    
        <details>
        <summary><b>📜 Click to expand Generated JDM Graph</b></summary>
        
        ```json
        {jdm_compact}
        ```
        
        </details>
        
        <details>
        <summary><b>🧪 Click to expand Generated Test Cases</b></summary>
        
        ```json
        {tests_compact}
        ```
        
        </details>
        """
    return {"messages": [_assistant_message_from_llm(final_output)]}


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
    }.get(state.get("intent", "CREATE"), "triage_node")


def route_after_human_review(state: AgentState):
    if state.get("triage_status") == "APPROVED":
        return "planner_node"
    else:
        # If the human review resulted in NEEDS_INFO, loop back to the correct triage AI!
        if state.get("mode") == "EXISTING":
            return "modify_triage_node"
        return "triage_node"


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
workflow.add_node("triage_node", triage_node)
workflow.add_node("modify_triage_node", modify_triage_node)
workflow.add_node("human_triage_review_node", human_triage_review_node)
workflow.add_node("planner_node", planner_node)
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

workflow.add_edge("modify_triage_node", "human_triage_review_node")
workflow.add_edge("triage_node", "human_triage_review_node")

# --- 4. The Human Review Router (The source of the bug) ---
workflow.add_conditional_edges(
    "human_triage_review_node",
    route_after_human_review,
    # CRITICAL FIX: Explicit path map prevents silent termination!
    {
        "planner_node": "planner_node",
        "modify_triage_node": "modify_triage_node",
        "triage_node": "triage_node"
    }
)

# generation for new and existing policy
# Planner feeds directly to Builder
workflow.add_edge("planner_node", "builder_node")
# The builder reports failure through build_status; either way the user sees the
# result, so this is a plain edge rather than a router with one destination.
workflow.add_edge("builder_node", "output_node")
workflow.add_edge("output_node", "human_final_approval_node")
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
