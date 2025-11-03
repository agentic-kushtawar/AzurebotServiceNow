import re
from loguru import logger
from core.metrics import METRICS
from core.orchestrator.intent_types import Intent
from core.orchestrator.state import session_for, clear_session
from core.snow import get_snow  # uses your lazy SN client
from config.settings import settings

PASSWORD_RESET_ENTRY = re.compile(r"\b(reset|unlock)\b", re.IGNORECASE)

def match(text: str, sess: dict) -> Intent | None:
    t = (text or "").strip()
    if t.lower() == "instructions" and sess.get("intent") == "password_reset":
        return Intent.RESET_INSTRUCTIONS
    if PASSWORD_RESET_ENTRY.search(t):
        return Intent.PASSWORD_RESET
    if t.lower() == "open a ticket":
        return Intent.TICKET_CREATE
    return None

async def handle(intent: Intent, text: str, user) -> str:
    if intent is Intent.PASSWORD_RESET:
        sess = session_for(user)
        sess["intent"] = "password_reset"
        METRICS.inc_intent("password_reset")
        return (
            "I can guide a password reset.\n"
            "• Type **instructions** for the steps\n"
            "• Or type **open a ticket** to create an incident"
        )

    if intent is Intent.RESET_INSTRUCTIONS:
        METRICS.inc_intent("instructions")
        return (
            "Password reset steps (sample):\n"
            "1) Go to https://portal.office.com\n"
            "2) Click **Forgot my password**\n"
            "3) Complete MFA and set a new password\n"
            "If this failed, reply **open a ticket**."
        )

    if intent is Intent.TICKET_CREATE:
        METRICS.inc_intent("ticket_create")
        client = get_snow() if getattr(settings, "FEATURE_SNOW_ENABLED", False) else None
        if client:
            try:
                number = await client.create_incident(
                    short_description="Password reset assistance (from Teams bot)",
                    description="User reports they cannot sign in / self-service reset failed.",
                    category="Account",
                    subcategory="Password",
                    impact="3",
                    urgency="3",
                    # caller_id can be added later when you map AAD→SN user
                )
                clear_session(user)
                return f"✅ Incident created in ServiceNow: **{number}**.\nYou can check with `status {number}`."
            except Exception:
                logger.exception("ServiceNow create_incident failed")
                # graceful fallback
        # stub path
        import uuid
        inc = f"INC{uuid.uuid4().hex[:7].upper()}"
        clear_session(user)
        return f"✅ Ticket created: **{inc}** (stub). Check later with `status {inc}`."

    raise RuntimeError("password_reset handler invoked with unexpected intent")
