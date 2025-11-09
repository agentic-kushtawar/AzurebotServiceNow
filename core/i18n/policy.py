# core/i18n/policy.py
from __future__ import annotations
import json
import os
from typing import Dict, Any

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "config", "languages.json")

def _load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # safe defaults
        return {
            "default": "en",
            "locales": {
                "en": {"enabled": True, "label": "English"},
                "es": {"enabled": True, "label": "Español"},
            },
            "banners": {
                "es": "Temporalmente respondo en inglés por un problema de traducción.",
                "en": "Temporarily responding in English due to a translation issue.",
            },
        }

_CFG = _load_config()
DEFAULT_LOCALE: str = (_CFG.get("default") or "en").split("-")[0]

def normalize_locale(locale: str) -> str:
    return (locale or "").split("-")[0].lower() or DEFAULT_LOCALE

def is_enabled_locale(locale: str) -> bool:
    loc = normalize_locale(locale)
    meta = _CFG.get("locales", {}).get(loc, {})
    return bool(meta.get("enabled"))

def label_for(locale: str) -> str:
    loc = normalize_locale(locale)
    return _CFG.get("locales", {}).get(loc, {}).get("label", loc)

def enabled_locales() -> Dict[str, Dict[str, Any]]:
    return {k: v for k, v in _CFG.get("locales", {}).items() if v.get("enabled")}

def fallback_banner_for(locale: str) -> str:
    loc = normalize_locale(locale)
    banners = _CFG.get("banners", {}) or {}
    return banners.get(loc, "")
