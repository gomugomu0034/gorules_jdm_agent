import json
import re
import os
from typing import TypedDict, Annotated
from dotenv import load_dotenv

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt
from langchain_core.messages import HumanMessage, AIMessage

# Provider SDKs
from google import genai
from google.genai import types
import openai

# Imports for your tools/prompts
from prompts.triage_node_prompt import PROMPT_TRIAGE
from prompts.planner_node_prompt import PROMPT_PLANNER
from prompts.builder_node_prompt import PROMPT_BUILDER
from prompts.modify_triage_node_prompt import PROMPT_MODIFY_TRIAGE
from prompts.explain_node_prompt import PROMPT_EXPLAIN, PROMPT_EXPLAIN_USER
from prompts.test_node_prompt import PROMPT_TEST, PROMPT_TEST_USER, PROMPT_TEST_REPORT

from tools.markdown_dsl_parser import parse_markdown_dsl
from tools.zen_evaluator import check_jdm_format, evaluate_against_zen





# ==========================================
# 1. CONFIGURATION
# ==========================================
load_dotenv()

# Select the active provider: "gemini", "litellm", or "huggingface"
ACTIVE_PROVIDER = os.getenv("LLM_PROVIDER", "litellm").lower()

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
HUGGINGFACE_BASE_URL = os.getenv("HUGGINGFACE_BASE_URL", "https://router.huggingface.co/v1")

# Initialize Clients Conditionally (so it doesn't crash if one key is missing)
gemini_client = None
litellm_client = None
huggingface_client = None

if ACTIVE_PROVIDER == "gemini":
    gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
elif ACTIVE_PROVIDER == "litellm":
    litellm_client = openai.OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)
elif ACTIVE_PROVIDER == "huggingface":
    huggingface_client = openai.OpenAI(
        base_url=HUGGINGFACE_BASE_URL,
        api_key=HUGGINGFACE_API_KEY,
    )
else:
    raise ValueError(f"Unsupported LLM_PROVIDER: {ACTIVE_PROVIDER}")


# ==========================================
# 2. UNIFIED LLM WRAPPER
# ==========================================

def _call_gemini(sys_prompt: str, messages: list) -> str:
    gemini_messages = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            gemini_messages.append(types.Content(role="user", parts=[types.Part.from_text(text=msg.content)]))
        elif isinstance(msg, AIMessage):
            gemini_messages.append(types.Content(role="model", parts=[types.Part.from_text(text=msg.content)]))

    config = types.GenerateContentConfig(system_instruction=sys_prompt, temperature=0.0)
    response = gemini_client.models.generate_content(
        model=GOOGLE_MODEL, contents=gemini_messages, config=config
    )
    return response.text


def _call_litellm(sys_prompt: str, messages: list) -> str:
    formatted = [{"role": "system", "content": sys_prompt}]
    for msg in messages:
        if isinstance(msg, HumanMessage):
            formatted.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            formatted.append({"role": "assistant", "content": msg.content})

    response = litellm_client.chat.completions.create(
        model=LITELLM_MODEL_NAME, messages=formatted,
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

    formatted = [{"role": "system", "content": sys_prompt}]
    for msg in messages:
        if isinstance(msg, HumanMessage):
            formatted.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            formatted.append({"role": "assistant", "content": msg.content})

    # Hugging Face selects a specific inference provider with the ':provider'
    # suffix. Preserve it if the configured model already includes one.
    model = HUGGINGFACE_MODEL_NAME
    if ":" not in model:
        model = f"{model}:{HUGGINGFACE_INFERENCE_PROVIDER}"

    response = huggingface_client.chat.completions.create(
        model=model,
        messages=formatted,
    )
    return response.choices[0].message.content


def call_llm(sys_prompt: str, messages: list) -> str:
    """Routes the request to the active LLM provider and returns the string response."""
    if ACTIVE_PROVIDER == "gemini":
        return _call_gemini(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "litellm":
        return _call_litellm(sys_prompt, messages)
    elif ACTIVE_PROVIDER == "huggingface":
        return _call_huggingface(sys_prompt, messages)


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
    selected_file: str  # The filename clicked
    existing_jdm_json: str  # The raw JSON content loaded from the file
    action_type: str  # "EXPLAIN", "MODIFY", or "TEST"


def _format_messages(langgraph_messages: list, system_prompt: str) -> list:
    formatted = [{"role": "system", "content": system_prompt}]
    for msg in langgraph_messages:
        if isinstance(msg, HumanMessage):
            formatted.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            formatted.append({"role": "assistant", "content": msg.content})
    return formatted

def _extract_two_jsons(text: str):
    blocks = re.findall(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
    if len(blocks) >= 2: return blocks[0].strip(), blocks[1].strip()
    return "{}", "[]"


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

# Step 1 : Welcome Node
def welcome_node(state: AgentState):
    print("\n[Welcome]: Scanning for existing policies...")

    # Ensure directory exists
    graphs_dir = "jdm_graphs"
    os.makedirs(graphs_dir, exist_ok=True)

    # Get list of existing JSON files
    existing_files = [f for f in os.listdir(graphs_dir) if f.endswith(".json")]

    # Combine the "New" option with the list of files
    options = ["✨ Create New Policy"] + existing_files

    payload = {
        "prompt": "👋 **Welcome to the GoRules Zen AI!**\n\nWould you like to create a new policy from scratch, or work on an existing one?",
        "options": options
    }

    user_response = interrupt(payload)
    response_text = str(user_response).strip()

    if response_text == "✨ Create New Policy":
        return {
            "mode": "NEW",
            # We don't add an AIMessage here, we just let the chat box prompt them
            # for their requirements, which will automatically resume the graph into Triage.
        }

    elif response_text in existing_files:
        # Load the JSON from the selected file
        filepath = os.path.join(graphs_dir, response_text)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                file_content = f.read()

            return {
                "mode": "EXISTING",
                "selected_file": response_text,
                "existing_jdm_json": file_content,
                "messages": [AIMessage(content=f"You selected to work on `{response_text}`.")]
            }
        except Exception as e:
            return {
                "mode": "NEW",
                "messages": [AIMessage(
                    content=f"❌ Error loading file: {str(e)}\n\nLet's create a new policy instead. What are your requirements?")]
            }
    else:
        # Fallback: if they ignored the chips and typed a custom prompt directly
        return {
            "mode": "NEW",
            "messages": [HumanMessage(content=response_text)]
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
    user_prompt = PROMPT_EXPLAIN_USER.format(existing_jdm=existing_jdm)

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
        "messages": [AIMessage(content=ui_message)]
    }


# Step 3 : Action Selection Node
def action_selection_node(state: AgentState):
    filename = state.get("selected_file", "this policy")
    print(f"\n[Action Selection]: Determining what to do with {filename}...")

    payload = {
        "prompt": "👉 **What would you like to do with this policy?**",
        "options": ["✏️ Modify Logic", "🧪 Test Logic"]
    }

    user_response = interrupt(payload)
    response_text = str(user_response).strip()

    action_mapping = {
        "✏️ Modify Logic": "MODIFY",
        "🧪 Test Logic": "TEST"
    }

    action_type = action_mapping.get(response_text, "TEST")

    return {
        "action_type": action_type,
        "messages": [AIMessage(content=f"You chose to {response_text.replace('✏️ ','').replace('🧪 ', '')}.")]
    }


# --- NEW: Input New Policy Node ---
def input_new_policy_node(state: AgentState):
    print("\n[Input New Policy]: Waiting for user requirements...")

    payload = {
        "prompt": "✨ **What kind of policy would you like to create?**\n*(e.g., 'Create a refund policy where VIPs get a 100% refund, and standard users get store credit.')*",
        "options": []  # Empty array forces the Streamlit text box
    }

    user_response = interrupt(payload)
    response_text = str(user_response).strip()

    return {
        "messages": [
            AIMessage(content="✨ **What kind of policy would you like to create?**"),
            HumanMessage(content=response_text)
        ]
    }

# Step 4a : Modify Input Node
def modify_input_node(state: AgentState):
    print("\n[Modify Input]: Waiting for user modification requests...")

    payload = {
        "prompt": "📝 **What specific changes would you like to make to this policy?**",
        "options": []  # Empty array forces the Streamlit text box
    }

    user_response = interrupt(payload)
    response_text = str(user_response).strip()

    return {
        "messages": [
            AIMessage(content="📝 **What specific changes would you like to make to this policy?**"),
            HumanMessage(content=response_text)
        ]
    }

# Step 4b : Modify Triage Node
def modify_triage_node(state: AgentState):
    print("\n[Modify Triage]: Evaluating requested changes against existing logic...")

    existing_jdm = state.get("existing_jdm_json", "")

    # 1. Dynamically inject the existing JDM into the prompt template
    prompt = PROMPT_MODIFY_TRIAGE.format(existing_jdm=existing_jdm)
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
            "messages": [AIMessage(content=triage_msg)]
        }
    except json.JSONDecodeError:
        error_msg = "Could you clarify how these changes should be applied?"
        return {
            "triage_status": "NEEDS_INFO",
            "triage_message": error_msg,
            "triage_options": ["Custom clarification"],
            "messages": [AIMessage(content=error_msg)]
        }


# Step 5 : Test Node
def test_node(state: AgentState):
    print("\n[Test Node]: Running standalone test execution...")

    existing_jdm = state.get("existing_jdm_json", "")
    filename = state.get("selected_file", "policy.json")

    # Ensure test directory exists
    test_dir = "jdm_tests"
    os.makedirs(test_dir, exist_ok=True)

    # 1. Check for existing test suite
    test_filepath = os.path.join(test_dir, filename.replace(".json", "_tests.json"))

    if os.path.exists(test_filepath):
        print("  -> Found existing test suite.")
        with open(test_filepath, "r", encoding="utf-8") as f:
            test_suite_json = f.read()
        source_msg = f"🧪 **Loaded existing tests from:** `{test_filepath}`"
    else:
        print("  -> No existing tests found. Generating new ones...")
        user_prompt = PROMPT_TEST_USER.format(existing_jdm=existing_jdm)

        messages = [HumanMessage(content=user_prompt)]
        content = call_llm(PROMPT_TEST, messages)
        test_suite_json = _extract_bounded_text(content, "---TESTS STARTS---", "---TESTS ENDS---", strip_lang="json")
        if not test_suite_json:
            test_suite_json = "[]"

        # Save the newly generated tests for future use!
        with open(test_filepath, "w", encoding="utf-8") as f:
            f.write(test_suite_json)
        source_msg = f"✨ **Generated new tests and saved to:** `{test_filepath}`"


    try:
        parsed_tests = json.loads(test_suite_json)
        success, eval_result = evaluate_against_zen(existing_jdm, parsed_tests)
    except json.JSONDecodeError as e:
        success = False
        eval_result = f"Failed to parse test suite JSON: {str(e)}"
    except Exception as e:
        success = False
        eval_result = f"Engine execution failed: {str(e)}"

    try:
        # If your engine doesn't format Pass/Fail nicely, you can ask the LLM to format the results into a report:
        report_prompt = f"""Here are the results of a GoRules Zen Engine test execution.
        Evaluation Results: {eval_result}
        """

        report_content = call_llm(PROMPT_TEST_REPORT,[HumanMessage(content=report_prompt)])
    except Exception as e:
        report_content = f"❌ **Error during test execution:**\n```\n{str(e)}\n```"

    # 3. Output the Final Report to the Chat
    ui_message = f"""{source_msg}
    📊 Testing Report
    {report_content}
    
    Refresh the page to work on another policy.
    """

    return {
        "messages": [AIMessage(content=ui_message)]
    }



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
            "messages": [AIMessage(content=triage_msg)]
        }
    except json.JSONDecodeError:
        error_msg = "Could you clarify the logic rules?"
        return {
            "triage_status": "NEEDS_INFO",
            "triage_message": error_msg,
            "triage_options": ["Custom clarification"],
            # CRITICAL FIX: Save the AI's error to the permanent chat history!
            "messages": [AIMessage(content=error_msg)]
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
    for attempt in range(8):

        # --- LLM FIXER (Only runs if Attempt 0 failed) ---
        if attempt > 0:
            print(f"  --> [Attempt {attempt}]: Calling LLM to fix errors...")

            content = call_llm(PROMPT_BUILDER, context)
            new_messages.append(AIMessage(content=content))
            context.append(AIMessage(content=content))

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


        # Save to file for debugging
        with open("debug_graph.md", "w", encoding="utf-8") as f:
            f.write(dsl_content)

        # --- COMPILATION & TESTING ---
        try:
            print("  --> [Tool Call]: Compiling Markdown DSL to JSON")
            jdm_dict = parse_markdown_dsl(dsl_content)
            jdm_json = json.dumps(jdm_dict)


            # Now Evaluate against Zen Engine
            is_valid, format_result = check_jdm_format(jdm_json, test_suite_json)
            if not is_valid:
                raise ValueError(f"Test JSON Format Error: {format_result}")

            parsed_jdm, parsed_tests = format_result
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
        "evaluation_feedback": "Failed to compile and test Markdown DSL after 8 ttempts."
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
    return {"messages": [AIMessage(content=final_output)]}


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

    # Define target directories
    graphs_dir = "jdm_graphs"
    tests_dir = "jdm_tests"

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
    return {"messages": [AIMessage(content=final_message)]}


# ==========================================
# 4. ROUTING & GRAPH COMPILATION
# ==========================================
def route_after_welcome(state: AgentState):
    if state.get("mode") == "EXISTING":
        return "explain_node"
    return "input_new_policy_node"


def route_after_action_selection(state: AgentState):
    action = state.get("action_type")
    if action == "MODIFY":
        return "modify_input_node"
    return "test_node" # TEST is the only other option


def route_after_triage(state: AgentState):
    if state["triage_status"] in ["NEEDS_INFO", "READY_FOR_APPROVAL"]:
        return "human_triage_review_node"
    return "planner_node"

def route_after_human_review(state: AgentState):
    if state.get("triage_status") == "APPROVED":
        return "planner_node"
    else:
        # If the human review resulted in NEEDS_INFO, loop back to the correct triage AI!
        if state.get("mode") == "EXISTING":
            return "modify_triage_node"
        return "triage_node"


def route_after_evaluation(state: AgentState):
    if state["build_status"] == "ERROR":
        return "builder_node"
    return "output_node"


def route_after_final_approval(state: AgentState):
    if state.get("final_approval_status") == "APPROVED":
        return "save_files_node"
    return "planner_node"

# Builder handles its own failures, so if it finishes, route to output or loop to human
def route_after_builder(state: AgentState):
    if state["build_status"] == "ERROR":
        # If it genuinely fails 5 times, you could route back to triage or human review.
        # For safety, let's show the output node so the user sees the failure.
        return "output_node"
    return "output_node"





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

workflow.add_node("welcome_node", welcome_node)
workflow.add_node("action_selection_node", action_selection_node)
workflow.add_node("input_new_policy_node", input_new_policy_node)
workflow.add_node("explain_node", explain_node)
workflow.add_node("test_node", test_node)
workflow.add_node("modify_input_node", modify_input_node)
workflow.add_node("triage_node", triage_node)
workflow.add_node("modify_triage_node", modify_triage_node)
workflow.add_node("human_triage_review_node", human_triage_review_node)
workflow.add_node("planner_node", planner_node)
workflow.add_node("builder_node", builder_node)
workflow.add_node("output_node", output_node)
workflow.add_node("human_final_approval_node", human_final_approval_node)
workflow.add_node("save_files_node", save_files_node)


# Define edges

# welcome
workflow.add_edge(START, "welcome_node")
workflow.add_conditional_edges(
    "welcome_node",
    route_after_welcome,
    # CRITICAL FIX: Explicit path map
    {
        "explain_node": "explain_node",
        "input_new_policy_node": "input_new_policy_node"
    }
)


# existing policy
workflow.add_edge("explain_node", "action_selection_node")
workflow.add_conditional_edges(
    "action_selection_node",
    route_after_action_selection,
    # CRITICAL FIX: Explicit path map
    {
        "explain_node": "explain_node", # Fallback
        "modify_input_node": "modify_input_node",
        "test_node": "test_node"
    }
)
workflow.add_edge("test_node", END)
workflow.add_edge("modify_input_node", "modify_triage_node")
workflow.add_edge("modify_triage_node", "human_triage_review_node")

# new policy
workflow.add_edge("input_new_policy_node", "triage_node")
# Direct edge into human review (NO conditional edge here)
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
workflow.add_conditional_edges("builder_node", route_after_builder)
workflow.add_edge("output_node", "human_final_approval_node")
workflow.add_conditional_edges("human_final_approval_node", route_after_final_approval)
workflow.add_edge("save_files_node", END)


# Compile
memory = MemorySaver()
graph = workflow.compile(checkpointer=memory)
