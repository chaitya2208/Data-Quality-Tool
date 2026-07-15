"""
Claude API client via AWS Bedrock.
Uses the same AWS credentials that Claude Code CLI uses in this environment.
"""
import json
import os
import re
import logging
from functools import lru_cache
from typing import Optional
from anthropic import AnthropicBedrock, DefaultHttpxClient

logger = logging.getLogger(__name__)

# Cross-region inference profile for us-east-2 (and compatible with us-east-1)
DEFAULT_MODEL = "us.anthropic.claude-opus-4-8"
FALLBACK_MODEL = "us.anthropic.claude-sonnet-4-6-20251114-v1:0"


@lru_cache(maxsize=1)
def get_claude_client() -> AnthropicBedrock:
    """
    Returns a singleton AnthropicBedrock client.
    Picks up AWS credentials from the environment automatically (same as Claude Code CLI).
    SSL verification disabled for corporate proxy compatibility.
    """
    region = os.environ.get("AWS_REGION", "us-east-2")
    logger.info(f"Initializing Claude client via AWS Bedrock in region: {region}")
    return AnthropicBedrock(
        aws_region=region,
        http_client=DefaultHttpxClient(verify=False),
    )


def ask_claude(prompt: str, system: str = None, max_tokens: int = 32000) -> str:
    """
    One-shot call to Claude via Bedrock. Returns the concatenated text response.
    Raises on API errors.

    Streams internally: the Anthropic SDK refuses / times out non-streaming
    requests with large max_tokens (idle-connection drop), so we always use
    the streaming helper and reassemble the final message. This keeps a single
    string return type for callers regardless of response size. Default
    max_tokens is 32000 — large enough for responses that must enumerate many
    items (e.g. classifying 100+ rules) without truncating mid-JSON.
    """
    client = get_claude_client()
    kwargs = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    # Concatenate all text blocks (thinking/tool blocks, if any, are skipped).
    return "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )


_FENCE_OPEN = re.compile(r"^\s*```[a-z]*\n?", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\n?```\s*$")


def _strip_fences(text: str) -> str:
    """Strip a wrapping ```json ... ``` (or plain ```) fence from a model
    response. Idempotent — bare JSON passes through unchanged."""
    if not text:
        return text
    text = _FENCE_OPEN.sub("", text).rstrip()
    text = _FENCE_CLOSE.sub("", text).rstrip()
    return text.strip()


def ask_claude_json(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 8000,
    retries: int = 1,
    label: str = "claude_json",
) -> Optional[dict]:
    """
    Call Claude, expect JSON back, return a parsed dict (or None on failure).

    Centralises what every structured-output caller in the app used to
    hand-roll (audit finding #7):
      - fence stripping (```json … ```)
      - JSON parse with the same fallback: try fenced first, then bare
      - one repair retry when parsing fails (prompted to return ONLY JSON)
      - consistent WARNING-level logging with `label` in the message so
        different call sites are distinguishable in the log stream

    Returns None when even the retry fails to produce parseable JSON. The
    caller decides whether that's fatal (raise) or soft (proceed with a
    fallback dict). `retries=0` disables the repair pass.

    For the agentic/tool-use path (get_sample_rows), use ask_claude_agentic
    directly — this helper is for one-shot structured completions.
    """
    def _try_parse(raw: str) -> Optional[dict]:
        if not raw:
            return None
        cleaned = _strip_fences(raw.strip())
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    raw = ask_claude(prompt, system=system, max_tokens=max_tokens)
    parsed = _try_parse(raw)
    if parsed is not None:
        return parsed

    for attempt in range(retries):
        logger.warning(
            f"[{label}] JSON parse failed on attempt {attempt + 1}, retrying with repair prompt "
            f"(raw first 200 chars: {(raw or '')[:200]!r})"
        )
        repair_prompt = (
            prompt
            + "\n\nYour previous response could not be parsed as JSON. "
            "Respond again with ONLY a single valid JSON object — no markdown "
            "fences, no prose before or after."
        )
        raw = ask_claude(repair_prompt, system=system, max_tokens=max_tokens)
        parsed = _try_parse(raw)
        if parsed is not None:
            return parsed

    logger.warning(f"[{label}] JSON parse failed after {retries + 1} attempt(s) — returning None")
    return None


def ask_claude_agentic(
    prompt: str,
    system: str = None,
    tools: list = None,
    tool_executor=None,
    max_tokens: int = 24000,
    effort: str = "high",
    max_tool_rounds: int = 5,
) -> dict:
    """
    Agentic tool-use loop with extended thinking (adaptive).

    Sends the initial prompt with tools defined; if Claude responds with
    stop_reason="tool_use" it executes each tool via tool_executor(name, inputs)
    and feeds results back as a new user turn.  Loops until end_turn or
    max_tool_rounds is exhausted.

    tool_executor: callable(name: str, inputs: dict) -> str

    Returns {"text": str, "thinking": str, "tool_calls": list[dict]}
    where tool_calls is [{name, input, result}, ...] in call order.

    Thinking blocks are carried forward in history (API requirement) and their
    text is accumulated in "thinking".
    """
    client = get_claude_client()
    messages = [{"role": "user", "content": prompt}]
    kwargs = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools

    all_thinking: list = []
    all_tool_calls: list = []
    message = None

    for _round in range(max_tool_rounds + 1):
        with client.messages.stream(**kwargs) as stream:
            message = stream.get_final_message()

        for block in message.content:
            if getattr(block, "type", None) == "thinking":
                all_thinking.append(getattr(block, "thinking", "") or "")

        if message.stop_reason != "tool_use" or _round >= max_tool_rounds:
            break

        # Build assistant content list — must preserve thinking/text/tool_use blocks
        assistant_content = []
        tool_results = []
        for block in message.content:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                bd = {"type": "thinking", "thinking": block.thinking}
                sig = getattr(block, "signature", None)
                if sig:
                    bd["signature"] = sig
                assistant_content.append(bd)
            elif btype == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif btype == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
                try:
                    result_str = tool_executor(block.name, block.input or {}) if tool_executor else "No executor provided."
                except Exception as exc:
                    result_str = f"Tool execution error: {exc}"
                    logger.warning(f"[agentic] tool {block.name} raised: {exc}")

                all_tool_calls.append({"name": block.name, "input": block.input, "result": result_str})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        kwargs["messages"] = kwargs["messages"] + [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": tool_results},
        ]

    final_text = "".join(
        block.text for block in (message.content if message else [])
        if getattr(block, "type", None) == "text"
    )
    return {
        "text": final_text,
        "thinking": "\n\n---\n\n".join(all_thinking),
        "tool_calls": all_tool_calls,
    }


def ask_claude_with_thinking(
    prompt: str,
    system: str = None,
    max_tokens: int = 24000,
    thinking_budget: int = 8000,  # kept for signature compat; ignored for claude-4.8+
    effort: str = "high",
) -> dict:
    """
    Call Claude via Bedrock with extended thinking enabled.
    Returns {"thinking": str, "text": str}.

    Claude 4.8+ uses thinking.type="adaptive" + output_config.effort instead
    of the old thinking.type="enabled" + budget_tokens API.
    Streams to keep the corporate proxy connection alive during long thinking.
    """
    client = get_claude_client()
    kwargs = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    with client.messages.stream(**kwargs) as stream:
        message = stream.get_final_message()

    thinking_text = "".join(
        block.thinking for block in message.content
        if getattr(block, "type", None) == "thinking"
    )
    output_text = "".join(
        block.text for block in message.content
        if getattr(block, "type", None) == "text"
    )
    return {"thinking": thinking_text, "text": output_text}
