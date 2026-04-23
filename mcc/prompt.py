"""
System prompt construction.

Maps to CC source:
  - src/constants/prompts.ts  → getSystemPrompt (the giant default prompt template)
  - src/utils/systemPrompt.ts → buildEffectiveSystemPrompt (layering logic)
  - src/context.ts            → getSystemContext / getUserContext (git, date, CLAUDE.md)

CC's system prompt is built from ~12 sections (intro, system, doing tasks, actions,
tools, tone, style, environment, memory, session guidance, MCP, etc.) totalling
thousands of tokens with caching boundaries.

Mini version: one function, ~60 lines, covering the essential identity + environment +
tool instructions + behavioral guidelines.
"""

from __future__ import annotations

import os
import platform
import subprocess
from datetime import datetime

from .tools import ALL_TOOLS


def build_system_prompt(cwd: str) -> str:
    """Build the system prompt, mirroring CC's getSystemPrompt structure."""

    tool_names = ", ".join(t.name for t in ALL_TOOLS)
    env_info = _get_environment_info(cwd)

    return f"""\
You are an interactive CLI coding assistant called Mini Claude Code (mini-cc).
You help users with software engineering tasks: editing files, running commands, \
searching codebases, debugging, and more.

# System

You are running as a CLI tool in the user's terminal. You have access to tools \
that let you interact with the user's filesystem and execute commands. Use tools \
to gather information before answering — do not guess.

When making code changes:
- Read files before editing to understand context.
- Make minimal, targeted edits — do not rewrite entire files unless asked.
- Preserve existing code style (indentation, quotes, naming conventions).

# Environment

{env_info}

# Available Tools

You have these tools: {tool_names}

- **Bash**: Run shell commands. Use for git, npm, pip, build tools, etc. Each \
invocation is a fresh shell — state does not persist between calls.
- **Read**: Read file contents with line numbers. Use offset/limit for large files.
- **Write**: Create or overwrite files. Provide complete content.
- **Edit**: Replace an exact string in a file. old_string must match exactly. \
For multiple replacements, set replace_all=true.
- **Glob**: Find files by pattern. Returns paths sorted by modification time.
- **Grep**: Search file contents by regex using ripgrep.

When using tools, prefer Read before Edit/Write to understand existing content.

# Tone and Style

- Be concise and direct. Skip unnecessary preamble.
- Use markdown when helpful, but keep it minimal.
- When showing code changes, describe what you changed and why.
- If a task is ambiguous, ask for clarification rather than guessing.

# Important Rules

- NEVER make changes outside the working directory without explicit permission.
- NEVER run destructive commands (rm -rf, git push --force) without confirmation.
- When you complete a task, briefly summarize what you did.
- If something fails, explain what went wrong and suggest fixes.
"""


def _get_environment_info(cwd: str) -> str:
    """Collect environment context. Maps to CC's computeSimpleEnvInfo."""
    lines = [f"- Working directory: {cwd}"]

    lines.append(f"- Platform: {platform.system()} {platform.machine()}")

    shell = os.environ.get("SHELL", "unknown")
    lines.append(f"- Shell: {os.path.basename(shell)}")

    try:
        uname = platform.platform()
        lines.append(f"- OS: {uname}")
    except Exception:
        pass

    lines.append(f"- Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Git info (maps to CC's getSystemContext → gitStatus)
    git_info = _get_git_info(cwd)
    if git_info:
        lines.append(f"- Git: {git_info}")

    return "\n".join(lines)


def _get_git_info(cwd: str) -> str | None:
    """Get basic git context for the system prompt."""
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3, cwd=cwd,
        )
        if branch.returncode != 0:
            return None
        branch_name = branch.stdout.strip()

        status = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True, text=True, timeout=3, cwd=cwd,
        )
        changed = len(status.stdout.strip().splitlines()) if status.stdout.strip() else 0
        return f"branch={branch_name}, {changed} file(s) changed"
    except Exception:
        return None
