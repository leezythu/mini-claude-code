"""
Query engine — the brain that drives the agentic loop.

Maps to CC source:
  - src/QueryEngine.ts              → submitMessage (session orchestrator)
  - src/query.ts → query / queryLoop → THE core while(true) loop
  - src/services/api/claude.ts      → queryModel / queryModelWithStreaming
  - src/services/tools/toolExecution.ts → runToolUse (execute + build tool_result)
  - src/services/tools/toolOrchestration.ts → runTools (batch/concurrent dispatch)

CC's query pipeline:
  QueryEngine.submitMessage
    → query()
      → queryLoop() [while(true)]
        → callModel (stream SSE from Anthropic API)
        → collect tool_use blocks from assistant response
        → runTools → for each tool_use: checkPermission → tool.call → tool_result
        → append assistant messages + tool_results to history
        → continue loop (next API call with updated history)
        → exit when: no tool_use (end_turn), abort, maxTurns, budget exceeded

Mini version: same loop structure, supporting both Anthropic and OpenAI-compatible APIs.
"""

from __future__ import annotations

import json
import os
from typing import Any, Generator

from .permissions import check_permission
from .prompt import build_system_prompt
from .tools import TOOL_MAP, get_tool_schemas

StreamEvent = dict[str, Any]


def detect_provider() -> str:
    """Detect which API provider to use based on environment variables."""
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
        return "openai"
    return "anthropic"


# ═══════════════════════════════════════════════════════════════════════════
# Anthropic backend
# ═══════════════════════════════════════════════════════════════════════════

def _create_anthropic_client():
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _run_anthropic_loop(
    client, messages: list[dict], cwd: str, model: str, max_turns: int,
) -> Generator[StreamEvent, None, None]:
    import anthropic

    system_prompt = build_system_prompt(cwd)
    tools = get_tool_schemas()

    for turn in range(1, max_turns + 1):
        assistant_content = []

        try:
            with client.messages.stream(
                model=model, max_tokens=8192,
                system=system_prompt, messages=messages, tools=tools,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_start":
                        if event.content_block.type == "text":
                            yield {"type": "text_start"}
                        elif event.content_block.type == "tool_use":
                            yield {"type": "tool_start", "name": event.content_block.name, "id": event.content_block.id}
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield {"type": "text_delta", "text": event.delta.text}
                        elif event.delta.type == "input_json_delta":
                            yield {"type": "tool_input_delta", "json": event.delta.partial_json}
                    elif event.type == "content_block_stop":
                        yield {"type": "block_stop"}

                response = stream.get_final_message()
                assistant_content = response.content

        except anthropic.APIError as e:
            yield {"type": "error", "message": f"API Error: {e.status_code} {e.message}"}
            return
        except Exception as e:
            yield {"type": "error", "message": f"Error: {e}"}
            return

        messages.append({
            "role": "assistant",
            "content": [_anthropic_block_to_dict(b) for b in assistant_content],
        })

        tool_use_blocks = [b for b in assistant_content if b.type == "tool_use"]
        if not tool_use_blocks:
            yield {"type": "turn_complete"}
            return

        tool_results = []
        for block in tool_use_blocks:
            result_msg = yield from _execute_tool(block.name, block.input, block.id, cwd)
            tool_results.append(result_msg)

        messages.append({"role": "user", "content": tool_results})
        yield {"type": "loop_continue", "turn": turn}

    yield {"type": "max_turns", "turns": max_turns}


def _anthropic_block_to_dict(block: Any) -> dict:
    if block.type == "text":
        return {"type": "text", "text": block.text}
    elif block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    elif block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking}
    return {"type": block.type}


# ═══════════════════════════════════════════════════════════════════════════
# OpenAI-compatible backend (OpenRouter, etc.)
# ═══════════════════════════════════════════════════════════════════════════

def _create_openai_client():
    from openai import OpenAI
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL")
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _get_openai_tools(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool schema format to OpenAI function calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


def _run_openai_loop(
    client, messages: list[dict], cwd: str, model: str, max_turns: int,
) -> Generator[StreamEvent, None, None]:
    system_prompt = build_system_prompt(cwd)
    tools = _get_openai_tools(get_tool_schemas())

    # OpenAI uses system message in messages array
    oai_messages = [{"role": "system", "content": system_prompt}]

    # Convert any existing messages (Anthropic format → OpenAI format)
    for msg in messages:
        oai_messages.append(_to_openai_message(msg))

    for turn in range(1, max_turns + 1):
        try:
            stream = client.chat.completions.create(
                model=model, max_tokens=8192,
                messages=oai_messages, tools=tools,
                tool_choice="auto", stream=True,
            )
        except Exception as e:
            yield {"type": "error", "message": f"API Error: {e}"}
            return

        # Accumulate streamed response
        content_text = ""
        tool_calls_acc: dict[int, dict] = {}  # index → {id, name, arguments}
        has_started_text = False

        try:
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Text content
                if delta.content:
                    if not has_started_text:
                        has_started_text = True
                        yield {"type": "text_start"}
                    yield {"type": "text_delta", "text": delta.content}
                    content_text += delta.content

                # Tool calls (streamed incrementally)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_acc:
                            if has_started_text:
                                yield {"type": "block_stop"}
                                has_started_text = False
                            tool_calls_acc[idx] = {
                                "id": tc.id or "",
                                "name": tc.function.name or "" if tc.function else "",
                                "arguments": "",
                            }
                            if tool_calls_acc[idx]["name"]:
                                yield {"type": "tool_start", "name": tool_calls_acc[idx]["name"], "id": tool_calls_acc[idx]["id"]}
                        if tc.function and tc.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc.function.arguments
                            yield {"type": "tool_input_delta", "json": tc.function.arguments}

                finish = chunk.choices[0].finish_reason if chunk.choices else None
                if finish:
                    if has_started_text:
                        yield {"type": "block_stop"}
                    break

        except Exception as e:
            yield {"type": "error", "message": f"Stream error: {e}"}
            return

        # Build assistant message for history
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content_text or None}
        if tool_calls_acc:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls_acc.values()
            ]
        oai_messages.append(assistant_msg)

        # Also maintain the original messages list for potential display
        messages.append({"role": "assistant", "content": content_text or ""})

        if not tool_calls_acc:
            yield {"type": "turn_complete"}
            return

        # Execute tools
        for tc in tool_calls_acc.values():
            try:
                inp = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                inp = {}

            result_event = yield from _execute_tool(tc["name"], inp, tc["id"], cwd)

            # OpenAI format: tool results are separate messages with role="tool"
            oai_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result_event.get("content", ""),
            })

        yield {"type": "loop_continue", "turn": turn}

    yield {"type": "max_turns", "turns": max_turns}


def _to_openai_message(msg: dict) -> dict:
    """Convert a message from Anthropic format to OpenAI format."""
    role = msg["role"]
    content = msg.get("content", "")
    if isinstance(content, str):
        return {"role": role, "content": content}
    if isinstance(content, list):
        # Extract text from content blocks
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return {"role": role, "content": "\n".join(texts) if texts else ""}
    return {"role": role, "content": str(content)}


# ═══════════════════════════════════════════════════════════════════════════
# Shared: tool execution
# ═══════════════════════════════════════════════════════════════════════════

def _execute_tool(name: str, inp: dict, tool_id: str, cwd: str) -> Generator[StreamEvent, None, dict]:
    """Execute a single tool call. Used by both backends."""
    tool = TOOL_MAP.get(name)
    if not tool:
        yield {"type": "tool_error", "name": name, "error": "unknown tool"}
        return {"type": "tool_result", "tool_use_id": tool_id, "content": f"Error: unknown tool '{name}'", "is_error": True}

    if not isinstance(inp, dict):
        inp = {}

    if not check_permission(tool, inp, cwd):
        yield {"type": "tool_denied", "name": tool.name}
        return {"type": "tool_result", "tool_use_id": tool_id, "content": "Permission denied by user.", "is_error": True}

    yield {"type": "tool_running", "name": tool.name}
    try:
        result = tool.run(inp, cwd)
    except Exception as e:
        result = f"Tool execution error: {e}"

    if len(result) > 50000:
        result = result[:50000] + "\n... [truncated]"

    yield {"type": "tool_result", "name": tool.name, "result": result}
    return {"type": "tool_result", "tool_use_id": tool_id, "content": result}


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def create_client():
    """Create the appropriate API client based on environment variables."""
    provider = detect_provider()
    if provider == "openai":
        return _create_openai_client()
    return _create_anthropic_client()


def run_agent_loop(
    client,
    messages: list[dict],
    cwd: str,
    model: str = "claude-sonnet-4-20250514",
    max_turns: int = 50,
) -> Generator[StreamEvent, None, None]:
    """
    The core agentic loop. This is the heart of Claude Code.

    Dispatches to the appropriate backend (Anthropic or OpenAI-compatible)
    based on environment variables.
    """
    provider = detect_provider()
    if provider == "openai":
        yield from _run_openai_loop(client, messages, cwd, model, max_turns)
    else:
        yield from _run_anthropic_loop(client, messages, cwd, model, max_turns)
