#!/usr/bin/env python3
"""
Shared configuration, constants, and path helpers for the comprehensive agent.

All modules import from here for consistency. This avoids circular imports
by keeping the shared state in one place.
"""

import os
import re
from pathlib import Path

# ── Paths ──
WORKDIR = Path.cwd()
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
TASKS_DIR = WORKDIR / ".tasks"
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
WORKTREES_DIR = WORKDIR / ".worktrees"
MAILBOX_DIR = WORKDIR / ".mailboxes"
CRON_DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"

# ── Tool Constants ──
DEFAULT_MAX_TOKENS = 8000
ESCALATED_MAX_TOKENS = 16000
MAX_RETRIES = 3
MAX_CONSECUTIVE_529 = 2
MAX_RECOVERY_RETRIES = 2
BASE_DELAY_MS = 500
CONTEXT_LIMIT = 50000
KEEP_RECENT_TOOL_RESULTS = 3
PERSIST_THRESHOLD = 30000
CONTINUATION_PROMPT = "Continue from the previous response. Do not repeat completed work."
PROMPT = "\033[36ms20 >> \033[0m"

# ── Worktree Constants ──
VALID_WT_NAME = re.compile(r'^[A-Za-z0-9._-]{1,64}$')

# ── Subagent Constants ──
SUBAGENT_MAX_STEPS = 30
IDLE_POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

# ── Model Constants ──
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
PRIMARY_MODEL = MODEL
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")

# ── CLI State ──
CLI_ACTIVE = False

# ── Helpers ──
DISALLOWED_CHARS = re.compile(r'[^a-zA-Z0-9_-]')


def normalize_mcp_name(name: str) -> str:
    """Replace non [a-zA-Z0-9_-] with underscore."""
    return DISALLOWED_CHARS.sub('_', name)


def safe_path(p: str, cwd: Path = None) -> Path:
    """Resolve a path and ensure it stays inside the workspace."""
    base = cwd or WORKDIR
    path = (base / p).resolve()
    if not path.is_relative_to(base):
        raise ValueError(f"Path escapes workspace: {p}")
    return path
