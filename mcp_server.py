"""IndexOps MCP server.

Exposes the plain tool functions in indexops/tools over the MCP protocol (stdio).
Run directly for manual testing:  python mcp_server.py
FastAPI spawns this file as a subprocess (see api/main.py).

NOTE: stdio transport uses stdout for protocol messages, so nothing here may print
to stdout. Logging goes to stderr.
"""
import logging
import os
import sys

# Make `indexops` importable regardless of the working directory the subprocess starts in.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# mcp SDK v2: FastMCP was renamed to MCPServer (same decorator-based API).
from mcp.server.mcpserver import MCPServer  # noqa: E402

from indexops.tools import ALL_TOOLS  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s mcp %(levelname)s %(message)s")

mcp = MCPServer(
    "indexops",
    instructions=(
        "Tools for monitoring, investigating and remediating the IndexOps Airflow -> OpenSearch "
        "ticket indexing pipeline. Remediation tools only queue actions for human approval."
    ),
)

for fn in ALL_TOOLS:
    # MCPServer derives the tool name, description (docstring) and JSON schema (type hints).
    mcp.tool()(fn)

if __name__ == "__main__":
    logging.info("starting IndexOps MCP server with %d tools", len(ALL_TOOLS))
    mcp.run(transport="stdio")
