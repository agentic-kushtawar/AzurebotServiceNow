from core.orchestrator.intent_types import Intent

def match(text: str, sess: dict) -> Intent | None:
    t = (text or "").strip().lower()
    if t in {"help", "hi", "hello"}:
        return Intent.HELP
    return None

async def handle() -> str:
    return (
        "I can help with password reset, VPN/app access, installs, or how-to.\n"
        "Try: **reset my password**.\n"
        "_Type **change language** to switch._"
    )
