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


def ask_claude(prompt: str, system: str = None, max_tokens: int = 4096) -> str:
    """
    Simple one-shot call to Claude. Returns the text response.
    Raises on API errors.
    """
    client = get_claude_client()
    kwargs = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    return response.content[0].text
