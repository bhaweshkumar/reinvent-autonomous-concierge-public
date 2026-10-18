"""Client for the hosted AWS Events MCP server (streamable HTTP).

It accepts the same Builder ID access token as the REST API: both resources name the same
authorization server and the ``events/access`` scope.
"""

import json
from typing import Any, Dict, List, Optional

from fastmcp import Client

DEFAULT_SERVER_URL = "https://api.awsevents.com/mcp"

# One tool per REST operation (docs/aws-service.md, "Available tools").
EXPECTED_TOOLS = frozenset(
    {
        "ListEvents",
        "GetEvent",
        "ListSessions",
        "GetSession",
        "GetSchedule",
        "ReserveSessions",
        "CancelReservation",
        "AssociateFavorites",
        "DisassociateFavorite",
        "CreatePersonalTime",
        "UpdatePersonalTime",
        "DeletePersonalTime",
    }
)


class HostedMCPClient:
    """Async context manager wrapping a fastmcp client with bearer authentication."""

    def __init__(self, access_token: str, url: str = DEFAULT_SERVER_URL, timeout: float = 30.0):
        self.url = url
        self._client = Client(url, auth=access_token, timeout=timeout)
        self._tool_name_map: Dict[str, str] = {}

    async def __aenter__(self) -> "HostedMCPClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def list_tool_names(self) -> List[str]:
        self._tool_name_map = {
            tool.name.rsplit("___", 1)[-1]: tool.name for tool in await self._client.list_tools()
        }
        return list(self._tool_name_map)

    async def call(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call a tool and return its structured result, or its text parsed as JSON.

        Raises:
            fastmcp.exceptions.ToolError: If the server reports a tool error
        """
        if tool not in self._tool_name_map:
            await self.list_tool_names()
        actual_tool_name = self._tool_name_map.get(tool, tool)
        result = await self._client.call_tool(actual_tool_name, arguments or {})
        data: Any = result.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]  # fastmcp wraps non-object return values
        if data is None:
            data = "".join(getattr(block, "text", "") for block in result.content)
        if isinstance(data, str):
            try:
                return json.loads(data)
            except ValueError:
                return data
        return data
