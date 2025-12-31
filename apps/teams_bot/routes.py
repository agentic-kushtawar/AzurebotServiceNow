import os
import json
import traceback
import httpx
import re
import urllib.parse
import asyncio

from fastapi import APIRouter, Request, Response
from botbuilder.core import (
    BotFrameworkAdapterSettings,
    BotFrameworkAdapter,
    TurnContext,
    CardFactory,
    MessageFactory,
)
from botbuilder.schema import Activity, HeroCard, CardAction, ActionTypes
from botframework.connector.auth import MicrosoftAppCredentials

from config.settings import settings
from .bot import TeamsBot

router = APIRouter()

# Fixed meeting URL
TEAMS_VOICE_MEETING_URL = os.getenv("TEAMS_VOICE_MEETING_URL", "")

# Sidecar URL
SIDECAR_BASE_URL = os.getenv("SIDECAR_BASE_URL", "http://localhost:5205").rstrip("/")
# Optional call-reset endpoint (your Windows bot server)
CALL_RESET_URL = (
    os.getenv("CALL_RESET_URL") or f"{SIDECAR_BASE_URL}/calls"
).rstrip("/")
# Endpoint that makes the bot join the Teams meeting (POST joinURL=…)
CALL_JOIN_URL = (os.getenv("CALL_JOIN_URL") or f"{SIDECAR_BASE_URL}/Calls").rstrip("/")

def _extract_thread_id(url: str) -> str:
    if not url:
        return ""
    decoded = urllib.parse.unquote(url)
    match = re.search(r"19:[^/?]+@thread\.v2", decoded)
    return match.group(0) if match else ""

MEETING_THREAD_ID = _extract_thread_id(TEAMS_VOICE_MEETING_URL)
print("MEETING_THREAD_ID",MEETING_THREAD_ID)

async def _reset_call_thread() -> None:
    if not (CALL_RESET_URL and MEETING_THREAD_ID):
        print("[CALL][RESET] Skipped (CALL_RESET_URL or thread ID missing)")
        return
    target = f"{CALL_RESET_URL}?threadId={urllib.parse.quote(MEETING_THREAD_ID)}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(target, timeout=10)
        print(
            f"[CALL][RESET] thread={MEETING_THREAD_ID} status={resp.status_code} "
            f"body={resp.text[:200]}"
        )
    except Exception as exc:
        print(f"[CALL][RESET][ERROR] Failed to reset thread {MEETING_THREAD_ID}: {exc}")

async def _sidecar_join(reason: str = "manual", allow_retry: bool = True) -> None:
    if not CALL_JOIN_URL:
        print(f"[CALL][JOIN][{reason}] Skipped (CALL_JOIN_URL unset)")
        return
    if not TEAMS_VOICE_MEETING_URL:
        print(f"[CALL][JOIN][{reason}] Skipped (meeting URL missing)")
        return
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                CALL_JOIN_URL,
                json={"joinURL": TEAMS_VOICE_MEETING_URL},
                timeout=10,
            )
        print(
            f"[CALL][JOIN][{reason}] status={resp.status_code} via {CALL_JOIN_URL} "
            f"body={resp.text[:200]}"
        )
        if resp.status_code >= 500 and allow_retry:
            await asyncio.sleep(2.0)
            await _reset_call_thread()
            await asyncio.sleep(2.0)
            await _sidecar_join(reason=f"{reason}_retry", allow_retry=False)
    except Exception as exc:
        print(f"[CALL][JOIN][ERROR] sidecar join failed ({reason}): {exc}")


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
                    print("inside callme trigger")
                    await turn.send_activity(
                        MessageFactory.text("Generating meeting event… hang tight.")
                    )
                    await _reset_call_thread()
                    await asyncio.sleep(1.0)
                    await _sidecar_join(reason="callme_auto")

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

                    print("[CALL][CARD] Sent join-card for meeting")
                    return

            # -------------------------------------------------------
            # 🔹 2. User clicks "Join call"
            #     In Teams this sends an invoke OR message
            # -------------------------------------------------------
            if turn.activity.type in {"invoke", "messageReaction"}:
                print(
                    f"[CALL][JOIN] invoke={turn.activity.type} "
                    f"name={getattr(turn.activity, 'name', None)} "
                    f"value={getattr(turn.activity, 'value', None)}"
                )

                await _sidecar_join(reason="button")

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
