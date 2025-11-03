from fastapi import APIRouter, Request, Response
from botbuilder.core import BotFrameworkAdapterSettings, BotFrameworkAdapter, TurnContext
from botbuilder.schema import Activity
from config.settings import settings  # this also sets os.environ for the SDK
from .bot import TeamsBot
import json, traceback

router = APIRouter()

adapter_settings = BotFrameworkAdapterSettings(
    app_id=settings.BOT_MICROSOFT_APP_ID,
    app_password=settings.BOT_MICROSOFT_APP_PASSWORD,
    # channel_auth_tenant is fine to pass too (not strictly required once envs are set)
    channel_auth_tenant=settings.BOT_MICROSOFT_APP_TENANT_ID or None,
)
adapter = BotFrameworkAdapter(adapter_settings)
bot = TeamsBot()

@router.get("/api/messages")
def info():
    return {"hint": "POST a Bot Framework Activity JSON here. Use the curl sample from the README."}

@router.post("/api/messages")
async def messages(req: Request) -> Response:
    body = await req.body()
    activity = Activity().deserialize(json.loads(body.decode("utf-8")))
    auth_header = req.headers.get("Authorization", "")
    try:
        async def aux(turn_context: TurnContext):
            await bot.on_turn(turn_context)
        await adapter.process_activity(activity, auth_header, aux)
        return Response(status_code=201)
    except Exception:
        traceback.print_exc()
        return Response("Adapter error", status_code=500, media_type="text/plain")
