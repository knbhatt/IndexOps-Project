import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexops.mcp_client import MCPConnection
from indexops.agent.loop import run_investigation
from indexops.db import fetch_all, fetch_one


async def main():
    q = "why are so many tickets incorrectly categorized?"
    print("question:", q)
    conn = MCPConnection()
    await conn.start()
    print("tools:", len(conn.tools))
    try:
        result = await run_investigation(conn, question=q)
    finally:
        await conn.stop()
    print("incident_id:", result.incident_id)
    print("status:", result.status)
    print("alert_id:", result.alert_id)
    print("llm_calls:", result.llm_calls)
    print("tools_used:", result.tools_used)
    if result.report:
        print("category:", result.report.get("root_cause_category"))
        print("root_cause:", result.report.get("root_cause"))
    steps = fetch_all(
        "SELECT step_id, tool_name FROM investigation_steps WHERE incident_id=%s ORDER BY step_id",
        (result.incident_id,),
    )
    print("steps:", [(s["step_id"], s["tool_name"]) for s in steps])
    # Confirm no new alert was required / created by this path
    print("open_alerts:", fetch_one("SELECT COUNT(*) AS n FROM alerts WHERE status='open'")["n"])


if __name__ == "__main__":
    asyncio.run(main())
