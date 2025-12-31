# core/i18n/lang_store.py
from __future__ import annotations
from typing import Optional

# Prefer your session store if available; else simple in-memory map
try:
    from core.orchestrator.state import session_for  # your existing pattern
except Exception:
    session_for = None  # type: ignore

Lang = str

_mem = {}

def get_user_lang(user_id: Optional[str]) -> Lang:
    if session_for:
        s = session_for({"id": user_id or "local"})
        return s.get("lang", "en")
    # fallback
    return _mem.get(user_id or "local", "en")

def set_user_lang(user_id: Optional[str], lang: Lang) -> None:
    if session_for:
        s = session_for({"id": user_id or "local"})
        s["lang"] = lang
        return
    _mem[user_id or "local"] = lang
