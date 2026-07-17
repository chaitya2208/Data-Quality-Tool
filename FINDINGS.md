# Thinking Pipeline — Engineering Findings

## Problem
`RULE_INTELLIGENCE_LOGS.THINKING` was always empty. The Cortex Search service
`RULE_INTELLIGENCE_SEARCH` indexes this column for semantic search, so it was
effectively indexing nothing.

---

## Investigation

### Step 1 — Wrong API for Claude 4.8+
The original code used `thinking.type="enabled"` with `budget_tokens`:
```python
"thinking": {"type": "enabled", "budget_tokens": 8000}
```
This is rejected by Bedrock with HTTP 400 for `claude-opus-4-8`:
```
"thinking.type.enabled" is not supported for this model.
Use "thinking.type.adaptive" and "output_config.effort" to control thinking behavior.
```
The exception was caught, the code fell back to `ask_claude` which always returns `thinking=""`.
The log row was written but with an empty `THINKING` column — silently, no error visible.

### Step 2 — Blocking call drops under corporate proxy
The original code used `messages.create()` (blocking). With an 8000-token thinking
budget, a call can take 30–60 seconds. The corporate proxy drops idle connections
before the response arrives, killing the call. Switched to `messages.stream()` which
sends chunks continuously and keeps the connection alive.

### Step 3 — Bedrock redacts thinking content by design
After fixing the API shape to `thinking.type="adaptive"` + `output_config.effort="high"`,
the call succeeded but `.thinking` was still always `""`.

Inspecting the raw stream events:
```
RawContentBlockStartEvent → content_block_start
SignatureEvent             → signature           ← AWS signs the thinking block
ParsedContentBlockStopEvent → content_block_stop
```

The thinking block is present in `message.content` with `type="thinking"`, but
`.thinking = ""` every time — confirmed across `effort=high`, `effort=max`, and
multiple prompt complexities.

**This is by AWS design.** Bedrock signs the thinking block internally (for
multi-turn chain-of-thought continuity) but never exposes the raw text to the
caller. It cannot be worked around within Bedrock.

### Step 4 — Confirmed `effort=max` still empty
Ran three separate calls with `effort="max"` and complex prompts. All returned
`thinking_len=0`. The thinking block exists, the signature fires, the text output
is high quality — the reasoning is happening internally, it just isn't returned.

---

## Solution — Post-hoc reasoning generation

Added `_generate_thinking()` in `rule_intelligence_agent.py`: a second Claude call
made after the main analysis completes, passing the original prompt and the model
output, asking Claude to write its full reasoning chain in continuous prose.

The prompt instructs Claude to cover:
- How it classified the table type and which signals drove that conclusion
- Which definitions were evaluated and why each was relevant or not
- Which column statistics stood out and how they shaped proposals
- For every proposed rule: why this column, this threshold, this severity, what
  failure mode it catches, whether current data already violates it
- What it considered proposing but rejected, and why

**Result**: 11,000+ characters of deep, table-specific reasoning per run.
`THINKING` now populates. `RULE_INTELLIGENCE_SEARCH` has real content to index.

The call is best-effort — a failure logs a warning and returns `""` so the
pipeline is never blocked.

---

## Additional fix — `max_tokens` too tight
`ask_claude_with_thinking` was called with `max_tokens=16000` and `thinking_budget=8000`,
leaving only 8000 tokens for the actual JSON response. For tables with many columns
and rule proposals this caused truncated output. Bumped to `max_tokens=24000`.

---

## Files Changed

| File | Change |
|---|---|
| `backend/app/services/claude_client.py` | `thinking.type="adaptive"` + `output_config.effort`, switched to streaming, `max_tokens=24000` |
| `backend/app/services/agents/rule_intelligence_agent.py` | Added `_generate_thinking()`, `_THINKING_SYSTEM` prompt, `max_tokens=24000` |
