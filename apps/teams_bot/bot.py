from __future__ import annotations
from typing import Any, Dict
import re

from botbuilder.core import ActivityHandler, TurnContext, MessageFactory
from botbuilder.schema import SuggestedActions, CardAction, ActionTypes
from botframework.connector.auth import MicrosoftAppCredentials

from core.orchestrator.engine import Orchestrator
from core.telemetry.ids import current_run_id


class TeamsBot(ActivityHandler):
    def __init__(self):
        super().__init__()
        self.orchestrator = Orchestrator()

    # ---------- helpers ----------
    async def _send_help(self, turn_context: TurnContext) -> None:
        await turn_context.send_activity(
            MessageFactory.text("You can try:\n\n• “raise a ticket: ”\n• “status of INC0012345”\n• “reset my password”")
        )

    async def _propose_ticket_ui(self, turn_context: TurnContext, reason: str) -> None:
        reason = (reason or "your issue").strip()
        prompt = f"🛠 I can raise a ticket for: **{reason}**.\nWould you like me to create it?"
        msg = MessageFactory.text(prompt)
        msg.suggested_actions = SuggestedActions(
            actions=[
                CardAction(type=ActionTypes.im_back, title="✅ Create ticket", value=f"create_ticket:{reason}"),
                CardAction(type=ActionTypes.im_back, title="No thanks", value="cancel_ticket"),
            ]
        )
        await turn_context.send_activity(msg)

    def _format_reply(self, result: Dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return "Sorry, I hit an unexpected response."

        action = result.get("action", "")

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
            return ("🤖 (Legacy route) I received: “{}”. You can say ‘raise a ticket’, "
                    "‘status of INC…’, or ‘reset my password’.").format(original)

        return "Sorry, I couldn’t understand that. Try: ‘raise a ticket’, ‘status of INC0012345’, or ‘reset my password’."

    # ---------- main ----------
    async def on_message_activity(self, turn_context: TurnContext):
        # Trust Teams service URL for token reuse
        MicrosoftAppCredentials.trust_service_url(turn_context.activity.service_url)

        raw_text = (turn_context.activity.text or "").strip()
        text_lc = raw_text.lower()

        # minimal user context
        from_prop = getattr(turn_context.activity, "from_property", None)
        user_ctx: Dict[str, Any] = {
            "user_id": getattr(from_prop, "id", "") if from_prop else "",
            "user_name": getattr(from_prop, "name", "") if from_prop else "",
            "channel_id": getattr(turn_context.activity, "channel_id", ""),
            "conversation_id": getattr(turn_context.activity.conversation, "id", ""),
            "run_id": current_run_id(),
        }

        # 0) quick help
        if text_lc in {"help", "menu", "options"}:
            await self._send_help(turn_context)
            return

        # 1) deterministic “raise a ticket: …” → ALWAYS propose buttons here (no orchestrator dependency)
        if text_lc.startswith("raise a ticket"):
            reason = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
            await self._propose_ticket_ui(turn_context, reason)
            return

        # 2) button click → create_ticket:<reason>
        if text_lc.startswith("create_ticket:"):
            reason = raw_text.split(":", 1)[1].strip() if ":" in raw_text else ""
            # pass a hard hint to orchestrator
            user_ctx.update({"intent_hint": "create_ticket", "reason": reason})
            result = await self.orchestrator.handle(f"create ticket: {reason}", user_ctx)
            # if orchestrator still doesn’t create, provide a graceful fallback
            if not isinstance(result, dict) or result.get("action") != "ticket_create":
                await turn_context.send_activity(
                    MessageFactory.text("I wasn’t able to create the ticket automatically. "
                                        "Please try rephrasing or say ‘reset my password’ / ‘status of INC…’."))
                return
            await turn_context.send_activity(MessageFactory.text(self._format_reply(result)))
            return

        # 3) status of INC…
        if text_lc.startswith("status of"):
            m = re.search(r"\binc\d{6,}\b", text_lc)
            if m:
                inc = m.group(0).upper()
                user_ctx.update({"intent_hint": "ticket_status", "inc_number": inc})
                result = await self.orchestrator.handle(f"status of {inc}", user_ctx)
                await turn_context.send_activity(MessageFactory.text(self._format_reply(result)))
                return

        # 4) everything else → orchestrator
        result = await self.orchestrator.handle(raw_text, user_ctx)
        await turn_context.send_activity(MessageFactory.text(self._format_reply(result)))
