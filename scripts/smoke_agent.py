"""Smoke test: two back-to-back tool-calling investigations against live Gemini via MCP.

Verifies mcp_tools_to_llm_tools() + llm_client.chat() against real API responses,
and reports per-investigation LLM call counts, any 429s, and whether backoff recovered.
Run: python scripts/smoke_agent.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import RateLimitError, APIStatusError

from indexops import llm_client as llm
from indexops.mcp_client import MCPConnection

# Instrument the raw client to count every API attempt and classify failures.
# "recovered" = an attempt that raised a retryable error (429 or 5xx) but the
# overall investigation still completed thanks to llm_client's backoff-retry.
stats = {"attempts": 0, "rate_limited": 0, "server_errors": 0, "other_errors": 0}
_orig_get_client = llm._get_client


def _patched():
    c = _orig_get_client()
    if not getattr(c, "_patched", False):
        orig = c.chat.completions.create

        def counted(**kw):
            stats["attempts"] += 1
            try:
                return orig(**kw)
            except RateLimitError:
                stats["rate_limited"] += 1
                raise
            except APIStatusError as e:
                if e.status_code >= 500:
                    stats["server_errors"] += 1
                else:
                    stats["other_errors"] += 1
                raise
            except Exception:
                stats["other_errors"] += 1
                raise

        c.chat.completions.create = counted
        c._patched = True
    return c


llm._get_client = _patched

SYSTEM = (
    "You are an SRE investigating an Airflow->OpenSearch ticket indexing pipeline. "
    "Use the tools to gather evidence (recent runs, run metrics, pipeline config, source-vs-index diff, "
    "knowledge base). Do NOT call remediation tools. When you have enough evidence, stop calling tools "
    "and answer with a short root-cause summary citing concrete numbers."
)


async def investigate(conn, question, label):
    tools = llm.mcp_tools_to_llm_tools(conn.tools)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]
    calls, tool_seq, t0 = 0, [], time.time()
    for _ in range(12):
        r = llm.chat(messages, tools=tools)
        calls += 1
        messages.append(r.raw_message)
        if not r.wants_tools:
            print(f"\n[{label}] DONE in {calls} LLM calls, {time.time()-t0:.0f}s, "
                  f"cached_tokens(last)={r.usage.get('cached_tokens')}")
            print(f"[{label}] tools used: {tool_seq}")
            print(f"[{label}] answer: {(r.text or '')[:700]}")
            return calls
        for tc in r.tool_calls:
            result = await conn.call_tool(tc.name, tc.arguments)
            tool_seq.append(tc.name)
            s = json.dumps(result, default=str)
            if len(s) > 6000:
                s = s[:6000] + "...<truncated>"
            messages.append(llm.tool_result_message(tc, s))
            print(f"[{label}] call {calls}: {tc.name}({json.dumps(tc.arguments)}) -> {len(s)} chars")
    print(f"[{label}] hit iteration cap")
    return calls


async def main():
    print("LLM:", llm.describe())
    conn = MCPConnection()
    await conn.start()
    print("MCP tools:", len(conn.tools))
    try:
        c1 = await investigate(conn, "Alert 1 fired on run_id 2: category/team mismatch exceeded threshold "
                                      "although Airflow reported SUCCESS. Investigate and explain the root cause.", "INV-1")
        c2 = await investigate(conn, "Investigate run_id 3: why were fewer tickets indexed than processed? "
                                      "Find the cause and any matching runbook.", "INV-2")
    finally:
        await conn.stop()
    logical_calls = c1 + c2
    retryable_failures = stats["rate_limited"] + stats["server_errors"]
    extra_attempts = stats["attempts"] - logical_calls
    # Both investigations returned a result, so every failed attempt was recovered by backoff-retry.
    # Consistency check: the number of extra attempts equals the number of retryable failures seen,
    # and no non-retryable ("other") error slipped through.
    all_recovered = (extra_attempts == retryable_failures) and stats["other_errors"] == 0
    print(f"\nTOTAL logical LLM calls: {logical_calls} | raw API attempts: {stats['attempts']} "
          f"(extra due to retries: {extra_attempts})")
    print(f"retryable failures -> 429s: {stats['rate_limited']}, 5xx: {stats['server_errors']}; "
          f"non-retryable errors: {stats['other_errors']}")
    print(f"all failed attempts recovered by backoff-retry: {all_recovered}")


if __name__ == "__main__":
    asyncio.run(main())
