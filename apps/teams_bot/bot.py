from __future__ import annotations
from typing import Any, Dict

from botbuilder.core import ActivityHandler, TurnContext, MessageFactory
from botbuilder.schema import SuggestedActions, CardAction, ActionTypes
from core.orchestrator.engine import Orchestrator

class TeamsBot(ActivityHandler):
    def __init__(self):
        super().__init__()
        self.orchestrator = Orchestrator()

    async def on_message_activity(self, turn_context: TurnContext):
        text = (turn_context.activity.text or "").strip()

        from_prop = getattr(turn_context.activity, "from_property", None)
        channel_data = getattr(turn_context.activity, "channel_data", {}) or {}
        user_email = ""
        try:
            cd_from = channel_data.get("from", {}) if isinstance(channel_data, dict) else {}
            user_email = (cd_from.get("userPrincipalName") or cd_from.get("email") or "").strip()
        except Exception:
            pass

        user_ctx: Dict[str, Any] = {
            "user_id": getattr(from_prop, "id", "") if from_prop else "",
            "user_name": getattr(from_prop, "name", "") if from_prop else "",
            "user_email": user_email,
            "channel_id": getattr(turn_context.activity, "channel_id", ""),
            "conversation_id": getattr(turn_context.activity.conversation, "id", ""),
        }

        result = await self.orchestrator.handle(text, user_ctx)

        # Special rendering for "propose_ticket" → show Yes/No buttons
        if isinstance(result, dict) and result.get("action") == "propose_ticket":
            reason = (result.get("reason") or "your issue").strip()
            tips = (result.get("tips") or "").strip()

            prompt = f"🛠 I can raise a ticket for: **{reason}**.\nWould you like me to create it?"
            if tips:
                prompt += f"\n\n{tips}"

            yes_value = f"create_ticket:{reason}"
            no_value  = "cancel_ticket"

            message = MessageFactory.text(prompt)
            message.suggested_actions = SuggestedActions(
                actions=[
                    CardAction(type=ActionTypes.im_back, title="✅ Create ticket", value=yes_value),
                    CardAction(type=ActionTypes.im_back, title="No thanks", value=no_value),
                ]
            )
            await turn_context.send_activity(message)
            return

        # Default rendering
        await turn_context.send_activity(MessageFactory.text(self._format_reply(result)))

    def _format_reply(self, result: Dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return "Sorry, I ran into an unexpected response."

        action = result.get("action")
        if action == "ticket_create":
            reason = (result.get("reason") or "your issue").strip()
            inc = (result.get("inc_number") or "").strip()
            tip = (result.get("tips") or "").strip()
            line1 = f"✅ Created ticket **{inc}** for: {reason}." if inc else f"✅ Created a ticket for: {reason}."
            return f"{line1}\n\n{tip}" if tip else line1

        if action == "ticket_status":
            inc = (result.get("inc_number") or "the incident").strip()
            state = (result.get("state") or "").strip()
            return f"ℹ️ Status for **{inc}**: {state}" if state else f"ℹ️ Status requested for **{inc}**."

        if action == "password_reset":
            text = (result.get("text") or "").strip()
            return f"🔒 Password reset steps:\n{text}" if text else "🔒 Let’s reset your password."

        if action == "help":
            return (result.get("text") or "Try: ‘raise a ticket’, ‘status of INC…’, or ‘reset my password’.").strip()

        if action == "legacy":
            original = (result.get("text") or "").strip()
            return f"🤖 (Legacy route) I received: “{original}”. You can say ‘raise a ticket’, ‘status of INC…’, or ‘reset my password’."

        return "Sorry, I couldn’t understand that. Try: ‘raise a ticket’, ‘status of INC0012345’, or ‘reset my password’."
