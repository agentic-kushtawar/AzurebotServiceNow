# core/orchestrator/engine.py
from __future__ import annotations

from typing import Any
from loguru import logger

from core.metrics import METRICS
from core.orchestrator.state import session_for
from core.orchestrator.intent_types import Intent



# intent modules (rule-based)
from .intents import (
    ticket_create,        # we’ll detect "open a ticket..." here first
    ticket_status,        # "status INC0012345"
    password_reset,       # "reset my password", "instructions"
    vpn,                  # "vpn help", "vpn keeps disconnecting"
    help as help_intent,  # "help"
    fallback,             # default safe message
    
)


class Orchestrator:
    """
    Small dispatcher that chooses an intent module, then calls its handler.

    Design goals:
    - Keep 'engine' thin: detection order + delegating to intent modules.
    - Put domain logic (SNOW calls, text templates) inside intent modules.
    - Make 'ticket_create' win over VPN/help when user starts with "open a ticket ...".
    """

    def __init__(self) -> None:
        pass

    async def handle(self, text: str, user: Any, locale: str = "en") -> str:
        # basic counters
        METRICS.inc("messages")

        sess = session_for(user)
        t = (text or "").strip()

        # 0) Empty input → help
        if not t:
            logger.debug("Empty message → help")
            return await help_intent.handle()

        # 1) Ticket CREATE must run BEFORE other domain intents so
        #    "open a ticket: vpn can't connect..." creates an incident
        is_create, reason = ticket_create.detect_ticket_create(t)
        if is_create:
            logger.debug("Detected ticket_create; reason={!r}", reason)
            # let the ticket_create module decide SNOW payloads and formatting
            return await ticket_create.handle(reason=reason, user=user)

        # 2) Ticket STATUS (returns tuple from its matcher)
        status_match = ticket_status.match(t, sess)
        if isinstance(status_match, tuple):
            _, number = status_match  # (Intent.TICKET_STATUS, "INC0012345")
            logger.debug("Detected ticket_status; number={}", number)
            return await ticket_status.handle(number)

        # 3) Other intents in order: password → vpn → help
        for matcher in (password_reset.match, vpn.match, help_intent.match):
            intent = matcher(t, sess)
            if intent:
                match intent:
                    case Intent.PASSWORD_RESET | Intent.RESET_INSTRUCTIONS | Intent.TICKET_CREATE:
                        # NOTE: password_reset.handle knows how to render
                        #       greeting/steps or delegate to its own flows.
                        return await password_reset.handle(intent, t, user)

                    case Intent.VPN_HELP:
                        return await vpn.handle()

                    case Intent.HELP:
                        return await help_intent.handle()

        # 4) Fallback
        logger.debug("No matcher hit → fallback")
        return await fallback.handle()
