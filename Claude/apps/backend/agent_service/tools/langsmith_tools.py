"""LangSmith tracing setup -- observability for the LangGraph workflow
(graphs/dq_workflow_graph.py) and the Claude/Bedrock call
(tools/claude_tools.py). Purely additive: nothing here changes what any
agent computes, validates, or stores; a failure to trace must never fail a
scan (same "must not fail the pipeline" convention this codebase applies
to the Claude call itself and to log_agent_run()).

TLS note (same root cause as tools/claude_tools.py and the Snowflake S3
gap in deferred-and-future-work.md #21, third time this network's
corporate-proxy TLS interception has needed a scoped fix in this
codebase): LangSmith's `Client` uses `requests`, not `httpx`, so
`claude_tools.py`'s `httpx.Client(verify=truststore.SSLContext(...))`
pattern doesn't apply directly. Verified directly against the real
LangSmith API: `Client(session=...)` does NOT work -- `Client.__init__`
mounts its own `_LangSmithHttpAdapter` on `https://` *after* accepting a
caller-supplied session, silently overwriting any adapter passed in. The
fix that verified successfully is to remount a truststore-backed
`HTTPAdapter` on `client.session` *after* constructing the `Client` (so it
overwrites LangSmith's own adapter, not the other way around) --
confirmed with a real trace round-tripped through `client.list_runs()`
against the real API, not just "no exception raised."

Deliberately NOT `truststore.inject_into_ssl()` -- same reason as
claude_tools.py: that patches the global `ssl` module process-wide and
previously broke the Snowflake connector's own TLS handshake the moment a
module using it was imported. Scoping the fix to one requests.Session
avoids that.
"""

from __future__ import annotations

import os
import ssl

# pyrefly: ignore [missing-import]
import truststore
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter

load_dotenv()


class _TruststoreHTTPAdapter(HTTPAdapter):
    """requests HTTPAdapter that verifies against the OS trust store (which
    trusts this network's corporate-proxy root CA) instead of certifi.
    """

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        return super().init_poolmanager(*args, **kwargs)


_configured = False


def configure_langsmith_tracing() -> None:
    """Set up a process-wide LangSmith client with the TLS fix applied, and
    register it as the global client langchain-core's tracer (used
    automatically by every graph.invoke() call) and any client-less
    @traceable call both read from (langsmith.run_trees.configure()).

    Idempotent and safe to call multiple times (e.g. once from main.py at
    startup, once from a standalone script) -- only does real work once.
    No-ops entirely if LANGSMITH_API_KEY isn't set, so tracing is opt-in via
    .env/.env.example, not a hard dependency for running the app.
    """
    global _configured
    if _configured:
        return
    _configured = True

    if not os.getenv("LANGSMITH_API_KEY"):
        return

    # pyrefly: ignore [missing-import]
    from langsmith import Client
    from langsmith.run_trees import configure

    client = Client()
    # Must happen AFTER Client() construction -- see module docstring.
    client.session.mount("https://", _TruststoreHTTPAdapter())
    configure(client=client)


def get_traced_client_for_anthropic(anthropic_client):
    """Wrap an Anthropic-SDK-shaped client (AnthropicBedrock included) with
    langsmith.wrappers.wrap_anthropic() for full input/output/token
    visibility on every Claude call, using the same TLS-fixed global client
    configure_langsmith_tracing() sets up.

    Returns the client unchanged if LANGSMITH_API_KEY isn't set -- tracing
    is additive, never required for claude_tools.py to function.
    """
    if not os.getenv("LANGSMITH_API_KEY"):
        return anthropic_client

    configure_langsmith_tracing()
    # pyrefly: ignore [missing-import]
    from langsmith.wrappers import wrap_anthropic

    return wrap_anthropic(anthropic_client)
