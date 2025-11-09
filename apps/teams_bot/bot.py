from __future__ import annotations
from typing import Any, Dict
import logging
import re

from botbuilder.core import ActivityHandler, TurnContext, MessageFactory
from botbuilder.schema import SuggestedActions, CardAction, ActionTypes

# Orchestrator (kept as-is)
from core.orchestrator.engine import Orchestrator

# i18n
from core.i18n.adapter import detect, translate
from core.i18n.lang_store import get_user_lang, set_user_lang
from core.i18n.policy import enabled_locales, label_for
from core.i18n.strings import t

log = logging.getLogger("app")


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
        teams_locale = (a.locale or "en").split("-")[0].lower()
        # prefer saved preference; else Teams locale; else detect on the fly
        saved = get_user_lang(user_id)
        resolved = saved or teams_locale
        return {
            "user_id": user_id,
            "user_name": user_name,
            "user_email": user_email,
            "locale": resolved or "en",
            "channel": a.channel_id or "msteams",
        }

    def _language_suggested_actions(self, current_lang: str) -> SuggestedActions:
        """
        Show a non-action chip for current locale + real actions for the other enabled locales.
        Labels are localized to the viewer's current language.
        """
        # Localized label for the "chip" (just show the name of the current language)
        chip_label = t("spanish", current_lang) if current_lang == "es" else t("english", current_lang)
        actions = [
            CardAction(type=ActionTypes.im_back, title=f"🌐 {chip_label}", value="noop_lang")
        ]
        for code in enabled_locales().keys():
            if code == current_lang:
                continue
            label = t("spanish", current_lang) if code == "es" else t("english", current_lang)
            actions.append(CardAction(type=ActionTypes.im_back, title=label, value=f"/language {code}"))
        return SuggestedActions(actions=actions)

    async def _send_help(self, turn_context, user_lang):
        text_out = f"{t('help_text', user_lang)}\n{t('switch_hint', user_lang)}"
        msg = MessageFactory.text(text_out)
        msg.suggested_actions = self._language_suggested_actions(user_lang)
        await turn_context.send_activity(msg)

    async def _propose_ticket_ui(
        self, turn_context: TurnContext, reason_en: str, tips_en: str, user_lang: str
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

        if action == "password_reset":
            text = (result.get("text") or "").strip()
            return f"🔒 Password reset steps:\n{text}" if text else "🔒 Let’s reset your password."

        if action == "help":
            return (result.get("text") or "Try: ‘raise a ticket’, ‘status of INC…’, or ‘reset my password’.").strip()

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
        preferred = get_user_lang(user_id)  # 'en' or saved locale
        teams_hint = (turn_context.activity.locale or "").lower()

        # Resolve locale: saved -> teams hint -> detect
        user_lang = preferred or detect(raw_text, hint=teams_hint) or "en"

        # Language switching UX (config-driven)
        if text_lc in {"change language", "language", "idioma", "cambiar idioma"} or text_lc == "noop_lang":
            locs = enabled_locales()
            msg = MessageFactory.text(t("current_language_choose", user_lang).format(lang=label_for(user_lang)))
            actions = []
            for code in locs.keys():
                if code == user_lang:
                    continue
                actions.append(CardAction(type=ActionTypes.im_back, title=label_for(code), value=f"/language {code}"))
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


        # ---------------------------------------------------------------------
        # Translate inbound -> EN (pivot), then orchestrate
        # ---------------------------------------------------------------------
        inbound_for_llm = raw_text if user_lang == "en" else translate(raw_text, user_lang, "en", banner=False)

        try:
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
        if action == "help":
            await self._send_help(turn_context, user_lang)
            return

        # If engine proposes a ticket, localize the proposal UI
        if action == "propose_ticket":
            await self._propose_ticket_ui(
                turn_context,
                reason_en=(result.get("reason") or ""),
                tips_en=(result.get("tips") or ""),
                user_lang=user_lang,
            )
            return

        # Format normal reply (EN), then translate out once if needed
        reply_en = self._format_reply_text(result or {})
        reply_out = reply_en if user_lang == "en" else translate(reply_en, "en", user_lang, banner=True)

        msg = MessageFactory.text(reply_out)
        msg.suggested_actions = self._language_suggested_actions(user_lang)
        await turn_context.send_activity(msg)
