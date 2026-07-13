"""
Claude API client via AWS Bedrock.
Uses the same AWS credentials that Claude Code CLI uses in this environment.
"""
import os
import logging
from functools import lru_cache
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


def ask_claude_with_thinking(
    prompt: str,
    system: str = None,
    max_tokens: int = 16000,
    thinking_budget: int = 8000,
) -> dict:
    """
    Call Claude via Bedrock with extended thinking enabled.
    Returns {"thinking": str, "text": str} — thinking is Claude's raw
    deliberation before it committed to its answer; text is the final output.

    max_tokens must be > thinking_budget (Anthropic requirement).
    thinking_budget controls how many tokens Claude can spend deliberating —
    higher = more thorough reasoning but slower and more expensive.
    Extended thinking disables streaming (SDK limitation), so this uses a
    blocking messages.create() call.
    """
    client = get_claude_client()
    kwargs = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "thinking": {"type": "enabled", "budget_tokens": thinking_budget},
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    message = client.messages.create(**kwargs)

    thinking_text = "".join(
        block.thinking for block in message.content
        if getattr(block, "type", None) == "thinking"
    )
    output_text = "".join(
        block.text for block in message.content
        if getattr(block, "type", None) == "text"
    )
    return {"thinking": thinking_text, "text": output_text}
