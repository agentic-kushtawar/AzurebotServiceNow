# core/i18n/adapter.py
from __future__ import annotations
from typing import Optional, Tuple
import os
import re

from .policy import (
    DEFAULT_LOCALE,
    is_enabled_locale,
    normalize_locale,
    fallback_banner_for,
)
from .providers.azure_translator import AzureTranslator


from .glossary.glossary_loader import Glossary          # relative

# --- provider wiring (swap later if needed) ---
_provider = AzureTranslator()
_glossary = Glossary.from_tsv(os.path.join(os.path.dirname(__file__), "glossary", "base.tsv"))

_PROTECT_L = "«"
_PROTECT_R = "»"

def _apply_protection(text: str) -> Tuple[str, dict]:
    """
    Replace glossary 'keep' terms with protected tokens, so the translator won't alter them.
    """
    if not text:
        return text, {}
    mapping = {}
    out = text
    for term, rule in _glossary.keep_terms().items():
        # whole-word, case-sensitive by default for acronyms
        pattern = r"\b" + re.escape(term) + r"\b"
        token = f"{_PROTECT_L}{len(mapping)}{_PROTECT_R}"
        new_out, n = re.subn(pattern, token, out)
        if n > 0:
            mapping[token] = term
            out = new_out
    return out, mapping

def _restore_protection(text: str, mapping: dict) -> str:
    if not mapping or not text:
        return text
    out = text
    for token, term in mapping.items():
        out = out.replace(token, term)
    return out

def detect(text: str, hint: Optional[str] = None) -> str:
    """
    Detect language using provider; fall back to hint or default.
    Returns a normalized BCP-47 language code (e.g., 'en', 'es').
    """
    if hint:
        hint = normalize_locale(hint)
    try:
        loc = _provider.detect(text)
        loc = normalize_locale(loc or "")
    except Exception:
        loc = None
    loc = loc or hint or DEFAULT_LOCALE
    # If disabled in config, fall back to default
    return loc if is_enabled_locale(loc) else DEFAULT_LOCALE

# core/i18n/adapter.py
def translate(text: str, src: str, dst: str, banner: bool = False) -> str:
    """
    Translate text from src -> dst with glossary protection and graceful fallback.
    - If src == dst or text empty: return as-is.
    - If provider fails:
        * banner=True  -> append one localized banner (user-visible)
        * banner=False -> return original text (no banner)
    """
    if not text or src == dst:
        return text

    src = normalize_locale(src)
    dst = normalize_locale(dst)

    protected_text, mapping = _apply_protection(text)

    try:
        translated = _provider.translate(protected_text, src, dst)
        translated = _restore_protection(translated, mapping)
        return translated
    except Exception:
        if not banner:
            # silent fallback
            return text
        if dst == "en":
            return text
        note = fallback_banner_for(dst)
        return f"{text}\n\n_{note}_" if note else text

