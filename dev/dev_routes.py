# dev/dev_routes.py
from fastapi import APIRouter, Body
from core.orchestrator import Orchestrator

router = APIRouter()
orc = Orchestrator()

@router.post("/dev/messages")
async def dev_messages(text: str = Body(..., embed=True), user: str = "local"):
    reply = await orc.handle(text, user={"id": user})
    return {"reply": reply}
