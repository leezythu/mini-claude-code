"""
Tool system — the hands and eyes of the agent.

Maps to CC source:
  - src/Tool.ts          → base Tool interface (name, description, input_schema, call)
  - src/tools.ts         → tool registry (getAllBaseTools, getTools)
  - src/tools/BashTool/  → BashTool
  - src/tools/FileReadTool/  → Read
  - src/tools/FileWriteTool/ → Write
  - src/tools/FileEditTool/  → Edit
  - src/tools/GlobTool/      → Glob
  - src/tools/GrepTool/      → Grep

CC has ~40 tools across ~1900 files. We keep 6 essential ones in <300 lines.
"""

from __future__ import annotations

import glob as globlib
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Base tool definition  (CC: src/Tool.ts → buildTool + Tool type)
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    """Minimal tool descriptor sent to the model and used for dispatch."""
    name: str
    description: str
    input_schema: dict          # JSON Schema object
    is_read_only: bool = False
    requires_permission: bool = True  # if True, ask user before running

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# BashTool  (CC: src/tools/BashTool/BashTool.tsx)
#
# CC version: 900+ lines handling sandboxing, background tasks, progress UI,
# sed simulation, timeout auto-background, and permission matchers.
# Mini version: subprocess.run with timeout.
# ---------------------------------------------------------------------------

class BashTool(Tool):
    def __init__(self):
        super().__init__(
            name="Bash",
            description=(
                "Run a shell command. Each invocation runs in a fresh shell — "
                "state (cd, env vars) is NOT preserved between calls. For "
                "multi-step operations, chain with && or ;. "
                "Avoid interactive commands (vim, less). "
                "Prefer non-destructive read commands when possible."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Optional timeout in seconds (default 30).",
                    },
                },
                "required": ["command"],
            },
            is_read_only=False,
            requires_permission=True,
        )

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        cmd = inp["command"]
        timeout = inp.get("timeout", 30)
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout, cwd=cwd,
            )
            out = ""
            if r.stdout:
                out += r.stdout
            if r.stderr:
                out += ("\n" if out else "") + f"[stderr]\n{r.stderr}"
            if r.returncode != 0:
                out += f"\n[exit code: {r.returncode}]"
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"[Command timed out after {timeout}s]"
        except Exception as e:
            return f"[Error: {e}]"


# ---------------------------------------------------------------------------
# FileReadTool  (CC: src/tools/FileReadTool/FileReadTool.ts)
#
# CC version: ~600 lines, handles images, PDFs, notebooks, dedup via
# readFileState, skill discovery, and token-budget-aware reading.
# Mini version: plain text read with optional offset/limit.
# ---------------------------------------------------------------------------

class FileReadTool(Tool):
    def __init__(self):
        super().__init__(
            name="Read",
            description=(
                "Read a file from disk. Returns numbered lines. "
                "Use offset/limit for large files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based start line (optional).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of lines to read (optional).",
                    },
                },
                "required": ["file_path"],
            },
            is_read_only=True,
            requires_permission=False,
        )

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        path = _resolve(inp["file_path"], cwd)
        if not os.path.isfile(path):
            return f"Error: file not found: {path}"
        try:
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return f"Error reading file: {e}"

        offset = inp.get("offset", 1)
        limit = inp.get("limit")
        start = max(0, offset - 1)
        end = start + limit if limit else len(lines)
        selected = lines[start:end]

        if not selected:
            return "(empty file or range)"
        numbered = [f"{start + i + 1:6}|{l.rstrip()}" for i, l in enumerate(selected)]
        total = len(lines)
        header = f"[{path}] ({total} lines total)"
        return header + "\n" + "\n".join(numbered)


# ---------------------------------------------------------------------------
# FileWriteTool  (CC: src/tools/FileWriteTool/FileWriteTool.ts)
#
# CC version: ~400 lines, staleness checks via readFileState/mtime, LSP
# integration, VS Code notifications, git diff, backup.
# Mini version: mkdir + write.
# ---------------------------------------------------------------------------

class FileWriteTool(Tool):
    def __init__(self):
        super().__init__(
            name="Write",
            description=(
                "Create or overwrite a file. Provide the full file content. "
                "Parent directories are created automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete file content to write.",
                    },
                },
                "required": ["file_path", "content"],
            },
            is_read_only=False,
            requires_permission=True,
        )

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        path = _resolve(inp["file_path"], cwd)
        content = inp["content"]
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            existed = os.path.isfile(path)
            with open(path, "w") as f:
                f.write(content)
            verb = "Updated" if existed else "Created"
            return f"{verb} {path} ({len(content)} chars)"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# FileEditTool  (CC: src/tools/FileEditTool/FileEditTool.ts)
#
# CC version: ~500 lines, quote normalization, staleness checks, structured
# patches, LSP/VS Code, diagnosticTracker.
# Mini version: string find-and-replace.
# ---------------------------------------------------------------------------

class FileEditTool(Tool):
    def __init__(self):
        super().__init__(
            name="Edit",
            description=(
                "Edit a file by replacing an exact string with new content. "
                "old_string must match EXACTLY (including whitespace/indentation). "
                "For creating new files, use Write instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the file.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "The exact text to find.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences (default false).",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            is_read_only=False,
            requires_permission=True,
        )

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        path = _resolve(inp["file_path"], cwd)
        old = inp["old_string"]
        new = inp["new_string"]
        replace_all = inp.get("replace_all", False)

        if not os.path.isfile(path):
            return f"Error: file not found: {path}"
        try:
            content = open(path, "r", errors="replace").read()
        except Exception as e:
            return f"Error reading file: {e}"

        count = content.count(old)
        if count == 0:
            return f"Error: old_string not found in {path}"
        if count > 1 and not replace_all:
            return (
                f"Error: old_string found {count} times. "
                "Set replace_all=true or provide more context to make it unique."
            )

        new_content = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        with open(path, "w") as f:
            f.write(new_content)
        n = count if replace_all else 1
        return f"Edited {path}: replaced {n} occurrence(s)"


# ---------------------------------------------------------------------------
# GlobTool  (CC: src/tools/GlobTool/GlobTool.ts)
#
# CC version: uses ripgrep --files with --glob and --sort=modified, applies
# ignore patterns and plugin cache exclusions, default limit 100.
# Mini version: Python glob with sorted-by-mtime results.
# ---------------------------------------------------------------------------

class GlobTool(Tool):
    def __init__(self):
        super().__init__(
            name="Glob",
            description=(
                "Find files matching a glob pattern. Returns paths sorted by "
                "modification time (newest first). Automatically searches "
                "recursively from the given directory."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: cwd).",
                    },
                },
                "required": ["pattern"],
            },
            is_read_only=True,
            requires_permission=False,
        )

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        pattern = inp["pattern"]
        base = _resolve(inp.get("path", cwd), cwd)
        if not pattern.startswith("**/") and not os.path.isabs(pattern):
            pattern = "**/" + pattern
        full_pattern = os.path.join(base, pattern)
        try:
            matches = globlib.glob(full_pattern, recursive=True)
            matches = [m for m in matches if os.path.isfile(m)]
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            matches = matches[:100]
            rel = [os.path.relpath(m, cwd) for m in matches]
            if not rel:
                return "No files matched."
            return f"{len(rel)} file(s):\n" + "\n".join(rel)
        except Exception as e:
            return f"Error: {e}"


# ---------------------------------------------------------------------------
# GrepTool  (CC: src/tools/GrepTool/GrepTool.ts)
#
# CC version: ~400 lines, builds ripgrep args with --hidden, excludes, multiline,
# context lines, type filters, head_limit, offset, mtime-sorted file mode.
# Mini version: subprocess call to rg (ripgrep) with common flags.
# ---------------------------------------------------------------------------

class GrepTool(Tool):
    def __init__(self):
        super().__init__(
            name="Grep",
            description=(
                "Search file contents using ripgrep (regex). Returns matching "
                "lines with file paths and line numbers."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search (default: cwd).",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Filter files by glob, e.g. '*.py'.",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Case-insensitive search (default false).",
                    },
                },
                "required": ["pattern"],
            },
            is_read_only=True,
            requires_permission=False,
        )

    def run(self, inp: dict[str, Any], cwd: str) -> str:
        pattern = inp["pattern"]
        base = _resolve(inp.get("path", cwd), cwd)
        args = ["rg", "--no-heading", "--line-number", "--color=never",
                "--max-columns=500", "--hidden", "--glob=!.git"]
        if inp.get("case_insensitive"):
            args.append("-i")
        if inp.get("glob"):
            args.append(f"--glob={inp['glob']}")
        args += ["-e", pattern, "--", base]
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=15, cwd=cwd)
            out = r.stdout.strip()
            if not out:
                return "No matches found."
            lines = out.split("\n")
            if len(lines) > 200:
                lines = lines[:200]
                return "\n".join(lines) + f"\n... (truncated, {len(lines)}+ matches)"
            return "\n".join(lines)
        except FileNotFoundError:
            return "Error: ripgrep (rg) not installed. Install via: brew install ripgrep"
        except subprocess.TimeoutExpired:
            return "[Grep timed out after 15s]"
        except Exception as e:
            return f"Error: {e}"


# ---------------------------------------------------------------------------
# Registry  (CC: src/tools.ts → getTools / getAllBaseTools)
# ---------------------------------------------------------------------------

ALL_TOOLS: list[Tool] = [
    BashTool(),
    FileReadTool(),
    FileWriteTool(),
    FileEditTool(),
    GlobTool(),
    GrepTool(),
]

TOOL_MAP: dict[str, Tool] = {t.name: t for t in ALL_TOOLS}


def get_tool_schemas() -> list[dict]:
    """Return Anthropic-compatible tool definitions for the API call."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in ALL_TOOLS
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(path: str, cwd: str) -> str:
    """Resolve a potentially relative path against cwd."""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(cwd, path))
