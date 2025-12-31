from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import io
import json
import re
from typing import Any, Optional

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient
from pydantic import BaseModel, Field, ValidationError

from config.settings import settings
from core.llm.client import LLM
import logging

log = logging.getLogger("app")


@dataclass
class ValidationIssue:
    step: str
    expected: str
    observed: str
    severity: str


@dataclass
class ValidationResult:
    status: str
    issues: list[ValidationIssue]
    sop_id: str = ""
    sop_title: str = ""
    transcript_steps: list[dict[str, Any]] | None = None
    compliance_percent: float | None = None
    checklist: dict[str, Any] | None = None
    matched_ids: list[str] | None = None
    missing_ids: list[str] | None = None
    evidence: dict[str, str] | None = None


class TranscriptStep(BaseModel):
    action: str = Field(default="")
    parameters: dict[str, Any] = Field(default_factory=dict)


class TranscriptSteps(BaseModel):
    steps: list[TranscriptStep] = Field(default_factory=list)

class ChecklistStep(BaseModel):
    id: str = ""
    action: str = ""
    keywords: list[str] = Field(default_factory=list)
    required: bool = True
    procedural: bool = True


class ChecklistSection(BaseModel):
    name: str = ""
    steps: list[ChecklistStep] = Field(default_factory=list)


class SopChecklist(BaseModel):
    sop_id: str = ""
    title: str = ""
    sections: list[ChecklistSection] = Field(default_factory=list)


class ChecklistMatch(BaseModel):
    matched_ids: list[str] = Field(default_factory=list)
    missing_ids: list[str] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)


_CHECKLIST_PROMPT = (
    "Convert the SOP steps into an executable checklist JSON.\n"
    "Input is a list of SOP step strings (may include headings/metadata).\n"
    "Rules:\n"
    "- Extract ONLY procedural actions that a lab assistant can perform.\n"
    "- Ignore titles, purpose/scope/definitions, or administrative headings.\n"
    "- Group actions into named sections (e.g., Phase I Investigation).\n"
    "- Each step must have a stable id (snake_case), an action, keywords, and required=true unless optional.\n"
    "- Keep actions short and specific (verb + object).\n"
    "Return JSON with shape: {\"sop_id\":\"...\",\"title\":\"...\",\"sections\":[{\"name\":\"...\",\"steps\":[{\"id\":\"...\",\"action\":\"...\",\"keywords\":[...],\"required\":true}]}]}\n"
)

_MATCH_PROMPT = (
    "You are validating a lab transcript against a checklist.\n"
    "Given the checklist JSON and the transcript, return which checklist step ids are matched.\n"
    "Rules:\n"
    "- Match semantically; use keywords as hints.\n"
    "- Only mark a step matched if the transcript clearly indicates it was performed.\n"
    "- Return missing_ids for required steps that were not performed.\n"
    "- Provide short evidence snippets from the transcript for matched ids when possible.\n"
    "Return JSON with shape: {\"matched_ids\":[...],\"missing_ids\":[...],\"evidence\":{\"id\":\"snippet\"}}\n"
)

_SOP_CHECKLIST_CACHE: dict[str, SopChecklist] = {}


_TRANSCRIPT_TO_STEPS_PROMPT = (
    "Convert the lab transcript into ordered structured steps.\n"
    "Return JSON with shape: {\"steps\": [{\"action\": \"...\", \"parameters\": {}}]}\n"
    "Rules:\n"
    "- Use short action verbs (e.g., \"Adjust temperature\", \"Observe sample\").\n"
    "- Extract numeric values for parameters when possible.\n"
    "- Keep units in parameter names (e.g., temperature_celsius, duration_minutes, wavelength_nm).\n"
    "- Only include steps that are explicitly mentioned.\n"
)

_SOP_GENERATION_PROMPT = (
    "Generate a lab SOP in structured JSON format.\n"
    "Output must match this schema:\n"
    "{\n"
    "  \"sop_id\": \"SOP-LAB-001\",\n"
    "  \"title\": \"Sample Preparation and Analysis SOP\",\n"
    "  \"steps\": [\n"
    "    {\"step_id\": 1, \"action\": \"Prepare sample\", \"parameters\": {}}\n"
    "  ]\n"
    "}\n"
    "Rules:\n"
    "- Only include procedural steps and actions.\n"
    "- Ignore document metadata or headers (title, purpose, scope, definitions, SOP ID, version, effective date, references).\n"
    "- Use ordered steps with step_id starting at 1.\n"
    "- Include parameters with measurable constraints (min/max, range, or allowed values).\n"
    "- If the document does not contain procedural steps, return an empty steps array.\n"
    "- No narrative prose outside JSON.\n"
)


_SOP_METADATA_HINTS = (
    "standard operating procedure",
    "operating procedure",
    "handling of",
    "out-of-specification",
    "out of specification",
    "oos",
    "oot",
    "gmp",
    "fda",
    "cfr",
    "sop id",
    "version",
    "effective date",
    "controlled document",
    "purpose",
    "scope",
    "definitions",
    "references",
    "table of contents",
    "glossary",
)


_SOP_VERB_HINTS = (
    "prepare",
    "adjust",
    "set",
    "observe",
    "measure",
    "record",
    "document",
    "store",
    "label",
    "verify",
    "inspect",
    "clean",
    "calibrate",
    "mix",
    "add",
    "remove",
    "incubate",
    "repeat",
)


def _looks_like_metadata(line: str) -> bool:
    lowered = line.strip().lower()
    if not lowered:
        return True
    return any(hint in lowered for hint in _SOP_METADATA_HINTS)


def _looks_like_step(line: str) -> bool:
    raw = line.strip()
    if not raw:
        return False
    stripped = re.sub(r"^[0-9]+[).\-:\s]+", "", raw)
    stripped = re.sub(r"^[\-•\*]+\s*", "", stripped).strip()
    if not stripped:
        return False
    if _looks_like_metadata(stripped.lower()):
        return False
    if re.match(r"^[0-9]+[).\-:\s]+", raw):
        return True
    if re.match(r"^[\-•\*]+\s*", raw):
        return True
    return any(stripped.lower().startswith(verb + " ") for verb in _SOP_VERB_HINTS)


def _clean_sop_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for step in steps:
        action = (step.get("action") or "").strip()
        if not action:
            continue
        if _looks_like_metadata(action):
            continue
        cleaned.append(step)
    return cleaned


def _fallback_sop_from_text(raw_text: str) -> dict[str, Any]:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    steps: list[dict[str, Any]] = []
    for line in lines:
        cleaned = re.sub(r"^[0-9]+[).\-:\s]+", "", line)
        cleaned = re.sub(r"^[\-•\*]+\s*", "", cleaned)
        if not cleaned:
            continue
        if not _looks_like_step(cleaned):
            continue
        steps.append({"step_id": len(steps) + 1, "action": cleaned, "parameters": {}})
    return {
        "sop_id": "",
        "title": "Uploaded SOP",
        "steps": steps,
    }


class SopStep(BaseModel):
    step_id: int = 0
    action: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class SopSchema(BaseModel):
    sop_id: str = ""
    title: str = ""
    steps: list[SopStep] = Field(default_factory=list)


@dataclass
class SopUploadResult:
    ok: bool
    sop_id: str = ""
    sop_title: str = ""
    sop_json_path: str = ""
    sop_raw_path: str = ""
    error: str = ""


def _parse_timestamp(ts: str) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _safe_user_tag(user: str) -> str:
    if not user:
        return "unknown"
    base = user.split("@")[0]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned or "unknown"


def _build_validation_blob_path(user: str, ts: datetime) -> str:
    date_path = ts.strftime("%Y/%m/%d")
    file_name = ts.strftime("%Y%m%dT%H%M%S") + ".json"
    prefix = (settings.LAB_VALIDATION_BLOB_PREFIX or "lab-validations").strip("/")
    return f"{prefix}/{date_path}/{_safe_user_tag(user)}/{file_name}"


def _extract_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            return float(match.group(0))
    return None


def _normalize_action(action: str) -> str:
    return re.sub(r"\s+", " ", (action or "").strip().lower())

def _slugify_action(action: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (action or "").strip().lower())
    base = base.strip("_")
    return base or "step"

_CHECKLIST_VERB_HINTS = (
    "verify",
    "review",
    "notify",
    "stop",
    "obtain",
    "approve",
    "document",
    "record",
    "investigate",
)

def _looks_like_checklist_action(line: str) -> bool:
    raw = (line or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if _looks_like_metadata(lowered):
        return False
    if lowered in {"documentation", "procedure", "scope", "purpose", "definitions", "references", "capa"}:
        return False
    if lowered.startswith("this sop defines"):
        return False
    if lowered.startswith("responsibilities"):
        return False
    if re.match(r"^(qc analyst|qc supervisor|qa)\s*:", lowered):
        return False
    if any(hint in lowered for hint in _CHECKLIST_VERB_HINTS):
        return True
    if " must " in lowered or " should " in lowered:
        return True
    return False

def _extract_keywords(action: str, max_terms: int = 8) -> list[str]:
    stop = {
        "the", "and", "or", "to", "of", "for", "in", "on", "a", "an", "is",
        "are", "was", "were", "be", "by", "with", "that", "this", "must",
        "should", "if", "no",
    }
    words = re.findall(r"[a-z0-9]+", (action or "").lower())
    keywords = []
    for w in words:
        if w in stop:
            continue
        if w not in keywords:
            keywords.append(w)
        if len(keywords) >= max_terms:
            break
    return keywords

def _strip_validation_prefix(text: str) -> str:
    """
    Remove command-like prefixes from user text (chat/voice), keeping only the narrative.
    """
    if not text:
        return text
    lowered = text.lower()
    if "validate against sop" in lowered:
        parts = text.split(":", 1)
        if len(parts) == 2:
            return parts[1].strip()
    return text

def _keyword_match(step: ChecklistStep, transcript: str) -> bool:
    if not step.keywords:
        return False
    lowered = transcript.lower()
    hits = 0
    for kw in step.keywords:
        if kw and kw in lowered:
            hits += 1
    threshold = max(2, len(step.keywords) // 3)
    return hits >= threshold

def _evidence_snippet(transcript: str, keyword: str, window: int = 120) -> str:
    if not keyword:
        return ""
    lowered = transcript.lower()
    idx = lowered.find(keyword.lower())
    if idx == -1:
        return ""
    start = max(0, idx - window // 2)
    end = min(len(transcript), idx + window // 2)
    return transcript[start:end].strip()

def limit_sop_steps(sop: dict[str, Any], max_steps: int | None) -> dict[str, Any]:
    """
    Return a shallow copy of the SOP with steps truncated to the first N entries.
    If max_steps is None or invalid, return the SOP unchanged.
    """
    if not max_steps or max_steps <= 0:
        return sop
    steps = list(sop.get("steps") or [])
    if not steps:
        return sop
    limited = dict(sop)
    limited["steps"] = steps[:max_steps]
    return limited


def load_sop_json() -> Optional[dict[str, Any]]:
    inline = (settings.LAB_SOP_JSON or "").strip()
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError:
            return None

    conn = settings.LAB_NOTES_BLOB_CONNECTION_STRING or ""
    if not conn:
        return None

    container = settings.LAB_SOP_BLOB_CONTAINER or ""
    prefix = (settings.LAB_SOP_BLOB_PREFIX or "lab-sops").strip("/")
    path = (settings.LAB_SOP_BLOB_PATH or "").lstrip("/")
    if not path:
        path = f"{prefix}/current.json"
    if not container or not path:
        return None

    service = BlobServiceClient.from_connection_string(conn)
    try:
        blob = service.get_blob_client(container, path)
        data = blob.download_blob().readall()
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


async def parse_transcript_steps(transcript: str) -> list[dict[str, Any]]:
    llm = LLM.auto()
    result = await llm.chat_json(_TRANSCRIPT_TO_STEPS_PROMPT, transcript, schema=TranscriptSteps)
    return [step.model_dump() for step in result.steps if step.action]


def _checklist_cache_key(sop: dict[str, Any]) -> str:
    sop_id = str(sop.get("sop_id") or "")
    title = str(sop.get("title") or "")
    steps = sop.get("steps") or []
    return f"{sop_id}|{title}|{len(steps)}"


async def build_sop_checklist(sop: dict[str, Any]) -> SopChecklist:
    cache_key = _checklist_cache_key(sop)
    cached = _SOP_CHECKLIST_CACHE.get(cache_key)
    if cached:
        return cached
    llm = LLM.auto()
    steps = [str(s.get("action") or "").strip() for s in (sop.get("steps") or []) if s.get("action")]
    payload = {
        "sop_id": str(sop.get("sop_id") or ""),
        "title": str(sop.get("title") or ""),
        "steps": steps,
    }
    result = await llm.chat_json(_CHECKLIST_PROMPT, json.dumps(payload, ensure_ascii=True), schema=SopChecklist)
    checklist = result
    for section in checklist.sections or []:
        for step in section.steps or []:
            step.procedural = True
    if not getattr(checklist, "sections", None):
        # Heuristic fallback: extract procedural actions directly from SOP steps.
        fallback_steps: list[ChecklistStep] = []
        for step in payload["steps"]:
            if not _looks_like_checklist_action(step):
                continue
            fallback_steps.append(
                ChecklistStep(
                    id=_slugify_action(step),
                    action=step,
                    keywords=_extract_keywords(step),
                    required=True,
                    procedural=True,
                )
            )
        checklist = SopChecklist(
            sop_id=payload["sop_id"],
            title=payload["title"],
            sections=[ChecklistSection(name="Procedure", steps=fallback_steps)],
        )
    _SOP_CHECKLIST_CACHE[cache_key] = checklist
    return checklist


def _flatten_checklist_steps(checklist: SopChecklist) -> list[ChecklistStep]:
    steps: list[ChecklistStep] = []
    for section in checklist.sections or []:
        steps.extend(section.steps or [])
    return steps


async def match_transcript_to_checklist(checklist: SopChecklist, transcript: str) -> ChecklistMatch:
    llm = LLM.auto()
    payload = {
        "checklist": checklist.model_dump(),
        "transcript": transcript,
    }
    result = await llm.chat_json(_MATCH_PROMPT, json.dumps(payload, ensure_ascii=True), schema=ChecklistMatch)
    return result


async def validate_transcript_against_sop(
    *,
    sop: dict[str, Any],
    transcript: str,
    scope: str | None = None,
) -> ValidationResult:
    """
    Validate a raw transcript against the SOP using LLM-derived checklist matching.
    Falls back to step extraction when checklist generation fails.
    """
    try:
        checklist = await build_sop_checklist(sop)
        steps = _flatten_checklist_steps(checklist)
        if scope and scope.lower() == "procedure":
            steps = [step for step in steps if _looks_like_checklist_action(step.action)]
        required_ids = [s.id for s in steps if s.required and s.id]
        if not steps:
            raise ValueError("checklist_empty")
        cleaned = _strip_validation_prefix(transcript)
        match = await match_transcript_to_checklist(checklist, cleaned)
        matched = set(match.matched_ids or [])
        missing_ids = set(match.missing_ids or [])
        evidence = dict(match.evidence or {})
        # Heuristic keyword fallback to reduce false negatives for obvious mentions.
        transcript_text = cleaned
        id_to_step = {s.id: s for s in steps if s.id}
        for step_id in list(missing_ids):
            step = id_to_step.get(step_id)
            if not step:
                continue
            if _keyword_match(step, transcript_text):
                matched.add(step_id)
                missing_ids.discard(step_id)
                if step.keywords and step_id not in evidence:
                    evidence[step_id] = _evidence_snippet(transcript_text, step.keywords[0])
        # Ensure missing includes required not matched.
        for rid in required_ids:
            if rid not in matched:
                missing_ids.add(rid)
        issues: list[ValidationIssue] = []
        for step in steps:
            if step.required and step.id in missing_ids:
                issues.append(
                    ValidationIssue(
                        step=step.action or step.id,
                        expected="step present",
                        observed="missing",
                        severity="major",
                    )
                )
        total_required = len(required_ids)
        matched_required = len([rid for rid in required_ids if rid in matched])
        compliance = round((matched_required / total_required) * 100, 1) if total_required else 100.0
        status = "COMPLIANT" if not issues else "PARTIALLY_COMPLIANT"
        return ValidationResult(
            status=status,
            issues=issues,
            sop_id=str(sop.get("sop_id") or ""),
            sop_title=str(sop.get("title") or ""),
            transcript_steps=None,
            compliance_percent=compliance,
            checklist=checklist.model_dump(),
            matched_ids=sorted(matched),
            missing_ids=sorted(missing_ids),
            evidence=evidence,
        )
    except Exception as exc:
        log.warning("SOP checklist validation failed; falling back to structured steps. error=%s", exc)
        # Fallback to existing structured-step validation
        steps = await parse_transcript_steps(transcript)
        return validate_steps(sop, steps)


def _build_sop_paths(user: str, ts: datetime, filename: str) -> tuple[str, str, str]:
    prefix = (settings.LAB_SOP_BLOB_PREFIX or "lab-sops").strip("/")
    sop_folder = ts.strftime("SOP_%Y%m%dT%H%M%S")
    base = f"{prefix}/{sop_folder}/{_safe_user_tag(user)}"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "sop_document")
    raw_path = f"{base}/raw/{safe_name}"
    sop_json_path = f"{base}/sop.json"
    current_path = f"{prefix}/current.json"
    return raw_path, sop_json_path, current_path


def _extract_text_from_bytes(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # defer import to avoid hard failure if dependency missing
        except Exception:
            return ""
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
        return "\n".join(parts).strip()
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore").strip()


async def generate_sop_json(raw_text: str) -> dict[str, Any]:
    llm = LLM.auto()
    try:
        result = await llm.chat_json(_SOP_GENERATION_PROMPT, raw_text, schema=SopSchema)
        if result.steps:
            payload = result.model_dump()
            payload["steps"] = _clean_sop_steps(payload.get("steps", []))
            if payload["steps"]:
                return payload
    except Exception:
        pass
    fallback = _fallback_sop_from_text(raw_text)
    if not fallback["steps"]:
        raise ValueError("sop_generation_failed")
    return fallback


def store_sop_assets(
    *,
    filename: str,
    data: bytes,
    sop_json: dict[str, Any],
    user: str,
    timestamp_utc: Optional[str],
) -> SopUploadResult:
    conn = settings.LAB_NOTES_BLOB_CONNECTION_STRING or ""
    if not conn:
        return SopUploadResult(ok=False, error="missing_connection_string")

    container = settings.LAB_SOP_BLOB_CONTAINER or "lab-sops"
    ts = _parse_timestamp(timestamp_utc or "")
    raw_path, sop_json_path, current_path = _build_sop_paths(user, ts, filename)

    service = BlobServiceClient.from_connection_string(conn)
    container_client = service.get_container_client(container)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    metadata = {
        "sop_id": str(sop_json.get("sop_id") or ""),
        "sop_title": str(sop_json.get("title") or ""),
        "sop_json_path": sop_json_path,
        "sop_raw_path": raw_path,
        "uploaded_at": ts.isoformat().replace("+00:00", "Z"),
        "filename": filename or "",
        "uploaded_by": user or "",
    }

    raw_blob = container_client.get_blob_client(raw_path)
    raw_blob.upload_blob(
        data,
        overwrite=True,
        content_type="application/octet-stream",
        metadata=metadata,
    )

    sop_blob = container_client.get_blob_client(sop_json_path)
    sop_payload = json.dumps(sop_json, ensure_ascii=True, indent=2).encode("utf-8")
    sop_blob.upload_blob(
        sop_payload,
        overwrite=True,
        content_type="application/json",
        metadata=metadata,
    )

    current_blob = container_client.get_blob_client(current_path)
    current_blob.upload_blob(
        sop_payload,
        overwrite=True,
        content_type="application/json",
        metadata=metadata,
    )

    return SopUploadResult(
        ok=True,
        sop_id=str(sop_json.get("sop_id") or ""),
        sop_title=str(sop_json.get("title") or ""),
        sop_json_path=sop_json_path,
        sop_raw_path=raw_path,
    )


def store_sop_raw(
    *,
    filename: str,
    data: bytes,
    user: str,
    timestamp_utc: Optional[str],
) -> SopUploadResult:
    conn = settings.LAB_NOTES_BLOB_CONNECTION_STRING or ""
    if not conn:
        return SopUploadResult(ok=False, error="missing_connection_string")

    container = settings.LAB_SOP_BLOB_CONTAINER or "lab-sops"
    ts = _parse_timestamp(timestamp_utc or "")
    raw_path, _, _ = _build_sop_paths(user, ts, filename)

    service = BlobServiceClient.from_connection_string(conn)
    container_client = service.get_container_client(container)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    raw_blob = container_client.get_blob_client(raw_path)
    raw_blob.upload_blob(
        data,
        overwrite=True,
        content_type="application/octet-stream",
        metadata={
            "sop_raw_path": raw_path,
            "uploaded_at": ts.isoformat().replace("+00:00", "Z"),
            "filename": filename or "",
            "uploaded_by": user or "",
        },
    )

    return SopUploadResult(ok=True, sop_raw_path=raw_path)


def get_latest_sop_info() -> dict[str, Any]:
    conn = settings.LAB_NOTES_BLOB_CONNECTION_STRING or ""
    if not conn:
        return {"ok": False, "error": "missing_connection_string"}

    container = settings.LAB_SOP_BLOB_CONTAINER or ""
    prefix = (settings.LAB_SOP_BLOB_PREFIX or "lab-sops").strip("/")
    path = (settings.LAB_SOP_BLOB_PATH or "").lstrip("/") or f"{prefix}/current.json"
    if not container or not path:
        return {"ok": False, "error": "missing_container_or_path"}

    service = BlobServiceClient.from_connection_string(conn)
    try:
        blob = service.get_blob_client(container, path)
        data = blob.download_blob().readall()
        sop = json.loads(data.decode("utf-8"))
        props = blob.get_blob_properties()
        meta = props.metadata or {}
        return {
            "ok": True,
            "current_path": path,
            "sop_id": meta.get("sop_id") or str(sop.get("sop_id") or ""),
            "sop_title": meta.get("sop_title") or str(sop.get("title") or ""),
            "sop_json_path": meta.get("sop_json_path") or "",
            "sop_raw_path": meta.get("sop_raw_path") or "",
            "uploaded_at": meta.get("uploaded_at") or "",
            "filename": meta.get("filename") or "",
            "uploaded_by": meta.get("uploaded_by") or "",
        }
    except Exception:
        return {"ok": False, "error": "sop_not_found"}


async def handle_sop_upload(
    *,
    filename: str,
    data: bytes,
    user: str,
    timestamp_utc: Optional[str],
) -> SopUploadResult:
    try:
        raw_text = _extract_text_from_bytes(filename, data)
        if not raw_text:
            result = store_sop_raw(
                filename=filename,
                data=data,
                user=user,
                timestamp_utc=timestamp_utc,
            )
            result.error = "sop_text_unreadable"
            return result
        sop_json = await generate_sop_json(raw_text)
        if not sop_json.get("sop_id"):
            sop_json["sop_id"] = _parse_timestamp(timestamp_utc or "").strftime("SOP_%Y%m%dT%H%M%S")
        return store_sop_assets(
            filename=filename,
            data=data,
            sop_json=sop_json,
            user=user,
            timestamp_utc=timestamp_utc,
        )
    except ValidationError:
        result = store_sop_raw(
            filename=filename,
            data=data,
            user=user,
            timestamp_utc=timestamp_utc,
        )
        result.error = "sop_json_invalid"
        return result
    except Exception:
        result = store_sop_raw(
            filename=filename,
            data=data,
            user=user,
            timestamp_utc=timestamp_utc,
        )
        result.error = "sop_upload_failed"
        return result


def validate_steps(sop: dict[str, Any], steps: list[dict[str, Any]]) -> ValidationResult:
    sop_steps = sop.get("steps") or []
    sop_steps = [step for step in sop_steps if _looks_like_step(str(step.get("action") or ""))]
    issues: list[ValidationIssue] = []
    indexed = { _normalize_action(s.get("action", "")): s for s in steps }

    for sop_step in sop_steps:
        action = sop_step.get("action") or ""
        norm = _normalize_action(action)
        observed_step = indexed.get(norm)
        if not observed_step:
            issues.append(
                ValidationIssue(
                    step=action,
                    expected="step present",
                    observed="missing",
                    severity="major",
                )
            )
            continue

        expected_params = sop_step.get("parameters") or {}
        observed_params = observed_step.get("parameters") or {}

        for param_name, rule in expected_params.items():
            observed_value = observed_params.get(param_name)
            if observed_value is None:
                issues.append(
                    ValidationIssue(
                        step=action,
                        expected=f"{param_name} specified",
                        observed="not specified",
                        severity="clarification",
                    )
                )
                continue

            if isinstance(rule, dict):
                if "min" in rule or "max" in rule:
                    value = _extract_number(observed_value)
                    if value is None:
                        issues.append(
                            ValidationIssue(
                                step=action,
                                expected=f"{param_name} numeric",
                                observed=str(observed_value),
                                severity="clarification",
                            )
                        )
                        continue
                    min_val = rule.get("min")
                    max_val = rule.get("max")
                    if min_val is not None and value < float(min_val):
                        issues.append(
                            ValidationIssue(
                                step=action,
                                expected=f">= {min_val}",
                                observed=str(observed_value),
                                severity="minor",
                            )
                        )
                    if max_val is not None and value > float(max_val):
                        issues.append(
                            ValidationIssue(
                                step=action,
                                expected=f"<= {max_val}",
                                observed=str(observed_value),
                                severity="minor",
                            )
                        )
                elif "range" in rule and isinstance(rule["range"], dict):
                    value = _extract_number(observed_value)
                    min_val = rule["range"].get("min")
                    max_val = rule["range"].get("max")
                    if value is None:
                        issues.append(
                            ValidationIssue(
                                step=action,
                                expected=f"{param_name} numeric",
                                observed=str(observed_value),
                                severity="clarification",
                            )
                        )
                    else:
                        if min_val is not None and value < float(min_val):
                            issues.append(
                                ValidationIssue(
                                    step=action,
                                    expected=f">= {min_val}",
                                    observed=str(observed_value),
                                    severity="minor",
                                )
                            )
                        if max_val is not None and value > float(max_val):
                            issues.append(
                                ValidationIssue(
                                    step=action,
                                    expected=f"<= {max_val}",
                                    observed=str(observed_value),
                                    severity="minor",
                                )
                            )
                elif "level" in rule and isinstance(rule["level"], list):
                    if str(observed_value).lower() not in {str(v).lower() for v in rule["level"]}:
                        issues.append(
                            ValidationIssue(
                                step=action,
                                expected=f"one of {rule['level']}",
                                observed=str(observed_value),
                                severity="minor",
                            )
                        )
            elif isinstance(rule, list):
                if str(observed_value).lower() not in {str(v).lower() for v in rule}:
                    issues.append(
                        ValidationIssue(
                            step=action,
                            expected=f"one of {rule}",
                            observed=str(observed_value),
                            severity="minor",
                        )
                    )
            else:
                expected_value = str(rule).lower()
                if str(observed_value).lower() != expected_value:
                    issues.append(
                        ValidationIssue(
                            step=action,
                            expected=str(rule),
                            observed=str(observed_value),
                            severity="minor",
                        )
                    )

    status = "COMPLIANT" if not issues else "PARTIALLY_COMPLIANT"
    return ValidationResult(
        status=status,
        issues=issues,
        sop_id=str(sop.get("sop_id") or ""),
        sop_title=str(sop.get("title") or ""),
        transcript_steps=steps,
    )


def _short_report_hint(detail_path: str) -> str:
    if not detail_path:
        return ""
    match = re.search(r"/([0-9]{8}T[0-9]{6})\.json$", detail_path)
    report_id = match.group(1) if match else ""
    if report_id:
        return f"Full report saved. Report ID {report_id}."
    return "Full report saved in storage."


def build_voice_response(result: ValidationResult, detail_path: str = "") -> str:
    if result.status == "COMPLIANT":
        base = "Recording reviewed. All required steps are complete."
        return f"{base} Full report saved in the log directory."
    issues = result.issues[:2]
    issue_labels = ", ".join(issue.step for issue in issues if issue.step)
    extra = f" Missing: {issue_labels}." if issue_labels else " Some steps are missing."
    base = f"Recording reviewed. Status: {result.status}.{extra}"
    return f"{base} Full report saved in the log directory."


def store_validation_result(
    *,
    result: ValidationResult,
    transcript: str,
    user: str,
    duration_seconds: int,
    timestamp_utc: Optional[str],
) -> str:
    conn = settings.LAB_NOTES_BLOB_CONNECTION_STRING or ""
    if not conn:
        return ""

    container = settings.LAB_VALIDATION_BLOB_CONTAINER or "lab-validations"
    ts = _parse_timestamp(timestamp_utc or "")
    blob_path = _build_validation_blob_path(user, ts)

    service = BlobServiceClient.from_connection_string(conn)
    container_client = service.get_container_client(container)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    payload = {
        "status": result.status,
        "issues": [asdict(issue) for issue in result.issues],
        "sop_id": result.sop_id,
        "sop_title": result.sop_title,
        "transcript": transcript,
        "transcript_steps": result.transcript_steps or [],
        "user": user or "unknown",
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "durationSeconds": int(max(duration_seconds, 0)),
        "compliance_percent": result.compliance_percent,
        "checklist": result.checklist or {},
        "matched_ids": result.matched_ids or [],
        "missing_ids": result.missing_ids or [],
        "evidence": result.evidence or {},
    }

    blob = container_client.get_blob_client(blob_path)
    blob.upload_blob(
        json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8"),
        overwrite=True,
        content_type="application/json",
    )

    return blob_path
