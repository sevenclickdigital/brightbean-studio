#!/usr/bin/env python3
"""Makes the implementation checklist part of every implementation.

`checklist.py prompt` (UserPromptSubmit) reminds Claude to read the
`implementation-checklist` skill before it starts implementing.

`checklist.py stop` (Stop) blocks the end of a turn once per distinct set of
uncommitted code changes, asking Claude to run the checklist on them first.
The same diff is never asked about twice, and a turn that was already resumed
by this hook (`stop_hook_active`) is let through, so it cannot loop.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
NON_CODE = re.compile(r"(\.md$|\.example$|^docs/|^\.claude/|^\.github/|^LICENSE$|^AGENTS\.md$|^\.mcp\.json$)")


def git(*args: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=PROJECT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout


def changed_code_files() -> list[str]:
    files = []
    for line in git("status", "--porcelain", "--untracked-files=all").splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path and not NON_CODE.search(path):
            files.append(path)
    return sorted(files)


def state_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session_id) or "default"
    return Path(tempfile.gettempdir()) / f"bb-checklist-{safe}.txt"


def on_prompt() -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        "If this request involves implementing or changing code, read the "
                        "`implementation-checklist` skill before you start and plan with it; "
                        "run it again when you finish."
                    ),
                }
            }
        )
    )
    return 0


def on_stop(event: dict) -> int:
    if event.get("stop_hook_active"):
        return 0
    files = changed_code_files()
    if not files:
        return 0

    digest = hashlib.sha256((git("diff", "HEAD") + "\n".join(files)).encode()).hexdigest()
    marker = state_file(str(event.get("session_id", "")))
    if marker.exists() and marker.read_text().strip() == digest:
        return 0
    marker.write_text(digest)

    listed = "\n".join(f"- {f}" for f in files[:30])
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    "Before finishing, run the `implementation-checklist` skill on the uncommitted code "
                    "changes below, fix what it finds, then give the short final report it describes "
                    "(only what changed or needs attention, and how it was verified). If you already ran "
                    "it on exactly these changes in this turn, just give the report.\n" + listed
                ),
            }
        )
    )
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        event = {}
    if mode == "prompt":
        return on_prompt()
    if mode == "stop":
        return on_stop(event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
