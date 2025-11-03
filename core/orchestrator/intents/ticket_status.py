import re
from core.metrics import METRICS
from core.orchestrator.intent_types import Intent
from core.snow import get_snow
from config.settings import settings

STATUS_RE = re.compile(r"status\s+(INC[0-9A-Z]+)", re.IGNORECASE)

def match(text: str, sess: dict):
    m = STATUS_RE.match(text or "")
    if m:
        return Intent.TICKET_STATUS, m.group(1).upper()
    return None

async def handle(number: str) -> str:
    METRICS.inc_intent("ticket_status")
    client = get_snow() if getattr(settings, "FEATURE_SNOW_ENABLED", False) else None
    if client:
        try:
            data = await client.get_incident(number)
            if data:
                state = (data.get("state_name") or data.get("state") or "Unknown")
                assigned = data.get("assignment_group_name") or data.get("assignment_group") or "Unassigned"
                prio = data.get("priority") or "—"
                return (
                    f"**{number}** status: **{state}**\n"
                    f"- Assignment group: {assigned}\n"
                    f"- Priority: {prio}"
                )
            return f"Couldn’t find **{number}** in ServiceNow."
        except Exception:
            return f"Couldn’t fetch **{number}** from ServiceNow."
    return f"Status for **{number}**: **New** (stub)."
