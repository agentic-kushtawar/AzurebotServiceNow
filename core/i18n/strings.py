from __future__ import annotations
import json, os
from functools import lru_cache
from .policy import normalize_locale, DEFAULT_LOCALE

_UI_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "prompts", "ui")

@lru_cache(maxsize=16)
def _load(locale: str) -> dict:
    path = os.path.join(_UI_DIR, f"{locale}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # fallback to default
        if locale != DEFAULT_LOCALE:
            return _load(DEFAULT_LOCALE)
        return {}

def t(key: str, locale: str) -> str:
    loc = normalize_locale(locale)
    d = _load(loc)
    if key in d:
        return d[key]
    # fallback chain
    if loc != DEFAULT_LOCALE:
        d_en = _load(DEFAULT_LOCALE)
        return d_en.get(key, key)
    return key
