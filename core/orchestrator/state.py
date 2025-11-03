from typing import Dict

# ultra-light per-user session store (MVP)
_STATE: Dict[str, Dict[str, str]] = {}

def session_for(user) -> Dict[str, str]:
    uid = getattr(user, "id", None)
    if uid is None and isinstance(user, dict):
        uid = user.get("id")
    uid = str(uid or "local")
    return _STATE.setdefault(uid, {})

def clear_session(user) -> None:
    s = session_for(user)
    s.clear()
