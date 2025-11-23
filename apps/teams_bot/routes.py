import os
import json
import traceback
import httpx

from fastapi import APIRouter, Request, Response
from botbuilder.core import (
    BotFrameworkAdapterSettings,
    BotFrameworkAdapter,
    TurnContext,
    CardFactory,
)
from botbuilder.schema import Activity, HeroCard, CardAction, ActionTypes
from botframework.connector.auth import MicrosoftAppCredentials

from config.settings import settings
from .bot import TeamsBot

router = APIRouter()

# Fixed meeting URL
TEAMS_VOICE_MEETING_URL = os.getenv("TEAMS_VOICE_MEETING_URL", "")

# Sidecar URL
SIDECAR_BASE_URL = os.getenv("SIDECAR_BASE_URL", "http://localhost:5205")


adapter_settings = BotFrameworkAdapterSettings(
    app_id=settings.BOT_MICROSOFT_APP_ID,
    app_password=settings.BOT_MICROSOFT_APP_PASSWORD,
    channel_auth_tenant=settings.BOT_MICROSOFT_APP_TENANT_ID or None,
)

adapter = BotFrameworkAdapter(adapter_settings)
bot = TeamsBot()


@router.options("/api/messages")
async def preflight() -> Response:
    return Response(status_code=200)


@router.get("/api/messages")
def info():
    return {"status": "Bot is running"}


@router.post("/api/messages")
async def messages(req: Request) -> Response:
    body = await req.body()
    activity = Activity().deserialize(json.loads(body.decode("utf-8")))

    MicrosoftAppCredentials.trust_service_url(activity.service_url)

    res = Response(status_code=201)

    try:
        async def aux(turn: TurnContext):

            # -------------------------------------------------------
            # 🔹 1. User types "call me"
            # -------------------------------------------------------
            if (
                TEAMS_VOICE_MEETING_URL
                and turn.activity.type == "message"
                and turn.activity.text
            ):
                text = turn.activity.text.strip().lower()

                if text in {"call me", "/callme", "callme"}:
                    card = HeroCard(
                        title="Starting a voice session",
                        text="Click **Join call** to talk to ServiceBot.",
                        buttons=[
                            CardAction(
                                type=ActionTypes.open_url,
                                title="Join call",
                                value=TEAMS_VOICE_MEETING_URL,
                            )
                        ],
                    )

                    await turn.send_activity(
                        Activity(
                            type="message",
                            attachments=[CardFactory.hero_card(card)],
                        )
                    )

                    print("DEBUG: User requested call me → join card sent")
                    return

            # -------------------------------------------------------
            # 🔹 2. User clicks "Join call"
            #     In Teams this sends an invoke OR message
            # -------------------------------------------------------
            if turn.activity.type in {"invoke", "messageReaction"}:
                print("DEBUG: User clicked join call (invoke/messageReaction detected)")

                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            f"{SIDECAR_BASE_URL}/media/bot/join_fixed_meeting",
                            json={"meetingUrl": TEAMS_VOICE_MEETING_URL},
                            timeout=10,
                        )
                    print("DEBUG: Successfully notified Sidecar to join meeting")
                except Exception as e:
                    print(f"ERROR: Sidecar join call failed: {e}")

                return

            # -------------------------------------------------------
            # Default bot pipeline
            # -------------------------------------------------------
            await bot.on_turn(turn)

        await adapter.process_activity(
            activity, req.headers.get("Authorization", ""), aux
        )
        return res

    except Exception:
        traceback.print_exc()
        return Response("Adapter error", 500)
