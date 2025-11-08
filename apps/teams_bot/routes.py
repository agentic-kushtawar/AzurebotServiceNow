# apps/teams_bot/routes.py
from fastapi import APIRouter, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity
from config.settings import settings
from .bot import TeamsBot
import json
import traceback
from botframework.connector.auth import MicrosoftAppCredentials

router = APIRouter()

# IMPORTANT: In single-tenant mode, set channel_auth_tenant to your AAD tenant ID
# Otherwise the SDK uses the 'botframework.com' tenant and you get AADSTS700016.
adapter_settings = BotFrameworkAdapterSettings(
    app_id=settings.BOT_MICROSOFT_APP_ID,
    app_password=settings.BOT_MICROSOFT_APP_PASSWORD,
    channel_auth_tenant=settings.BOT_MICROSOFT_APP_TENANT_ID or None,  # <-- fix
)

adapter = BotFrameworkAdapter(adapter_settings)
bot = TeamsBot()

@router.options("/api/messages")
async def preflight() -> Response:
    return Response(status_code=200)

@router.get("/api/messages")
def info():
    return {"hint": "POST a Bot Framework Activity JSON here. Use Azure Bot Service / Teams to deliver messages."}

@router.post("/api/messages")
async def messages(req: Request) -> Response:
    body = await req.body()
    activity = Activity().deserialize(json.loads(body.decode("utf-8")))
    print(f"DEBUG bot recipient.id from Teams: {activity.recipient and activity.recipient.id}")

    # Trust the Teams service URL for the duration of this conversation
    MicrosoftAppCredentials.trust_service_url(activity.service_url)

    res = Response(status_code=201)

    try:
        async def aux(turn_context: TurnContext):
            await bot.on_turn(turn_context)

        auth_header = req.headers.get("Authorization", "")
        await adapter.process_activity(activity, auth_header, aux)
        return res
    except Exception:
        traceback.print_exc()
        return Response(content="Adapter error", status_code=500, media_type="text/plain")
