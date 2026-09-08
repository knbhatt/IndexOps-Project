"""Long-lived MCP client connection to the IndexOps MCP server (stdio subprocess).

FastAPI opens one MCPConnection in its lifespan and keeps it open for the whole
process lifetime; the agent loop and the API endpoints share it.
"""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_SERVER_PATH = os.path.join(PROJECT_ROOT, "mcp_server.py")


class MCPConnection:
    def __init__(self, server_path: str = MCP_SERVER_PATH):
        self.server_path = server_path
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self._lock = asyncio.Lock()
        self.tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        params = StdioServerParameters(
            command=sys.executable,
            args=[self.server_path],
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        await self.refresh_tools()

    async def stop(self) -> None:
        # On Windows, tearing down the stdio subprocess transport can occasionally block;
        # cap it so a shutdown can never hang the whole process.
        if self._stack:
            try:
                await asyncio.wait_for(self._stack.aclose(), timeout=10)
            except (asyncio.TimeoutError, Exception):
                pass
        self._stack, self.session = None, None

    async def refresh_tools(self) -> list[dict[str, Any]]:
        result = await self.session.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in result.tools
        ]
        return self.tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Call a tool over MCP and return its result parsed as JSON (or raw text)."""
        if self.session is None:
            raise RuntimeError("MCP connection not started")
        async with self._lock:  # one request in flight per stdio session
            result = await self.session.call_tool(name, arguments or {})
        if result.structured_content:
            data = result.structured_content
            # MCPServer wraps non-object returns as {"result": ...}
            if isinstance(data, dict) and set(data) == {"result"}:
                data = data["result"]
        else:
            text = "\n".join(c.text for c in result.content if getattr(c, "text", None))
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"text": text}
        if result.is_error:
            return {"error": data}
        return data
