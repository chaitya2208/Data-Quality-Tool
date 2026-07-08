from fastapi import APIRouter, HTTPException
from app.services.snowflake_session import session as sf_session

router = APIRouter()


@router.get("/health")
def health_check():
    return {"status": "healthy"}


@router.get("/health/snowflake")
def snowflake_health():
    """
    Live connection check — actually pings Snowflake rather than trusting the
    startup cache, so the frontend badge reflects the real session state.
    Returns 200 with status=connected, or 200 with status=disconnected so the
    client can render the state without treating it as a request error.
    """
    try:
        result = sf_session.query(
            "SELECT CURRENT_USER() as u, CURRENT_ROLE() as r"
        )
        # Prefer live values; fall back to the cached context for user/role labels
        ctx = sf_session.get_cached_context() or {}
        return {
            "status": "connected",
            "user": (result[0].get("U") if result else None) or ctx.get("user"),
            "role": (result[0].get("R") if result else None) or ctx.get("current_role"),
        }
    except Exception as e:
        return {
            "status": "disconnected",
            "user": None,
            "role": None,
            "detail": str(e),
        }
