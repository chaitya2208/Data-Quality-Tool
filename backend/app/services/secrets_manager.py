"""
AWS Secrets Manager client for RDS/Postgres connection passwords.

Postgres/RDS connection secrets are stored here, not in plaintext in the
Snowflake CONNECTIONS table — the table only keeps a pointer (the secret name).
Snowflake connections use SSO and have no real secret, so they never touch this.

Reuses the same AWS setup as the Bedrock client (see claude_client.py):
credentials come from the default credential chain (env / instance profile),
region from AWS_REGION (default us-east-2), TLS verification disabled for the
corporate proxy.

Fail-loud policy: put/get raise on any failure — we never silently fall back to
storing or reading a plaintext password.
"""
import os
import json
import logging
from functools import lru_cache

import boto3
import urllib3

logger = logging.getLogger(__name__)

# We call Secrets Manager with verify=False for corporate-proxy compatibility
# (same as the Bedrock client). Silence the resulting per-request
# InsecureRequestWarning so it doesn't flood the logs — this is a deliberate,
# known trade-off, not a warning worth repeating on every call.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Stored SECRET-column values starting with this prefix are Secrets Manager
# names (pointers); anything else is treated as a legacy plaintext password.
POINTER_PREFIX = os.environ.get("AWS_SECRETS_PREFIX", "data-quality/rds")


@lru_cache(maxsize=1)
def get_client():
    """Singleton Secrets Manager client — same credential/region/proxy setup as Bedrock."""
    region = os.environ.get("AWS_REGION", "us-east-2")
    logger.info(f"Initializing AWS Secrets Manager client in region: {region}")
    session = boto3.session.Session(region_name=region)
    # verify=False mirrors claude_client's DefaultHttpxClient(verify=False) for
    # corporate-proxy compatibility.
    return session.client("secretsmanager", verify=False)


def secret_name_for(connection_id: str) -> str:
    """Deterministic Secrets Manager name for a connection's password."""
    return f"{POINTER_PREFIX}/{connection_id}"


def is_pointer(value: str | None) -> bool:
    """True if a stored SECRET value is a Secrets Manager pointer (vs legacy plaintext)."""
    return bool(value) and value.startswith(f"{POINTER_PREFIX}/")


def put_secret(connection_id: str, password: str) -> str:
    """
    Store (create or update) a connection's password in Secrets Manager.
    Returns the secret NAME (the pointer to persist in CONNECTIONS.SECRET).
    Raises on failure — never falls back to plaintext.
    """
    client = get_client()
    name = secret_name_for(connection_id)
    payload = json.dumps({"password": password})
    try:
        client.create_secret(Name=name, SecretString=payload)
        logger.info(f"[SecretsManager] Created secret {name}")
    except client.exceptions.ResourceExistsException:
        client.put_secret_value(SecretId=name, SecretString=payload)
        logger.info(f"[SecretsManager] Updated secret {name}")
    return name


def get_secret(name_or_pointer: str) -> str:
    """
    Fetch the password from Secrets Manager given its name/pointer.
    Raises on failure — never falls back to treating the pointer as a password.
    """
    client = get_client()
    resp = client.get_secret_value(SecretId=name_or_pointer)
    raw = resp.get("SecretString")
    if not raw:
        raise ValueError(f"Secret {name_or_pointer} has no SecretString")
    try:
        return json.loads(raw)["password"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"Secret {name_or_pointer} is not in expected {{'password': ...}} form: {e}")


def delete_secret(name_or_pointer: str) -> None:
    """Best-effort delete (connection removal shouldn't fail if the secret is already gone)."""
    try:
        get_client().delete_secret(
            SecretId=name_or_pointer,
            ForceDeleteWithoutRecovery=True,
        )
        logger.info(f"[SecretsManager] Deleted secret {name_or_pointer}")
    except Exception as e:
        logger.warning(f"[SecretsManager] Could not delete secret {name_or_pointer}: {e}")
