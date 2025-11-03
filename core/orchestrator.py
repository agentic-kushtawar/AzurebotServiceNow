# core/orchestrator.py
import re
import uuid
from typing import Any, Dict, Optional
from loguru import logger
from config.settings import settings
from core.metrics import METRICS

# Option A import path (you created skills/servicenow/client.py)
try:
    from skills.servicenow.client import ServiceNowClient
except Exception:
    ServiceNowClient = None  # type: ignore


# ultra-light per-user session (MVP only)
_STATE: Dict[str, Dict[str, str]] = {}


class Orchestrator:
    """
    Routes user messages to skills (password reset, VPN help, ticket create/status).
    - Real ServiceNow is used only when FEATURE_SNOW_ENABLED=true and credentials work.
    - Keeps minimal session state to drive short multi-turn flows.
    """

    def __init__(self) -> None:
        self._snow_enabled: bool = bool(getattr(settings, "FEATURE_SNOW_ENABLED", False))
        self._snow: Optional[ServiceNowClient] = None

    # ---------- helpers ----------

    def _session(self, user: Any) -> Dict[str, str]:
        uid = getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else "local")
        return _STATE.setdefault(str(uid), {})

    def _ensure_snow(self) -> Optional["ServiceNowClient"]:
        """Lazy-init the SN client when the feature flag is on."""
        if not self._snow_enabled:
            return None
        if self._snow is None and ServiceNowClient is not None:
            self._snow = ServiceNowClient(
                base_url=settings.SNOW_BASE_URL,
                username=settings.SNOW_USERNAME,
                password=settings.SNOW_PASSWORD,
            )
        return self._snow

    # ---------- main entry ----------

    async def handle(self, text: str, user: Any, locale: str = "en") -> str:
        METRICS.inc("messages")

        t = (text or "").strip()
        t_lower = t.lower()
        sess = self._session(user)

        # ---- intents ----

        # Password reset entry
        if re.search(r"\b(reset|unlock)\b", t_lower):
            sess["intent"] = "password_reset"
            METRICS.inc_intent("password_reset")
            return (
                "I can guide a password reset.\n"
                "• Type **instructions** for the steps\n"
                "• Or type **open a ticket** to create an incident"
            )

        # Reset instructions
        if t_lower == "instructions" and sess.get("intent") == "password_reset":
            METRICS.inc_intent("instructions")
            return (
                "Password reset steps (sample):\n"
                "1) Go to https://portal.office.com\n"
                "2) Click **Forgot my password**\n"
                "3) Complete MFA and set a new password\n"
                "If this failed, reply **open a ticket**."
            )

        # Create incident
        if t_lower == "open a ticket":
            METRICS.inc_intent("ticket_create")

            client = self._ensure_snow()
            if client:
                try:
                    number = await client.create_incident(
                        short_description="Password reset assistance (from Teams bot)",
                        description="User reports they cannot sign in / self-service reset failed.",
                        category="Account",
                        subcategory="Password",
                        impact="3",
                        urgency="3",
                    )
                    sess.clear()
                    return f"✅ Incident created in ServiceNow: **{number}**.\nYou can check with `status {number}`."
                except Exception as e:
                # 👉 print full stack trace + important context to the console
                    logger.exception(
                        "ServiceNow create_incident failed",
                        extra={
                            "feature_snow_enabled": self._snow_enabled,
                            "base_url": getattr(settings, "SNOW_BASE_URL", None),
                            "username": getattr(settings, "SNOW_USERNAME", None),
                        },
                    )
                    inc = f"INC{uuid.uuid4().hex[:7].upper()}"
                    sess.clear()
                    return (
                        "⚠️ ServiceNow is actually unavailable right now. "
                        f"I’ve created a local reference **{inc}**. "
                        "Please try again later or contact the service desk."
                    )

            # feature disabled → stub
            inc = f"INC{uuid.uuid4().hex[:7].upper()}"
            sess.clear()
            return f"✅ Ticket created: **{inc}** (stub). Check later with `status {inc}`."

        # Ticket status
        m = re.match(r"status\s+(INC[0-9A-Z]+)", t, flags=re.IGNORECASE)
        if m:
            METRICS.inc_intent("ticket_status")
            number = m.group(1).upper()

            client = self._ensure_snow()
            if client:
                try:
                    data = await client.get_incident(number)
                    state = (data.get("state_name") or data.get("state") or "Unknown")
                    assigned = data.get("assignment_group_name") or data.get("assignment_group") or "Unassigned"
                    prio = data.get("priority") or "—"
                    return (
                        f"**{number}** status: **{state}**\n"
                        f"- Assignment group: {assigned}\n"
                        f"- Priority: {prio}"
                    )
                except Exception:
                    return f"Couldn’t find **{number}** in ServiceNow. Please verify the number."

            return f"Status for **{number}**: **New** (stub)."

        # VPN help
        if "vpn" in t_lower:
            METRICS.inc_intent("vpn_help")
            return "For VPN issues, verify MFA and reconnect. Say **open a ticket** if it persists."

        # Software install
        if "install" in t_lower or "update" in t_lower:
            METRICS.inc_intent("software_install")
            sess["intent"] = "software_install"
            return "Tell me the software name & version; I can raise a request."

        # “help” / greetings
        if t_lower in {"help", "hi", "hello"}:
            return (
                "I can help with password reset, VPN/app access, installs, or how-to.\n"
                "Try: **reset my password**."
            )

        # Fallback
        return (
            "I can help with password reset, VPN/app access, installs, or how-to. "
            "Try: **reset my password**."
        )
