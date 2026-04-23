"""
CLI entry point and REPL — the user-facing shell.

Maps to CC source:
  - src/entrypoints/cli.tsx  → bootstrap, fast-path flags (--version, etc.)
  - src/main.tsx             → Commander.js CLI parser, option handling, run()
  - src/screens/REPL.tsx     → interactive REPL (onSubmit → processUserInput →
                                onQuery → query() stream → render)

CC uses React + Ink for a rich terminal UI with components, hooks, state
management, and streaming rendering at ~60fps.

Mini version: prompt_toolkit for input + rich for output, same flow structure
in ~120 lines.
"""

from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from . import __version__
from .engine import StreamEvent, create_client, detect_provider, run_agent_loop
from .permissions import set_non_interactive

console = Console()


def main():
    """Entry point. Maps to CC's cli.tsx → main() → run()."""
    parser = argparse.ArgumentParser(
        prog="mcc",
        description="Mini Claude Code — a minimal reimplementation of Claude Code",
    )
    parser.add_argument("-v", "--version", action="version", version=f"mini-cc {__version__}")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true",
                        help="Non-interactive: read prompt from args, print response, exit")
    parser.add_argument("-m", "--model", default=None,
                        help="Model to use (default: claude-sonnet-4-20250514)")
    parser.add_argument("prompt", nargs="*", help="Initial prompt (optional)")
    args = parser.parse_args()

    provider = detect_provider()
    if provider == "openai":
        model = args.model or os.environ.get("OPENAI_MODEL", "anthropic/claude-sonnet-4-20250514")
    else:
        model = args.model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    cwd = os.getcwd()

    # Validate API key early (CC: getAnthropicApiKeyWithSource)
    has_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not has_key:
        console.print("[red]Error: No API key set.[/red]")
        console.print("Set one of:")
        console.print("  export ANTHROPIC_API_KEY='your-key'    # Anthropic / proxy")
        console.print("  export OPENAI_API_KEY='your-key'       # OpenRouter / OpenAI-compatible")
        sys.exit(1)

    client = create_client()

    if args.print_mode and args.prompt:
        # Non-interactive mode (CC: --print flag → print.ts)
        set_non_interactive(True)
        prompt = " ".join(args.prompt)
        _run_single(client, prompt, cwd, model)
    else:
        # Interactive REPL (CC: screens/REPL.tsx)
        initial = " ".join(args.prompt) if args.prompt else None
        _run_repl(client, cwd, model, initial)


def _run_repl(client, cwd: str, model: str, initial_prompt: str | None):
    """Interactive REPL loop. Maps to CC's REPL.tsx component."""
    _print_banner(model, cwd)

    messages = []
    first = True

    while True:
        # --- Get user input (CC: PromptInput → onSubmit) ---
        if first and initial_prompt:
            user_input = initial_prompt
            first = False
            console.print(f"\n[bold cyan]> {user_input}[/bold cyan]")
        else:
            first = False
            try:
                console.print()
                user_input = _get_input()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/dim]")
                break

        if not user_input.strip():
            continue

        # Slash commands (CC: processSlashCommand)
        if user_input.strip() == "/exit":
            console.print("[dim]Goodbye![/dim]")
            break
        elif user_input.strip() == "/clear":
            messages.clear()
            console.print("[dim]Conversation cleared.[/dim]")
            continue
        elif user_input.strip() == "/help":
            _print_help()
            continue
        elif user_input.strip() == "/cost":
            _print_cost_stub()
            continue

        # --- Build user message and run agent loop ---
        messages.append({"role": "user", "content": user_input})

        _render_stream(client, messages, cwd, model)


def _render_stream(client, messages: list, cwd: str, model: str):
    """
    Consume the agent loop generator and render output.
    Maps to CC's REPL.tsx → onQueryEvent → handleMessageFromStream → setMessages.
    """
    text_buffer = ""
    in_text = False

    for event in run_agent_loop(client, messages, cwd, model):
        etype = event["type"]

        if etype == "text_start":
            in_text = True
            text_buffer = ""
        elif etype == "text_delta":
            sys.stdout.write(event["text"])
            sys.stdout.flush()
            text_buffer += event["text"]
        elif etype == "block_stop":
            if in_text and text_buffer:
                sys.stdout.write("\n")
                sys.stdout.flush()
                in_text = False
        elif etype == "tool_start":
            if in_text:
                sys.stdout.write("\n")
                in_text = False
            console.print(f"\n  [bold yellow]⚡ {event['name']}[/bold yellow]", end="")
        elif etype == "tool_input_delta":
            pass  # input JSON streams in — skip for cleanliness
        elif etype == "tool_running":
            console.print(f"  [dim]running...[/dim]")
        elif etype == "tool_result":
            result = event["result"]
            if len(result) > 500:
                result = result[:500] + "..."
            console.print(f"  [dim]{result}[/dim]")
        elif etype == "tool_denied":
            console.print(f"  [red]✗ {event['name']} denied[/red]")
        elif etype == "tool_error":
            console.print(f"  [red]✗ {event.get('error', 'error')}[/red]")
        elif etype == "error":
            console.print(f"\n[red]{event['message']}[/red]")
            return
        elif etype == "loop_continue":
            pass  # next turn, model will process tool results
        elif etype == "turn_complete":
            pass
        elif etype == "max_turns":
            console.print(f"\n[yellow]Reached max turns ({event['turns']})[/yellow]")


def _run_single(client, prompt: str, cwd: str, model: str):
    """Non-interactive print mode. Maps to CC's print.ts."""
    messages = [{"role": "user", "content": prompt}]
    for event in run_agent_loop(client, messages, cwd, model):
        if event["type"] == "text_delta":
            sys.stdout.write(event["text"])
            sys.stdout.flush()
    sys.stdout.write("\n")


def _get_input() -> str:
    """Get user input with a simple prompt."""
    try:
        return input("\033[1;36m> \033[0m")
    except EOFError:
        raise


def _print_banner(model: str, cwd: str):
    """Print startup banner. Maps to CC's LogoV2 / WelcomeV2 components."""
    console.print()
    console.print("[bold]╔══════════════════════════════════╗[/bold]")
    console.print("[bold]║     Mini Claude Code  v{:<9s}║[/bold]".format(__version__))
    console.print("[bold]╚══════════════════════════════════╝[/bold]")
    console.print()
    console.print(f"  [dim]Model:[/dim]  {model}")
    console.print(f"  [dim]CWD:[/dim]    {cwd}")
    console.print(f"  [dim]Tools:[/dim]  Bash, Read, Write, Edit, Glob, Grep")
    console.print(f"  [dim]Help:[/dim]   /help  |  [dim]Exit:[/dim]  /exit  |  [dim]Clear:[/dim]  /clear")
    console.print()


def _print_help():
    console.print("""
[bold]Slash Commands[/bold]
  /help    Show this help
  /clear   Clear conversation history
  /cost    Show token usage (stub)
  /exit    Exit

[bold]Tips[/bold]
  - Ask questions about your codebase
  - Request file edits, code reviews, refactors
  - Run shell commands through the agent
  - The agent will ask permission before writing files or running commands
""")


def _print_cost_stub():
    console.print("[dim]Cost tracking not implemented in mini version.[/dim]")


if __name__ == "__main__":
    main()
