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

do you think we should remove all this template r just keep very few and let claude generate sql we laready havesql vlaidator agent anyway 
and yes we should definitely increase the cap or relaly make it adaptive and let it tell as much as it feels and wants  we need mrore novel rules .  improve the prompt this 5 max thing is defeinltely wrong 
remove self critique for novel proposals liek dont rmeove ocde comment it out 
do you think this template shape is althgether problmatic htan helpful
if claude is feeling to create new rule definiton it hsould create for referece see  the Claude folder in that we did not had this problem mostly i am not sure though 
we shoul not mkae iased towards reusing existing definition  it should be balance like use those deifnitrons if needed but dont hesitate to proposen ew onnes 
you are saying to bring 2 new tools null row sample and column stats we have sample tool shouldwe modify that so claude can like get what it wants gin general and for column stats profiler agent is giving already we can improve to give tail mid or some other values also 
right now i am not thinking of applying saved workflow on naother table 
fingerprint dedeup on re scan is perfect thats ot a problem 
ine more thing for monte carlo and all, we can have similair problem like in future when our rule library will become a lot huge then what will happen will our become slow as llm will have to see all rules 
see the findings iprovement i am not bale to see in frontend for sampel failed rows and such 
so the fixes you suggested i guess i ocvered most of them to let you know baout your tasks if something uncetain do ask me 
100% context used is shown shuld we continuew here 