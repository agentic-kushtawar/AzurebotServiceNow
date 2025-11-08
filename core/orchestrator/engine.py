# core/orchestrator/engine.py
from __future__ import annotations
import os
from typing import Any, Dict, Optional

from core.llm.client import LLM
from core.orchestrator.llm_router import LLMIntentRouter, IntentResult
from core.snow import get_snow
from config.settings import settings
from core.orchestrator.playbooks import (
    password_reset_playbook,
    help_playbook,
    vpn_tip_playbook,
)

# ---------- helpers ----------

def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}

async def _resolve_caller_id(user: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort mapping of Teams user -> ServiceNow caller.
    Tries email, then display name, then SNOW_CALLER_USER fallback.
    """
    client = get_snow()

    email = (user.get("user_email") or "").strip()
    if email:
        try:
            sys_id = await client.get_user_sys_id(email)
            if sys_id:
                return sys_id
        except Exception:
            pass

    name = (user.get("user_name") or "").strip()
    if name:
        try:
            sys_id = await client.get_user_sys_id(name)
            if sys_id:
                return sys_id
        except Exception:
            pass

    fallback = (getattr(settings, "SNOW_CALLER_USER", "") or "").strip()
    if fallback:
        try:
            sys_id = await client.get_user_sys_id(fallback)
            return sys_id or fallback
        except Exception:
            return fallback

    return None

# ---------- SNOW-backed handlers ----------

async def handle_ticket_create(reason: str, user: Dict[str, Any]) -> Dict[str, Any]:
    client = get_snow()

    caller_id = await _resolve_caller_id(user)
    short = reason or "Ticket creation requested"
    desc = f"Requested by {user.get('user_name') or user.get('user_id') or 'unknown'} via Teams."

    inc_number = await client.create_incident(
        short_description=short,
        description=desc,
        category="inquiry",
        subcategory="general",
        impact="3",
        urgency="3",
        caller_id=caller_id,
    )

    state = ""
    try:
        rec = await client.get_incident(inc_number)
        if rec:
            state = (rec.get("state") or "").strip()
    except Exception:
        state = ""

    return {
        "ok": bool(inc_number),
        "action": "ticket_create",
        "reason": reason,
        "inc_number": inc_number,
        "state": state,
    }

async def handle_ticket_status(inc_number: str, user: Dict[str, Any]) -> Dict[str, Any]:
    client = get_snow()
    rec = await client.get_incident(inc_number)
    return {
        "ok": bool(rec),
        "action": "ticket_status",
        "inc_number": inc_number,
        "state": (rec or {}).get("state", ""),
        "short_description": (rec or {}).get("short_description", ""),
    }

async def handle_password_reset(user: Dict[str, Any]) -> Dict[str, Any]:
    # Playbook-only stub (no SNOW call)
    return {"ok": True, "action": "password_reset", "text": password_reset_playbook(user)}

async def handle_vpn_proposal(reason: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Don’t auto-create for generic VPN issues; propose a ticket and add tips.
    UI renders Yes/No in bot.py when action == 'propose_ticket'.
    """
    return {
        "ok": True,
        "action": "propose_ticket",
        "reason": reason or "VPN not connecting",
        "tips": vpn_tip_playbook(),
    }

async def handle_help(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "help", "text": help_playbook()}

# ---------- legacy fallback ----------

async def legacy_route(text: str, user: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "legacy", "text": text}

# ---------- Orchestrator ----------

class Orchestrator:
    """
    LLM-first router with graceful fallback to legacy rules.
    Also supports simple command-style messages from buttons:
      - "create_ticket:<reason>"
      - "cancel_ticket"
    """
    def __init__(self):
        # Keep both names for backward compatibility with older code/flags.
        self.use_llm = _env_bool("FEATURE_LLM_ROUTER", False)
        self.use_llm_router = self.use_llm

        if self.use_llm:
            llm = LLM.auto()
            self.router = LLMIntentRouter(llm)
        else:
            self.router = None

    async def handle(self, text: str, user: Dict[str, Any]) -> Dict[str, Any]:
        # 1) Button/command short-circuits
        lower = (text or "").strip().lower()

        if lower.startswith("create_ticket:"):
            reason = text.split(":", 1)[1].strip() if ":" in text else ""
            return await handle_ticket_create(reason=reason, user=user)

        if lower.startswith("cancel_ticket"):
            return {"ok": True, "action": "help", "text": help_playbook()}

        # 2) Normal router flow (LLM) with safe fallback
        if self.use_llm and self.router:
            try:
                res: IntentResult = await self.router.classify(text)
                intent = (res.intent or "other").strip().lower()

                if intent == "ticket_create":
                    reason = res.reason or ""
                    # If the user explicitly asked to “raise/open/create a ticket”, create directly.
                    if reason and any(k in lower for k in ["raise a ticket", "open a ticket", "create a ticket"]):
                        return await handle_ticket_create(reason=reason, user=user)
                    # Otherwise propose a ticket with tips (good UX for fuzzy problem statements).
                    return await handle_vpn_proposal(reason=reason, user=user)

                if intent == "ticket_status" and res.inc_number:
                    return await handle_ticket_status(inc_number=res.inc_number, user=user)

                if intent == "password_reset":
                    return await handle_password_reset(user=user)

                if intent == "vpn":
                    return await handle_vpn_proposal(reason=res.reason, user=user)

                if intent in {"help", "other"}:
                    return await handle_help(user=user)

            except Exception:
                # Any LLM/parse issue → legacy passthrough
                pass

        # 3) Legacy behavior
        return await legacy_route(text, user)
