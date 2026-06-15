

import json

from shared.config import MCP_CONFIG_PATH, normalize_mcp_name
from mcp.stdio_client import StdioMCPClient
from mcp.http_client import HttpMCPClient

# ── Registry ──
mcp_clients: dict[str, "BaseMCPClient"] = {}

"""
MCP Config — .mcp.json loading, connect/disconnect, tool pool assembly.

Config format:

  "mcpServers": {
          "filesystem": {
              "command": "npx",
              "args": [
                "-y",
                "@modelcontextprotocol/server-filesystem",
                "/path/to/allowed/directory"
              ],
              "env": {
                "NODE_ENV": "production"
              },
              "cwd": "/working/directory"
            },
          "github-server": {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp/",
            "headers": {
                "Authorization": "Bearer ${GITHUB_TOKEN}"
            }
        }
  }

"""
def _load_mcp_config() -> dict:
    """Load .mcp.json from the workspace root."""
    if not MCP_CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(MCP_CONFIG_PATH.read_text())
        return raw.get("mcpServers", {})
    except Exception:
        return {}


def connect_mcp(name: str) -> str:
    """Connect to an MCP server by name.

    Resolution:
      1. Already connected → message.
      2. Look up in .mcp.json → launch per type (stdio / http).
      3. Not found → error with available server list.
    """
    if name in mcp_clients:
        return f"MCP server '{name}' already connected"

    config = _load_mcp_config()
    server_cfg = config.get(name)

    if not server_cfg:
        configured = ", ".join(config.keys()) if config else "(none)"
        return (f"Unknown MCP server '{name}'. "
                f"Configured in .mcp.json: {configured}")

    stype = server_cfg.get("type", "stdio")

    # ── stdio (local subprocess) ──
    if stype == "stdio":
        command = server_cfg.get("command")
        if not command:
            return f"Missing 'command' for stdio server '{name}'"
        client = StdioMCPClient(
            name=name,
            command=command,
            args=server_cfg.get("args", []),
            env=server_cfg.get("env", {}),
        )
        result = client.connect()
        if isinstance(result, str) and result.startswith("MCP error"):
            return result

    # ── http (remote server over HTTP + SSE) ──
    elif stype == "http":
        url = server_cfg.get("url")
        if not url:
            return f"Missing 'url' for HTTP server '{name}'"
        client = HttpMCPClient(
            name=name,
            url=url,
            headers=server_cfg.get("headers", {}),
            connect_timeout=float(server_cfg.get("connect_timeout", 30)),
        )
        result = client.connect()
        if isinstance(result, str) and result.startswith("MCP HTTP error"):
            return result

    else:
        return f"Unknown MCP transport type '{stype}' for '{name}'. Use 'stdio' or 'http'."

    mcp_clients[name] = client
    tool_names = [t["name"] for t in client.tools]
    transport = "stdio" if isinstance(client, StdioMCPClient) else "http"
    print(f"  \033[31m[mcp] connected ({transport}): {name} → {tool_names}\033[0m")
    return (f"Connected to MCP server '{name}' ({transport}). "
            f"Discovered {len(client.tools)} tools: {', '.join(tool_names)}")


def disconnect_mcp(name: str) -> str:
    """Disconnect and clean up an MCP server."""
    client = mcp_clients.pop(name, None)
    if not client:
        return f"MCP server '{name}' not connected"
    client.disconnect()
    print(f"  \033[31m[mcp] disconnected: {name}\033[0m")
    return f"Disconnected from '{name}'"


def assemble_tool_pool(builtin_tools: list, builtin_handlers: dict) -> tuple[list, dict]:
    """Merge builtin tools + all connected MCP tools.

    MCP tools get prefixed: mcp__{server}__{tool}
    """
    tools = list(builtin_tools)
    handlers = dict(builtin_handlers)
    for server_name, client in mcp_clients.items():
        safe_server = normalize_mcp_name(server_name)
        for tool_def in client.tools:
            safe_tool = normalize_mcp_name(tool_def["name"])
            prefixed = f"mcp__{safe_server}__{safe_tool}"
            tools.append({
                "name": prefixed,
                "description": tool_def.get("description", ""),
                "input_schema": tool_def.get("input_schema", {}),
            })
            handlers[prefixed] = (
                lambda *, c=client, t=tool_def["name"], **kw: c.call_tool(t, kw))
    return tools, handlers
