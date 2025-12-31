# dev/dev_routes.py
from fastapi import APIRouter, Body, Query
from core.orchestrator import Orchestrator
from core.analytics.incident_intel import build_stats_query
from core.snow import get_snow
from config.settings import settings

router = APIRouter()
orc = Orchestrator()

@router.post("/dev/messages")
async def dev_messages(text: str = Body(..., embed=True), user: str = "local"):
    reply = await orc.handle(text, user={"id": user})
    return {"reply": reply}


@router.get("/dev/incident-intel-stats")
async def dev_incident_intel_stats(days: int = Query(30, ge=1, le=365)):
    active_only = bool(getattr(settings, "INCIDENT_INTEL_ACTIVE_ONLY", True))
    query_now = build_stats_query(days_start=days, active_only=active_only)
    query_prev = build_stats_query(days_start=days * 2, days_end=days, active_only=active_only)
    client = get_snow()
    current = await client.get_incident_stats(query=query_now, group_by="short_description")
    previous = await client.get_incident_stats(query=query_prev, group_by="short_description")
    return {
        "query_now": query_now,
        "query_prev": query_prev,
        "current": current,
        "previous": previous,
    }
