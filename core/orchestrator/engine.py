# core/orchestrator/engine.py
from __future__ import annotations
import os
import asyncio
import re
import json
import time
from typing import Any, Dict, Optional
import logging
from pydantic import BaseModel

from core.llm.client import LLM
from core.orchestrator.llm_router import LLMIntentRouter, IntentResult
from core.orchestrator.intents.ticket_create import detect_ticket_create
from core.snow import get_snow
from config.settings import settings
from core.orchestrator.state import session_for
from core.graph.entra import EntraGraphClient
from core.i18n.strings import t
from core.orchestrator.playbooks import (
    help_playbook,
    vpn_tip_playbook,
    ticket_propose_playbook,   # NEW: generic tips for proposals
    ticket_howto_playbook,
    bot_profile_playbook,
    INTEGRATION_SUMMARY,
)
from core.analytics.incident_intel import (
    build_stats_query,
    compute_insights,
    summarize_insights,
    extract_raw_issues,
    apply_issue_map,
    raw_issue_counts,
    top_issues_from_rows,
)
from core.analytics.incident_cache import set_cached_response
from core.voice.sop_validation import (
    get_latest_sop_info,
    load_sop_json,
    limit_sop_steps,
    parse_transcript_steps,
    validate_transcript_against_sop,
    validate_steps,
    store_validation_result,
)
from datetime import datetime, timezone

# ---------- helpers ----------

_LOCATION_PHRASES = {
    "where are you located",
    "where are you based",
    "what is your location",
    "where do you live",
    "what location are you",
}
_INTEGRATION_PHRASES = {
    "with which system are you currently integrated",
    "what systems are you integrated with",
    "which systems are you integrated with",
    "which system are you integrated with",
}
_WEATHER_CITY_PATTERN = re.compile(r"weather\s+(?:like\s+)?in\s+([a-z0-9 ,.'-]+)")
_CENTRALUS_LOCATION = "I'm based in the Azure Central US (centralus) region."
_CENTRALUS_WEATHER = (
    "Around Dallas in the Central US region it's currently mild with clear skies."
)
_SNOW_PROCESSING_HINT = "Please hold while I check ServiceNow. This may take a few seconds."
_CAPABILITIES_TEXT = INTEGRATION_SUMMARY
_WEATHER_SYSTEM_PROMPT = (
    "You are Break Voice, a helpful Service Desk assistant. "
    "When users ask about weather in a specific city, provide a concise 1-2 sentence update. "
    "You may estimate conditions based on typical patterns if live data is unavailable, "
    "and be transparent that the info is approximate. "
    "Always respond as JSON with the shape {\"text\": \"your reply\"} and nothing else."
)

_SESSION_TIMEOUT_SECS = 2700
_INTERRUPT_PHRASES = {
    "cancel",
    "stop",
    "start over",
    "start again",
    "reset",
    "nevermind",
    "never mind",
    "scratch that",
    "abort",
}

def _short_reason(text: str, max_words: int = 12) -> str:
    words = [w for w in (text or "").strip().split() if w]
    return " ".join(words[:max_words]).strip()

def _is_generic_ticket_request(text: str) -> bool:
    if not text:
        return True
    lower = text.lower()
    return bool(
        re.search(r"\b(raise|open|create|file|submit|log)\b", lower)
        and re.search(r"\b(ticket|incident)\b", lower)
        and re.search(r"\b(this|that|it)\b", lower)
    )

def _is_generic_reason(text: str) -> bool:
    if not text:
        return True
    lower = text.lower().strip()
    generic = {
        "this",
        "that",
        "it",
        "issue",
        "an issue",
        "a issue",
        "problem",
        "a problem",
        "the issue",
        "the problem",
    }
    if lower in generic:
        return True
    return bool(re.search(r"\b(this|that|it)\b", lower) and re.search(r"\b(issue|problem)\b", lower))

def _remember_ticket_reason(sess: Dict[str, Any], reason: str, fallback_text: str) -> None:
    final = (reason or "").strip()
    if not final:
        if _is_generic_ticket_request(fallback_text):
            return
        final = _short_reason(fallback_text)
    if final and not _is_generic_reason(final):
        sess["pending_ticket_reason"] = final
_INTENT_ESCAPE_HINTS = (
    "help",
    "capabil",
    "what can you do",
    "what do you do",
    "who are you",
    "integrated",
    "integration",
    "services",
)

_LOG_PATH_HINTS = (
    "log path",
    "log directory",
    "report path",
    "report directory",
    "validation path",
    "validation report",
    "where is the report",
)
_SOP_VALIDATE_HINT = (
    "Please paste the lab note text you want validated. "
    "Example: validate this against SOP: <your lab note>"
)

_UPDATE_STATUS_HINT = (
    "Supported statuses: New, In Progress, or On Hold. "
    "To change a ticket status, include the incident number, target status, and a short reason. "
    "Example: \"Set INC0010059 to In Progress because user confirmed the fix.\""
)

_VOICE_LANG_MAP = {
    "english": ("en-US", "English"),
    "en": ("en-US", "English"),
    "spanish": ("es-ES", "Spanish"),
    "espanol": ("es-ES", "Spanish"),
    "es": ("es-ES", "Spanish"),
    "german": ("de-DE", "German"),
    "deutsch": ("de-DE", "German"),
    "de": ("de-DE", "German"),
}


def _looks_like_upn(value: str) -> bool:
    if not value:
        return False
    v = value.strip()
    if "@" not in v:
        return False
    user, domain = v.split("@", 1)
    return bool(user) and "." in domain


def _extract_device_name(text: str, hint: str = "") -> str:
    if hint:
        return hint.strip()
    raw = (text or "").strip()
    if not raw:
        return ""
    quoted = re.findall(r'"([^"]+)"|\'([^\']+)\'', raw)
    for a, b in quoted:
        candidate = (a or b).strip()
        if candidate:
            return candidate
    m = re.search(r"device\s+(?:named\s+)?([A-Za-z0-9._-]+)", raw, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"for\s+([A-Za-z0-9._-]+)", raw, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return raw


def _is_intune_count_request(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return False
    count_terms = (
        "how many devices",
        "how many systems",
        "number of devices",
        "number of systems",
        "count of devices",
        "count of systems",
        "devices registered",
        "systems registered",
        "devices enrolled",
        "systems enrolled",
        "devices enrolled with you",
        "systems enrolled with you",
        "registered with you",
        "enrolled with you",
        "do you manage",
        "total devices",
        "total systems",
        "registered devices",
        "registered systems",
    )
    if any(term in lower for term in count_terms):
        return True
    if ("device" in lower or "system" in lower or "systems" in lower) and any(
        term in lower
        for term in (
            "enroll",
            "enrolled",
            "enrollment",
            "register",
            "registered",
            "registration",
        )
    ):
        return True
    return False


def _is_user_count_request(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return False
    user_terms = (
        "how many users",
        "number of users",
        "count of users",
        "users registered",
        "registered users",
        "total users",
        "users in azure",
        "users in entra",
        "users in ad",
        "users in azure ad",
    )
    return any(term in lower for term in user_terms)


def _extract_voice_language(text: str, hint: str = "") -> tuple[str, str]:
    raw = (hint or text or "").strip().lower()
    if not raw:
        return "", ""
    for key, (code, label) in _VOICE_LANG_MAP.items():
        if key in raw:
            return code, label
    return "", ""

def _looks_like_language_change(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    if lower.startswith("/language"):
        return True
    code, _ = _extract_voice_language(text, "")
    if code:
        return True
    return any(term in lower for term in ("language", "idioma", "sprache", "lang"))


def _interrupt_action(text: str) -> Optional[str]:
    lower = (text or "").strip().lower()
    if not lower:
        return None
    if lower == "help" or lower.startswith("help "):
        return "help"
    if lower in _INTERRUPT_PHRASES:
        return "reset"
    return None


def _asks_bot_name(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(
        phrase in lower
        for phrase in (
            "your name",
            "who are you",
            "who am i chatting",
            "who am i talking",
            "what is your name",
            "what's your name",
        )
    )

def _is_log_path_request(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(hint in lower for hint in _LOG_PATH_HINTS)


def _extract_sop_transcript(text: str) -> str:
    if not text:
        return ""
    raw = text.strip()
    quoted = re.findall(r"\"([^\"]+)\"", raw)
    if quoted:
        candidate = quoted[-1].strip()
        if candidate:
            return candidate
    patterns = [
        r"validate\s+(?:this|it)?\s*(?:against|with)\s*sop[:\-]?\s*(.+)",
        r"validate\s+against\s+sop[:\-]?\s*(.+)",
        r"validate\s+sop[:\-]?\s*(.+)",
        r"sop\s+validation[:\-]?\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if match:
            return raw[match.start(1):].strip()
    return ""

def _extract_sop_step_limit(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"(?:first|top)\s+(\d+)\s+steps?", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return None


def _format_sop_validation(result: Any, detail_path: str = "") -> str:
    title = result.sop_title or result.sop_id or "SOP"
    if not result.issues:
        lines = [f"Validation result: COMPLIANT.", f"SOP: {title}", "No deviations detected."]
        if detail_path:
            lines.append(f"Full report stored at: {detail_path}")
        return "\n".join(lines)
    issues = result.issues[:3]
    lines = [
        f"Validation result: {result.status}.",
        f"SOP: {title}",
        "Top issues:",
    ]
    for issue in issues:
        lines.append(f"- {issue.step}: expected {issue.expected}, observed {issue.observed}")
    remaining = len(result.issues) - len(issues)
    if remaining > 0:
        lines.append(f"+{remaining} more issue(s) recorded.")
    if detail_path:
        lines.append(f"Full report stored at: {detail_path}")
    return "\n".join(lines)


def _normalize_ticket_status(raw: str) -> tuple[str, str]:
    """
    Normalize human/LLM status to ServiceNow state code + label.
    Returns ("", "") when unsupported.
    """
    key = (raw or "").strip().lower().replace(" ", "_")
    mapping = {
        "new": ("1", "New"),
        "in_progress": ("2", "In Progress"),
        "inprogress": ("2", "In Progress"),
        "on_hold": ("3", "On Hold"),
        "onhold": ("3", "On Hold"),
    }
    return mapping.get(key, ("", ""))


def _snow_state_matches(current: str, target_code: str, target_label: str) -> bool:
    cur = (current or "").strip()
    if not cur:
        return False
    cur_norm = cur.lower().replace(" ", "_")
    label_norm = (target_label or "").strip().lower().replace(" ", "_")
    return cur_norm in {target_code, label_norm}


def _is_integration_question(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    triggers = (
        "integrated",
        "integration",
        "connected",
        "connect",
        "work with",
        "systems",
        "platforms",
        "supported",
        "support",
    )
    return any(t in lower for t in triggers)


def _is_capability_question(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(
        phrase in lower
        for phrase in (
            "core capabilities",
            "capabilities",
            "what can you do",
            "what do you do",
            "how can you help",
        )
    )


def _extract_user_query(text: str) -> str:
    if not text:
        return ""
    if "User:" in text:
        return text.split("User:")[-1].strip()
    return text.strip()


def _extract_inc_from_text(text: str) -> str:
    if not text:
        return ""
    lowered = text.lower()
    lowered = lowered.replace("i n c", "inc").replace("i,n,c", "inc")
    lowered = lowered.replace("ink", "inc")
    match = re.search(r"\binc[\s\.-]*([0-9][0-9\s]{6,})\b", lowered)
    if not match:
        return ""
    digits = re.sub(r"\s+", "", match.group(1))
    if len(digits) != 7 or not digits.isdigit():
        return ""
    return f"INC{digits}"

def _should_cancel_pending_status_update(text: str) -> bool:
    if not text:
        return False
    if _looks_like_language_change(text):
        return True
    if detect_ticket_create(text)[0]:
        return True
    if _is_integration_question(text) or _is_capability_question(text):
        return True
    if _extract_inc_from_text(text):
        return True
    if _is_user_count_request(text) or _is_intune_count_request(text):
        return True
    if _is_log_path_request(text):
        return True
    return False


def _clarify_incident_request(action: str) -> Dict[str, Any]:
    verb = "check the status of" if action == "status" else "update"
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            f"I can {verb} an incident, but I didn't catch the incident number. "
            "Please say it like:\n"
            "- \"Status of INC0010077\"\n"
            "- \"INC 001 0077 status\"\n"
            "- \"Update INC0010077 to In Progress\""
        ),
    }


def _clarify_status_request() -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            "I can update the incident, but I didn't catch the target status. "
            "Please say it like:\n"
            "- \"Update INC0010077 to In Progress\"\n"
            "- \"Set INC0010077 to On Hold\"\n"
            "- \"Change INC0010077 to New\""
        ),
    }


def _confirm_status_update_prompt(inc_number: str, status_label: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": f"Do you want me to update {inc_number} to {status_label}? Please say yes or no.",
    }


_INTEGRATION_ALIASES = {
    "azure ad": "Azure Active Directory",
    "active directory": "Azure Active Directory",
    "entra": "Microsoft Entra ID",
    "entra id": "Microsoft Entra ID",
    "servicenow": "ServiceNow",
    "service now": "ServiceNow",
    "intune": "Microsoft Intune",
    "microsoft intune": "Microsoft Intune",
    "sap": "SAP",
    "salesforce": "Salesforce",
    "jira": "Jira",
    "confluence": "Confluence",
    "zendesk": "Zendesk",
    "freshservice": "Freshservice",
    "okta": "Okta",
    "slack": "Slack",
    "teams": "Microsoft Teams",
}


def _integration_list_from_summary() -> list[str]:
    summary = (INTEGRATION_SUMMARY or "").strip()
    if not summary:
        return []
    cleaned = re.sub(
        r"^i[' ]?m currently integrated with\s+",
        "",
        summary,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = cleaned.rstrip(".")
    parts = re.split(r",| and ", cleaned)
    items = [p.strip() for p in parts if p.strip()]
    return items


def _extract_integration_targets(text: str) -> list[str]:
    lower = (text or "").strip().lower()
    if not lower:
        return []
    matches: list[str] = []
    for key, label in _INTEGRATION_ALIASES.items():
        if key in lower and label not in matches:
            matches.append(label)
    if matches:
        return matches
    match = re.search(
        r"(?:integrated with|integrate with|connected to|connect to|work with|support)\s+([a-z0-9 ._-]{3,60})",
        lower,
    )
    if not match:
        return []
    candidate = match.group(1).strip().rstrip("?!.")
    candidate = re.sub(r"\b(are|you|me|us)\b", "", candidate).strip()
    parts = re.split(r"\s*(?:,|/| and | or )\s*", candidate)
    for part in parts:
        cleaned = part.strip().rstrip("?!.")
        if cleaned:
            title = cleaned.title()
            if title not in matches:
                matches.append(title)
    return matches


def _join_with_and(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _build_integration_reply(user_text: str) -> str:
    integrations = _integration_list_from_summary()
    if not integrations:
        integrations = ["Azure Active Directory", "ServiceNow", "Microsoft Intune"]
    targets = _extract_integration_targets(user_text)
    summary = _join_with_and(integrations)
    if not targets:
        return f"I'm currently integrated with {summary}."
    known = {item.lower() for item in integrations}
    known_targets: list[str] = []
    unknown_targets: list[str] = []
    for target in targets:
        normalized = target.lower()
        if normalized in known or any(normalized in k for k in known):
            known_targets.append(target)
        else:
            unknown_targets.append(target)
    if known_targets and not unknown_targets:
        return f"Yes — I'm currently integrated with {_join_with_and(known_targets)}."
    if unknown_targets and not known_targets:
        return f"I'm currently integrated with {summary}. I'm not integrated with {_join_with_and(unknown_targets)} yet."
    return (
        f"I'm integrated with {_join_with_and(known_targets)}, "
        f"but not with {_join_with_and(unknown_targets)} yet."
    )


def _should_exit_flow(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    if "?" in lower:
        return True
    return any(hint in lower for hint in _INTENT_ESCAPE_HINTS)
_FALLBACK_TEXT = (
    "I didn't quite catch that. Try asking me to raise a ticket, check an incident, "
    "reset a password, troubleshoot VPN, or learn about my integrations."
)
_SAP_SUPPORT_TEXT = (
    f"{INTEGRATION_SUMMARY or 'Right now I work with Azure Active Directory and ServiceNow'}, so I can't work directly inside SAP. "
    "If you need SAP help I can raise a ServiceNow ticket for the SAP team."
)
_INTEGRATION_SYSTEM_PROMPT = (
    "You are Break Voice, a Service Desk assistant. "
    f"When users ask about integrations or connected systems, mention: {INTEGRATION_SUMMARY or 'Azure Active Directory and ServiceNow'}. "
    "Keep replies to one or two sentences and stay friendly. "
    'Respond as JSON with {"text": "your reply"}.'
)
_INTEGRATION_CLASSIFIER_PROMPT = (
    "You are Break Voice, a Service Desk routing assistant. "
    "If the user is asking about integrations, connected systems, supported platforms, "
    "or wants to know what systems or tools you work with (for example: \"what kind of systems can you help me with\"), "
    "reply with a concise sentence that "
    f"mentions {INTEGRATION_SUMMARY or 'Azure Active Directory and ServiceNow'}. "
    "If the message is not about integrations, respond with an empty string."
)
_CAPABILITY_SYSTEM_PROMPT = (
    "You are Break Voice, a Service Desk assistant. "
    "When users ask what you can do, summarize your core capabilities: "
    "triaging and raising ServiceNow tickets, checking incident status, resetting passwords, "
    "helping with VPN issues, and chatting in English, Spanish, or German. Keep it under two sentences."
)
_GENERAL_QA_SYSTEM_PROMPT = (
    "You are Break Voice, a helpful enterprise Service Desk bot. "
    "If the user asks a question that is not about ServiceNow or tickets but can be answered "
    "with general knowledge, provide a concise answer (1-2 sentences). "
    "If you truly cannot answer, politely say you do not know. "
    'Respond as JSON with {"text": "your reply"}.'
)

_INCIDENT_INTEL_SYSTEM_PROMPT = (
    "You are Break Voice, an IT service desk analytics assistant. "
    "When users ask about recurring incidents, trends, or what problems to focus on, "
    "summarize the top repeated issues from the last 30 days in 3-5 bullets. "
    "If data is unavailable, say so. Respond as JSON with {\"text\":\"your reply\"}."
)

class _ShortReply(BaseModel):
    text: str = ""


class _IssueMap(BaseModel):
    mapping: dict[str, str] = {}


def _coerce_issue_mapping(data: object) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    if "mapping" in data and isinstance(data["mapping"], dict):
        return {str(k): str(v) for k, v in data["mapping"].items() if k and v}
    if "groups" in data and isinstance(data["groups"], list):
        mapping: dict[str, str] = {}
        for group in data["groups"]:
            if not isinstance(group, dict):
                continue
            label = group.get("label") or group.get("name")
            items = group.get("items") or group.get("issues")
            if not label or not isinstance(items, list):
                continue
            for item in items:
                if item:
                    mapping[str(item)] = str(label)
        return mapping
    return {str(k): str(v) for k, v in data.items() if k and v}


def _strip_code_fence(raw: str) -> str:
    if not raw:
        return raw
    trimmed = raw.strip()
    if trimmed.startswith("```"):
        # Drop leading ```json and trailing ```
        parts = trimmed.split("```")
        if len(parts) >= 3:
            return parts[1].replace("json", "", 1).strip()
    return raw


def _extract_json_object(raw: str) -> dict:
    if not raw:
        return {}
    raw = _strip_code_fence(raw).strip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = raw.find("{")
    if start == -1:
        return {}
    depth = 0
    end = -1
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return {}
    try:
        obj = json.loads(raw[start : end + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_mapping_from_raw(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    raw = _strip_code_fence(raw)
    idx = raw.find('"mapping"')
    if idx == -1:
        return {}
    brace_start = raw.find("{", idx)
    if brace_start == -1:
        return {}
    depth = 0
    end = -1
    for i in range(brace_start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return {}
    try:
        mapping_obj = json.loads(raw[brace_start : end + 1])
        if isinstance(mapping_obj, dict):
            return {str(k): str(v) for k, v in mapping_obj.items() if k and v}
    except Exception:
        return {}
    return {}


def _extract_mapping_pairs(raw: str) -> dict[str, str]:
    """
    Fallback parser for truncated JSON. Extracts "key": "value" pairs.
    """
    if not raw:
        return {}
    raw = _strip_code_fence(raw)
    pairs = re.findall(r'"([^"\\]+)"\s*:\s*"([^"\\]+)"', raw)
    return {k: v for k, v in pairs if k and v}


def _apply_label_map(mapping: dict[str, str], label_map: dict[str, str]) -> dict[str, str]:
    if not mapping or not label_map:
        return mapping
    remapped: dict[str, str] = {}
    for k, v in mapping.items():
        remapped[k] = label_map.get(v, v)
    return remapped

_GENERAL_LLM: LLM | None = None
_GRAPH_CLIENT: EntraGraphClient | None = None
log = logging.getLogger("app")

def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}

async def _resolve_caller_id(user: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort mapping of Teams user -> ServiceNow caller.
    Tries email, then display name, then SNOW_CALLER_USER fallback.
    """
    client = get_snow()

    email = (user.get("user_email") or "").strip()
    if email:
        try:
            sys_id = await client.get_user_sys_id(email)
            if sys_id:
                return sys_id
        except Exception:
            pass

    name = (user.get("user_name") or "").strip()
    if name:
        try:
            sys_id = await client.get_user_sys_id(name)
            if sys_id:
                return sys_id
        except Exception:
            pass

    fallback = (getattr(settings, "SNOW_CALLER_USER", "") or "").strip()
    if fallback:
        try:
            sys_id = await client.get_user_sys_id(fallback)
            return sys_id or fallback
        except Exception:
            return fallback

    return None

def _extract_sop_scope(text: str) -> str | None:
    if not text:
        return None
    lower = text.lower()
    if "sop procedure" in lower or "sop procedures" in lower or "procedure only" in lower:
        return "procedure"
    return None

def _smalltalk_response(text: str) -> Optional[Dict[str, Any]]:
    lower = (text or "").strip().lower()
    if not lower:
        return None

    if any(phrase in lower for phrase in _LOCATION_PHRASES) or (
        "location" in lower and "you" in lower
    ):
        return {"ok": True, "action": "direct_reply", "text": _CENTRALUS_LOCATION}

    if any(term in lower for term in ("sap", "oracle", "sap hana")) and not _is_integration_question(lower):
        return {"ok": True, "action": "direct_reply", "text": _SAP_SUPPORT_TEXT}

    if "weather" in lower:
        if _WEATHER_CITY_PATTERN.search(lower):
            return None
        return {"ok": True, "action": "direct_reply", "text": _CENTRALUS_WEATHER}

    return None

def _get_general_llm() -> Optional[LLM]:
    global _GENERAL_LLM
    if _GENERAL_LLM:
        return _GENERAL_LLM
    try:
        _GENERAL_LLM = LLM.auto()
    except Exception:
        _GENERAL_LLM = None
    return _GENERAL_LLM

async def _offline_general_response(text: str) -> Optional[Dict[str, Any]]:
    lower = (text or "").strip().lower()
    if not lower:
        return None

    if "weather" in lower:
        match = _WEATHER_CITY_PATTERN.search(lower)
        if match:
            city = match.group(1).strip()
            reply = await _llm_short_reply(
                text,
                f"{_WEATHER_SYSTEM_PROMPT} Always mention {city} explicitly.",
            )
            if reply:
                return {"ok": True, "action": "direct_reply", "text": reply}
    return None

async def _llm_short_reply(user_text: str, system_prompt: str) -> Optional[str]:
    llm = _get_general_llm()
    if not llm:
        return None
    try:
        result = await llm.chat_json(system_prompt, user_text, schema=_ShortReply)
    except Exception:
        return None
    reply = (getattr(result, "text", "") or "").strip()
    return reply or None

async def _maybe_integration_reply(user_text: str) -> Optional[str]:
    if _is_capability_question(user_text):
        return None
    if _extract_integration_targets(user_text):
        return _build_integration_reply(user_text)
    reply = await _llm_short_reply(user_text, _INTEGRATION_CLASSIFIER_PROMPT)
    if reply:
        stripped = reply.strip()
        if stripped:
            return stripped
    return None

async def _maybe_capability_reply(user_text: str) -> Optional[str]:
    if _is_integration_question(user_text):
        return None
    reply = await _llm_short_reply(user_text, _CAPABILITY_SYSTEM_PROMPT)
    if reply:
        stripped = reply.strip()
        if stripped:
            return stripped
    return None

async def _general_llm_reply(user_text: str) -> Optional[str]:
    return await _llm_short_reply(user_text, _GENERAL_QA_SYSTEM_PROMPT)

def _get_graph_client() -> Optional[EntraGraphClient]:
    global _GRAPH_CLIENT
    if _GRAPH_CLIENT:
        return _GRAPH_CLIENT
    try:
        _GRAPH_CLIENT = EntraGraphClient()
    except Exception as exc:
        log.warning("Graph client unavailable: %s", exc)
        _GRAPH_CLIENT = None
    return _GRAPH_CLIENT

def _is_affirmative(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return lower in {
        "yes",
        "y",
        "ok",
        "okay",
        "proceed",
        "confirm",
        "sure",
        "go ahead",
        "please do",
        "affirm",
    }


def _is_affirmative_i18n(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return lower in {
        "yes",
        "y",
        "ok",
        "okay",
        "proceed",
        "confirm",
        "sure",
        "go ahead",
        "please do",
        "affirm",
        "ja",
        "jawohl",
        "si",
        "sí",
        "vale",
        "por favor",
    }


def _is_negative_i18n(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(
        token in lower
        for token in (
            "no",
            "nope",
            "nah",
            "cancel",
            "cancelar",
            "nevermind",
            "never mind",
            "stop",
            "nein",
            "abbrechen",
            "nicht",
        )
    )

def _password_reset_prompt() -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            "Sure, I can help you reset your password in Azure Active Directory (Microsoft Entra ID).\n"
            "Please tell me your username (UPN or email address)."
        ),
    }

def _password_reset_not_found() -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": "I couldn't find this username in Azure Active Directory. Please check and try again.",
    }

def _password_reset_email_missing() -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            "I couldn't find a registered recovery email for this account in Microsoft Entra ID. "
            "Please update your authentication methods and try again."
        ),
    }

def _password_reset_confirm(upn: str, email: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            "I've verified your account in Microsoft Entra ID.\n\n"
            f"Username: {upn}\n\n"
            "For security reasons, Microsoft manages password resets directly.\n"
            "A password reset link will be sent to the registered recovery email address:\n"
            f"{email}\n\n"
            "Are you happy to continue?"
        ),
    }

def _password_reset_redirect(email: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            "Great. Redirecting you to Microsoft's secure password reset flow.\n\n"
            "https://passwordreset.microsoftonline.com\n\n"
            "Microsoft will now send the password reset verification to your registered email address.\n\n"
            "The password reset process has been initiated successfully.\n"
            f"Please check your email {email} and follow the instructions from Microsoft to complete the reset."
        ),
    }

async def _handle_password_reset_followup(
    text: str, user: Dict[str, Any], sess: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    stage = sess.get("pwd_reset_stage")
    if stage == "awaiting_username":
        upn = (text or "").strip()
        if not upn:
            return _password_reset_prompt()
        if _should_exit_flow(upn):
            sess.pop("pwd_reset_stage", None)
            sess.pop("pwd_reset_upn", None)
            sess.pop("pwd_reset_email", None)
            sess.pop("pwd_reset_misses", None)
            return None
        if not _looks_like_upn(upn):
            misses = int(sess.get("pwd_reset_misses") or 0) + 1
            sess["pwd_reset_misses"] = misses
            if misses >= 2:
                sess.pop("pwd_reset_stage", None)
                sess.pop("pwd_reset_upn", None)
                sess.pop("pwd_reset_email", None)
                sess.pop("pwd_reset_misses", None)
                return {
                    "ok": True,
                    "action": "direct_reply",
                    "text": "No problem. I've reset this flow. How can I help you?",
                }
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "Please tell me your username (UPN or email address), or say cancel.",
            }
        client = _get_graph_client()
        if not client:
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "Password reset is not configured. Please contact your administrator.",
            }
        try:
            user_rec = await client.get_user_by_upn(upn)
        except Exception as exc:
            log.warning("Password reset lookup failed for UPN=%s error=%s", upn, exc)
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "I couldn't verify this username in Microsoft Entra ID right now. Please try again later.",
            }
        if not user_rec:
            return _password_reset_not_found()
        user_upn = (user_rec.get("userPrincipalName") or "").strip()
        if not user_upn or user_upn.lower() != upn.lower():
            return _password_reset_not_found()
        try:
            recovery_email = await client.get_recovery_email(user_rec.get("id") or "")
        except Exception as exc:
            log.warning("Password reset recovery email lookup failed for UPN=%s error=%s", upn, exc)
            recovery_email = None
        if not recovery_email:
            return _password_reset_email_missing()
        sess["pwd_reset_stage"] = "awaiting_consent"
        sess["pwd_reset_upn"] = user_upn
        sess["pwd_reset_email"] = recovery_email
        sess.pop("pwd_reset_misses", None)
        return _password_reset_confirm(user_upn, recovery_email)

    if stage == "awaiting_consent":
        email = (sess.get("pwd_reset_email") or "").strip()
        if _is_affirmative(text):
            sess.pop("pwd_reset_stage", None)
            sess.pop("pwd_reset_upn", None)
            sess.pop("pwd_reset_email", None)
            sess.pop("pwd_reset_misses", None)
            if not email:
                return _password_reset_email_missing()
            log.info(
                "Password reset redirect: Microsoft Entra ID enforces that password resets are always initiated by the end user through the official SSPR flow. This bot verifies identity and assists navigation but does not perform password resets directly."
            )
            return _password_reset_redirect(email)
        if _should_exit_flow(text):
            sess.pop("pwd_reset_stage", None)
            sess.pop("pwd_reset_upn", None)
            sess.pop("pwd_reset_email", None)
            sess.pop("pwd_reset_misses", None)
            return None
        sess.pop("pwd_reset_stage", None)
        sess.pop("pwd_reset_upn", None)
        sess.pop("pwd_reset_email", None)
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Okay. If you want to reset your password later, just ask.",
        }

    return None


async def _handle_intune_restart_followup(
    text: str, user: Dict[str, Any], sess: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if not sess.get("intune_restart_pending"):
        return None
    if _should_exit_flow(text):
        sess.pop("intune_restart_pending", None)
        sess.pop("intune_restart_device_id", None)
        sess.pop("intune_restart_device_name", None)
        return {"ok": True, "action": "direct_reply", "text": "Okay, I won't restart the device."}

    if _is_affirmative(text):
        device_id = (sess.get("intune_restart_device_id") or "").strip()
        device_name = (sess.get("intune_restart_device_name") or "the device").strip()
        client = _get_graph_client()
        if not client:
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "Microsoft Graph is not configured for Intune actions. Please contact your administrator.",
            }
        try:
            ok, err = await client.restart_managed_device(device_id)
        except Exception as exc:
            log.warning("Intune restart failed for device=%s error=%s", device_id, exc)
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "I couldn't restart that device right now. Please try again later.",
            }
        sess.pop("intune_restart_pending", None)
        sess.pop("intune_restart_device_id", None)
        sess.pop("intune_restart_device_name", None)
        if ok:
            return {
                "ok": True,
                "action": "direct_reply",
                "text": f"Restart initiated for {device_name}.",
            }
        if err:
            return {
                "ok": True,
                "action": "direct_reply",
                "text": f"Restart failed: {err}",
            }
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't restart that device right now. Please try again later.",
        }

    return {
        "ok": True,
        "action": "direct_reply",
        "text": "Please confirm. Say “yes” to restart the device, or “cancel” to stop.",
    }

# ---------- SNOW-backed handlers ----------

async def handle_ticket_create(reason: str, user: Dict[str, Any]) -> Dict[str, Any]:
    client = get_snow()

    caller_id = await _resolve_caller_id(user)
    short = reason or "Ticket creation requested"
    desc = f"Requested by {user.get('user_name') or user.get('user_id') or 'unknown'} via Teams."

    inc_number = await client.create_incident(
        short_description=short,
        description=desc,
        category="inquiry",
        subcategory="general",
        impact="3",
        urgency="3",
        caller_id=caller_id,
    )

    state = ""
    try:
        rec = await client.get_incident(inc_number)
        if rec:
            state = (rec.get("state") or "").strip()
    except Exception:
        state = ""

    return {
        "ok": bool(inc_number),
        "action": "ticket_create",
        "reason": reason,
        "inc_number": inc_number,
        "state": state,
        "processing_hint": _SNOW_PROCESSING_HINT,
        "long_running": True,
    }

async def handle_ticket_status(inc_number: str, user: Dict[str, Any]) -> Dict[str, Any]:
    client = get_snow()
    rec = await client.get_incident(inc_number)
    return {
        "ok": bool(rec),
        "action": "ticket_status",
        "inc_number": inc_number,
        "state": (rec or {}).get("state", ""),
        "short_description": (rec or {}).get("short_description", ""),
        "processing_hint": _SNOW_PROCESSING_HINT,
        "long_running": True,
    }

async def handle_ticket_update_status(
    inc_number: str,
    status: str,
    reason: str,
    user: Dict[str, Any],
) -> Dict[str, Any]:
    code, label = _normalize_ticket_status(status)
    if not inc_number:
        return {
            "ok": False,
            "action": "direct_reply",
            "text": (
                "Please provide the incident number. "
                + _UPDATE_STATUS_HINT
            ),
        }
    if not code:
        return {
            "ok": False,
            "action": "direct_reply",
            "text": (
                "Please provide the exact status: New, In Progress, or On Hold. "
                + _UPDATE_STATUS_HINT
            ),
        }
    if not reason:
        return {
            "ok": False,
            "action": "direct_reply",
            "text": "Please add a short reason for the status update. " + _UPDATE_STATUS_HINT,
        }

    client = get_snow()
    current = await client.get_incident(inc_number)
    if not current:
        return {
            "ok": False,
            "action": "direct_reply",
            "text": f"I couldn't find incident {inc_number}. Please double-check the number.",
        }
    if _snow_state_matches(current.get("state", ""), code, label):
        return {
            "ok": True,
            "action": "direct_reply",
            "text": f"{inc_number} is already {label}. No change was needed.",
        }
    updated = await client.update_incident_status(inc_number, state=code, reason=reason)
    return {
        "ok": bool(updated),
        "action": "ticket_update_status",
        "inc_number": inc_number,
        "status": label,
        "reason": reason,
        "processing_hint": _SNOW_PROCESSING_HINT,
        "long_running": True,
    }

async def handle_password_reset(user: Dict[str, Any]) -> Dict[str, Any]:
    sess = session_for(user)
    sess["pwd_reset_stage"] = "awaiting_username"
    return _password_reset_prompt()

async def handle_vpn_proposal(reason: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Don't auto-create for generic VPN issues; propose a ticket and add tips.
    UI renders Yes/No in bot.py when action == 'propose_ticket'.
    """
    return {
        "ok": True,
        "action": "propose_ticket",
        "reason": reason or "VPN not connecting",
        "tips": vpn_tip_playbook(),
    }

async def handle_ticket_proposal(reason: str, user: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic proposal + how-to tips (used for bare 'open a ticket', etc.).
    """
    return {
        "ok": True,
        "action": "propose_ticket",
        "reason": reason or "an issue",
        "tips": ticket_propose_playbook(),
    }


def _ticket_reason_prompt() -> Dict[str, Any]:
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            "Do you want me to raise a ticket? If yes, please provide a short reason, for example:\n"
            "- \"Raise a ticket: VPN keeps dropping\"\n"
            "- \"Open a ticket for server HP8919 not responding\""
        ),
    }

async def handle_help(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "help", "text": help_playbook()}

async def handle_greeting(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "greeting"}

async def handle_bot_profile(user: Dict[str, Any], text: str = "") -> Dict[str, Any]:
    if _asks_bot_name(text):
        name = settings.BOT_PERSONA_NAME or "Vox AI Service"
        return {"ok": True, "action": "bot_profile", "text": f"I'm {name}."}
    return {"ok": True, "action": "bot_profile", "text": bot_profile_playbook(user)}

async def handle_ticket_howto(user: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "ticket_howto", "text": ticket_howto_playbook()}

async def handle_incident_intel(user: Dict[str, Any], days: int, threshold: int) -> Dict[str, Any]:
    client = get_snow()
    active_only = bool(getattr(settings, "INCIDENT_INTEL_ACTIVE_ONLY", True))
    query_now = build_stats_query(days_start=days, active_only=active_only)
    query_prev = build_stats_query(days_start=days * 2, days_end=days, active_only=active_only)

    current = await client.get_incident_stats(query=query_now, group_by="short_description")
    previous = await client.get_incident_stats(query=query_prev, group_by="short_description")

    if getattr(settings, "FEATURE_LLM_INCIDENT_NORMALIZE", False):
        try:
            counts = raw_issue_counts(current)
            for k, v in raw_issue_counts(previous).items():
                counts[k] = counts.get(k, 0) + v
            ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            max_issues = int(getattr(settings, "INCIDENT_INTEL_LLM_MAX_ISSUES", 50) or 50)
            issues = [i for i, _ in ranked[:max_issues]]

            if issues:
                llm = LLM.auto()
                system_prompt = (
                    "Return ONLY JSON with shape {\"mapping\": {\"<original>\": \"<canonical>\"}}. "
                    "Canonical labels must be short (<=5 words) in Title Case. "
                    "Only map provided strings; if unsure, map to itself."
                )
                user_text = json.dumps(issues, ensure_ascii=False)
                result = await llm.chat_json(system_prompt, user_text, schema=_IssueMap)
                mapping = getattr(result, "mapping", {}) or {}
                if not mapping:
                    try:
                        raw = llm._adapter.call(system_prompt, user_text)  # type: ignore[attr-defined]
                        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if getattr(settings, "INCIDENT_INTEL_DEBUG", False):
                            log.info("INCIDENT_INTEL LLM raw content length=%s", len(content or ""))
                            log.info("INCIDENT_INTEL LLM raw content sample=%s", (content or "")[:800])
                        data = _extract_json_object(content)
                        mapping = _coerce_issue_mapping(data)
                        if not mapping:
                            mapping = _extract_mapping_from_raw(content or "")
                        if not mapping:
                            mapping = _extract_mapping_pairs(content or "")
                    except Exception as exc:
                        if getattr(settings, "INCIDENT_INTEL_DEBUG", False):
                            log.info("INCIDENT_INTEL LLM raw parse failed: %s", exc)
                if mapping:
                    labels = sorted({v for v in mapping.values() if v})
                    if len(labels) > 1:
                        try:
                            label_prompt = (
                                "Group similar labels. Return ONLY JSON {\"mapping\": {\"<label>\": \"<canonical>\"}}. "
                                "Use short Title Case labels. If unsure, map to itself."
                            )
                            label_text = json.dumps(labels, ensure_ascii=False)
                            label_result = await llm.chat_json(label_prompt, label_text, schema=_IssueMap)
                            label_map = getattr(label_result, "mapping", {}) or {}
                            if not label_map:
                                raw = llm._adapter.call(label_prompt, label_text)  # type: ignore[attr-defined]
                                content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
                                data = _extract_json_object(content)
                                label_map = _coerce_issue_mapping(data)
                                if not label_map:
                                    label_map = _extract_mapping_from_raw(content or "")
                                if not label_map:
                                    label_map = _extract_mapping_pairs(content or "")
                            mapping = _apply_label_map(mapping, label_map)
                        except Exception as exc:
                            if getattr(settings, "INCIDENT_INTEL_DEBUG", False):
                                log.info("INCIDENT_INTEL label merge failed: %s", exc)
                if mapping:
                    mapping = {str(k): str(v) for k, v in mapping.items() if k and v}
                if getattr(settings, "INCIDENT_INTEL_DEBUG", False):
                    log.info("INCIDENT_INTEL LLM mapping size=%s", len(mapping))
                    log.info("INCIDENT_INTEL LLM mapping sample=%s", list(mapping.items())[:10])
                apply_issue_map(current, mapping)
                apply_issue_map(previous, mapping)
            elif getattr(settings, "INCIDENT_INTEL_DEBUG", False):
                log.info("INCIDENT_INTEL LLM normalization skipped (no issues)")
        except Exception as exc:
            log.warning("Incident intel LLM normalization failed: %s", exc)

    if getattr(settings, "INCIDENT_INTEL_DEBUG", False):
        log.info(
            "INCIDENT_INTEL debug → days=%s threshold=%s active_only=%s",
            days,
            threshold,
            active_only,
        )
        log.info("INCIDENT_INTEL query_now=%s", query_now)
        log.info("INCIDENT_INTEL query_prev=%s", query_prev)
        log.info("INCIDENT_INTEL current_count=%s prev_count=%s", len(current or []), len(previous or []))
        log.info("INCIDENT_INTEL current_sample=%s", (current or [])[:3])
        log.info("INCIDENT_INTEL prev_sample=%s", (previous or [])[:3])

    insights = compute_insights(current=current, previous=previous, threshold=threshold)
    if len(insights) < 3:
        supplemental = top_issues_from_rows(current, 3)
        existing = {i.issue for i in insights}
        for item in supplemental:
            if item.issue in existing:
                continue
            insights.append(item)
            existing.add(item.issue)
            if len(insights) >= 3:
                break
    insights = [
        item for item in insights
        if re.sub(r"[\W_]+", "", (item.issue or "")).strip()
    ]
    text = summarize_insights(insights, days)
    voice_text = "No repeated incidents detected in the last 30 days."
    if insights:
        top = ", ".join(f"{i.issue} {i.count}" for i in insights[:3])
        voice_text = f"Top recurring issues last {days} days: {top}."
    host = (settings.PUBLIC_BASE_HOST or "").strip()
    public_base = f"https://{host}" if host else ""
    if public_base:
        text = f"{text}\n\nView dashboard: {public_base}/dashboard/incident-intel"
    try:
        total = await client.get_incident_total(query=query_now)
        repeated = sum(i.count for i in insights)
        repeat_rate = round((repeated / total) * 100, 1) if total else 0.0
        candidates = [i for i in insights if i.is_problem_candidate]
        set_cached_response(
            f"incident-intel:{days}",
            {
                "days": days,
                "total_incidents": total,
                "repeated_incidents": repeated,
                "repeat_rate": repeat_rate,
                "problem_candidates": [c.issue for c in candidates],
                "insights": [
                    {
                        "issue": i.issue,
                        "count": i.count,
                        "trend_percent": i.trend_percent,
                        "assignment_group": i.assignment_group,
                        "problem_candidate": i.is_problem_candidate,
                    }
                    for i in insights
                ],
            },
        )
    except Exception:
        pass
    return {
        "ok": True,
        "action": "incident_intel",
        "text": text,
        "voice_text": voice_text,
        "processing_hint": _SNOW_PROCESSING_HINT,
        "long_running": True,
        "dashboard_url": f"{public_base}/dashboard/incident-intel" if public_base else "",
        "insights": [
            {
                "issue": i.issue,
                "count": i.count,
                "trend_percent": i.trend_percent,
                "assignment_group": i.assignment_group,
                "problem_candidate": i.is_problem_candidate,
            }
            for i in insights
        ],
    }


async def handle_intune_device_status(device_name: str, user: Dict[str, Any]) -> Dict[str, Any]:
    device_name = (device_name or "").strip()
    if not device_name:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Please provide the device name you want to check.",
        }
    if device_name.lower() in {"all devices", "all", "everyone", "all device", "all managed devices"}:
        client = _get_graph_client()
        if not client:
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "Microsoft Graph is not configured for Intune lookups. Please contact your administrator.",
            }
        try:
            devices = await client.list_managed_devices(top=10)
        except Exception as exc:
            log.warning("Intune list failed error=%s", exc)
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "I couldn't query Intune right now. Please try again later.",
            }
        if not devices:
            return {"ok": True, "action": "direct_reply", "text": "No managed devices found in Intune."}
        lines = []
        for d in devices:
            name = d.get("deviceName") or "Unknown device"
            compliance = (d.get("complianceState") or "unknown").replace("_", " ")
            last_sync = d.get("lastSyncDateTime") or "unknown"
            os_name = d.get("operatingSystem") or "unknown OS"
            os_version = d.get("osVersion") or ""
            os_label = f"{os_name} {os_version}".strip()
            model = d.get("model") or "unknown model"
            mgmt_state = (d.get("managementState") or "unknown").replace("_", " ")
            lines.append(
                f"- {name}: {compliance}, last check-in {last_sync}, state {mgmt_state}, "
                f"OS {os_label}, model {model}"
            )
    return {
        "ok": True,
        "action": "direct_reply",
        "source": "intune",
        "text": "Top managed devices (latest 10):\n" + "\n".join(lines),
    }


async def handle_intune_device_count(user: Dict[str, Any]) -> Dict[str, Any]:
    is_voice = (user.get("channel") or "").lower() == "voice"
    hint = t("hold_intune", "en") if is_voice else ""
    client = _get_graph_client()
    if not client:
        result = {
            "ok": True,
            "action": "direct_reply",
            "text": "Microsoft Graph is not configured for Intune lookups. Please contact your administrator.",
        }
        if is_voice:
            result["processing_hint"] = hint
            result["long_running"] = True
        return result
    try:
        count, by_os = await client.count_managed_devices_by_os()
    except Exception as exc:
        log.warning("Intune count failed error=%s", exc)
        result = {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't query Intune right now. Please try again later.",
        }
        if is_voice:
            result["processing_hint"] = hint
            result["long_running"] = True
        return result
    if by_os:
        os_parts = []
        for os_name, os_count in sorted(by_os.items(), key=lambda kv: kv[0].lower()):
            os_parts.append(f"{os_name}: {os_count}")
        os_summary = "; ".join(os_parts)
        text = f"Total managed devices in Intune: {count}. By OS: {os_summary}."
    else:
        text = f"Total managed devices in Intune: {count}."
    result = {
        "ok": True,
        "action": "direct_reply",
        "source": "intune",
        "text": text,
    }
    if is_voice:
        result["processing_hint"] = hint
        result["long_running"] = True
    return result


async def handle_user_count(user: Dict[str, Any]) -> Dict[str, Any]:
    client = _get_graph_client()
    if not client:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Microsoft Graph is not configured for Entra user lookups. Please contact your administrator.",
        }
    try:
        count = await client.count_users()
    except Exception as exc:
        log.warning("Graph user count failed error=%s", exc)
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't query Entra right now. Please try again later.",
        }
    return {
        "ok": True,
        "action": "direct_reply",
        "text": f"There are {count} users registered in Microsoft Entra ID (Azure AD).",
    }
    client = _get_graph_client()
    if not client:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Microsoft Graph is not configured for Intune lookups. Please contact your administrator.",
        }
    try:
        devices = await client.get_managed_devices_by_name(device_name)
    except Exception as exc:
        log.warning("Intune lookup failed for device=%s error=%s", device_name, exc)
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't query Intune right now. Please try again later.",
        }
    if not devices:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": f"I couldn't find a managed device named {device_name}.",
        }

    if len(devices) == 1:
        d = devices[0]
        name = d.get("deviceName") or device_name
        compliance = (d.get("complianceState") or "unknown").replace("_", " ")
        last_sync = d.get("lastSyncDateTime") or "unknown"
        os_name = d.get("operatingSystem") or "unknown OS"
        os_version = d.get("osVersion") or ""
        model = d.get("model") or "unknown model"
        mgmt_state = (d.get("managementState") or "unknown").replace("_", " ")
        reg_state = (d.get("deviceRegistrationState") or "unknown").replace("_", " ")
        return {
            "ok": True,
            "action": "direct_reply",
            "source": "intune",
            "text": (
                f"Intune device status for {name} — Compliance: {compliance}. "
                f"Last check-in: {last_sync}. State: {mgmt_state} "
                f"(registration: {reg_state}). OS: {os_name} {os_version}. "
                f"Model: {model}. "
                "To see assigned user/ownership, open the device in Intune."
            ).strip(),
        }

    lines = []
    for d in devices[:3]:
        name = d.get("deviceName") or device_name
        compliance = (d.get("complianceState") or "unknown").replace("_", " ")
        last_sync = d.get("lastSyncDateTime") or "unknown"
        os_name = d.get("operatingSystem") or "unknown OS"
        os_version = d.get("osVersion") or ""
        os_label = f"{os_name} {os_version}".strip()
        model = d.get("model") or "unknown model"
        mgmt_state = (d.get("managementState") or "unknown").replace("_", " ")
        lines.append(
            f"- {name}: {compliance}, last check-in {last_sync}, state {mgmt_state}, "
            f"OS {os_label}, model {model}"
        )
    return {
        "ok": True,
        "action": "direct_reply",
        "source": "intune",
        "text": "I found multiple devices with that name:\n" + "\n".join(lines),
    }


async def handle_intune_device_restart(device_name: str, user: Dict[str, Any]) -> Dict[str, Any]:
    device_name = (device_name or "").strip()
    if not device_name:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Please provide the device name you want to restart.",
        }
    if device_name.lower() in {"all devices", "all", "everyone", "all device", "all managed devices"}:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "For safety, I can only restart a single named device at a time.",
        }
    client = _get_graph_client()
    if not client:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Microsoft Graph is not configured for Intune actions. Please contact your administrator.",
        }
    try:
        devices = await client.get_managed_devices_by_name(device_name)
    except Exception as exc:
        log.warning("Intune restart lookup failed for device=%s error=%s", device_name, exc)
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't query Intune right now. Please try again later.",
        }
    if not devices:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": f"I couldn't find a managed device named {device_name}.",
        }
    if len(devices) > 1:
        lines = []
        for d in devices[:3]:
            name = d.get("deviceName") or device_name
            last_sync = d.get("lastSyncDateTime") or "unknown"
            os_name = d.get("operatingSystem") or "unknown OS"
            os_version = d.get("osVersion") or ""
            os_label = f"{os_name} {os_version}".strip()
            model = d.get("model") or "unknown model"
            mgmt_state = (d.get("managementState") or "unknown").replace("_", " ")
            lines.append(
                f"- {name}: last check-in {last_sync}, state {mgmt_state}, "
                f"OS {os_label}, model {model}"
            )
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I found multiple devices with that name. Please confirm the exact device:\n"
            + "\n".join(lines),
        }
    device = devices[0]
    device_id = (device.get("id") or "").strip()
    resolved_name = device.get("deviceName") or device_name
    os_name = (device.get("operatingSystem") or "").strip()
    if os_name and "windows" not in os_name.lower():
        return {
            "ok": True,
            "action": "direct_reply",
            "text": f"Restart is not supported for {os_name} devices. Please pick a Windows device.",
        }
    if not device_id:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't resolve the device ID needed for a restart.",
        }
    sess = session_for(user)
    sess["intune_restart_pending"] = True
    sess["intune_restart_device_id"] = device_id
    sess["intune_restart_device_name"] = resolved_name
    return {
        "ok": True,
        "action": "direct_reply",
        "text": (
            f"I can restart {resolved_name}. This will interrupt the user. "
            "Should I proceed?"
        ),
    }


async def handle_intune_device_apps(device_name: str, user: Dict[str, Any]) -> Dict[str, Any]:
    device_name = (device_name or "").strip()
    if not device_name:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Please provide the device name you want to check.",
        }
    client = _get_graph_client()
    if not client:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "Microsoft Graph is not configured for Intune lookups. Please contact your administrator.",
        }
    try:
        devices = await client.get_managed_devices_by_name(device_name)
    except Exception as exc:
        log.warning("Intune app lookup failed for device=%s error=%s", device_name, exc)
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't query Intune right now. Please try again later.",
        }
    if not devices:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": f"I couldn't find a managed device named {device_name}.",
        }
    if len(devices) > 1:
        lines = []
        for d in devices[:3]:
            name = d.get("deviceName") or device_name
            last_sync = d.get("lastSyncDateTime") or "unknown"
            os_name = d.get("operatingSystem") or "unknown OS"
            os_version = d.get("osVersion") or ""
            os_label = f"{os_name} {os_version}".strip()
            model = d.get("model") or "unknown model"
            lines.append(f"- {name}: last check-in {last_sync}, OS {os_label}, model {model}")
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I found multiple devices with that name. Please confirm the exact device:\n"
            + "\n".join(lines),
        }

    device = devices[0]
    device_id = (device.get("id") or "").strip()
    resolved_name = device.get("deviceName") or device_name
    if not device_id:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't resolve the device ID needed to list apps.",
        }
    try:
        apps = await client.list_detected_apps(device_id, top=50)
    except Exception as exc:
        log.warning("Intune detected apps failed for device=%s error=%s", device_id, exc)
        message = str(exc) or ""
        if "detected apps requires" in message or "detected apps" in message.lower():
            return {
                "ok": True,
                "action": "direct_reply",
                "text": (
                    "Installed apps per device are only available via Microsoft Graph beta "
                    "and may not be enabled for this tenant."
                ),
            }
        return {
            "ok": True,
            "action": "direct_reply",
            "text": "I couldn't retrieve installed apps right now. Please try again later.",
        }
    if not apps:
        return {
            "ok": True,
            "action": "direct_reply",
            "text": f"No detected apps found for {resolved_name}.",
        }
    lines = []
    for app in apps:
        name = app.get("displayName") or "Unknown app"
        version = app.get("version") or ""
        publisher = app.get("publisher") or ""
        details = ", ".join(part for part in (version, publisher) if part)
        lines.append(f"- {name}" + (f" ({details})" if details else ""))
    return {
        "ok": True,
        "action": "direct_reply",
        "source": "intune",
        "text": f"Installed apps for {resolved_name} (top {min(len(lines), 50)}):\n" + "\n".join(lines[:50]),
    }

# ---------- legacy fallback ----------

async def legacy_route(text: str, user: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "action": "direct_reply", "text": _FALLBACK_TEXT}

# ---------- Orchestrator ----------

class Orchestrator:
    """
    LLM-first router with graceful fallback to legacy rules.
    Also supports simple command-style messages from buttons:
      - "create_ticket:<reason>"
      - "cancel_ticket"
    """
    def __init__(self):
        self.use_llm = _env_bool("FEATURE_LLM_ROUTER", False)
        self.use_llm_router = self.use_llm

        if self.use_llm:
            llm = LLM.auto()
            self.router = LLMIntentRouter(llm)
        else:
            self.router = None

    async def handle(self, text: str, user: Dict[str, Any]) -> Dict[str, Any]:
        sess = session_for(user)
        raw_text = text or ""
        user_text = _extract_user_query(raw_text)
        text = user_text
        now = time.time()
        last_seen = float(sess.get("last_active_ts") or 0)
        if last_seen and (now - last_seen) > _SESSION_TIMEOUT_SECS:
            sess.clear()
        sess["last_active_ts"] = now
        log.info(
            "INTENT TRACE → text=%s | user_text=%s | integration_q=%s | capability_q=%s",
            raw_text,
            user_text,
            _is_integration_question(user_text),
            _is_capability_question(user_text),
        )

        interrupt = _interrupt_action(text)
        if interrupt:
            sess.clear()
            if interrupt == "help":
                return await handle_help(user=user)
            return {"ok": True, "action": "direct_reply", "text": "Okay. How can I help you?"}

        if _is_log_path_request(text):
            path = (sess.get("last_validation_path") or "").strip()
            if path:
                return {
                    "ok": True,
                    "action": "direct_reply",
                    "text": f"Full report saved at: {path}",
                }
            return {
                "ok": True,
                "action": "direct_reply",
                "text": "I don't have a recent validation report yet.",
            }

        pending_update = sess.get("pending_status_update")
        if pending_update:
            if _is_affirmative_i18n(text):
                sess.pop("pending_status_update", None)
                return await handle_ticket_update_status(
                    inc_number=pending_update.get("inc_number", ""),
                    status=pending_update.get("status", ""),
                    reason=pending_update.get("reason", ""),
                    user=user,
                )
            if _is_negative_i18n(text):
                sess.pop("pending_status_update", None)
                return {"ok": True, "action": "direct_reply", "text": "Okay, I won't update that incident."}
            if _should_cancel_pending_status_update(text) or _should_exit_flow(text):
                sess.pop("pending_status_update", None)
                pending_update = None
            if pending_update is not None:
                return _confirm_status_update_prompt(
                    pending_update.get("inc_number", "the incident"),
                    pending_update.get("status_label", "that status"),
                )

        followup = await _handle_password_reset_followup(text, user, sess)
        if followup:
            return followup

        followup = await _handle_intune_restart_followup(text, user, sess)
        if followup:
            return followup

        # 1) Button/command short-circuits
        lower = (text or "").strip().lower()

        if lower.startswith("create_ticket:"):
            reason = text.split(":", 1)[1].strip() if ":" in text else ""
            return await handle_ticket_create(reason=reason, user=user)

        if lower.startswith("cancel_ticket"):
            return {"ok": True, "action": "help", "text": help_playbook()}

        explicit_create, extracted_reason = detect_ticket_create(text)
        if explicit_create:
            pending_reason = sess.get("pending_ticket_reason", "")
            final_reason = extracted_reason or pending_reason
            if final_reason:
                result = await handle_ticket_create(reason=final_reason, user=user)
                sess.pop("pending_ticket_reason", None)
                return result
            return await handle_ticket_proposal(reason="", user=user)

        if _is_intune_count_request(text):
            return await handle_intune_device_count(user=user)

        if _is_user_count_request(text):
            return await handle_user_count(user=user)

        if _is_integration_question(text) and not _is_capability_question(text):
            log.info("INTENT TRACE → integration short-circuit")
            integration_reply = await _maybe_integration_reply(text)
            if integration_reply:
                return {"ok": True, "action": "direct_reply", "text": integration_reply}

        smalltalk = _smalltalk_response(text)
        if smalltalk:
            return smalltalk

        offline = await _offline_general_response(text)
        if offline:
            return offline

        # 2) Normal router flow (LLM) with safe fallback
        if self.use_llm and self.router:
            try:
                res: IntentResult = await self.router.classify(text)
                intent = (res.intent or "other").strip().lower()
                log.info("INTENT TRACE → llm_intent=%s reason=%s", intent, getattr(res, "reason", ""))

                if intent == "ticket_create":
                    # Only create when user explicitly asked to open/create/raise a ticket.
                    reason = (res.reason or "").strip()
                    explicit, extracted_reason = detect_ticket_create(text)
                    if not explicit:
                        _remember_ticket_reason(sess, reason, text)
                        pending_reason = sess.get("pending_ticket_reason", "")
                        proposal_reason = "" if _is_generic_reason(reason) else reason
                        if not proposal_reason and pending_reason:
                            proposal_reason = pending_reason
                        return await handle_ticket_proposal(reason=proposal_reason, user=user)
                    final_reason = reason or extracted_reason
                    if _is_generic_reason(final_reason):
                        final_reason = ""
                    if final_reason:
                        result = await handle_ticket_create(reason=final_reason, user=user)
                        sess.pop("pending_ticket_reason", None)
                        return result
                    if (user.get("channel") or "").lower() == "voice":
                        return _ticket_reason_prompt()
                    return await handle_ticket_proposal(reason="", user=user)

                if intent == "ticket_status":
                    inc_number = _extract_inc_from_text(text) or (res.inc_number or "")
                    if not inc_number:
                        return _clarify_incident_request("status")
                    return await handle_ticket_status(inc_number=inc_number, user=user)

                if intent == "ticket_update_status":
                    inc_number = _extract_inc_from_text(text) or (res.inc_number or "")
                    if not inc_number:
                        return _clarify_incident_request("update")
                    status_value = getattr(res, "status", "") or ""
                    code, label = _normalize_ticket_status(status_value)
                    if not code:
                        return _clarify_status_request()
                    if (user.get("channel") or "").lower() == "voice":
                        sess["pending_status_update"] = {
                            "inc_number": inc_number,
                            "status": status_value,
                            "status_label": label,
                            "reason": res.reason or "",
                        }
                        return _confirm_status_update_prompt(inc_number, label)
                    return await handle_ticket_update_status(
                        inc_number=inc_number,
                        status=status_value,
                        reason=res.reason,
                        user=user,
                    )

                if intent == "sop_latest":
                    info = get_latest_sop_info()
                    if not info.get("ok"):
                        return {
                            "ok": True,
                            "action": "direct_reply",
                            "text": "I don't have an active SOP yet. Upload one by saying “upload sop.”",
                        }
                    title = info.get("sop_title") or info.get("sop_id") or "SOP"
                    details = [f"Latest SOP (active): {title}"]
                    if info.get("filename"):
                        details.append(f"Document: {info['filename']}")
                    if info.get("sop_id"):
                        details.append(f"Version/ID: {info['sop_id']}")
                    if info.get("uploaded_at"):
                        details.append(f"Uploaded: {info['uploaded_at']}")
                    if info.get("uploaded_by"):
                        details.append(f"Uploaded by: {info['uploaded_by']}")
                    if info.get("sop_raw_path"):
                        details.append(f"Raw path: {info['sop_raw_path']}")
                    if info.get("sop_json_path"):
                        details.append(f"JSON path: {info['sop_json_path']}")
                    details.append(
                        f"Active pointer: {info.get('current_path','lab-sops/current.json')}"
                    )
                    return {"ok": True, "action": "direct_reply", "text": "\n".join(details)}

                if intent == "sop_validate":
                    transcript = _extract_sop_transcript(text)
                    if not transcript:
                        return {"ok": True, "action": "direct_reply", "text": _SOP_VALIDATE_HINT}
                    sop = load_sop_json()
                    if not sop:
                        return {
                            "ok": True,
                            "action": "direct_reply",
                            "text": "I don't have an active SOP yet. Upload one by saying “upload sop.”",
                        }
                    step_limit = _extract_sop_step_limit(text)
                    if step_limit:
                        sop = limit_sop_steps(sop, step_limit)
                    scope = _extract_sop_scope(text)
                    try:
                        validation_result = await validate_transcript_against_sop(
                            sop=sop,
                            transcript=transcript,
                            scope=scope,
                        )
                        validation_path = await asyncio.to_thread(
                            store_validation_result,
                            result=validation_result,
                            transcript=transcript,
                            user=user.get("user_email") or user.get("user_name") or user.get("user_id") or "",
                            duration_seconds=0,
                            timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        )
                        return {
                            "ok": True,
                            "action": "direct_reply",
                            "text": _format_sop_validation(validation_result, validation_path),
                        }
                    except Exception as exc:
                        log.warning("SOP validation failed: %s", exc)
                        return {
                            "ok": True,
                            "action": "direct_reply",
                            "text": "I couldn't validate that note right now. Please try again.",
                        }

                if intent == "sop_upload":
                    return {"ok": True, "action": "sop_upload_prompt"}

                if intent == "password_reset":
                    return await handle_password_reset(user=user)

                if intent == "vpn":
                    # Keep VPN-specific proposal (with VPN tips)
                    _remember_ticket_reason(sess, res.reason, text)
                    return await handle_vpn_proposal(reason=res.reason, user=user)

                if intent == "bot_profile":
                    if _is_integration_question(text):
                        integration_reply = await _maybe_integration_reply(text)
                        if integration_reply:
                            return {"ok": True, "action": "direct_reply", "text": integration_reply}
                    capability_reply = await _maybe_capability_reply(text)
                    if capability_reply:
                        return {"ok": True, "action": "direct_reply", "text": capability_reply}
                    integration_reply = await _maybe_integration_reply(text)
                    if integration_reply:
                        return {"ok": True, "action": "direct_reply", "text": integration_reply}
                    return await handle_bot_profile(user=user, text=text)

                if intent == "ticket_howto":
                    if any(k in lower for k in ("change status", "update status", "set status")):
                        return {"ok": True, "action": "direct_reply", "text": _UPDATE_STATUS_HINT}
                    return await handle_ticket_howto(user=user)

                if intent == "incident_intel":
                    days = int(getattr(settings, "INCIDENT_INTEL_DAYS", 30) or 30)
                    threshold = int(getattr(settings, "INCIDENT_INTEL_THRESHOLD", 2) or 2)
                    return await handle_incident_intel(user=user, days=days, threshold=threshold)

                if intent == "intune_device_status":
                    if _is_intune_count_request(text):
                        return await handle_intune_device_count(user=user)
                    device_name = _extract_device_name(text, res.reason)
                    return await handle_intune_device_status(device_name=device_name, user=user)

                if intent == "intune_device_restart":
                    device_name = _extract_device_name(text, res.reason)
                    return await handle_intune_device_restart(device_name=device_name, user=user)

                if intent == "intune_device_apps":
                    device_name = _extract_device_name(text, res.reason)
                    return await handle_intune_device_apps(device_name=device_name, user=user)

                if intent == "language_set":
                    code, label = _extract_voice_language(text, res.reason)
                    if not code:
                        return {
                            "ok": True,
                            "action": "direct_reply",
                            "text": "Please choose English, Spanish, or German.",
                        }
                    if (user.get("channel") or "").lower() == "voice":
                        return {
                            "ok": True,
                            "action": "direct_reply",
                            "text": f"Speech language set to {label}.",
                            "voice_lang": code,
                        }
                    return {
                        "ok": True,
                        "action": "direct_reply",
                        "text": f"Language set to {label}. In chat, you can also use /language es or /language de.",
                    }

                if intent == "help":
                    return await handle_help(user=user)

                if intent == "repeat_last":
                    return {"ok": True, "action": "repeat_last"}

                if intent == "integration":
                    if _is_capability_question(text):
                        return await handle_help(user=user)
                    reply = await _llm_short_reply(text, _INTEGRATION_SYSTEM_PROMPT)
                    if reply:
                        return {"ok": True, "action": "direct_reply", "text": reply}
                    if INTEGRATION_SUMMARY:
                        return {"ok": True, "action": "direct_reply", "text": INTEGRATION_SUMMARY}
                    return await handle_bot_profile(user=user, text=text)

                if intent == "greeting":
                    return await handle_greeting(user=user)

                if intent == "other":
                    integration_reply = await _maybe_integration_reply(text)
                    if integration_reply:
                        return {"ok": True, "action": "direct_reply", "text": integration_reply}
                    if _is_user_count_request(text):
                        return await handle_user_count(user=user)
                    if _is_intune_count_request(text):
                        return await handle_intune_device_count(user=user)
                    if any(k in lower for k in ("change status", "update status", "set status")):
                        return {"ok": True, "action": "direct_reply", "text": _UPDATE_STATUS_HINT}
                    if any(
                        w in lower
                        for w in (
                            "intune",
                            "compliance",
                            "device compliance",
                            "compliance report",
                            "last check-in",
                            "check-in",
                            "check in",
                            "installed apps",
                            "list of apps",
                            "list apps",
                            "apps on",
                            "applications on",
                        )
                    ):
                        if any(
                            w in lower
                            for w in (
                                "installed apps",
                                "list of apps",
                                "list apps",
                                "apps on",
                                "applications on",
                            )
                        ):
                            device_name = _extract_device_name(text, "")
                            return await handle_intune_device_apps(device_name=device_name, user=user)
                        device_name = "all devices" if "all" in lower else _extract_device_name(text, "")
                        return await handle_intune_device_status(device_name=device_name, user=user)
                    general = await _general_llm_reply(text)
                    if general:
                        return {"ok": True, "action": "direct_reply", "text": general}
                    if any(w in lower for w in ("recurring", "repeated", "trends", "problems to focus", "focus on this month")):
                        days = int(getattr(settings, "INCIDENT_INTEL_DAYS", 30) or 30)
                        threshold = int(getattr(settings, "INCIDENT_INTEL_THRESHOLD", 2) or 2)
                        return await handle_incident_intel(user=user, days=days, threshold=threshold)
                    if any(w in lower for w in ("ticket", "incident", "case")):
                        return await handle_ticket_proposal(reason="", user=user)
                    return await handle_help(user=user)

            except Exception:
                # Any LLM/parse issue → legacy passthrough
                pass

        if self.use_llm:
            fallback_reply = await _general_llm_reply(text)
            if fallback_reply:
                return {"ok": True, "action": "direct_reply", "text": fallback_reply}

        # 3) Legacy behavior
        return await legacy_route(text, user)
