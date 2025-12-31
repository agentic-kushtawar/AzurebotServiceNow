from __future__ import annotations
from typing import Any, Dict
from datetime import datetime, timezone
import logging
import re
import asyncio

import httpx

from botbuilder.core import ActivityHandler, TurnContext, MessageFactory, CardFactory
from botbuilder.schema import Activity, SuggestedActions, CardAction, ActionTypes

# Orchestrator (kept as-is)
from core.orchestrator.engine import Orchestrator
from core.orchestrator.state import session_for

# i18n
from core.i18n.adapter import detect, translate
from core.i18n.lang_store import get_user_lang, set_user_lang
from core.i18n.policy import enabled_locales, label_for
from core.i18n.strings import t
from config.settings import settings
from core.voice.sop_validation import handle_sop_upload

log = logging.getLogger("app")

LANG_LABEL_KEYS = {
    "en": "english",
    "es": "spanish",
    "de": "german",
    "he": "hebrew",
}


class TeamsBot(ActivityHandler):
    """
    Teams chat bot façade.
    - Translates inbound to EN (reasoning pivot), outbound back to user's locale.
    - Localizes fixed UI strings via prompts/ui/*.json through t(key, locale).
    - Delegates intent handling to core.orchestrator.engine.Orchestrator.
    """

    def __init__(self) -> None:
        super().__init__()
        self.orchestrator = Orchestrator()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _user_from_context(self, turn_context: TurnContext) -> Dict[str, Any]:
        a = turn_context.activity
        user_id = (a.from_property and a.from_property.id) or ""
        user_name = (a.from_property and a.from_property.name) or ""
        user_email = ""
        conversation_id = (a.conversation and a.conversation.id) or ""
        teams_locale = (a.locale or "en").split("-")[0].lower()
        # prefer saved preference; else Teams locale; else detect on the fly
        saved = get_user_lang(user_id)
        resolved = saved or teams_locale
        return {
            "user_id": user_id,
            "user_name": user_name,
            "user_email": user_email,
            "conversation_id": conversation_id,
            "locale": resolved or "en",
            "channel": a.channel_id or "msteams",
        }

    def _language_label(self, locale: str, viewer_lang: str) -> str:
        key = LANG_LABEL_KEYS.get(locale)
        if key:
            return t(key, viewer_lang)
        return label_for(locale)

    def _language_suggested_actions(self, current_lang: str) -> SuggestedActions:
        """
        Show a non-action chip for current locale + real actions for the other enabled locales.
        Labels are localized to the viewer's current language.
        """
        chip_label = self._language_label(current_lang, current_lang)
        actions = [CardAction(type=ActionTypes.im_back, title=f"🌐 {chip_label}", value="noop_lang")]
        for code in enabled_locales().keys():
            if code == current_lang:
                continue
            label = self._language_label(code, current_lang)
            actions.append(CardAction(type=ActionTypes.im_back, title=label, value=f"/language {code}"))
        return SuggestedActions(actions=actions)

    def _remember_reply(self, conversation_id: str, text: str) -> None:
        if not text:
            return
        session_for({"conversation_id": conversation_id})["last_chat_reply"] = text

    def _extract_attachment_info(self, attachments: list) -> dict[str, str] | None:
        for attachment in attachments or []:
            if attachment.content_type == "application/vnd.microsoft.teams.file.download.info":
                content = attachment.content or {}
                download_url = content.get("downloadUrl") or ""
                file_name = attachment.name or content.get("fileName") or "document"
                if download_url:
                    return {"download_url": download_url, "file_name": file_name}
            if attachment.content_url:
                return {"download_url": attachment.content_url, "file_name": attachment.name or "document"}
        return None

    def _is_yes(self, text_lc: str) -> bool:
        return text_lc in {"yes", "y", "confirm", "ok", "sure", "please do"}

    def _is_no(self, text_lc: str) -> bool:
        return text_lc in {"no", "n", "cancel", "discard"}

    async def _safe_send_typing(self, turn_context: TurnContext) -> None:
        try:
            await asyncio.wait_for(
                turn_context.send_activity(Activity(type="typing")),
                timeout=2.0,
            )
        except Exception:
            # Typing indicator failures shouldn't block the request.
            return

    async def _send_help(self, turn_context, user_lang, conversation_id: str):
        help_en = (
            "I can chat in English, Spanish, or German (default is English).\n"
            "You can try:\n"
            "- “raise a ticket: <reason>”\n"
            "- “status of INC0012345”\n"
            "- “reset my password”\n"
            "- “vpn not connecting”"
        )
        help_text = help_en if user_lang == "en" else translate(help_en, "en", user_lang, banner=True)
        text_out = f"{help_text}\n{t('switch_hint', user_lang)}"
        msg = MessageFactory.text(text_out)
        msg.suggested_actions = self._language_suggested_actions(user_lang)
        await turn_context.send_activity(msg)
        self._remember_reply(conversation_id, text_out)

    async def _send_greeting(self, turn_context, user_lang, conversation_id: str):
        bot_name = settings.BOT_PERSONA_NAME or "Vox AI Service"
        bot_role = settings.BOT_PERSONA_ROLE or "your virtual Service Desk assistant"
        greet_en = (
            f"Hi, I'm {bot_name}, {bot_role}. "
            "I can triage ServiceNow tickets, check incident status, reset passwords, and troubleshoot VPN issues. "
            "I can speak English, Spanish, or German."
        )
        greet_text = greet_en if user_lang == "en" else translate(greet_en, "en", user_lang, banner=True)
        text_out = f"{greet_text}\n{t('switch_hint', user_lang)}"
        msg = MessageFactory.text(text_out)
        msg.suggested_actions = self._language_suggested_actions(user_lang)
        await turn_context.send_activity(msg)
        self._remember_reply(conversation_id, text_out)

    async def _propose_ticket_ui(
        self, turn_context: TurnContext, reason_en: str, tips_en: str, user_lang: str, conversation_id: str
    ) -> None:
        """
        Localize proposal UI using t(...), translate only dynamic content (reason/tips).
        """
        # Fixed UI text from locale files
        title = t("propose_title", user_lang)
        question = t("propose_question", user_lang)

        # Dynamic parts (translate without banner)
        reason_disp = reason_en or "your issue"
        tips_disp = tips_en or ""
        if user_lang != "en":
            reason_disp = translate(reason_disp, "en", user_lang, banner=False)
            if tips_disp:
                tips_disp = translate(tips_disp, "en", user_lang, banner=False)

        prompt = f"{title} **{reason_disp}**.\n{question}"
        if tips_disp:
            prompt += f"\n\n{tips_disp}"

        # Buttons (labels from locale files; values stay EN for routing simplicity)
        btn_yes = t("btn_create", user_lang)
        btn_no = t("btn_cancel", user_lang)

        msg = MessageFactory.text(prompt)
        msg.suggested_actions = self._language_suggested_actions(user_lang)
        msg.suggested_actions.actions.insert(
            0, CardAction(type=ActionTypes.im_back, title=btn_yes, value=f"create_ticket:{reason_en}")
        )
        msg.suggested_actions.actions.insert(
            1, CardAction(type=ActionTypes.im_back, title=btn_no, value="cancel_ticket")
        )
        await turn_context.send_activity(msg)
        self._remember_reply(conversation_id, prompt)

    def _format_reply_text(self, result: Dict[str, Any]) -> str:
        """
        Compose the English reply text from orchestrator result.
        We translate the whole message later (one-shot) for UI cleanliness.
        """
        if not isinstance(result, dict):
            return "Sorry, I hit an unexpected response."

        action = (result.get("action") or "").strip().lower()
       

        if action == "ticket_create":
            reason = (result.get("reason") or "your issue").strip()
            inc = (result.get("inc_number") or "").strip()
            tip = (result.get("tips") or "").strip()
            line1 = f"✅ Created ticket **{inc}** for: {reason}." if inc else f"✅ Created a ticket for: {reason}."
            return f"{line1}\n\n{tip}" if tip else line1

        if action == "ticket_status":
            inc = (result.get("inc_number") or "the incident").strip()
            state = (result.get("state") or "").strip()
            extra = (result.get("short_description") or "").strip()
            if state and extra:
                return f"ℹ️ Status for **{inc}**: {state}\n{extra}"
            return f"ℹ️ Status for **{inc}**: {state or 'requested'}"

        if action == "ticket_update_status":
            inc = (result.get("inc_number") or "the incident").strip()
            status = (result.get("status") or "").strip() or "Updated"
            reason = (result.get("reason") or "").strip()
            line = f"✅ Updated **{inc}** to **{status}**."
            return f"{line}\nReason: {reason}" if reason else line

        if action == "password_reset":
            text = (result.get("text") or "").strip()
            return f"🔒 Password reset steps:\n{text}" if text else "🔒 Let’s reset your password."

        if action == "help":
            return (result.get("text") or "Try: ‘raise a ticket’, ‘status of INC…’, or ‘reset my password’.").strip()

        if action == "bot_profile":
            return (result.get("text") or "Hi, I'm your virtual Service Desk assistant.").strip()

        if action == "direct_reply":
            return (result.get("text") or "Here to help from centralus.").strip()

        if action == "ticket_howto":
            return (result.get("text") or "Say “raise a ticket: <reason>” and I’ll get it started.").strip()

        if action == "legacy":
            original = (result.get("text") or "").strip()
            return (
                "🤖 (Legacy route) I received: “{}”. You can say ‘raise a ticket’, "
                "‘status of INC…’, or ‘reset my password’."
            ).format(original)

        if action == "propose_ticket":
            reason = (result.get("reason") or "an issue").strip()
            return f"I can open a ticket for **{reason}**. Do you want me to create it?"

        return "I didn’t quite catch that. Try ‘help’."

    # -------------------------------------------------------------------------
    # Activity flow
    # -------------------------------------------------------------------------

    async def on_turn(self, turn_context: TurnContext):
        if turn_context.activity.type == "message":
            return await self.on_message_activity(turn_context)
        return

    async def on_message_activity(self, turn_context: TurnContext):
        raw_text = (turn_context.activity.text or "").strip()
        text_lc = raw_text.lower()
        user = self._user_from_context(turn_context)
        user_id = user.get("user_id") or ""
        conversation_id = (turn_context.activity.conversation and turn_context.activity.conversation.id) or user_id
        preferred = get_user_lang(user_id)  # 'en' or saved locale
        teams_hint = (turn_context.activity.locale or "").lower()

        # Resolve locale: saved -> teams hint -> detect
        user_lang = preferred or detect(raw_text, hint=teams_hint) or "en"
        await self._safe_send_typing(turn_context)

        # Language switching UX (config-driven)
        if text_lc in {"change language", "language", "idioma", "cambiar idioma"} or text_lc == "noop_lang":
            locs = enabled_locales()
            msg = MessageFactory.text(
                t("current_language_choose", user_lang).format(lang=self._language_label(user_lang, user_lang))
            )
            actions = []
            for code in locs.keys():
                if code == user_lang:
                    continue
                actions.append(
                    CardAction(
                        type=ActionTypes.im_back,
                        title=self._language_label(code, user_lang),
                        value=f"/language {code}",
                    )
                )
            msg.suggested_actions = SuggestedActions(actions=actions)
            await turn_context.send_activity(msg)
            return

        if text_lc.startswith("/language"):
            m = re.search(r"/language\s+([a-z]{2})", text_lc)
            if m:
                target = m.group(1)
                if target in enabled_locales().keys():
                    set_user_lang(user_id, target)
                    user_lang = target  # switch immediately

                    # localized confirmation
                    lang_label = label_for(target)  # e.g., "Español"
                    confirm = t("language_set", user_lang).format(lang=lang_label)
                    await turn_context.send_activity(MessageFactory.text(confirm))

                    cont = t("continue_below", user_lang)
                    msg = MessageFactory.text(cont)
                    msg.suggested_actions = self._language_suggested_actions(user_lang)
                    await turn_context.send_activity(msg)
                else:
                    # unsupported message localized to user's current language (before switching)
                    await turn_context.send_activity(
                        MessageFactory.text(
                            translate("Unsupported language.", "en", user_lang, banner=False)
                        )
                    )
            else:
                await turn_context.send_activity(
                    MessageFactory.text(
                        translate("Use `/language en` or `/language es`.", "en", user_lang, banner=False)
                    )
                )
            return

        session = session_for({"conversation_id": conversation_id})
        pending_sop = session.get("pending_sop")
        if pending_sop and self._is_yes(text_lc):
            await turn_context.send_activity(Activity(type="typing"))
            try:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                    resp = await client.get(pending_sop["download_url"])
                    resp.raise_for_status()
                upload = await handle_sop_upload(
                    filename=pending_sop.get("file_name", "sop_document"),
                    data=resp.content,
                    user=user.get("user_email") or user.get("user_name") or user_id,
                    timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                )
                if upload.ok and upload.error == "sop_text_unreadable":
                    msg_text = (
                        "I saved the SOP file, but the text wasn’t readable for validation. "
                        "Please upload a text-based PDF or a .txt file."
                    )
                elif upload.ok and upload.error:
                    msg_text = (
                        "I saved the SOP file, but couldn’t generate a valid SOP JSON from it. "
                        "Please upload a clearer text-based SOP."
                    )
                elif upload.ok:
                    msg_text = (
                        f"SOP saved. I'll use it for validation.\n"
                        f"SOP ID: {upload.sop_id or 'SOP'}"
                    )
                else:
                    msg_text = "I couldn't process that SOP document. Please try again with a text or PDF file."
                await turn_context.send_activity(MessageFactory.text(msg_text))
            except Exception:
                log.exception("SOP upload failed")
                await turn_context.send_activity(
                    MessageFactory.text("I couldn't download that file. Please re-upload and try again.")
                )
            session.pop("pending_sop", None)
            return
        if pending_sop and self._is_no(text_lc):
            session.pop("pending_sop", None)
            await turn_context.send_activity(MessageFactory.text("Okay, I won't save that document as an SOP."))
            return
        attachments = turn_context.activity.attachments or []
        if attachments:
            info = self._extract_attachment_info(attachments)
            if info:
                session["last_attachment"] = info
                if session.get("awaiting_sop_upload"):
                    session["pending_sop"] = info
                    session.pop("awaiting_sop_upload", None)
                    prompt = f"I found `{info['file_name']}`. Save this as the SOP for validation?"
                    msg = MessageFactory.text(prompt)
                    msg.suggested_actions = SuggestedActions(
                        actions=[
                            CardAction(type=ActionTypes.im_back, title="Yes", value="yes"),
                            CardAction(type=ActionTypes.im_back, title="No", value="no"),
                        ]
                    )
                    await turn_context.send_activity(msg)
                    return

        # ---------------------------------------------------------------------
        # Translate inbound -> EN (pivot), then orchestrate
        # ---------------------------------------------------------------------
        inbound_for_llm = raw_text if user_lang == "en" else translate(raw_text, user_lang, "en", banner=False)

        try:
            if any(k in text_lc for k in ("recurring", "repeated", "trend", "trends", "problems to focus")):
                await self._safe_send_typing(turn_context)
            if any(
                k in text_lc
                for k in (
                    "intune",
                    "compliance",
                    "check-in",
                    "check in",
                    "managed devices",
                    "device status",
                )
            ):
                await turn_context.send_activity(MessageFactory.text(t("hold_intune", user_lang)))
            result = await self.orchestrator.handle(text=inbound_for_llm, user=user)
        except Exception:
            log.exception("Orchestrator error")
            err_text_en = "Sorry, I ran into a problem handling that request."
            err_text = err_text_en if user_lang == "en" else translate(err_text_en, "en", user_lang, banner=True)
            await turn_context.send_activity(MessageFactory.text(err_text))
            msg = MessageFactory.text(
                "Try again or switch language." if user_lang == "en" else translate("Try again or switch language.", "en", user_lang, banner=True)
            )
            msg.suggested_actions = self._language_suggested_actions(user_lang)
            await turn_context.send_activity(msg)
            return

        action = (result or {}).get("action", "")
        processing_hint = (result or {}).get("processing_hint") if isinstance(result, dict) else ""
        if processing_hint and (result or {}).get("long_running"):
            hint_text = t("hold_snow", user_lang)
            await turn_context.send_activity(MessageFactory.text(hint_text))
        if action == "repeat_last":
            last = session_for({"conversation_id": conversation_id}).get("last_chat_reply")
            if last:
                msg = MessageFactory.text(last)
            else:
                fallback_en = "I don't have anything to repeat yet."
                fallback = fallback_en if user_lang == "en" else translate(fallback_en, "en", user_lang, banner=True)
                msg = MessageFactory.text(fallback)
            msg.suggested_actions = self._language_suggested_actions(user_lang)
            await turn_context.send_activity(msg)
            return

        if action == "help":
            await self._send_help(turn_context, user_lang, conversation_id)
            return
        if action == "greeting":
            await self._send_greeting(turn_context, user_lang, conversation_id)
            return
        if action == "sop_upload_prompt":
            last_attachment = session_for({"conversation_id": conversation_id}).get("last_attachment")
            if last_attachment:
                session = session_for({"conversation_id": conversation_id})
                session["pending_sop"] = last_attachment
                prompt = f"I found `{last_attachment['file_name']}`. Save this as the SOP for validation?"
                msg = MessageFactory.text(prompt)
                msg.suggested_actions = SuggestedActions(
                    actions=[
                        CardAction(type=ActionTypes.im_back, title="Yes", value="yes"),
                        CardAction(type=ActionTypes.im_back, title="No", value="no"),
                    ]
                )
                await turn_context.send_activity(msg)
            else:
                session_for({"conversation_id": conversation_id})["awaiting_sop_upload"] = True
                await turn_context.send_activity(
                    MessageFactory.text("Please attach the SOP document (PDF or text), then say “this is SOP.”")
                )
            return

        # If engine proposes a ticket, localize the proposal UI
        if action == "propose_ticket":
            await self._propose_ticket_ui(
                turn_context,
                reason_en=(result.get("reason") or ""),
                tips_en=(result.get("tips") or ""),
                user_lang=user_lang,
                conversation_id=conversation_id,
            )
            return

        if action == "bot_profile":
            msg = MessageFactory.text(t("capabilities_overview", user_lang))
            msg.suggested_actions = self._language_suggested_actions(user_lang)
            await turn_context.send_activity(msg)
            self._remember_reply(conversation_id, msg.text)
            return

        if (result or {}).get("source") == "intune" and action == "direct_reply":
            result = dict(result)
            result["text"] = f"{t('intune_source', user_lang)}\n{result.get('text', '')}"

        if action == "incident_intel":
            insights = (result or {}).get("insights") or []
            dashboard_url = (result or {}).get("dashboard_url") or ""
            header = t("incident_intel_title", user_lang)
            subtitle = t("incident_intel_subtitle", user_lang)
            col_issue = t("incident_intel_issue", user_lang)
            col_count = t("incident_intel_count", user_lang)
            col_trend = t("incident_intel_trend", user_lang)
            col_group = t("incident_intel_group", user_lang)
            empty_text = t("incident_intel_no_repeats", user_lang)
            open_title = t("open_dashboard", user_lang)

            rows = []
            for item in insights[:3]:
                trend = item.get("trend_percent", 0)
                if trend > 5:
                    trend_label = f"+{trend}%"
                elif trend < -5:
                    trend_label = f"{trend}%"
                else:
                    trend_label = "stable"
                rows.append({
                    "type": "ColumnSet",
                    "columns": [
                        {"type": "Column", "width": 3, "items": [{"type": "TextBlock", "text": str(item.get("issue") or ""), "wrap": True}]},
                        {"type": "Column", "width": 1, "items": [{"type": "TextBlock", "text": str(item.get("count") or 0), "horizontalAlignment": "Center"}]},
                        {"type": "Column", "width": 1, "items": [{"type": "TextBlock", "text": trend_label, "horizontalAlignment": "Center"}]},
                        {"type": "Column", "width": 2, "items": [{"type": "TextBlock", "text": str(item.get("assignment_group") or "Unassigned"), "wrap": True}]},
                    ],
                })

            body = [
                {"type": "TextBlock", "size": "Large", "weight": "Bolder", "text": header},
                {"type": "TextBlock", "text": subtitle, "isSubtle": True, "wrap": True},
                {
                    "type": "ColumnSet",
                    "columns": [
                        {"type": "Column", "width": 3, "items": [{"type": "TextBlock", "text": col_issue, "weight": "Bolder"}]},
                        {"type": "Column", "width": 1, "items": [{"type": "TextBlock", "text": col_count, "weight": "Bolder", "horizontalAlignment": "Center"}]},
                        {"type": "Column", "width": 1, "items": [{"type": "TextBlock", "text": col_trend, "weight": "Bolder", "horizontalAlignment": "Center"}]},
                        {"type": "Column", "width": 2, "items": [{"type": "TextBlock", "text": col_group, "weight": "Bolder"}]},
                    ],
                },
            ] + rows

            if not rows:
                body.append({"type": "TextBlock", "text": empty_text, "wrap": True})

            card = {
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": body,
                "actions": [
                    {"type": "Action.OpenUrl", "title": open_title, "url": dashboard_url}
                ] if dashboard_url else [],
            }

            msg = MessageFactory.attachment(CardFactory.adaptive_card(card))
            await turn_context.send_activity(msg)

            if (result or {}).get("text"):
                self._remember_reply(conversation_id, (result or {}).get("text"))
            return

        # Format normal reply (EN), then translate out once if needed
        reply_en = self._format_reply_text(result or {})
        reply_out = reply_en if user_lang == "en" else translate(reply_en, "en", user_lang, banner=True)

        msg = MessageFactory.text(reply_out)
        msg.suggested_actions = self._language_suggested_actions(user_lang)
        await turn_context.send_activity(msg)
        self._remember_reply(conversation_id, reply_out)
