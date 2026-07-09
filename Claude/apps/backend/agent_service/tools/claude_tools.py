"""Claude Tools -- the first LLM integration in this codebase. Everything
built before this (tools/, skills/, agents/) is plain deterministic Python,
per explicit instruction to get those working first ("do not start with
Claude first"). This module is that intentional next step.

Connects via **Amazon Bedrock**, not the first-party Anthropic API -- per
explicit instruction ("we will use bedrock api key"). Two Bedrock SDK
surfaces exist (see Anthropic's docs): the newer `AnthropicBedrockMantle`
client (Messages API at /anthropic/v1/messages, needs the
`bedrock-mantle:CreateInference` IAM action) and the legacy `AnthropicBedrock`
client (InvokeModel API, needs `bedrock-runtime:InvokeModel`). Verified
directly against this account: the Mantle endpoint 403s ("not authorized to
perform: bedrock-mantle:CreateInference") for every model, while the legacy
`AnthropicBedrock` + `InvokeModel` path works. This module uses
`AnthropicBedrock` for that reason, not by default preference.

Model ID: `us.anthropic.claude-sonnet-5` (an inference-profile ID, not the
bare `anthropic.claude-sonnet-5` -- verified directly: the bare form 404s on
InvokeModel with "on-demand throughput isn't supported... use an inference
profile"). Sonnet, not Opus, chosen deliberately for this task: rule
recommendation runs once per profiled table in a request path (see
rule_recommendation_agent.py), a volume/latency/cost profile Sonnet fits
better than Opus for a well-specified, structured-JSON extraction task --
not a long-horizon agentic one. `output_config.format` (native structured
outputs) 400s on this legacy InvokeModel surface ("Extra inputs are not
permitted") -- confirmed directly -- so structured output here uses forced
tool use (`tool_choice: {"type": "tool", "name": ...}`) instead, which does
work on this path and is the documented fallback structured-output
mechanism.

TLS note: this network intercepts HTTPS with a corporate proxy whose
certificate isn't in Python's bundled certifi store (calls failed with
CERTIFICATE_VERIFY_FAILED / self-signed certificate in certificate chain).
`truststore.SSLContext` makes an httpx client verify against the OS
certificate store (which does trust the proxy's root CA) instead of
certifi -- verified this specifically fixes it, passed in via the anthropic
client's `http_client=` param.

Deliberately NOT `truststore.inject_into_ssl()` -- that patches the global
`ssl` module process-wide, which broke the Snowflake connector's own TLS
handshake the moment this module was imported anywhere in the same process
(observed directly: `OperationalError: maximum recursion depth exceeded`
inside snowflake-connector's certificate validation, immediately after
adding the global inject). Scoping the fix to one httpx.Client instance
avoids that cross-module interference entirely.
"""

from __future__ import annotations

import json
import os
import ssl
from typing import Any

import httpx
import truststore

# pyrefly: ignore [missing-import]
from anthropic import AnthropicBedrock
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

from tools.langsmith_tools import get_traced_client_for_anthropic
from tools.storage_tools import _json_default

load_dotenv()

_http_client = httpx.Client(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

# Module-level cache, same pattern as tools/snowflake_connection.py's
# _source_conn/_app_conn -- one client reused across calls instead of
# reconstructing (and re-resolving Bedrock credentials) on every call.
_client: AnthropicBedrock | None = None


def _get_client() -> AnthropicBedrock:
    global _client
    if _client is None:
        _client = AnthropicBedrock(aws_region=os.getenv("AWS_REGION"), http_client=_http_client)
        # Wraps with langsmith's wrap_anthropic() for full input/output/token
        # visibility per call, IF LANGSMITH_API_KEY is set -- no-ops (returns
        # the client unchanged) otherwise. See tools/langsmith_tools.py.
        _client = get_traced_client_for_anthropic(_client)
    return _client

# Inference-profile ID, not the bare model ID -- see module docstring.
_MODEL_ID = "us.anthropic.claude-sonnet-5"

# PII columns must never reach the LLM as raw values -- architecture.md §7's
# masking floor. No PII/Sensitivity Agent exists yet (deferred-and-future-work.md
# #3), so column_profile["is_pii"] is always False today; this check is the
# enforcement point that agent's output will flow through once it exists,
# not a no-op guard against something that can't happen yet.
_MASKED_PLACEHOLDER = "***MASKED***"

_TABLE_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "table_type": {
            "type": "string",
            "enum": ["fact", "dimension", "staging", "config", "audit", "reference", "unknown"],
            "description": (
                "Functional classification of this table. "
                "fact = transactional records (orders, events, measurements); "
                "dimension = descriptive reference data (customers, products, employees); "
                "staging = raw/landing area for ingestion; "
                "config = small lookup/parameter tables; "
                "audit = change/activity logs; "
                "reference = code/type/status lookups; "
                "unknown = genuinely ambiguous."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "0-1: how confident you are in this classification.",
        },
        "reasoning": {
            "type": "string",
            "description": (
                "One sentence explaining the primary signal that drove this "
                "classification (e.g. 'Contains ORDER_DATE, AMOUNT, and CUSTOMER_ID "
                "columns typical of a transaction fact table')."
            ),
        },
    },
    "required": ["table_type", "confidence", "reasoning"],
    "additionalProperties": False,
}

_TOOL_NAME = "submit_recommended_rules"

_RULE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "rule_name": {"type": "string"},
        "rule_type": {
            "type": "string",
            "description": (
                "One of the common DQ categories (COMPLETENESS, UNIQUENESS, "
                "VALIDITY, FRESHNESS, VOLUME, ACCURACY, CONSISTENCY, "
                "REFERENTIAL_INTEGRITY, SCHEMA_DRIFT, DISTRIBUTION) or a "
                "business/domain-specific type you name yourself if none fit."
            ),
        },
        "column_name": {
            "type": ["string", "null"],
            "description": "Column this rule applies to, or null for a table-level rule.",
        },
        "description": {"type": "string"},
        "severity": {"type": "string", "enum": ["CRITICAL", "WARNING", "INFO"]},
        "confidence": {
            "type": "number",
            "description": "0-1: how sure you are this rule is logically correct.",
        },
        "priority": {
            "type": "number",
            "description": "0-1: combined business importance (severity x confidence x domain judgment).",
        },
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "threshold_config": {
            "type": "object",
            "description": (
                "Concrete, machine-checkable parameters for this rule (e.g. "
                "{\"max_null_percentage\": 5} or {\"accepted_values\": [...]})"
                " -- not prose."
            ),
        },
        "generated_sql": {
            "type": ["string", "null"],
            "description": (
                "A single Snowflake SELECT statement that tests this rule. "
                "MUST return exactly two columns: FAILED_COUNT (rows violating the rule) "
                "and TOTAL_COUNT (rows checked). Use the fully-qualified table name "
                "database_name.schema_name.table_name. Example shape: "
                "SELECT COUNT_IF(<condition>) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT "
                "FROM <db>.<schema>.<table>. "
                "Set to null only if you genuinely cannot express the rule as a single "
                "aggregate SELECT (e.g. a schema-drift check)."
            ),
        },
    },
    "required": [
        "rule_name",
        "rule_type",
        "column_name",
        "description",
        "severity",
        "confidence",
        "priority",
        "reason",
        "evidence",
        "threshold_config",
        "generated_sql",
    ],
    "additionalProperties": False,
}

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "table_classification": _TABLE_TYPE_SCHEMA,
        "rules": {"type": "array", "items": _RULE_ITEM_SCHEMA},
    },
    "required": ["table_classification", "rules"],
    "additionalProperties": False,
}

_SUBMIT_RULES_TOOL = {
    "name": _TOOL_NAME,
    "description": "Submit the recommended data quality rules for this table.",
    "input_schema": _RESPONSE_SCHEMA,
}

_SYSTEM_PROMPT = """You are the Rule Recommendation Agent in an agentic Data \
Quality platform for Snowflake. You are given one table's metadata, column \
statistics, and the deterministic template rules already found for it.

Before proposing rules, first classify what kind of table this is: fact \
(transactional records — orders, events, measurements), dimension (descriptive \
reference data — customers, products, employees), staging (raw/landing area for \
ingestion), config (small lookup/parameter tables), audit (change/activity logs), \
reference (code/type/status lookups), or unknown (genuinely ambiguous). Use the \
table name, column names, data types, row count, and any comment fields as signals. \
Provide a confidence score (0-1) and a single reasoning sentence. This \
classification shapes which rules matter most: a fact table needs VOLUME/FRESHNESS/ \
REFERENTIAL checks most critically; a dimension table needs UNIQUENESS/COMPLETENESS/ \
VALIDITY; a staging table may tolerate more nulls but should flag schema drift.

Your job: propose additional data quality rules a domain expert would catch \
but a fixed template can't -- rules that require understanding what this \
table *means* (business/domain judgment), not just its column names and \
null percentages. Do not repeat a rule already listed in \
template_rules_already_found OR in rules_already_pending_human_review (if \
that field is present) -- those are rules already waiting for a human to \
approve or reject, so re-suggesting them wastes the reviewer's time. Your \
value is in what templates and prior scans miss \
(business consistency, referential integrity you can infer from names/\
comments, plausible-range checks a generic template wouldn't know, schema \
drift risk, distribution expectations implied by the domain, etc.).

Numbers you're given (null percentages, distinct counts, min/max) are \
already computed deterministically -- trust them, don't recompute or \
second-guess them. Your job is judgment and explanation on top of those \
numbers, not new arithmetic.

Values marked "***MASKED***" are sensitive (PII) and were withheld from \
you -- reason about that column using its name, data type, and null/distinct \
statistics only. Never invent or guess a masked value.

Every rule must be concrete and testable -- threshold_config must contain \
actual parameters (numbers, value lists, column names), not vague language. \
If you have no genuinely new rule to add for this table, return an empty \
rules array rather than padding with a restated template rule.

For EVERY rule you propose, you MUST also write the generated_sql: a single \
Snowflake SELECT that tests the rule. It must return exactly two columns: \
FAILED_COUNT (integer: rows that violate the rule) and TOTAL_COUNT (integer: \
rows checked). Use COUNT_IF() for row-level checks. Use the fully-qualified \
table name as given (database_name.schema_name.table_name). Example:
  SELECT COUNT_IF(column < 0) AS FAILED_COUNT, COUNT(*) AS TOTAL_COUNT
  FROM db.schema.table
Set generated_sql to null ONLY when the rule genuinely cannot be expressed \
as a single aggregate SELECT (e.g. cross-table referential integrity with no \
join key available). For range checks, ratio checks, value-set checks, \
consistency between columns, and distribution checks you must produce SQL."""


def _mask_column_profile(column: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive values from one column's profile before it reaches
    the LLM. Per architecture.md §7, PII masking is a deterministic floor an
    agent cannot bypass -- this function is that floor for this call site.

    Masks min_value/max_value/top_values when is_pii is set; null_percentage
    and distinct_count are statistics, not raw values, and pass through
    unmasked even for PII columns (matching architecture.md §7's
    ALLOW_STATS_ONLY tier -- stats are the one thing always shareable).
    """
    if not column.get("is_pii"):
        return column

    masked = dict(column)
    masked["min_value"] = _MASKED_PLACEHOLDER
    masked["max_value"] = _MASKED_PLACEHOLDER
    masked["top_values"] = [{"value": _MASKED_PLACEHOLDER, "count": None}]
    return masked


def build_claude_input(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_profiles: list[dict[str, Any]],
    template_rules: list[dict[str, Any]],
    row_count: int | None = None,
    existing_pending_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the input_json for recommend_rules_with_claude().

    existing_pending_rules: rules already in the approval queue (PENDING)
    for this table from a previous scan. Passed to Claude so it knows what's
    already waiting for human review and focuses on genuinely new suggestions
    rather than re-proposing the same concepts.
    """
    columns_payload = []
    for col in column_profiles:
        masked = _mask_column_profile(col)
        columns_payload.append(
            {
                "column_name": masked["column_name"],
                "data_type": masked["data_type"],
                "comment": masked.get("comment"),
                "null_percentage": masked.get("null_percentage"),
                "distinct_count": masked.get("distinct_count"),
                "min_value": masked.get("min_value"),
                "max_value": masked.get("max_value"),
                "top_values": masked.get("top_values"),
            }
        )

    template_rules_payload = [
        {
            "rule_name": r.get("rule_name"),
            "rule_type": r.get("rule_type"),
            "column_name": r.get("column_name"),
            "description": r.get("description"),
        }
        for r in template_rules
    ]

    pending_payload = [
        {
            "rule_name": r.get("rule_name"),
            "rule_type": r.get("rule_type"),
            "column_name": r.get("column_name"),
            "description": r.get("description"),
        }
        for r in (existing_pending_rules or [])
    ]

    payload = {
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "row_count": row_count,
        "columns": columns_payload,
        "template_rules_already_found": template_rules_payload,
    }
    if pending_payload:
        payload["rules_already_pending_human_review"] = pending_payload
    return payload


def recommend_rules_with_claude(input_json: dict[str, Any]) -> dict[str, Any]:
    """Call Claude (via Bedrock) to recommend business/domain DQ rules for
    one table, given the deterministic profile + already-found template
    rules.

    Output: {"rules": [...]} -- each item has rule_name, rule_type,
    description, severity, confidence, priority, reason, evidence,
    threshold_config, plus column_name (matching this codebase's existing
    rule-candidate convention of column_name: None for table-level rules --
    the ask's own example schema omits column_name, but every other rule
    producer in this codebase, e.g. skills/_shared.py, requires it, and the
    caller (a future hybrid rule_recommendation_agent) needs it to store a
    rule against the right column).

    Uses forced tool use for structured output (tool_choice pins the model
    to _SUBMIT_RULES_TOOL) rather than output_config.format -- verified
    directly that output_config.format 400s ("Extra inputs are not
    permitted") on this account's Bedrock InvokeModel path. `strict: true`
    on the tool definition also 400s here ("tools.0.custom.strict: Extra
    inputs are not permitted") -- also verified directly -- so this relies
    on tool_choice forcing + the JSON schema's required/enum/additionalProperties
    constraints alone, not a server-enforced strict-validation guarantee.
    """
    message = _get_client().messages.create(
        model=_MODEL_ID,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        tools=[_SUBMIT_RULES_TOOL],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": (
                    "Table profile and template rules already found:\n\n"
                    f"{json.dumps(input_json, default=_json_default)}"
                ),
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            result = block.input
            if isinstance(result, str):
                result = json.loads(result)
            return result

    raise RuntimeError(
        f"Claude did not return the expected {_TOOL_NAME} tool call; "
        f"stop_reason={message.stop_reason!r}"
    )


# ---------------------------------------------------------------------------
# Library-aware instance recommendation -- rules-architecture.md §5.4/§5.5.
# Parallel to recommend_rules_with_claude() above, not a replacement for it
# -- that function/its schema constants/its system prompt are untouched.
# This is Layer 1+2 aware: it sees the rule definition library and existing
# instances (running/pending/rejected) and proposes RECOMMENDED_INSTANCES
# candidates (existing definition or new) instead of flat column_name rules.
# ---------------------------------------------------------------------------

_INSTANCE_TOOL_NAME = "submit_recommended_instances"

_INSTANCE_NEW_DEFINITION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "category": {
            "type": "string",
            "enum": [
                "COMPLETENESS",
                "UNIQUENESS",
                "VALIDITY",
                "FRESHNESS",
                "VOLUME",
                "GOVERNANCE",
                "CUSTOM",
            ],
            "description": "Display/filter grouping label only -- never used for SQL dispatch.",
        },
        "description": {"type": "string"},
        "check_logic": {
            "type": "string",
            "description": "Prose description of what the check's SQL tests and why.",
        },
        "allowed_scopes": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["COLUMN", "MULTI_COLUMN", "TABLE", "CROSS_TABLE", "CONDITIONAL"],
            },
            "description": "Which instance scopes this definition can be applied with.",
        },
        "default_severity": {"type": "string", "enum": ["CRITICAL", "WARNING", "INFO"]},
        "draft_sql_template": {
            "type": ["string", "null"],
            "description": (
                "DRAFT ONLY -- a parameterized SQL template sketch for this new "
                "definition, never directly executable. Null if you cannot sketch "
                "one yet."
            ),
        },
    },
    "required": [
        "name",
        "category",
        "description",
        "check_logic",
        "allowed_scopes",
        "default_severity",
        "draft_sql_template",
    ],
    "additionalProperties": False,
}

_INSTANCE_TARGET_CONFIG_SCHEMA = {
    "type": "object",
    "description": (
        "Shape depends on this suggestion's `scope`: COLUMN -> "
        '{"column": str}; MULTI_COLUMN -> {"columns": [str, ...]}; '
        "TABLE -> {} (empty -- the table is identified by "
        "database_name/schema_name/table_name alone); CROSS_TABLE -> "
        '{"column": str, "ref_database": str, "ref_schema": str, '
        '"ref_table": str, "ref_column": str}; CONDITIONAL -> '
        '{"column": str, "when_column": str, "when_operator": str, '
        '"when_value": str}.'
    ),
}

_INSTANCE_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "definition_id": {
            "type": ["string", "null"],
            "description": (
                "ID of an existing ACTIVE definition from rule_definition_library "
                "this instance applies. Null if this instance is for a definition "
                "you are proposing new -- see new_definition_index instead."
            ),
        },
        "new_definition_index": {
            "type": ["integer", "null"],
            "description": (
                "Index into this response's new_definitions array (0-based). "
                "Mutually exclusive with definition_id -- exactly one of the two "
                "must be non-null."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["COLUMN", "MULTI_COLUMN", "TABLE", "CROSS_TABLE", "CONDITIONAL"],
        },
        "target_config": _INSTANCE_TARGET_CONFIG_SCHEMA,
        "threshold_config": {
            "type": "object",
            "description": (
                "Concrete, machine-checkable parameters for this instance (e.g. "
                '{"max_null_percentage": 5} or {"accepted_values": [...]}) -- not '
                "prose."
            ),
        },
        "severity": {"type": "string", "enum": ["CRITICAL", "WARNING", "INFO"]},
        "confidence": {
            "type": "number",
            "description": "0-1: how sure you are this instance is logically correct.",
        },
        "reason": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "draft_generated_sql": {
            "type": ["string", "null"],
            "description": (
                "DRAFT ONLY -- meaningful only when definition_id is null (a "
                "CUSTOM/new-definition instance). Must return exactly two columns, "
                "FAILED_COUNT and TOTAL_COUNT, using the fully-qualified table name "
                "database_name.schema_name.table_name. Must pass SQL Generation + "
                "Validation before it can ever become executable rule_sql -- never "
                "trusted directly. Null when using an existing definition's "
                "sql_template, which code renders deterministically instead, or when "
                "you genuinely cannot express the check as a single aggregate SELECT."
            ),
        },
        "suggested_group_name": {
            "type": ["string", "null"],
            "description": (
                "Shared name when this instance belongs with others applying the "
                "same definition across multiple columns/tables, so a human can "
                "review and act on them as one group. Null if this instance stands "
                "alone."
            ),
        },
    },
    "required": [
        "definition_id",
        "new_definition_index",
        "scope",
        "target_config",
        "threshold_config",
        "severity",
        "confidence",
        "reason",
        "evidence",
        "draft_generated_sql",
        "suggested_group_name",
    ],
    "additionalProperties": False,
}

_INSTANCE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "table_classification": _TABLE_TYPE_SCHEMA,
        "new_definitions": {"type": "array", "items": _INSTANCE_NEW_DEFINITION_SCHEMA},
        "instance_suggestions": {"type": "array", "items": _INSTANCE_SUGGESTION_SCHEMA},
    },
    "required": ["table_classification", "new_definitions", "instance_suggestions"],
    "additionalProperties": False,
}

_INSTANCE_SUBMIT_TOOL = {
    "name": _INSTANCE_TOOL_NAME,
    "description": "Submit the recommended rule instances (and any new rule definitions) for this table.",
    "input_schema": _INSTANCE_RESPONSE_SCHEMA,
}

_INSTANCE_SYSTEM_PROMPT = """You are the Rule Recommendation Agent in an \
agentic Data Quality platform for Snowflake, operating in library-aware mode \
(rules-architecture.md §5.4). You are given one table's metadata and column \
statistics, plus the full context of what already exists for this table: the \
rule definition library, what is already running, what is already pending \
human review, and what a human already rejected.

Before proposing anything, first classify what kind of table this is: fact \
(transactional records — orders, events, measurements), dimension (descriptive \
reference data — customers, products, employees), staging (raw/landing area \
for ingestion), config (small lookup/parameter tables), audit (change/activity \
logs), reference (code/type/status lookups), or unknown (genuinely ambiguous). \
Use the table name, column names, data types, row count, and any comment \
fields as signals. Provide a confidence score (0-1) and a single reasoning \
sentence.

Your job, in order:

1. Look at rule_definition_library first -- the existing ACTIVE definitions, \
ordered by how often they've been approved. For every definition that \
genuinely applies to this table's columns/shape and has no active or pending \
instance yet, propose an instance_suggestion referencing it by definition_id.
2. Identify groups: when the same definition applies to multiple columns or \
multiple tables, give those instance_suggestions the same suggested_group_name \
so a human can review and act on them as one unit.
3. Propose a new_definitions entry only when a check concept is genuinely \
absent from rule_definition_library -- never re-invent a definition that \
already covers the same concept, even if you would phrase it slightly \
differently. Reference a new definition from an instance_suggestion via \
new_definition_index (the position of your new_definitions entry in this \
response), never via definition_id, and leave definition_id null in that case.
4. Never propose an instance that duplicates something in \
existing_approved_instances (already running) or existing_pending_instances \
(already awaiting human review). Never re-propose a concept a human already \
rejected -- rejected_instances_with_reasons gives you the human's stated \
reason for each; use it to understand why and avoid proposing the same \
underlying concept again, not just the exact same instance.

If inferred_relationships is present and non-empty, use it to inform \
CROSS_TABLE instance_suggestions only where a genuine foreign-key \
relationship is inferable from the column/ref_table/ref_column pairing \
given -- do not invent cross-table relationships that aren't in that list.

If feedback_signals is present, use EDIT entries to seed threshold_config \
starting points and FALSE_POSITIVE entries as a signal to lower your \
confidence for that same definition and target -- this is a judgment aid for \
you, not a hard rule; code applies the real priority/suppression logic \
afterward regardless of what you return.

Numbers you're given (null percentages, distinct counts, min/max) are already \
computed deterministically -- trust them, don't recompute or second-guess \
them.

Values marked "***MASKED***" are sensitive (PII) and were withheld from you -- \
reason about that column using its name, data type, and null/distinct \
statistics only. Never invent or guess a masked value.

Every threshold_config must contain concrete, machine-checkable parameters \
(numbers, value lists, column names), not vague language. draft_generated_sql \
and draft_sql_template are always drafts, never directly executed -- see each \
field's own description for its exact SQL shape requirements.

If you have no genuinely new instance or definition to propose for this \
table, return empty new_definitions and instance_suggestions arrays rather \
than padding with something already covered."""


def build_instance_claude_input(
    database_name: str,
    schema_name: str,
    table_name: str,
    column_profiles: list[dict[str, Any]],
    row_count: int | None,
    rule_definition_library: list[dict[str, Any]],
    existing_approved_instances: list[dict[str, Any]],
    existing_pending_instances: list[dict[str, Any]],
    rejected_instances_with_reasons: list[dict[str, Any]],
    feedback_signals: list[dict[str, Any]] | None = None,
    inferred_relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the input_json for recommend_instances_with_claude() -- the
    library-aware counterpart to build_claude_input() (rules-architecture.md
    §5.4's "What the agent receives" table).

    column_profiles masking mirrors build_claude_input() exactly (same
    _mask_column_profile() floor, same columns_payload shape). rule_definition_
    library, existing_approved_instances, existing_pending_instances,
    rejected_instances_with_reasons, feedback_signals, and
    inferred_relationships are passed through as given -- ordering (e.g.
    rule_definition_library by approval_count desc) is the caller's
    responsibility; storage_tools.list_rule_definitions() already orders it
    that way.
    """
    columns_payload = []
    for col in column_profiles:
        masked = _mask_column_profile(col)
        columns_payload.append(
            {
                "column_name": masked["column_name"],
                "data_type": masked["data_type"],
                "comment": masked.get("comment"),
                "null_percentage": masked.get("null_percentage"),
                "distinct_count": masked.get("distinct_count"),
                "min_value": masked.get("min_value"),
                "max_value": masked.get("max_value"),
                "top_values": masked.get("top_values"),
            }
        )

    payload = {
        "database_name": database_name,
        "schema_name": schema_name,
        "table_name": table_name,
        "row_count": row_count,
        "columns": columns_payload,
        "rule_definition_library": rule_definition_library,
        "existing_approved_instances": existing_approved_instances,
        "existing_pending_instances": existing_pending_instances,
        "rejected_instances_with_reasons": rejected_instances_with_reasons,
    }
    if feedback_signals:
        payload["feedback_signals"] = feedback_signals
    if inferred_relationships:
        payload["inferred_relationships"] = inferred_relationships
    return payload


def recommend_instances_with_claude(input_json: dict[str, Any]) -> dict[str, Any]:
    """Call Claude (via Bedrock) for library-aware instance recommendation --
    rules-architecture.md §5.4. Given one table's profile plus the full
    definition-library/existing-instance/rejection context from
    build_instance_claude_input(), returns table_classification,
    new_definitions, and instance_suggestions (see _INSTANCE_RESPONSE_SCHEMA
    for the exact shape of each).

    Every draft_sql_template/draft_generated_sql this returns is DRAFT ONLY
    per §5.5's SQL trust chain -- callers must run it through SQL Generation
    + Validation before it can ever become rule_sql on a RULE_INSTANCES row.

    Same forced-tool-use pattern as recommend_rules_with_claude() -- see that
    function's docstring for why (output_config.format/strict both 400 on
    this account's Bedrock InvokeModel path). max_tokens raised to 8192 (vs
    4096 there) since this schema's per-item shape (target_config plus
    threshold_config plus a parallel new_definitions array) is larger.
    """
    message = _get_client().messages.create(
        model=_MODEL_ID,
        max_tokens=8192,
        system=_INSTANCE_SYSTEM_PROMPT,
        tools=[_INSTANCE_SUBMIT_TOOL],
        tool_choice={"type": "tool", "name": _INSTANCE_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": (
                    "Table profile, rule definition library, and existing instances:\n\n"
                    f"{json.dumps(input_json, default=_json_default)}"
                ),
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _INSTANCE_TOOL_NAME:
            result = block.input
            if isinstance(result, str):
                result = json.loads(result)
            return result

    raise RuntimeError(
        f"Claude did not return the expected {_INSTANCE_TOOL_NAME} tool call; "
        f"stop_reason={message.stop_reason!r}"
    )


# ---------------------------------------------------------------------------
# PII/Sensitivity classification -- LLM-assist tier for ambiguous columns
# (tools/pii_detection_tools.py is the deterministic first tier; only
# columns that tier can't confidently classify reach this function).
# architecture.md §7: "Column -> PII detector ... + LLM assist for
# ambiguous cases". One call per table (all its ambiguous columns batched
# together), not one call per column, to keep LLM call volume proportional
# to tables scanned, matching recommend_rules_with_claude()'s own "one call
# per profiled table" convention.
# ---------------------------------------------------------------------------

_PII_CLASSIFY_TOOL_NAME = "submit_pii_classifications"

_PII_CLASSIFICATION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "column_name": {"type": "string"},
        "is_pii": {"type": "boolean"},
        "pii_type": {
            "type": ["string", "null"],
            "enum": ["EMAIL", "PHONE", "PAN", "AADHAAR", "NAME", "ADDRESS", "FINANCIAL_ID", None],
            "description": "Null if is_pii is false.",
        },
        "sensitivity_level": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"],
            "description": (
                "HIGH for anything directly identifying (email, phone, "
                "financial id); MEDIUM for indirectly identifying (name, "
                "address); LOW for non-identifying columns."
            ),
        },
    },
    "required": ["column_name", "is_pii", "pii_type", "sensitivity_level"],
    "additionalProperties": False,
}

_PII_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {"type": "array", "items": _PII_CLASSIFICATION_ITEM_SCHEMA},
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

_PII_CLASSIFY_TOOL = {
    "name": _PII_CLASSIFY_TOOL_NAME,
    "description": "Submit PII/sensitivity classifications for the given ambiguous columns.",
    "input_schema": _PII_RESPONSE_SCHEMA,
}

_PII_CLASSIFY_SYSTEM_PROMPT = """You are the PII/Sensitivity Classification \
Agent in an agentic Data Quality platform for Snowflake. You are given a \
list of columns from one table -- name, data type, and a few sample values \
-- that a deterministic regex/heuristic pass could NOT confidently classify \
as PII or not-PII.

Classify each one: is it PII (personally identifiable information)? If so, \
what type (EMAIL/PHONE/PAN/AADHAAR/NAME/ADDRESS/FINANCIAL_ID), and how \
sensitive (HIGH = directly identifying like an email or financial id, \
MEDIUM = indirectly identifying like a name or address, LOW = not \
identifying)? If not PII, set is_pii=false, pii_type=null, and \
sensitivity_level="LOW".

Sample values may already be masked ("***MASKED***") if a column was \
already flagged as PII upstream -- for a masked column, classify based on \
its name and data type alone. When genuinely unsure, prefer the stricter \
(more sensitive) classification -- a false positive here costs a stats-only \
view; a false negative could leak real PII to a future LLM call."""


def classify_columns_with_claude(
    table_fqn: str, ambiguous_columns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ask Claude to classify columns the deterministic pass
    (tools/pii_detection_tools.py) left ambiguous. ambiguous_columns is a
    list of {column_name, data_type, top_values} dicts (a subset of a
    column_profiles list) -- only sample values, never full column data.

    Output: list of {column_name, is_pii, pii_type, sensitivity_level} --
    agents/pii_agent.py maps sensitivity_level to llm_sharing_policy itself
    (same fixed LOW/MEDIUM/HIGH -> ALLOW_*/ALLOW_*/ALLOW_STATS_ONLY mapping
    tools/pii_detection_tools.py uses), rather than trusting Claude to
    invent a policy string -- keeps the sensitivity->policy mapping in one
    deterministic place regardless of which classification tier produced
    the sensitivity_level.

    Same forced-tool-use pattern as recommend_rules_with_claude() -- see
    that function's docstring for why (output_config.format/strict both
    400 on this account's Bedrock path).
    """
    if not ambiguous_columns:
        return []

    payload = {
        "table": table_fqn,
        "columns": [
            {
                "column_name": c.get("column_name"),
                "data_type": c.get("data_type"),
                "sample_values": [tv.get("value") for tv in (c.get("top_values") or [])],
            }
            for c in ambiguous_columns
        ],
    }
    message = _get_client().messages.create(
        model=_MODEL_ID,
        max_tokens=2048,
        system=_PII_CLASSIFY_SYSTEM_PROMPT,
        tools=[_PII_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": _PII_CLASSIFY_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": f"Ambiguous columns:\n\n{json.dumps(payload, default=_json_default)}",
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _PII_CLASSIFY_TOOL_NAME:
            result = block.input
            if isinstance(result, str):
                result = json.loads(result)
            return result["classifications"]

    raise RuntimeError(
        f"Claude did not return the expected {_PII_CLASSIFY_TOOL_NAME} tool call; "
        f"stop_reason={message.stop_reason!r}"
    )


# ---------------------------------------------------------------------------
# Rule / Alert explanation -- "Add Better Claude Explanations"
#
# Golden rule (per the ask): Claude explains and recommends. Code validates
# and executes. Human approves. These two functions are explanation-only --
# neither one ever produces SQL, a threshold, or a severity; those are still
# 100% deterministic (skills/_shared.py, tools/rule_template_tools.py,
# tools/sql_validation_tools.py, agents/rule_execution_agent.py -- none of
# that is touched by this addition). If Claude is unavailable, callers fall
# back to a plain-Python templated sentence rather than leaving these fields
# blank or failing the request -- same "LLM failing must not fail the
# pipeline" convention as recommend_rules_with_claude()'s caller.
# ---------------------------------------------------------------------------

_EXPLANATION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "business_explanation": {
            "type": "string",
            "description": (
                "1-3 plain-language sentences a non-technical business "
                "stakeholder could read -- why this rule/alert matters, "
                "no SQL/column-internals jargon."
            ),
        },
        "business_impact": {
            "type": "string",
            "description": (
                "1-2 sentences: what breaks downstream (reporting, billing, "
                "compliance, customer-facing systems, etc.) if this rule's "
                "check keeps failing, in business terms."
            ),
        },
        "false_positive_risk": {
            "type": "string",
            "description": (
                "1-2 sentences: how likely this rule/alert is to fire on "
                "data that is actually fine (e.g. a legitimately sparse "
                "column, an expected seasonal dip, a known upstream quirk), "
                "and what would make it more or less trustworthy."
            ),
        },
    },
    "required": ["business_explanation", "business_impact", "false_positive_risk"],
    "additionalProperties": False,
}

_EXPLAIN_TOOL_NAME = "submit_explanation"

_EXPLAIN_TOOL = {
    "name": _EXPLAIN_TOOL_NAME,
    "description": "Submit the business-friendly explanation for this rule or alert.",
    "input_schema": _EXPLANATION_ITEM_SCHEMA,
}

_RULE_EXPLANATION_SYSTEM_PROMPT = """You are the Explanation Agent in an \
agentic Data Quality platform for Snowflake. You are given one recommended \
data quality rule -- its type, target table/column, description, severity, \
confidence, evidence, and the exact SQL check that will run (already \
generated and safety-validated by deterministic code; you are not asked to \
change it, and nothing you say can alter the SQL, the threshold, or whether \
it runs).

Your only job: write a short, business-friendly explanation of this rule for \
a non-technical stakeholder (a business admin, not a data engineer) --
why it matters, what breaks if it keeps failing, and how likely it is to be \
a false alarm rather than a real problem. Do not restate the SQL or column \
statistics verbatim; translate them into plain business language. Do not \
propose a different rule, threshold, or severity -- that is not your role \
here."""

_ALERT_EXPLANATION_SYSTEM_PROMPT = """You are the Explanation Agent in an \
agentic Data Quality platform for Snowflake. You are given one alert -- the \
rule that fired, the table/column it checks, its severity, and the actual \
failure counts from a real run that already happened (deterministic code \
already decided this failed; you are not asked to re-judge that).

Your only job: write a short, business-friendly explanation of this alert \
for a non-technical stakeholder -- why it fired, what it likely means for \
the business (bad data reaching a report, a broken pipeline, a real \
customer-facing issue, etc.), and how likely it is to be a false alarm \
(e.g. a known noisy rule, a small sample, an edge case) versus a genuine \
problem worth acting on. Do not change the alert's severity or status --
that is not your role here."""


def explain_rule_with_claude(rule: dict[str, Any]) -> dict[str, str]:
    """Ask Claude for a business-friendly explanation of one recommended
    rule. rule is the dict shape agents/rule_recommendation_agent.py /
    storage_tools.store_recommended_rule() already use (rule_name,
    rule_type, database_name/schema_name/table_name/column_name,
    description, severity, confidence, evidence, generated_sql, ...).

    Output: {"business_explanation": str, "business_impact": str,
    "false_positive_risk": str} -- text only. Never returns or implies SQL,
    a threshold, or a severity; those fields are not in the tool schema
    Claude is forced to call, so it structurally cannot smuggle a rule
    change back through this call.

    Same forced-tool-use pattern as recommend_rules_with_claude() (see that
    function's docstring for why: output_config.format/strict both 400 on
    this account's Bedrock path).
    """
    payload = {
        "rule_name": rule.get("rule_name"),
        "rule_type": rule.get("rule_type"),
        "database_name": rule.get("database_name"),
        "schema_name": rule.get("schema_name"),
        "table_name": rule.get("table_name"),
        "column_name": rule.get("column_name"),
        "description": rule.get("description"),
        "reason": rule.get("reason"),
        "evidence": rule.get("evidence"),
        "severity": rule.get("severity"),
        "confidence": rule.get("confidence"),
        "threshold_config": rule.get("threshold_config"),
        "generated_sql": rule.get("generated_sql"),
    }
    message = _get_client().messages.create(
        model=_MODEL_ID,
        max_tokens=1024,
        system=_RULE_EXPLANATION_SYSTEM_PROMPT,
        tools=[_EXPLAIN_TOOL],
        tool_choice={"type": "tool", "name": _EXPLAIN_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": f"Recommended rule:\n\n{json.dumps(payload, default=_json_default)}",
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _EXPLAIN_TOOL_NAME:
            result = block.input
            if isinstance(result, str):
                result = json.loads(result)
            return result

    raise RuntimeError(
        f"Claude did not return the expected {_EXPLAIN_TOOL_NAME} tool call; "
        f"stop_reason={message.stop_reason!r}"
    )


def explain_alert_with_claude(
    rule: dict[str, Any], execution_result: dict[str, Any]
) -> dict[str, str]:
    """Ask Claude for a business-friendly explanation of one alert, given
    the rule that fired and the real execution result that triggered it.

    rule is the dict shape storage_tools.get_approved_rule() returns.
    execution_result is the dict shape agents/rule_execution_agent._execute()
    produces (status, failed_count, total_count, failure_percentage,
    error_message) -- always a real, already-computed FAILED result by the
    time this is called (agents/alert_agent.py only calls this on FAILED).

    Output: same shape as explain_rule_with_claude() -- text only, never a
    severity/status change (store_alert()/update_alert_status() are the
    only things that ever set those, both unchanged by this addition).
    """
    payload = {
        "rule_name": rule.get("rule_name"),
        "rule_type": rule.get("rule_type"),
        "database_name": rule.get("database_name"),
        "schema_name": rule.get("schema_name"),
        "table_name": rule.get("table_name"),
        "column_name": rule.get("column_name"),
        "severity": rule.get("severity"),
        "failed_count": execution_result.get("failed_count"),
        "total_count": execution_result.get("total_count"),
        "failure_percentage": execution_result.get("failure_percentage"),
    }
    message = _get_client().messages.create(
        model=_MODEL_ID,
        max_tokens=1024,
        system=_ALERT_EXPLANATION_SYSTEM_PROMPT,
        tools=[_EXPLAIN_TOOL],
        tool_choice={"type": "tool", "name": _EXPLAIN_TOOL_NAME},
        messages=[
            {
                "role": "user",
                "content": f"Alert (rule failure):\n\n{json.dumps(payload, default=_json_default)}",
            }
        ],
    )

    for block in message.content:
        if block.type == "tool_use" and block.name == _EXPLAIN_TOOL_NAME:
            result = block.input
            if isinstance(result, str):
                result = json.loads(result)
            return result

    raise RuntimeError(
        f"Claude did not return the expected {_EXPLAIN_TOOL_NAME} tool call; "
        f"stop_reason={message.stop_reason!r}"
    )
