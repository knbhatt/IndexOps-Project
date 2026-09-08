"""IndexOps FastAPI backend.

On startup it spawns mcp_server.py as a stdio subprocess (real MCP protocol) and
keeps that session open for the app's lifetime. Run with:

    uvicorn api.main:app --reload --port 8000
"""
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexops.mcp_client import MCPConnection  # noqa: E402
from indexops.agent.loop import run_investigation  # noqa: E402
from indexops.approval import approve  # noqa: E402
from indexops.db import fetch_all, fetch_one  # noqa: E402
from indexops import llm_client as llm  # noqa: E402

mcp_conn = MCPConnection()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await mcp_conn.start()
    app.state.mcp = mcp_conn
    print(f"[api] MCP server connected with {len(mcp_conn.tools)} tools", flush=True)
    print(f"[api] LLM: {llm.describe()}", flush=True)
    try:
        yield
    finally:
        await mcp_conn.stop()


app = FastAPI(title="Intelligent IndexOps", version="0.1.0", lifespan=lifespan)


class ToolCall(BaseModel):
    arguments: dict[str, Any] = {}


class InvestigateRequest(BaseModel):
    alert_id: int | None = None
    run_id: int | None = None
    question: str | None = None


class ApproveRequest(BaseModel):
    action_id: int = Field(..., description="remediation_actions.action_id to execute")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mcp_connected": mcp_conn.session is not None,
        "tool_count": len(mcp_conn.tools),
        "llm": llm.describe(),
    }


@app.get("/tools")
async def list_tools():
    return {"tools": mcp_conn.tools}


@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, body: ToolCall):
    """Call any MCP tool through the live subprocess connection (debug / smoke test)."""
    if tool_name not in {t["name"] for t in mcp_conn.tools}:
        raise HTTPException(404, f"unknown tool '{tool_name}'")
    return await mcp_conn.call_tool(tool_name, body.arguments)


@app.post("/investigate")
async def investigate(body: InvestigateRequest):
    """Run the agent loop from an alert / run / free-form question. Returns the incident report."""
    if body.alert_id is None and body.run_id is None and not body.question:
        raise HTTPException(400, "Provide alert_id, run_id, or question")
    result = await run_investigation(
        mcp_conn, alert_id=body.alert_id, run_id=body.run_id, question=body.question,
    )
    return {
        "incident_id": result.incident_id,
        "status": result.status,
        "run_id": result.run_id,
        "alert_id": result.alert_id,
        "llm_calls": result.llm_calls,
        "tools_used": result.tools_used,
        "usage": result.usage_totals,
        "report": result.report,
    }


@app.get("/incidents")
async def list_incidents(limit: int = 20):
    rows = fetch_all(
        """SELECT incident_id, alert_id, run_id, status, confidence,
                  root_cause, recommended_action, created_at
           FROM incidents ORDER BY incident_id DESC LIMIT %s""",
        (limit,),
    )
    return {"incidents": rows}


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: int):
    inc = fetch_one("SELECT * FROM incidents WHERE incident_id = %s", (incident_id,))
    if not inc:
        raise HTTPException(404, f"incident {incident_id} not found")
    steps = fetch_all(
        """SELECT step_id, tool_name, tool_input, tool_output, created_at
           FROM investigation_steps WHERE incident_id = %s ORDER BY step_id""",
        (incident_id,),
    )
    return {"incident": inc, "steps": steps}


@app.post("/approve")
async def approve_action(body: ApproveRequest):
    """Human-approval gate: execute a pending remediation_actions row."""
    result = approve(body.action_id)
    if result.get("error") and "not found" in str(result.get("error", "")):
        raise HTTPException(404, result["error"])
    if result.get("error") and "not 'pending'" in str(result.get("error", "")):
        raise HTTPException(400, result["error"])
    return result
