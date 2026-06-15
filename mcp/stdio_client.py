"""
MCP Stdio Transport — JSON-RPC over subprocess stdin/stdout.

Launches a local MCP server process, performs the initialize→tools/list
handshake, then routes tool calls as JSON-RPC requests.
"""

import json
import subprocess
import threading
import time

from mcp.base import BaseMCPClient


class StdioMCPClient(BaseMCPClient):
    """Launches an MCP server process and talks JSON-RPC over stdin/stdout.

    Protocol handshake (MCP specification):
      1. Client → initialize  → Server returns capabilities
      2. Client → initialized (notification)
      3. Client → tools/list  → Server returns tool schemas
      4. Client → tools/call  → Server executes, returns content
    """

    def __init__(self, name: str, command: str,
                 args: list[str] = None,
                 env: dict[str, str] = None,
                 connect_timeout: float = 30.0):
        super().__init__(name)
        self._command = command
        self._args = args or []
        self._env = env or {}
        self._connect_timeout = connect_timeout
        self._process: subprocess.Popen = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread = None
        self._pending: dict[int, dict] = {}
        self._buffer = b""

    # ── Connection lifecycle ──

    def connect(self) -> str:
        import os
        full_env = {**os.environ, **self._env}

        try:
            self._process = subprocess.Popen(
                [self._command] + self._args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
            )
        except FileNotFoundError:
            return f"MCP error: command not found: {self._command}"
        except Exception as e:
            return f"MCP error: failed to start '{self._command}': {e}"

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # Handshake
        init_result = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mini_agent", "version": "1.0.0"},
        })
        if isinstance(init_result, dict) and "error" in init_result:
            self.disconnect()
            return f"MCP init error: {init_result['error']}"

        self._send_notification("notifications/initialized", {})

        # Discover tools
        tools_result = self._request("tools/list", {})
        if isinstance(tools_result, dict) and "error" in tools_result:
            self.disconnect()
            return f"MCP tools/list error: {tools_result['error']}"

        raw_tools = tools_result.get("tools", [])
        self.tools = _normalize_tools(raw_tools)

        tool_names = [t["name"] for t in self.tools]
        print(f"  \033[31m[mcp] {self.name}: {len(self.tools)} tools "
              f"→ {', '.join(tool_names[:10])}"
              + ("..." if len(tool_names) > 10 else "") + "\033[0m")
        return (f"Connected to MCP server '{self.name}' via stdio. "
                f"Discovered {len(self.tools)} tool(s).")

    def disconnect(self):
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.close()
            except Exception:
                pass
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    # ── Tool execution ──

    def call_tool(self, tool_name: str, args: dict) -> str:
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": args,
        })
        if isinstance(result, dict) and "error" in result:
            return f"MCP error: {result['error']}"
        return _extract_content(result.get("content", []))

    # ── JSON-RPC core ──

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _request(self, method: str, params: dict,
                 timeout: float = None) -> dict:
        timeout = timeout or self._connect_timeout
        req_id = self._next_id()
        payload = {"jsonrpc": "2.0", "id": req_id,
                   "method": method, "params": params}
        with self._lock:
            self._pending[req_id] = {}
        self._send_line(json.dumps(payload))

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                entry = self._pending.get(req_id, {})
                if "result" in entry or "error" in entry:
                    del self._pending[req_id]
                    return entry
            time.sleep(0.01)

        with self._lock:
            self._pending.pop(req_id, None)
        return {"error": f"Timeout waiting for '{method}' response"}

    def _send_notification(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send_line(json.dumps(payload))

    def _send_line(self, line: str):
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write((line + "\n").encode("utf-8"))
                self._process.stdin.flush()
            except Exception:
                pass

    def _read_loop(self):
        try:
            while self._process and self._process.poll() is None:
                chunk = self._process.stdout.read(1)
                if not chunk:
                    break
                self._buffer += chunk
                if chunk == b"\n":
                    line = self._buffer.decode("utf-8").strip()
                    self._buffer = b""
                    if line:
                        self._dispatch_line(line)
        except Exception:
            pass

    def _dispatch_line(self, line: str):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        rid = msg.get("id")
        if rid is not None:
            with self._lock:
                if rid in self._pending:
                    entry = self._pending[rid]
                    if "result" in msg:
                        entry["result"] = msg["result"]
                    if "error" in msg:
                        entry["error"] = msg["error"]


# ====================================================================
#  Shared helpers (used by both transports)
# ====================================================================

def _normalize_tools(raw_tools: list[dict]) -> list[dict]:
    """MCP inputSchema → Anthropic input_schema."""
    normalized = []
    for t in raw_tools:
        normalized.append({
            "name": t.get("name", "unknown"),
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema",
                                  {"type": "object", "properties": {}}),
        })
    return normalized


def _extract_content(content_blocks: list[dict]) -> str:
    """MCP content blocks → plain text."""
    texts = []
    for block in content_blocks:
        t = block.get("type", "")
        if t == "text":
            texts.append(block.get("text", ""))
        elif t == "resource":
            texts.append(f"[resource: {json.dumps(block.get('resource', {}))}]")
        elif t == "image":
            texts.append(f"[image: {block.get('data', '')[:50]}...]")
        else:
            texts.append(json.dumps(block))
    return "\n".join(texts) if texts else "(empty result)"
