# core/redact.py
from __future__ import annotations
import re
from typing import Any, Dict

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b")
_INC   = re.compile(r"\bINC\d{7,}\b", re.I)
_GUID  = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)

# keys whose values we should aggressively mask if they look like PII
_SUSPECT_KEYS = {"email", "user_email", "username", "name", "full_name",
                 "caller", "caller_name", "phone", "mobile", "employee_id"}

def _mask_text(s: str) -> str:
    s = _EMAIL.sub("[redacted-email]", s)
    s = _PHONE.sub("[redacted-phone]", s)
    s = _INC.sub("[redacted-inc]", s)
    s = _GUID.sub("[redacted-guid]", s)
    return s

def _scrub_value(v: Any) -> Any:
    if isinstance(v, str):
        return _mask_text(v)
    if isinstance(v, dict):
        return {k: _scrub_value(v) for k, v in v.items()}
    if isinstance(v, list):
        return [_scrub_value(x) for x in v]
    return v

def scrub(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a deep-copied, redacted version of `event`.
    - Masks emails, phone numbers, incident numbers (INC…), GUIDs in any strings.
    - If a key looks like PII (e.g., 'email', 'name'), its value is fully masked.
    """
    if not isinstance(event, dict):
        return _scrub_value(event)  # be lenient
    redacted: Dict[str, Any] = {}
    for k, v in event.items():
        if k.lower() in _SUSPECT_KEYS:
            redacted[k] = "[redacted]"
        else:
            redacted[k] = _scrub_value(v)
    return redacted
