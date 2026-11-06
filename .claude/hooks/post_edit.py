#!/usr/bin/env python3
"""Checks every file Claude writes, for the mistakes this project has actually shipped.

Runs on PostToolUse for Write/Edit/MultiEdit. The harness passes the event as
JSON on stdin (there is no file-path environment variable). Problems are
returned as {"decision": "block"} so the reason is fed back to Claude, which
must fix them; the edit itself is not undone. Reminders that are not errors go
out as additionalContext.

Each check here exists because the mistake reached main at least once:
multi-line {# #} printing on the page, fuzzy .po entries shipping English,
models changed without a migration, and near-duplicate helpers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
VENV_BIN = PROJECT / ".venv" / "bin"

SKIP_PARTS = {".venv", "node_modules", "staticfiles", ".git", "__pycache__", "migrations"}

REUSE_WHERE = [
    ("providers/", "providers/ (base.py helpers, meta_*.py)"),
    ("/services", "the app's services.py and apps/common/"),
    ("/views", "the app's existing views and apps/common/"),
    ("/tasks", "the app's tasks.py and apps/common/background.py"),
    ("templatetags/", "apps/*/templatetags/"),
    ("templates/", "templates/components/, templates/partials/ and the app's partials/"),
    ("static/js/", "static/js/ and the inline Alpine components in templates/"),
    ("apps/common/", "apps/common/"),
    ("apps/", "the same app and apps/common/"),
]


def tool(name: str) -> str:
    candidate = VENV_BIN / name
    return str(candidate) if candidate.exists() else name


def run(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, cwd=PROJECT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 0, f"(skipped: {exc})"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_python(path: Path, rel: str) -> list[str]:
    problems: list[str] = []
    run([tool("ruff"), "format", "--quiet", str(path)], timeout=20)
    code, out = run([tool("ruff"), "check", "--output-format", "concise", str(path)], timeout=20)
    if code != 0 and out:
        problems.append(f"ruff check failed on {rel}:\n" + "\n".join(out.splitlines()[:20]))
    return problems


def check_models(rel: str) -> list[str]:
    parts = Path(rel).parts
    if len(parts) < 3 or parts[0] != "apps":
        return []
    app_label = parts[1]
    code, out = run(
        [tool("python"), "manage.py", "makemigrations", app_label, "--check", "--dry-run"],
        timeout=50,
    )
    if code == 1 and "No changes detected" not in out:
        return [
            f"{rel} changed the schema of '{app_label}' but there is no migration for it yet. "
            f"Run `.venv/bin/python manage.py makemigrations {app_label}` once the model is final "
            "(CI runs makemigrations --check).\n" + "\n".join(out.splitlines()[:10])
        ]
    return []


MULTILINE_COMMENT = re.compile(r"\{#(?![^\n]*#\})")
I18N_TAG = re.compile(r"\{%\s*(translate|trans|blocktranslate|blocktrans)\b")
I18N_LOAD = re.compile(r"\{%\s*load\s+[^%]*\bi18n\b")


def check_template(path: Path, rel: str, text: str) -> list[str]:
    problems: list[str] = []
    lines = [i + 1 for i, line in enumerate(text.splitlines()) if MULTILINE_COMMENT.search(line)]
    if lines:
        problems.append(
            f"{rel}: '{{#' without '#}}' on the same line (line {', '.join(map(str, lines[:10]))}). "
            "Django's {# #} is single-line only; spread over lines it renders as page text. "
            "Use {% comment %}…{% endcomment %}."
        )
    if I18N_TAG.search(text) and not I18N_LOAD.search(text):
        problems.append(
            f"{rel} uses translate/blocktranslate without '{{% load i18n %}}'. "
            "Loads are not inherited from base templates or by includes: add it to this file."
        )
    return problems


def check_po(rel: str, text: str) -> list[str]:
    fuzzy: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#,") and "fuzzy" in line:
            window = lines[i + 1 : i + 8]
            idx = next((j for j, nxt in enumerate(window) if nxt.startswith("msgid ")), None)
            if idx is None:
                continue
            msgid = window[idx]
            if msgid == 'msgid ""':
                continuation = window[idx + 1] if idx + 1 < len(window) else ""
                if not continuation.startswith('"'):
                    continue  # the catalogue header, not a translation
                msgid = f"msgid {continuation}…"
            fuzzy.append(f"line {i + 1}: {msgid}")
    if not fuzzy:
        return []
    return [
        f"{rel} has {len(fuzzy)} '#, fuzzy' entr{'y' if len(fuzzy) == 1 else 'ies'}. compilemessages skips them, "
        "so they ship in English, and makemessages often guesses them from an unrelated string. "
        "Correct each translation and remove the fuzzy flag:\n" + "\n".join(fuzzy[:15])
    ]


def reuse_question(rel: str) -> str:
    where = next((hint for marker, hint in REUSE_WHERE if marker in rel), "the rest of the project")
    return (
        f"[reuse-check] {Path(rel).name} is a new file. In one line: does something already do this? "
        f"Look in {where}. Could an existing function, partial or class be extended instead "
        "(one more argument, one more branch)? Say what you searched and why nothing fitted. "
        "The `reuse-check` skill has the method; answer, don't restate this."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    tool_name = event.get("tool_name", "")
    if tool_name not in {"Write", "Edit", "MultiEdit"}:
        return 0

    raw_path = (event.get("tool_input") or {}).get("file_path") or ""
    if not raw_path:
        return 0
    path = Path(raw_path)
    try:
        rel = str(path.resolve().relative_to(PROJECT.resolve()))
    except ValueError:
        return 0
    if SKIP_PARTS.intersection(Path(rel).parts) or not path.is_file():
        return 0

    problems: list[str] = []
    notes: list[str] = []

    if path.suffix == ".py":
        problems += check_python(path, rel)
        if path.name == "models.py" or "/models/" in rel:
            problems += check_models(rel)
    elif path.suffix in {".html", ".txt"} and "templates/" in rel:
        problems += check_template(path, rel, path.read_text(encoding="utf-8", errors="replace"))
    elif path.suffix == ".po":
        problems += check_po(rel, path.read_text(encoding="utf-8", errors="replace"))

    response = event.get("tool_response") or {}
    is_new = tool_name == "Write" and isinstance(response, dict) and response.get("type") == "create"
    code_like = path.suffix in {".py", ".html", ".js", ".css"} and not Path(rel).name.startswith("test")
    if is_new and code_like and not rel.startswith(("docs/", ".claude/")):
        notes.append(reuse_question(rel))

    if problems:
        out = {"decision": "block", "reason": "\n\n".join(problems)}
        if notes:
            out["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": "\n".join(notes)}
        print(json.dumps(out))
    elif notes:
        print(
            json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(notes)}})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
