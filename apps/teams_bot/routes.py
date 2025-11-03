# apps/teams_bot/routes.py
from fastapi import APIRouter, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity
from config.settings import settings
from .bot import TeamsBot
import json
import traceback

router = APIRouter()

# Adapter with tenant-aware auth (single-tenant AAD)
adapter_settings = BotFrameworkAdapterSettings(
    app_id=settings.BOT_MICROSOFT_APP_ID,
    app_password=settings.BOT_MICROSOFT_APP_PASSWORD,
    channel_auth_tenant=getattr(settings, "BOT_MICROSOFT_APP_TENANT_ID", None),
)
adapter = BotFrameworkAdapter(adapter_settings)
bot = TeamsBot()

@router.options("/api/messages")
async def preflight() -> Response:
    # Allow Teams service to preflight (no CORS body needed for Bot Service,
    # but 200 avoids noisy 405 logs)
    return Response(status_code=200)

@router.get("/api/messages")
def info():
    return {"hint": "POST a Bot Framework Activity JSON here. Use Azure Bot Service / Teams to deliver messages."}

@router.post("/api/messages")
async def messages(req: Request) -> Response:
    body = await req.body()
    activity = Activity().deserialize(json.loads(body.decode("utf-8")))
    auth_header = req.headers.get("Authorization", "")
    res = Response(status_code=201)
    try:
        async def aux(turn_context: TurnContext):
            await bot.on_turn(turn_context)

        await adapter.process_activity(activity, auth_header, aux)
        return res
    except Exception:
        traceback.print_exc()
        return Response(content="Adapter error", status_code=500, media_type="text/plain")
