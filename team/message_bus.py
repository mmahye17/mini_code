"""
MessageBus — append-only JSONL mailboxes for team communication.

Keeps the protocol inspectable on disk and lets background teammates
send messages asynchronously.
"""

import json
import time
import threading

from shared.config import MAILBOX_DIR


class MessageBus:
    def __init__(self):
        MAILBOX_DIR.mkdir(exist_ok=True)

    def send(self, from_agent: str, to_agent: str, content: str,
             msg_type: str = "message", metadata: dict = None):
        msg = {"from": from_agent, "to": to_agent,
               "content": content, "type": msg_type,
               "ts": time.time(), "metadata": metadata or {}}
        inbox = MAILBOX_DIR / f"{to_agent}.jsonl"
        with open(inbox, "a") as f:
            f.write(json.dumps(msg) + "\n")
        self._print_send(from_agent, to_agent, msg_type, content)

    def read_inbox(self, agent: str) -> list[dict]:
        inbox = MAILBOX_DIR / f"{agent}.jsonl"
        if not inbox.exists():
            return []
        msgs = [json.loads(line) for line in inbox.read_text().splitlines()
                if line.strip()]
        inbox.unlink()
        return msgs

    def _print_send(self, from_agent, to_agent, msg_type, content):
        if threading.current_thread() is threading.main_thread():
            print(f"  \033[33m[bus] {from_agent} → {to_agent}: "
                  f"({msg_type}) {content[:50]}\033[0m")


# Singleton instances shared across the project
BUS = MessageBus()
active_teammates: dict[str, bool] = {}
