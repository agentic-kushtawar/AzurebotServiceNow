from core.metrics import METRICS
from core.orchestrator.intent_types import Intent

def match(text: str, sess: dict) -> Intent | None:
    if "vpn" in (text or "").lower():
        return Intent.VPN_HELP
    return None

async def handle() -> str:
    METRICS.inc_intent("vpn_help")
    return "For VPN issues, verify MFA and reconnect. Say **open a ticket** if it persists."
