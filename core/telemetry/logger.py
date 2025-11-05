# core/telemetry/logger.py
from __future__ import annotations
import hashlib, json, logging, os, re, time
from contextvars import ContextVar
from typing import Any, Dict, Optional
from contextlib import contextmanager

# ---- context (correlation) ----
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
conv_id_var:     ContextVar[str] = ContextVar("conv_id", default="-")
user_hash_var:   ContextVar[str] = ContextVar("user_hash", default="-")

# ---- config ----
LOG_JSON = (os.getenv("LOG_JSON", "true").lower() in {"1","true","yes","on"})
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_REDACT = (os.getenv("LOG_REDACT_PII","true").lower() in {"1","true","yes","on"})
LOG_RAW_TEXT = (os.getenv("LOG_RAW_TEXT","false").lower() in {"1","true","yes","on"})
PII_SALT = os.getenv("PII_HASH_SALT", "changeme_salt")

# ---- PII helpers ----
EMAIL_RE   = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE   = re.compile(r"(?:\+?\d[\d\-\s]{6,}\d)")
IP_RE      = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
INC_RE     = re.compile(r"\bINC\d{7,}\b", re.IGNORECASE)
UPN_RE     = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")

def pii_hash(value: str) -> str:
    h = hashlib.sha256((PII_SALT + "|" + value).encode("utf-8")).hexdigest()
    return f"hash:{h[:16]}"

def redact(text: str) -> str:
    if not LOG_REDACT:
        return text
    def mask(m): return "[redacted]"
    text = EMAIL_RE.sub(mask, text)
    text = PHONE_RE.sub(mask, text)
    text = IP_RE.sub(mask, text)
    text = INC_RE.sub(lambda m: f"[{m.group(0)[:3]}…]", text)
    text = UPN_RE.sub(mask, text)
    return text

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "lvl": record.levelname,
            "msg": record.getMessage(),
            "ts": int(time.time() * 1000),
            "request_id": request_id_var.get(),
            "conversation_id": conv_id_var.get(),
            "user_hash": user_hash_var.get(),
            "logger": record.name,
        }
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_initialized", False):
        return logger
    logger.setLevel(LOG_LEVEL)
    handler = logging.StreamHandler()
    handler.setLevel(LOG_LEVEL)
    handler.setFormatter(JsonFormatter() if LOG_JSON else logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    logger._initialized = True  # type: ignore[attr-defined]
    return logger

log = get_logger("app")

def set_context(request_id: Optional[str]=None, conv_id: Optional[str]=None, user_id: Optional[str]=None, user_email: Optional[str]=None):
    if request_id: request_id_var.set(request_id)
    if conv_id: conv_id_var.set(conv_id)
    # hash stable identifier (email preferred)
    ident = (user_email or user_id or "-")
    if ident != "-":
        user_hash_var.set(pii_hash(ident))

def event(name: str, **fields: Any) -> None:
    # sanitize/log
    if not LOG_RAW_TEXT:
        # scrub typical text fields
        for k in ("text","reason","error","prompt","response"):
            if k in fields and isinstance(fields[k], str):
                fields[k] = redact(fields[k])
    msg = json.dumps({"event": name, **fields}, ensure_ascii=False)
    log.info(msg)

@contextmanager
def timer(name: str, **fields: Any):
    start = time.perf_counter()
    try:
        yield
        dur_ms = int(1000 * (time.perf_counter() - start))
        event(f"{name}.ok", duration_ms=dur_ms, **fields)
    except Exception as e:
        dur_ms = int(1000 * (time.perf_counter() - start))
        event(f"{name}.err", duration_ms=dur_ms, error=str(e), **fields)
        raise
