import streamlit as st
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import Command
import uuid

# Import your compiled graph from agent.py
from lang_graph_agent import graph

st.set_page_config(page_title="Zen Engine Agent", page_icon="⚙️", layout="wide")
st.title("⚙️ GoRules Zen AI")

# ==========================================
# 1. INITIALIZE SESSION STATE
# ==========================================
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

thread_config = {"configurable": {"thread_id": st.session_state.thread_id}}
state = graph.get_state(thread_config)

# If the state is completely empty, give it an initial push so it hits the welcome_node
if not state.values and not state.next:
    with st.spinner("Initializing Agent..."):
        # Send an empty message array to kick off the START edge
        for _ in graph.stream({"messages": []}, config=thread_config):
            pass
        # Rerun to instantly show the welcome_node chips
        st.rerun()

# ==========================================
# 2. RENDER CHAT HISTORY
# ==========================================
if "messages" in state.values:
    for msg in state.values["messages"]:
        if isinstance(msg, HumanMessage):
            with st.chat_message("user"):
                st.write(msg.content)
        elif isinstance(msg, AIMessage):
            with st.chat_message("assistant"):
                # CRITICAL CHANGE: Use st.markdown with unsafe_allow_html=True
                st.markdown(msg.content, unsafe_allow_html=True)

# Flag to control when the text box appears
show_chat_input = False

# ==========================================
# 3. RENDER INTERRUPT UI (CHIPS)
# ==========================================
if state.next:
    # Safely extract the interrupt payload if it exists
    interrupt_payload = {}
    if state.tasks and hasattr(state.tasks[0], 'interrupts') and state.tasks[0].interrupts:
        interrupt_payload = state.tasks[0].interrupts[0].value

    if isinstance(interrupt_payload, dict):
        prompt_text = interrupt_payload.get("prompt", "")
        options = interrupt_payload.get("options", [])

        with st.chat_message("assistant"):
            st.markdown(prompt_text)

            # STEP 1: Show Chips (Buttons) vertically if options exist
            if len(options) > 0:
                st.write("👉 **Please select an option below:**")

                # Render buttons vertically (no columns used)
                for idx, option_text in enumerate(options):
                    if st.button(option_text, key=f"chip_{idx}", use_container_width=True):
                        with st.spinner("Processing selection..."):
                            # FIX: You MUST iterate over the generator to execute the graph!
                            command = Command(resume=option_text)
                            for _ in graph.stream(command, config=thread_config):
                                pass
                        st.rerun()

            # STEP 2: Show text prompt if options are empty (Custom Clarification)
            else:
                show_chat_input = True
                st.info("👇 Please type your response in the chat box below.")
else:
    # If the graph is not paused, we are ready for a brand new request
    show_chat_input = True

# ==========================================
# 4. MAIN CHAT INPUT
# ==========================================
# Only render the chat box if the flag is True
if show_chat_input:
    user_input = st.chat_input("Type your requirements or responses here...")

    if user_input:
        with st.chat_message("user"):
            st.write(user_input)

        with st.spinner("Agent is thinking..."):
            if state.next:
                # Resume graph with text
                command = Command(resume=user_input)
                for _ in graph.stream(command, config=thread_config):
                    pass
            else:
                # Start new graph
                initial_input = {"messages": [HumanMessage(content=user_input)]}
                for _ in graph.stream(initial_input, config=thread_config):
                    pass

        st.rerun()