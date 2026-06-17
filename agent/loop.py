
import sys
from pathlib import Path

# Ensure project root is on sys.path — allows running from any directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import os
import threading

# Load .env from project root (not cwd) — one config for all projects
from dotenv import load_dotenv
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)

# Allow custom base URL (e.g. for proxies)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from anthropic import Anthropic

# ── Shared State ──
from shared.config import (
    WORKDIR, MEMORY_INDEX,
    DEFAULT_MAX_TOKENS, ESCALATED_MAX_TOKENS,
    MAX_RECOVERY_RETRIES, CONTINUATION_PROMPT,
    PROMPT, CLI_ACTIVE, MODEL,
)

# ── Initialize Subsystems ──
from skills.skill_loader import scan_skills
scan_skills()

from tools.dispatch import assemble_tool_pool, init_subagent
from tools.handlers import run_bash, run_read, run_write, call_tool_handler

from hooks import trigger_hooks, register_default_hooks
register_default_hooks()

from compact import (
    prepare_context, compact_history, reactive_compact,
    init_compaction,
)

from recovery import RecoveryState, with_retry, is_prompt_too_long_error
from cron import (
    consume_cron_queue, cron_scheduler_loop, load_durable_jobs,
)
load_durable_jobs()

from background import (
    should_run_background, start_background_task, collect_background_results,
)

from context import assemble_system_prompt
from mcp import mcp_clients
from team.message_bus import BUS, active_teammates
from team.protocol import consume_lead_inbox, pending_requests
from team.teammate import init_teammate

# ── Anthropic Client ──
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# ── Init subsystems that need the client ──
init_compaction(client, MODEL)
init_subagent(client, MODEL, trigger_hooks)
init_teammate(client, MODEL, run_bash, run_read, run_write)

# ── Global State ──
rounds_since_todo = 0
agent_lock = threading.Lock()


# ── Context Management ──

def update_context(context: dict, messages: list) -> dict:
    """Refresh memory, MCP, and teammate context each turn."""
    memories = ""
    if MEMORY_INDEX.exists():
        memories = MEMORY_INDEX.read_text()[:2000]
    return {
        "memories": memories,
        "connected_mcp": list(mcp_clients.keys()),
        "active_teammates": list(active_teammates.keys()),
    }


def inject_background_notifications(messages: list):
    """Add completed background task notifications to messages."""
    notes = collect_background_results()
    if notes:
        messages.append({"role": "user", "content": [
            {"type": "text", "text": note} for note in notes]})


def build_user_content(results: list[dict]) -> list[dict]:
    """Tool results + background notifications as user-side content."""
    content = list(results)
    for note in collect_background_results():
        content.append({"type": "text", "text": note})
    return content


# ── LLM Call ──

def has_tool_use(content) -> bool:
    """Check if response contains any tool_use blocks."""
    if not isinstance(content, list):
        return False
    return any(getattr(block, "type", None) == "tool_use"
               for block in content)


def call_llm(messages: list, context: dict, tools: list,
             state: RecoveryState, max_tokens: int):
    """Call the LLM with retry and error recovery."""
    system = assemble_system_prompt(context)
    return with_retry(
        lambda: client.messages.create(
            model=state.current_model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens),
        state)


# ── Agent Loop ──

def agent_loop(messages: list, context: dict):
    """Main agent loop: one cycle = inject → compact → call → execute → repeat.

    The loop structure:
      user input → cron/background inject → context compact →
      memory+skills+MCP prompt assembly → LLM call →
      has tool_use? → no: Stop hooks, return
                    → yes: PreToolUse hooks + permission
                           → dispatch to handlers / MCP / background
                           → PostToolUse hooks
                           → tool_results → next turn
    """
    global rounds_since_todo
    tools, handlers = assemble_tool_pool()
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS

    while True:
        # ── Pre-LLM: inject scheduled and background work ──
        fired = consume_cron_queue()
        for job in fired:
            messages.append({"role": "user",
                             "content": f"[Scheduled] {job.prompt}"})
            print(f"  \033[35m[cron inject] {job.prompt[:60]}\033[0m")

        inject_background_notifications(messages)


        if rounds_since_todo >= 3:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

        # ── Context compaction pipeline ──
        prepare_context(messages)
        context = update_context(context, messages)
        tools, handlers = assemble_tool_pool()

        # ── LLM call with error recovery ──
        try:
            response = call_llm(messages, context, tools, state, max_tokens)
        except Exception as e:
            if is_prompt_too_long_error(e) and not state.has_attempted_reactive_compact:
                messages[:] = reactive_compact(messages)
                state.has_attempted_reactive_compact = True
                continue
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return

        # ── Handle max_tokens stop ──
        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(f"  \033[33m[max_tokens] retry with {max_tokens}\033[0m")
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                continue
            return

        # ── Normal completion ──
        max_tokens = DEFAULT_MAX_TOKENS
        state.has_escalated = False
        messages.append({"role": "assistant", "content": response.content})

        if not has_tool_use(response.content):
            trigger_hooks("Stop", messages)
            return

        # ── Tool execution ──
        results = []
        compacted_now = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            print(f"\033[36m> {block.name}\033[0m")

            # compact tool: summarize and restart with compacted context
            if block.name == "compact":
                messages[:] = compact_history(messages)
                messages.append({"role": "user",
                                 "content": "[Compacted. Continue with summarized context.]"})
                compacted_now = True
                break

            # PreToolUse hooks (permission, logging)
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": str(blocked)})
                continue

            # Background dispatch for slow operations
            if should_run_background(block.name, block.input):
                bg_id = start_background_task(block,
                                               handlers.get(block.name),
                                               trigger_hooks)
                output = (f"[Background task {bg_id} started] "
                          "Result will arrive as a task_notification.")
                results.append({"type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output})
                continue

            # Execute tool
            handler = handlers.get(block.name)
            output = call_tool_handler(handler, block.input, block.name)
            trigger_hooks("PostToolUse", block, output)
            print(str(output)[:300])

            if block.name == "todo_write":
                rounds_since_todo = 0
            else:
                rounds_since_todo += 1

            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": output})

        # Skip appending results if we compacted (context is restarted)
        if compacted_now:
            continue

        messages.append({"role": "user", "content": build_user_content(results)})


# ── CLI Helpers ──

def print_turn_assistants(messages: list, turn_start: int):
    """Print assistant text responses for the current turn."""
    for msg in messages[turn_start:]:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if getattr(block, "type", None) == "text":
                print(block.text)


def cron_autorun_loop(history: list, context: dict):
    """Background thread: poll cron queue, inject into history, run agent."""
    import time as _time
    while True:
        _time.sleep(1)
        fired = consume_cron_queue()
        if not fired:
            continue
        with agent_lock:
            turn_start = len(history)
            for job in fired:
                history.append({"role": "user",
                                "content": f"[Scheduled] {job.prompt}"})
                print(f"  \033[35m[cron auto] {job.prompt[:60]}\033[0m")
            agent_loop(history, context)
            context.update(update_context(context, history))
            print_turn_assistants(history, turn_start)


# ── Entry Point ──

if __name__ == "__main__":
    CLI_ACTIVE = True
    print("s20: comprehensive agent")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    # Start cron scheduler daemon (shared with the module-level scheduler)
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()

    history = []
    context = update_context({}, [])

    # Start cron autorun background thread
    threading.Thread(target=cron_autorun_loop,
                     args=(history, context), daemon=True).start()

    # Main CLI loop
    while True:
        try:
            query = input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "/exit", ""):
            break

        trigger_hooks("UserPromptSubmit", query)
        turn_start = len(history)
        history.append({"role": "user", "content": query})

        with agent_lock:
            agent_loop(history, context)
            context = update_context(context, history)
            print_turn_assistants(history, turn_start)

        # Process any protocol responses in the inbox
        inbox = consume_lead_inbox(route_protocol=True)
        if inbox:
            def inbox_label(msg):
                req_id = msg.get("metadata", {}).get("request_id", "")
                suffix = f" req:{req_id}" if req_id else ""
                return f"{msg.get('type', 'message')}{suffix}"

            inbox_text = "\n".join(
                f"From {m['from']} [{inbox_label(m)}]: "
                f"{m['content'][:200]}" for m in inbox)
            history.append({"role": "user",
                            "content": f"[Inbox]\n{inbox_text}"})
        print()
