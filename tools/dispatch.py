"""
Tool Dispatch — assemble the full tool pool and handler map.

Every round, assemble_tool_pool() merges built-in tools with
all connected MCP server tools, returning (tools, handlers).
"""

from tools.definitions import BUILTIN_TOOLS, SUB_TOOLS
from tools.handlers import run_bash, run_read, run_write, run_edit, run_glob, call_tool_handler
from skills.skill_loader import load_skill
from tasks.task_system import (
    create_task, list_tasks, get_task_json, claim_task, complete_task)
from worktree.worktree import create_worktree, remove_worktree, keep_worktree
from cron import run_schedule_cron, run_list_crons, run_cancel_cron
from mcp import connect_mcp, assemble_tool_pool as mcp_assemble
from team.teammate import spawn_teammate_thread
from team.protocol import (
    consume_lead_inbox, run_request_shutdown, run_request_plan, run_review_plan)
from team.message_bus import BUS




CURRENT_TODOS: list[dict] = []


def run_todo_write(todos: list) -> str:
    global CURRENT_TODOS
    for i, todo in enumerate(todos):
        if "content" not in todo or "status" not in todo:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if todo["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{todo['status']}'"
    CURRENT_TODOS = todos
    print(f"  \033[33m[todo] updated {len(CURRENT_TODOS)} item(s)\033[0m")
    return f"Updated {len(CURRENT_TODOS)} todos"


# ── Subagent ──

_SUBAGENT_CLIENT = None
_SUBAGENT_MODEL = None
_SUBAGENT_SYSTEM = None
_SUBAGENT_HOOKS = None  # (trigger_hooks_fn)


def init_subagent(client, model, trigger_hooks_fn):
    global _SUBAGENT_CLIENT, _SUBAGENT_MODEL, _SUBAGENT_HOOKS
    _SUBAGENT_CLIENT = client
    _SUBAGENT_MODEL = model
    _SUBAGENT_HOOKS = trigger_hooks_fn
    from shared.config import WORKDIR as wd
    global _SUBAGENT_SYSTEM
    _SUBAGENT_SYSTEM = (
        f"You are a coding subagent at {wd}. "
        "Complete the task, then return a concise final summary. "
        "Do not spawn more agents."
    )


def _has_tool_use(content) -> bool:
    return any(getattr(block, "type", None) == "tool_use"
               for block in (content if isinstance(content, list) else []))


def _extract_text(content) -> str:
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text").strip()


def spawn_subagent(description: str) -> str:
    """Launch a fire-and-forget subagent. Returns final text summary."""
    messages = [{"role": "user", "content": description}]
    sub_handlers = {
        "bash": run_bash, "read_file": run_read,
        "write_file": run_write, "edit_file": run_edit,
        "glob": run_glob,
    }
    for _ in range(30):
        response = _SUBAGENT_CLIENT.messages.create(
            model=_SUBAGENT_MODEL, system=_SUBAGENT_SYSTEM, messages=messages,
            tools=SUB_TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if not _has_tool_use(response.content):
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            blocked = _SUBAGENT_HOOKS("PreToolUse", block)
            if blocked:
                output = str(blocked)
            else:
                handler = sub_handlers.get(block.name)
                output = call_tool_handler(handler, block.input, block.name)
                _SUBAGENT_HOOKS("PostToolUse", block, output)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output)})
        messages.append({"role": "user", "content": results})
    for msg in reversed(messages):
        if msg["role"] == "assistant":
            text = _extract_text(msg["content"])
            if text:
                return text
    return "Subagent finished without a text summary."


# ── Task Tool Wrappers ──

def run_create_task(subject: str, description: str = "",
                    blockedBy: list[str] | None = None) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks."
    return "\n".join(
        f"  {t.id}: {t.subject} [{t.status}]"
        + (f" (wt:{t.worktree})" if t.worktree else "")
        for t in tasks)


def run_get_task(task_id: str) -> str:
    try:
        return get_task_json(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: task {task_id} not found"


# ── Team Tool Wrappers ──

def run_spawn_teammate(name: str, role: str, prompt: str) -> str:
    return spawn_teammate_thread(name, role, prompt)


def run_send_message(to: str, content: str) -> str:
    BUS.send("lead", to, content)
    return f"Sent to {to}"


def run_check_inbox() -> str:
    msgs = consume_lead_inbox(route_protocol=True)
    if not msgs:
        return "(inbox empty)"
    lines = []
    for m in msgs:
        meta = m.get("metadata", {})
        req_id = meta.get("request_id", "")
        tag = f" [{m['type']} req:{req_id}]" if req_id else f" [{m['type']}]"
        lines.append(f"  [{m['from']}]{tag} {m['content'][:200]}")
    return "\n".join(lines)


# ── MCP Tool Wrapper ──

def run_connect_mcp(name: str) -> str:
    return connect_mcp(name)


# ── Worktree Tool Wrappers ──

def run_create_worktree(name: str, task_id: str = "") -> str:
    return create_worktree(name, task_id)


def run_remove_worktree(name: str, discard_changes: bool = False) -> str:
    return remove_worktree(name, discard_changes)


def run_keep_worktree(name: str) -> str:
    return keep_worktree(name)


# ── Built-in Handler Map ──

BUILTIN_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
    "todo_write": run_todo_write, "task": spawn_subagent,
    "load_skill": load_skill,
    "create_task": run_create_task, "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task, "complete_task": run_complete_task,
    "schedule_cron": run_schedule_cron,
    "list_crons": run_list_crons,
    "cancel_cron": run_cancel_cron,
    "spawn_teammate": run_spawn_teammate,
    "send_message": run_send_message, "check_inbox": run_check_inbox,
    "request_shutdown": run_request_shutdown,
    "request_plan": run_request_plan, "review_plan": run_review_plan,
    "create_worktree": run_create_worktree,
    "remove_worktree": run_remove_worktree,
    "keep_worktree": run_keep_worktree,
    "connect_mcp": run_connect_mcp,
}


# ── Pool Assembly ──

def assemble_tool_pool() -> tuple[list, dict]:
    """Merge builtin tools + MCP tools into the full tool pool."""
    return mcp_assemble(BUILTIN_TOOLS, BUILTIN_HANDLERS)
