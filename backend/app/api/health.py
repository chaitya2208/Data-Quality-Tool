from fastapi import APIRouter, HTTPException
from app.services.snowflake_session import session as sf_session

router = APIRouter()


@router.get("/health")
def health_check():
    return {"status": "healthy"}


@router.get("/health/snowflake")
def snowflake_health():
    """Returns cached connection status — no new SSO."""
    ctx = sf_session.get_cached_context()
    if ctx:
        return {
            "status": "connected",
            "user": ctx["user"],
            "role": ctx["current_role"],
        }
    # Context not ready yet — try a quick ping
    try:
        result = sf_session.query(
            "SELECT CURRENT_USER() as u, CURRENT_ROLE() as r"
        )
        return {
            "status": "connected",
            "user": result[0].get("U") if result else None,
            "role": result[0].get("R") if result else None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Snowflake not connected: {str(e)}")
