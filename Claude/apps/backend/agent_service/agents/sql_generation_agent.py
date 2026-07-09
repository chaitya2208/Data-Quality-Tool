"""SQL Generation Agent -- compiles a rule (structured JSON) into SQL.

architecture.md §4a lists Rule Recommendation and SQL Generation as two
separate pipeline steps ("Recommend -> SQL-gen -> SQL-validate"), even though
today's only rule source (the 5 deterministic skills) already attaches
generated_sql itself via rule_template_tools -- see rule_recommendation_agent.py's
docstring. This agent is what makes that a real, separate step rather than
a no-op: it fills in generated_sql for any rule that doesn't already have
one (template-first, via tools/rule_template_tools.render_sql_for_rule()),
and passes through rules that already carry SQL unchanged.

This split matters for what's coming next, not just what exists today:
architecture.md's SQL Generation agent is "template-first, LLM only when a
template can't express it" -- now that rule_recommendation_agent.py is
hybrid (template skills + Claude-suggested business/domain rules, see that
module's docstring), Claude's rules routinely carry a rule_type the template
dispatcher (render_sql_for_rule()) has never heard of -- e.g. ACCURACY,
CONSISTENCY, REFERENTIAL_INTEGRITY -- since those are Claude's own invented
categories, not one of the 5 skills' fixed set. render_sql_for_rule() raises
ValueError for any rule_type it doesn't recognize (verified directly); this
agent catches that and leaves generated_sql unset rather than crashing the
pipeline over a rule template dispatch was never going to handle.

This is the exact seam architecture.md flags for an LLM SQL-generation
fallback ("LLM only when template can't express it") -- not built here.
Per deferred-and-future-work.md, LLM-generated (non-template) SQL is a
separate, deliberately deferred piece: it needs to run through the SQL
Validator (tools/sql_validation_tools.py) just as hard as template SQL
does, and building that fallback is future work, not silently expanded into
this task. A Claude-sourced rule with no generated_sql simply reaches
sql_validation_agent.py with nothing to validate, and is correctly marked
INVALID there ("SQL is empty") -- visible to a human as "recommended, not
yet executable" rather than hidden or crashing the run.
"""

from __future__ import annotations

from typing import Any

from tools.rule_template_tools import render_sql_for_rule


def run_sql_generation_agent(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Ensure every rule in `rules` has a generated_sql where a template can
    produce one, filling in any that don't via the template dispatcher.

    Output: {"rules": [...]} -- same rule dicts. Rules that already carry
    SQL (the normal case for template-sourced rules, since every skill
    already calls rule_template_tools itself) pass through untouched --
    render_sql_for_rule() is only called for rules missing it, so a rule
    with intentionally hand-edited SQL (e.g. from a future approval-screen
    edit) is never silently overwritten. Rules whose rule_type has no
    template (Claude-sourced business/domain rules) get generated_sql=None,
    not a crash -- see module docstring.
    """
    generated = []
    for rule in rules:
        if not rule.get("generated_sql"):
            try:
                rule = {**rule, "generated_sql": render_sql_for_rule(rule)}
            except ValueError:
                rule = {**rule, "generated_sql": None}
        generated.append(rule)

    return {"rules": generated}
