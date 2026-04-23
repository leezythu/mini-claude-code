"""
Permission system — the safety gate between the model and real-world actions.

Maps to CC source:
  - src/utils/permissions/permissions.ts → hasPermissionsToUseTool (core decision)
  - src/hooks/toolPermission/           → interactive UI, classifiers, auto-mode
  - src/tools/BashTool/bashPermissions.ts → bash-specific permission checks

CC's permission system is a multi-layered pipeline:
  1. Deny rules (config) → instant deny
  2. Ask rules (config) → force prompt
  3. tool.checkPermissions() → tool-specific logic
  4. bypassPermissions / plan mode → auto-allow
  5. Always-allow rules → auto-allow
  6. dontAsk mode → auto-deny
  7. Auto mode → AI classifier decides
  8. Interactive prompt → user decides

Mini version: read-only tools auto-allow, write tools prompt with y/n,
session-level "allow all" option.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from .tools import Tool

console = Console()

_session_allow_all = False
_non_interactive = False
_allowed_commands: set[str] = set()


def set_non_interactive(value: bool = True):
    """In non-interactive mode (--print), auto-allow all actions."""
    global _non_interactive
    _non_interactive = value


def check_permission(tool: Tool, inp: dict, cwd: str) -> bool:
    """
    Decide whether a tool invocation is allowed.
    Returns True if allowed, False if denied.
    """
    global _session_allow_all

    if _session_allow_all or _non_interactive:
        return True

    if not tool.requires_permission:
        return True

    if tool.is_read_only:
        return True

    # Build a human-readable summary of what will happen
    summary = _describe_action(tool, inp, cwd)
    fingerprint = f"{tool.name}:{summary}"
    if fingerprint in _allowed_commands:
        return True

    return _prompt_user(tool.name, summary, fingerprint)


def _prompt_user(tool_name: str, summary: str, fingerprint: str) -> bool:
    """Interactive permission prompt. Maps to CC's PermissionRequest component."""
    global _session_allow_all

    console.print()
    title = Text(f"  {tool_name} ", style="bold white on red")
    console.print(title)
    console.print(f"  {summary}", style="dim")
    console.print()
    console.print("  [y] Allow  [n] Deny  [a] Allow all for session", style="bold")

    while True:
        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if choice in ("y", "yes", ""):
            _allowed_commands.add(fingerprint)
            return True
        elif choice in ("n", "no"):
            return False
        elif choice in ("a", "all"):
            _session_allow_all = True
            return True
        else:
            console.print("  Please enter y, n, or a", style="dim")


def _describe_action(tool: Tool, inp: dict, cwd: str) -> str:
    """Create a human-readable description of a tool action."""
    if tool.name == "Bash":
        cmd = inp.get("command", "")
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        return f"$ {cmd}"
    elif tool.name == "Write":
        path = inp.get("file_path", "?")
        return f"Write to {path}"
    elif tool.name == "Edit":
        path = inp.get("file_path", "?")
        return f"Edit {path}"
    return f"{tool.name}({', '.join(f'{k}={v!r}' for k, v in inp.items())})"
