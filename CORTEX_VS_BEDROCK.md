# Cortex vs Bedrock — Decision Reference

This doc covers the tradeoffs between Snowflake Cortex and AWS Bedrock (Anthropic SDK)
as LLM backends for this app. Written to inform routing decisions as the rule intelligence
feature grows.

---

## What Each One Is

### Snowflake Cortex
Snowflake's built-in AI layer. Exposes LLMs (Claude, Llama, Mistral, Arctic) as SQL
functions. You call them like any other SQL expression — no API keys, no HTTP client,
no infrastructure. The model call is just a column in a query result.

```sql
SELECT SNOWFLAKE.CORTEX.COMPLETE('claude-opus-4-8', my_prompt_column)
FROM my_table
```

Snowflake routes the request to the model provider (Anthropic, Meta, etc.) through their
own private backend network — not the public internet. Your data stays within Snowflake's
trust boundary.

### AWS Bedrock (Anthropic SDK)
Anthropic's Claude models hosted on AWS infrastructure, accessed via the `AnthropicBedrock`
client. This is the full Anthropic API surface — all parameters, all features — running
inside AWS rather than going direct to Anthropic. Your app's `claude_client.py` uses this.

---

## Comparison

| Capability | Cortex | Bedrock |
|---|---|---|
| Extended thinking | No | Yes |
| Streaming | No | Yes (used in this app) |
| Max output tokens | ~4k effective cap | 32k+ |
| System prompt (separate channel) | No — must prepend to user prompt | Yes |
| Temperature / top_p / other params | No | Yes |
| Tool use / function calling | No | Yes |
| Thinking blocks (separate from output) | No | Yes |
| Structured output control | No | Yes |
| Retry / timeout control | No (SQL statement timeout only) | Full control |

| Operational factor | Cortex | Bedrock |
|---|---|---|
| Credentials needed | None — reuses Snowflake SSO session | AWS credentials (IAM role / env vars) |
| Data leaves Snowflake | No — private network to provider | Yes — leaves Snowflake, goes to AWS |
| Snowflake query history / audit | Yes — appears as a SQL query | No |
| Snowflake cost attribution | Yes — billed as Cortex credits | No — billed to AWS |
| Governance (column masking, row access) | Inherits Snowflake policies | None |
| Run LLM over a whole table column | Yes — trivially, in a single SQL | Requires fetching rows first |
| Latency | Higher (SQL parsing + routing) | Lower (direct API) |
| Reliability | Can hit Snowflake warehouse timeouts | Purpose-built API, independent of warehouse |

| Use case fit | Cortex | Bedrock |
|---|---|---|
| Bulk column-level inference (summarize 10k rows) | Excellent | Awkward |
| Short, simple one-shot prompts | Good | Good |
| Large structured JSON output | Poor (output cap truncates) | Excellent |
| Complex agent prompts (large system prompt + context) | Poor | Excellent |
| Extended thinking / chain of thought capture | Not supported | Native support |
| Prompt iteration / debugging | Hard (buried in SQL) | Easy |

---

## How This App Uses Each

### Cortex — `cortex_client.py`
Used for **fix recommendations** on findings. Short prompt (finding + schema context),
short response (JSON with explanation + SQL). This is a good fit for Cortex:
- Small input, small output — well within the token cap
- Snowflake-native data (table schema from DESCRIBE TABLE)
- No complex output structure needed
- Falls back to Bedrock automatically on failure

### Bedrock — `claude_client.py`
Used as the **primary backend for rule intelligence** (`rule_intelligence_agent.py`).
Was originally a fallback behind Cortex, but effectively became primary because:
- Rule intelligence prompts are large (schema + stats + definitions + sample data)
- Responses are large structured JSON (table classification + N rule proposals)
- Cortex's output cap silently truncates responses mid-JSON
- Extended thinking (planned) is not available on Cortex

The `_call_model` method in `rule_intelligence_agent.py` tries Cortex first then falls
back to Bedrock — in practice, Bedrock handles the actual work every time for this agent.

---

## Planned Change: Rule Intelligence → Bedrock Primary

For the rule intelligence logging feature (extended thinking capture + vector store),
`rule_intelligence_agent.py` will be changed to call Bedrock directly as the primary
path, removing the Cortex-first attempt for this specific agent.

**Why:** Extended thinking requires Bedrock. The Cortex attempt adds latency (it fails
and retries every time) and provides no benefit for this call profile.

**Cortex stays for:** `cortex_client.py` (fix recommendations) — that use case is a
genuine fit for Cortex's strengths (short, Snowflake-native, no extra credentials).

---

## When to Use Which

```
Need to run LLM over many rows of Snowflake data in bulk?
  → Cortex (it's what it's built for)

Need extended thinking / chain of thought capture?
  → Bedrock only

Need large output (>4k tokens)?
  → Bedrock only

Short, simple prompt with Snowflake-native context (schema, metadata)?
  → Cortex first, Bedrock fallback (current cortex_client.py pattern)

Complex agent prompt with large context window?
  → Bedrock directly

Need full audit trail inside Snowflake governance?
  → Cortex (or log Bedrock calls separately)
```

---

## The One Thing Cortex Does That Bedrock Cannot

Running an LLM call **inside a SQL query over a full table**:

```sql
-- Summarize every support ticket in one SQL statement
SELECT
    ticket_id,
    SNOWFLAKE.CORTEX.COMPLETE('claude-haiku-4-5', 'Summarize: ' || ticket_text) AS summary
FROM support_tickets
WHERE created_date > DATEADD('day', -7, CURRENT_DATE)
```

With Bedrock you'd have to fetch the rows, loop in Python, make N API calls, write
results back. With Cortex it's one SQL statement. For bulk inference jobs over Snowflake
data this is a real advantage — not relevant to this app today, but worth knowing.

---

## Summary

Cortex trades capability for convenience. It's the right tool when you want to stay
entirely within Snowflake, run bulk inference over table data, and don't need the full
API surface. Bedrock is the right tool when you need the full API — extended thinking,
large outputs, streaming, fine-grained control. For this app's core intelligence work,
Bedrock is the correct primary backend.
