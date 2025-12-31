from __future__ import annotations

import time
from typing import Any, Dict, Optional

_CACHE: Dict[str, Dict[str, Any]] = {}


def get_cached_response(key: str) -> Optional[dict]:
    item = _CACHE.get(key)
    if not item:
        return None
    if time.time() > item.get("expires_at", 0):
        _CACHE.pop(key, None)
        return None
    return item.get("payload")


def set_cached_response(key: str, payload: dict, ttl_seconds: int = 600) -> None:
    _CACHE[key] = {
        "payload": payload,
        "expires_at": time.time() + ttl_seconds,
    }
