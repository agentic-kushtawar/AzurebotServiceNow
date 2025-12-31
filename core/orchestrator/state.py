from __future__ import annotations

import json
from typing import Dict, MutableMapping, Any

from config.settings import settings

# ultra-light per-user session store (MVP)
_STATE: Dict[str, Dict[str, Any]] = {}
_REDIS_CLIENT = None


def _redis_enabled() -> bool:
    return (settings.SESSION_STORE or "").strip().lower() == "redis" and bool(settings.REDIS_URL)


def _get_redis():
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        import redis

        _REDIS_CLIENT = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _REDIS_CLIENT


def _session_key(uid: str) -> str:
    return f"session:{uid}"


def _load_session(uid: str) -> Dict[str, Any]:
    if not _redis_enabled():
        return _STATE.setdefault(uid, {})
    raw = _get_redis().get(_session_key(uid))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _save_session(uid: str, data: Dict[str, Any]) -> None:
    if not _redis_enabled():
        _STATE[uid] = data
        return
    payload = json.dumps(data, default=str)
    ttl = max(int(settings.SESSION_TTL_SECS or 0), 1)
    _get_redis().set(_session_key(uid), payload, ex=ttl)


def _touch_session(uid: str, data: Dict[str, Any]) -> None:
    if _redis_enabled():
        _save_session(uid, data)


class _SessionDict(MutableMapping[str, Any]):
    def __init__(self, uid: str, data: Dict[str, Any]):
        self._uid = uid
        self._data = data

    def __getitem__(self, key: str) -> Any:
        _touch_session(self._uid, self._data)
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        _save_session(self._uid, self._data)

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        _save_session(self._uid, self._data)

    def __iter__(self):
        _touch_session(self._uid, self._data)
        return iter(self._data)

    def __len__(self) -> int:
        _touch_session(self._uid, self._data)
        return len(self._data)

    def get(self, key: str, default=None):
        _touch_session(self._uid, self._data)
        return self._data.get(key, default)

    def pop(self, key: str, default=None):
        value = self._data.pop(key, default)
        _save_session(self._uid, self._data)
        return value

    def clear(self) -> None:
        self._data.clear()
        _save_session(self._uid, self._data)

    def update(self, *args, **kwargs) -> None:
        self._data.update(*args, **kwargs)
        _save_session(self._uid, self._data)

    def setdefault(self, key: str, default=None):
        if key not in self._data:
            self._data[key] = default
            _save_session(self._uid, self._data)
        else:
            _touch_session(self._uid, self._data)
        return self._data[key]


def session_for(user) -> MutableMapping[str, Any]:
    uid = getattr(user, "conversation_id", None) or getattr(user, "call_id", None)
    if uid is None:
        uid = getattr(user, "id", None)
    if uid is None and isinstance(user, dict):
        uid = (
            user.get("conversation_id")
            or user.get("call_id")
            or user.get("id")
            or user.get("user_id")
        )
    uid = str(uid or "local")
    data = _load_session(uid)
    _touch_session(uid, data)
    return _SessionDict(uid, data) if _redis_enabled() else data


def clear_session(user) -> None:
    uid = getattr(user, "conversation_id", None) or getattr(user, "call_id", None)
    if uid is None:
        uid = getattr(user, "id", None)
    if uid is None and isinstance(user, dict):
        uid = (
            user.get("conversation_id")
            or user.get("call_id")
            or user.get("id")
            or user.get("user_id")
        )
    uid = str(uid or "local")
    if _redis_enabled():
        _get_redis().delete(_session_key(uid))
        return
    s = _STATE.get(uid)
    if s is not None:
        s.clear()
