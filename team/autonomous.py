"""
Autonomous Agent — idle polling and task auto-claiming.

Teammates wake up for inbox messages first, then look for unclaimed
tasks. Direct protocol messages have higher priority than task claiming.
"""

import time
import json

from tasks.task_system import scan_unclaimed_tasks, claim_task
from team.message_bus import BUS
from shared.config import WORKTREES_DIR, IDLE_POLL_INTERVAL, IDLE_TIMEOUT


def idle_poll(agent_name: str, messages: list,
              name: str, role: str,
              worktree_context: dict | None = None) -> str:
    """Autonomous idle loop: check inbox, then check for unclaimed tasks.

    Returns "work" if new work was found, "shutdown" if shutdown requested,
    or "timeout" after IDLE_TIMEOUT seconds of no activity.
    """
    for _ in range(IDLE_TIMEOUT // IDLE_POLL_INTERVAL):
        time.sleep(IDLE_POLL_INTERVAL)
        inbox = BUS.read_inbox(agent_name)
        if inbox:
            for msg in inbox:
                if msg.get("type") == "shutdown_request":
                    req_id = msg.get("metadata", {}).get("request_id", "")
                    BUS.send(name, "lead", "Shutting down.",
                             "shutdown_response",
                             {"request_id": req_id, "approve": True})
                    return "shutdown"
            messages.append({"role": "user",
                "content": "<inbox>" + json.dumps(inbox) + "</inbox>"})
            return "work"
        unclaimed = scan_unclaimed_tasks()
        if unclaimed:
            task_data = unclaimed[0]
            result = claim_task(task_data["id"], agent_name)
            if "Claimed" in result:
                wt_info = ""
                if task_data.get("worktree"):
                    wt_path = WORKTREES_DIR / task_data["worktree"]
                    wt_info = f"\nWork directory: {wt_path}"
                    if worktree_context is not None:
                        worktree_context["path"] = str(wt_path)
                messages.append({"role": "user",
                    "content": f"<auto-claimed>Task {task_data['id']}: "
                               f"{task_data['subject']}{wt_info}</auto-claimed>"})
                return "work"
    return "timeout"
