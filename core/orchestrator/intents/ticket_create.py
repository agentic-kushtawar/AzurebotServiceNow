# core/orchestrator/intents/ticket_create.py
from __future__ import annotations

import re
from typing import Tuple
from loguru import logger

from core.snow import get_snow
from config.settings import settings

CREATE_PATTERNS = [
    re.compile(r"^\s*(?:please\s+)?open\s+(?:a\s+)?ticket\s*[:\-]?\s*(?P<reason>.+)$", re.I),
    re.compile(r"^\s*(?:please\s+)?create\s+(?:an?\s+)?(incident|ticket)\s*(?:for\s+)?(?P<reason>.+)$", re.I),
    re.compile(r"^\s*(?:please\s+)?(raise|log|file|submit)\s+(?:an?\s+)?(incident|ticket)\s*(?:for\s+)?(?P<reason>.+)$", re.I),
]
TERSE_CREATE = re.compile(
    r"^\s*(open\s+(?:a\s+)?ticket|create\s+(?:an?\s+)?(incident|ticket)|(raise|log|file|submit)\s+(?:an?\s+)?(incident|ticket))\s*$",
    re.I,
)


_PRONOUN_REASON = {"this", "that", "it", "this issue", "that issue", "this problem", "that problem", "this one", "that one"}

def detect_ticket_create(text: str) -> Tuple[bool, str]:
    t = (text or "").strip()
    for pat in CREATE_PATTERNS:
        m = pat.match(t)
        if m:
            reason = (m.group("reason") or "").strip()
            if reason.lower() in _PRONOUN_REASON:
                reason = ""
            return True, reason
    if TERSE_CREATE.match(t):
        return True, ""
    return False, ""


async def handle(*, reason: str, user) -> str:
    if not settings.FEATURE_SNOW_ENABLED:
        return "ServiceNow is disabled right now. Turn it on with `FEATURE_SNOW_ENABLED=true`."

    client = get_snow()

    short_description = (reason or "User requested to open a ticket").strip()
    description = short_description

    # categories just to keep your old behaviour
    lower = short_description.lower()
    category = "Network" if "vpn" in lower else "Inquiry"
    subcategory = "VPN" if "vpn" in lower else "General"

    display_name = getattr(user, "name", None) or getattr(user, "aad_object_id", "") or "Teams User"
    description = f"{description}\n\nRaised by: {display_name} (via Teams bot)"

    # --- NEW: resolve configured caller ("botuser") -> sys_id and pass as caller_id ---
    caller_id: str | None = None
    caller_uname = (settings.SNOW_CALLER_USER or "").strip()
    if caller_uname:
        try:
            # user_name exact match first; falls back to nameLIKE
            q = f"user_name={caller_uname}"
            resp = await client._request(  # use client's pooled HTTP + auth
                "GET",
                f"/api/now/table/sys_user?sysparm_query={q}&sysparm_fields=sys_id&sysparm_limit=1",
            )
            result = (resp.json() or {}).get("result") or []
            if not result:
                q = f"nameLIKE{caller_uname}"
                resp = await client._request(
                    "GET",
                    f"/api/now/table/sys_user?sysparm_query={q}&sysparm_fields=sys_id&sysparm_limit=1",
                )
                result = (resp.json() or {}).get("result") or []
            if result:
                caller_id = result[0].get("sys_id")
                logger.debug("Resolved SNOW_CALLER_USER='{}' -> sys_id={}", caller_uname, caller_id)
            else:
                logger.warning("SNOW_CALLER_USER='{}' not found; proceeding without caller_id", caller_uname)
        except Exception:
            logger.exception("Failed to resolve SNOW_CALLER_USER='{}'", caller_uname)

    try:
        number = await client.create_incident(
            short_description=short_description,
            description=description,
            category=category,
            subcategory=subcategory,
            impact="3",
            urgency="3",
            caller_id=caller_id,   # << this sets Caller if we resolved it
        )
    except Exception as e:
        logger.exception("SNOW create_incident failed in ticket_create.handle")
        return f"Sorry — I couldn’t create a ticket right now ({type(e).__name__}). Please try again."

    return (
        f"✅ Incident created in ServiceNow: **{number}**.\n"
        f"You can check it later with `status {number}`."
    )
