# core/orchestrator/llm_router.py
from __future__ import annotations
import os, re, pathlib
from pydantic import BaseModel, Field
from core.llm.client import LLM

_ALLOWED = "ticket_create|ticket_status|ticket_update_status|password_reset|vpn|help|greeting|bot_profile|ticket_howto|repeat_last|integration|incident_intel|intune_device_status|intune_device_restart|intune_device_apps|language_set|sop_upload|sop_latest|sop_validate|other"

class IntentResult(BaseModel):
    intent: str = Field(default="other", pattern=f"^({_ALLOWED})$")
    reason: str = ""
    inc_number: str = ""
    status: str = ""

class LLMIntentRouter:
    def __init__(self, llm: LLM):
        self.llm = llm
        self._prompt = self._load_prompt()

    def _load_prompt(self) -> str:
        # Try prompts/intent_router.md if present; otherwise fallback
        path = pathlib.Path("prompts/intent_router.md")
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return (
            "You classify IT service desk requests. "
            "Return strict JSON with keys: intent, reason, inc_number, status. "
            f"Allowed intents: {_ALLOWED}. "
            "If no incident number, set inc_number to ''."
        )

    @staticmethod
    def _normalize(inc: str) -> str:
        inc = (inc or "").strip().upper()
        return inc if re.fullmatch(r"INC\d{7}", inc) else ""

    async def classify(self, text: str) -> IntentResult:
        res = await self.llm.chat_json(self._prompt, text, schema=IntentResult)
        # normalize fields for downstream handlers
        res.intent = (res.intent or "other").strip().lower()
        res.reason = (res.reason or "").strip()
        res.inc_number = self._normalize(res.inc_number)
        res.status = (res.status or "").strip().lower().replace(" ", "_")
        return res
