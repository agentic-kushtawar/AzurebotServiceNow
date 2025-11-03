# apps/teams_bot/bot.py
from botbuilder.core import ActivityHandler, TurnContext
from core.orchestrator import Orchestrator

class TeamsBot(ActivityHandler):
    def __init__(self):
        self.orchestrator = Orchestrator()

    async def on_message_activity(self, turn_context: TurnContext):
        user = turn_context.activity.from_property
        text = (turn_context.activity.text or "").strip()
        reply = await self.orchestrator.handle(text, user=user)
        await turn_context.send_activity(reply)

    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        await turn_context.send_activity("👋 Hello! Say **reset my password** or **VPN help**.")
