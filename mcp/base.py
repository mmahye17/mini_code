"""
MCP Base — abstract client.

Real transports are in stdio_client.py (local subprocess)
and sse_client.py (remote HTTP+SSE).
"""


class BaseMCPClient:
    """Every MCP client has a name, discovered tools, and call_tool."""

    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []

    def call_tool(self, tool_name: str, args: dict) -> str:
        raise NotImplementedError

    def disconnect(self):
        pass
