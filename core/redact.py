# core/redact.py
import re

_INC = re.compile(r"\bINC(\d{5,})\b", re.IGNORECASE)
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+)\b")

def mask_inc(text: str) -> str:
    return _INC.sub(lambda m: f"INC…{m.group(1)[-4:]}", text or "")

def mask_email(text: str) -> str:
    return _EMAIL.sub(lambda m: f"{m.group(1)[0]}***@{m.group(2)}", text or "")
