"""
MCP HTTP Transport — JSON-RPC over HTTP with Server-Sent Events.

Connects to a remote MCP server over the network. Requests are sent via
HTTP POST; responses come back through an SSE (Server-Sent Events) stream.

Config in .mcp.json:
  { "type": "http", "url": "https://mcp.example.com",
    "headers": {"Authorization": "Bearer xxx"} }

Only uses urllib (stdlib) — no extra dependencies.
"""

import json
import threading
import time
import urllib.request
import urllib.error
from http.client import HTTPResponse
from urllib.parse import urlparse

from mcp.base import BaseMCPClient
from mcp.stdio_client import _normalize_tools, _extract_content


class HttpMCPClient(BaseMCPClient):
    """Talks JSON-RPC to a remote MCP server over HTTP + SSE.

    Protocol flow:
      1. GET  {url}/sse          → open SSE stream, receive session endpoint
      2. POST {session_url}      → send JSON-RPC request
      3. SSE stream              → read JSON-RPC response
      4. POST {session_url}      → send tools/call, etc.

    The SSE stream is read in a background thread. Requests are sent via
    HTTP POST and matched by JSON-RPC id.
    """

    def __init__(self, name: str, url: str,
                 headers: dict[str, str] = None,
                 connect_timeout: float = 30.0):
        super().__init__(name)
        self._base_url = url.rstrip("/")
        self._headers = headers or {}
        self._connect_timeout = connect_timeout
        self._session_url: str = None
        self._session_id: str = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._reader_thread: threading.Thread = None
        self._sse_response: HTTPResponse = None
        self._running = False

    # ── Connection lifecycle ──

    def connect(self) -> str:
        """Open HTTP SSE stream, handshake, discover tools."""
        sse_url = f"{self._base_url}/sse"
        try:
            req = urllib.request.Request(sse_url, headers={
                "Accept": "text/event-stream",
                **self._headers,
            })
            self._sse_response = urllib.request.urlopen(req, timeout=self._connect_timeout)
        except urllib.error.URLError as e:
            return f"MCP HTTP error: cannot connect to {sse_url}: {e}"
        except Exception as e:
            return f"MCP HTTP error: {e}"

        # Read the first SSE event to get the session endpoint
        event = self._read_sse_event(timeout=self._connect_timeout)
        if not event:
            self.disconnect()
            return f"MCP HTTP error: no endpoint event from {sse_url}"

        endpoint = event.get("data", "")
        if not endpoint:
            self.disconnect()
            return f"MCP HTTP error: empty endpoint in first event"

        # Resolve relative URL
        if endpoint.startswith("/"):
            parsed = urlparse(self._base_url)
            self._session_url = f"{parsed.scheme}://{parsed.netloc}{endpoint}"
        elif endpoint.startswith("http"):
            self._session_url = endpoint
        else:
            self._session_url = f"{self._base_url}/{endpoint}"

        # Start background reader
        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # Initialize handshake
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

        self.tools = _normalize_tools(tools_result.get("tools", []))

        tool_names = [t["name"] for t in self.tools]
        print(f"  \033[31m[mcp] {self.name} (HTTP): {len(self.tools)} tools "
              f"→ {', '.join(tool_names[:10])}"
              + ("..." if len(tool_names) > 10 else "") + "\033[0m")
        return (f"Connected to MCP server '{self.name}' via HTTP ({self._base_url}). "
                f"Discovered {len(self.tools)} tool(s).")

    def disconnect(self):
        self._running = False
        if self._sse_response:
            try:
                self._sse_response.close()
            except Exception:
                pass
        self._sse_response = None

    # ── Tool execution ──

    def call_tool(self, tool_name: str, args: dict) -> str:
        result = self._request("tools/call", {
            "name": tool_name,
            "arguments": args,
        })
        if isinstance(result, dict) and "error" in result:
            return f"MCP error: {result['error']}"
        return _extract_content(result.get("content", []))

    # ── JSON-RPC over HTTP POST ──

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

        self._post_json(payload)

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                entry = self._pending.get(req_id, {})
                if "result" in entry or "error" in entry:
                    del self._pending[req_id]
                    return entry
            time.sleep(0.05)

        with self._lock:
            self._pending.pop(req_id, None)
        return {"error": f"Timeout waiting for '{method}' response"}

    def _send_notification(self, method: str, params: dict):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._post_json(payload)

    def _post_json(self, payload: dict):
        """Send a JSON-RPC message via HTTP POST to the session endpoint."""
        if not self._session_url:
            return
        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                **self._headers,
            }
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            req = urllib.request.Request(
                self._session_url, data=data, headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            resp.read()
        except Exception as e:
            with self._lock:
                for rid in list(self._pending.keys()):
                    self._pending[rid]["error"] = str(e)
                    break

    # ── SSE stream reader ──

    def _read_loop(self):
        """Background: continuously read SSE events, dispatch JSON-RPC responses."""
        while self._running:
            event = self._read_sse_event(timeout=5.0)
            if event is None:
                continue
            if event == {}:
                break
            self._handle_event(event)

    def _read_sse_event(self, timeout: float = None) -> dict | None:
        """Read one SSE event.

        Returns dict with event/data/id keys, {} for EOF, None for timeout.
        """
        if not self._sse_response:
            return {}

        sock = self._sse_response.fp.raw if hasattr(self._sse_response, 'fp') else None
        if sock and timeout:
            try:
                sock.settimeout(timeout)
            except Exception:
                pass

        event_type = ""
        data_lines = []
        event_id = ""

        try:
            while True:
                line = self._sse_response.readline()
                if not line:
                    return {}

                line = line.decode("utf-8", errors="replace").rstrip("\r\n")

                if line == "":
                    if data_lines:
                        return {
                            "event": event_type or "message",
                            "data": "\n".join(data_lines),
                            "id": event_id,
                        }
                    event_type = ""
                    data_lines = []
                    event_id = ""
                    continue

                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line.startswith("id:"):
                    event_id = line[3:].strip()
        except (TimeoutError, OSError):
            if data_lines:
                return {
                    "event": event_type or "message",
                    "data": "\n".join(data_lines),
                    "id": event_id,
                }
            return None
        except Exception:
            return {}

    def _handle_event(self, event: dict):
        """Parse SSE event as JSON-RPC response, match to pending request."""
        data = event.get("data", "")
        if not data:
            return
        try:
            msg = json.loads(data)
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
