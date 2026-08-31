# backend/main.py
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class RuleRequest(BaseModel):
    requirements: str

@app.post("/api/generate")
async def generate_rule(request: RuleRequest):
    # TODO: Replace with: jdm_json = run_langgraph_agent(request.requirements)
    
    # Temporary skeleton graph to test the UI integration
    skeleton_jdm = {
        "nodes": [
            {"id": "node-1", "name": "Input", "type": "inputNode", "position": {"x": 100, "y": 150}},
            {"id": "node-2", "name": "Output", "type": "outputNode", "position": {"x": 500, "y": 150}}
        ],
        "edges": []
    }
    
    return {"jdm": skeleton_jdm}