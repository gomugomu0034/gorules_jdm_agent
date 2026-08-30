# backend/main.py
from fastapi import FastAPI
from backend.lang_graph_agent import graph

app = FastAPI()

@app.post("/api/generate")
async def generate_rule(requirements: str):
    # Pass input to your existing LangGraph setup
    return graph(requirements)