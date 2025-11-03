import re, uuid
from typing import Any, Dict


from core.metrics import METRICS

# super-light in-memory state just for MVP local tests
_STATE: Dict[str, Dict[str, str]] = {}

class Orchestrator:
    async def handle(self, text: str, user: Any, locale: str = "en") -> str:
        METRICS.inc("messages")

        t = (text or "").lower()
        uid = getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else "local")
        session = _STATE.setdefault(uid, {})

        if re.search(r"\b(reset|unlock)\b", t):
            session["intent"] = "password_reset"
            METRICS.inc_intent("password_reset")
            return ("I can guide a password reset.\n"
                    "• Type **instructions** for the steps\n"
                    "• Or type **open a ticket** to create an incident")

        if t.strip() == "instructions" and session.get("intent") == "password_reset":
            METRICS.inc_intent("Instruction_Ticket")
            return (
                "Password reset steps (sample, grounded KB later):\n"
                "1) Go to https://portal.office.com\n"
                "2) Choose **Forgot my password**\n"
                "3) Verify MFA and set a new password\n"
                "If this failed, reply **open a ticket**."
            )

        if t.strip() == "open a ticket":
            METRICS.inc_intent("Open_Ticket")
            inc = f"INC{uuid.uuid4().hex[:7].upper()}"
            session.clear()
            # later we’ll call ServiceNowClient.create_incident(...) here
            return f"✅ Ticket created: **{inc}** (stub). You’ll be able to check status with `status {inc}`."

        m = re.match(r"status\s+(INC\w+)", t)
        if m:
            number = m.group(1)
            return f"Status for {number}: **New** (stub)."

        if "vpn" in t:
            METRICS.inc_intent("vpn_help")
            return "For VPN issues, verify MFA and reconnect. Say **open a ticket** to create an incident."
        if "install" in t or "update" in t:
            METRICS.inc_intent("software_install")
            return "Tell me the software name & version; I can raise a request."
        if "status" in t and "inc" in t:
            METRICS.inc_intent("ticket_status")
            return "Share the incident number, e.g., `status INC0012345`."
        return "I can help with password reset, VPN/app access, installs, or how-to. Try: **reset my password**."
