"""Client the Streamlit UI uses to call concierge tools, over SSE or in-memory."""

import asyncio
from typing import Any, Dict, Union

from fastmcp import Client, FastMCP

# A URL for ``concierge serve --transport sse`` (live mode), or a ``FastMCP`` server instance
# for the hosted demo's in-memory transport (SPEC-21 §21.2) — fastmcp's ``Client`` accepts
# either.
ServerTransport = Union[str, FastMCP]


class MCPClient:
    """Calls concierge tools over SSE (a URL) or in-process (a FastMCP server instance)."""

    def __init__(self, server: ServerTransport):
        self.server = server

    async def call_async(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        async with Client(self.server) as client:
            result = await client.call_tool(tool, arguments)
        return result.structured_content or {}

    def call(self, tool: str, **arguments: Any) -> Dict[str, Any]:
        """Blocking call for Streamlit's synchronous script model.

        Raises:
            fastmcp.exceptions.ToolError: If the tool reports an error
        """
        payload = {key: value for key, value in arguments.items() if value is not None}
        return asyncio.run(self.call_async(tool, payload))
