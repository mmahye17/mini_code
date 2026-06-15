"""
Tool Handler Functions — Python implementations for each built-in tool.

Each handler receives keyword arguments matching the tool's input_schema.
Handlers are stateless; mutable state (todos, tasks, etc.) is managed elsewhere.
"""

import subprocess
import glob as _glob
from pathlib import Path

from shared.config import WORKDIR, safe_path


# ── File / Shell Handlers ──

def run_bash(command: str, cwd: Path = None,
             run_in_background: bool = False) -> str:
    """Execute a shell command. run_in_background is consumed by dispatch."""
    try:
        r = subprocess.run(command, shell=True, cwd=cwd or WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int | None = None,
             offset: int = 0, cwd: Path = None) -> str:
    """Read file contents with optional offset and limit."""
    try:
        lines = safe_path(path, cwd).read_text().splitlines()
        offset = max(int(offset or 0), 0)
        limit = int(limit) if limit is not None else None
        lines = lines[offset:]
        if limit is not None and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str, cwd: Path = None) -> str:
    """Write content to a file (creates parent directories)."""
    try:
        fp = safe_path(path, cwd)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str,
             cwd: Path = None) -> str:
    """Replace the first occurrence of old_text in a file."""
    try:
        fp = safe_path(path, cwd)
        text = fp.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        fp.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str, cwd: Path = None) -> str:
    """Find files matching a glob pattern within the workspace."""
    try:
        base = cwd or WORKDIR
        results = []
        for match in _glob.glob(pattern, root_dir=base):
            if (base / match).resolve().is_relative_to(base):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ── Generic Handler Call ──

def call_tool_handler(handler, args: dict, name: str) -> str:
    """Call a handler with args dict, handling TypeError."""
    if not handler:
        return f"Unknown: {name}"
    try:
        return handler(**(args or {}))
    except TypeError as e:
        return f"Error: {e}"
