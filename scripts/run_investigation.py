"""Run one IndexOps investigation from the command line (Task 17 style: from a real alert).

Usage:
  python scripts/run_investigation.py --alert 1
  python scripts/run_investigation.py --run 2
  python scripts/run_investigation.py --question "why did indexation fail on the latest run?"
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from indexops import llm_client as llm
from indexops.mcp_client import MCPConnection
from indexops.agent.loop import run_investigation
from indexops.db import fetch_all


def _print_step(tool_name, data):
    if tool_name == "submit_incident_report":
        print(f"  >> submit_incident_report (finalizing)")
    else:
        args = data.get("input", {})
        print(f"  -> {tool_name}({json.dumps(args, default=str)})")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alert", type=int)
    ap.add_argument("--run", type=int)
    ap.add_argument("--question", type=str)
    args = ap.parse_args()

    print("LLM:", llm.describe())
    conn = MCPConnection()
    await conn.start()
    print(f"MCP connected: {len(conn.tools)} tools\n")
    try:
        result = await run_investigation(
            conn, alert_id=args.alert, run_id=args.run, question=args.question, on_step=_print_step,
        )
    finally:
        await conn.stop()

    print("\n" + "=" * 70)
    print(f"incident_id : {result.incident_id}")
    print(f"status      : {result.status}")
    print(f"llm_calls   : {result.llm_calls}")
    print(f"tools_used  : {result.tools_used}")
    print(f"usage       : {result.usage_totals}")
    if result.report:
        r = result.report
        print("\n--- REPORT ---")
        print(f"category    : {r.get('root_cause_category')}")
        print(f"severity    : {r.get('severity')}  confidence: {r.get('confidence')}")
        print(f"root_cause  : {r.get('root_cause')}")
        print(f"affected    : {r.get('affected_ticket_count')}")
        print(f"recommend   : {r.get('recommended_action')}")
        print(f"remediation : {r.get('proposed_remediation_tool')}")
        print("evidence    :")
        for e in r.get("evidence", []):
            print(f"   - [{e.get('source_tool')}] {e.get('finding')} :: {e.get('detail', '')}")

    print("\n--- investigation_steps (from DB) ---")
    for s in fetch_all(
        "SELECT step_id, tool_name FROM investigation_steps WHERE incident_id = %s ORDER BY step_id",
        (result.incident_id,),
    ):
        print(f"   #{s['step_id']} {s['tool_name']}")


if __name__ == "__main__":
    asyncio.run(main())
