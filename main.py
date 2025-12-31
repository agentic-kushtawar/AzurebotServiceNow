from dataclasses import asdict
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from dotenv import load_dotenv
import os, json, time, uuid, threading, requests, logging, asyncio, re
from typing import Any, Dict  # NEW
from datetime import datetime, timezone

# --- Telemetry & middleware ---
from core.telemetry.otel import setup_tracing, setup_log_export
from middleware.telemetry import TelemetryMiddleware
from core.metrics import METRICS
from core.orchestrator.engine import Orchestrator
from core.orchestrator.state import session_for
from core.orchestrator.playbooks import ticket_howto_playbook
from core.orchestrator.engine import (
    handle_ticket_create,
    handle_ticket_update_status,
    _is_intune_count_request,
    _is_user_count_request,
)
from core.orchestrator.intents.ticket_create import detect_ticket_create
from core.voice.lab_notes import upload_lab_transcript
from core.voice.sop_validation import (
    build_voice_response,
    get_latest_sop_info,
    load_sop_json,
    parse_transcript_steps,
    store_validation_result,
    validate_transcript_against_sop,
    validate_steps,
)
from core.analytics.incident_intel import (
    build_stats_query,
    compute_insights,
    extract_raw_issues,
    apply_issue_map,
    raw_issue_counts,
    top_issues_from_rows,
)
from core.analytics.incident_cache import get_cached_response, set_cached_response
from core.llm.client import LLM
from core.i18n.policy import is_enabled_locale, label_for, normalize_locale
from core.i18n.adapter import translate
from pydantic import BaseModel
from core.snow import get_snow

# --- Routers (import BEFORE include) ---
from apps.teams_bot.routes import router as bot_router
from skills.directory_mock import router as directory_router
from dev.dev_routes import router as dev_router
from apps.teams_bot.calls import router as calls_router

load_dotenv(override=True)

# NEW: load typed settings + apply BotFramework shim + one-line runtime dump
from config.settings import settings, dump_runtime_flags  # noqa: F401
dump_runtime_flags()


# ====== Config & Constants ======
GRAPH_TENANT = os.getenv("GRAPH_TENANT_ID") or "organizations"
GRAPH_TOKEN_URL = f"https://login.microsoftonline.com/{GRAPH_TENANT}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

BOT_ID = os.environ["BOT_MICROSOFT_APP_ID"]
BOT_SECRET = os.environ["BOT_MICROSOFT_APP_PASSWORD"]

# Where Graph should POST subsequent notifications for THIS app
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
if not PUBLIC_BASE_URL:
    print("[CALL][WARN] PUBLIC_BASE_URL not set – answering will fail.")

# Forward every call notification to Windows sidecar? (your Kestrel endpoint)
CALLS_FORWARD_TO = (os.getenv("CALLS_FORWARD_TO", "") or "").rstrip("/")
# Auto-answer with Graph /answer?
CALLS_AUTO_ANSWER = os.getenv("CALLS_AUTO_ANSWER", "true").lower() == "true"

# De-bounce map: callId -> first-seen timestamp
_ANSWERED: dict[str, float] = {}

# ====== App ======
app = FastAPI(title="Teams AI Service Desk (MVP)")
log = logging.getLogger("app")
voice_orchestrator = Orchestrator()

# Simple in-memory cache for incident intel normalization to reduce latency.
_INCIDENT_INTEL_CACHE: dict[str, object] = {"key": "", "mapping": {}, "ts": 0.0}

setup_tracing(app)

_VOICE_LANG_SWITCH_MAP = {
    "english": ("en-US", "English"),
    "inglish": ("en-US", "English"),
    "inglés": ("en-US", "English"),
    "ingles": ("en-US", "English"),
    "en": ("en-US", "English"),
    "german": ("de-DE", "Deutsch"),
    "deutsch": ("de-DE", "Deutsch"),
    "de": ("de-DE", "Deutsch"),
    "spanish": ("es-ES", "Español"),
    "español": ("es-ES", "Español"),
    "espanol": ("es-ES", "Español"),
    "es": ("es-ES", "Español"),
    "alemán": ("de-DE", "Deutsch"),
    "aleman": ("de-DE", "Deutsch"),
}

_VOICE_LANG_SWITCH_TRIGGERS = {
    "switch",
    "change",
    "language",
    "speak",
    "set",
    "wechsel",
    "sprache",
    "sprich",
    "cambiar",
    "idioma",
    "habla",
    "hablar",
}

_VOICE_LANG_CONFIRM = {
    "en-US": "Language set to English.",
    "de-DE": "Sprache auf Deutsch gesetzt.",
    "es-ES": "Idioma configurado a Español.",
}


def _detect_voice_lang_switch(text: str) -> tuple[str, str]:
    raw = (text or "").strip().lower()
    if not raw:
        return "", ""
    if raw in _VOICE_LANG_SWITCH_MAP:
        return _VOICE_LANG_SWITCH_MAP[raw]

    tokens = re.findall(r"[\wáéíóúüñ]+", raw, flags=re.IGNORECASE)
    has_trigger = any(trigger in raw for trigger in _VOICE_LANG_SWITCH_TRIGGERS)
    for key, (code, label) in _VOICE_LANG_SWITCH_MAP.items():
        if key in tokens:
            if has_trigger:
                return code, label
            # Allow short, direct mentions like "english" or "sprichst du englisch".
            if len(tokens) <= 4:
                return code, label
    return "", ""


def _voice_locale_to_lang(locale: str) -> str:
    loc = (locale or "").lower()
    if loc.startswith("de"):
        return "de"
    if loc.startswith("es"):
        return "es"
    return "en"


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


def _is_voice_password_reset_request(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return False
    return any(
        phrase in lower
        for phrase in (
            "reset my password",
            "reset password",
            "forgot my password",
            "forgot password",
            "password reset",
            "passwort",
            "kennwort",
            "contraseña",
        )
    )


def _extract_inc_number(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\binc[\s\.\-]*?(\d{6,8})\b", text, re.IGNORECASE)
    if not m:
        m = re.search(r"\bi\s*[,.\-\s]*n\s*[,.\-\s]*c\s*(\d{6,8})\b", text, re.IGNORECASE)
        if not m:
            return ""
    digits = m.group(1)
    if len(digits) < 7:
        digits = digits.zfill(7)
    return f"INC{digits[:7]}"


def _extract_status_from_text(text: str) -> str:
    lower = (text or "").lower()
    if "in progress" in lower or "in_progress" in lower or "in bearbeitung" in lower:
        return "in_progress"
    if "on hold" in lower or "on_hold" in lower:
        return "on_hold"
    if re.search(r"\bnew\b", lower) or re.search(r"\bneu\b", lower):
        return "new"
    return ""


def _extract_update_reason(text: str, status: str) -> str:
    if not text:
        return ""
    lower = text.lower()
    if status == "in_progress":
        marker = "in progress"
        marker_alt = "in bearbeitung"
    elif status == "on_hold":
        marker = "on hold"
        marker_alt = ""
    elif status == "new":
        marker = "new"
        marker_alt = "neu"
    else:
        return ""
    idx = lower.find(marker)
    if idx == -1 and marker_alt:
        idx = lower.find(marker_alt)
        marker = marker_alt
    if idx == -1:
        return ""
    remainder = text[idx + len(marker):].strip(" .,:;-")
    return remainder


def _is_status_update_request(text: str) -> bool:
    lower = (text or "").lower()
    if not lower:
        return False
    if not any(
        k in lower
        for k in (
            "update status",
            "change status",
            "set status",
            "status to",
            "change the status",
            "änder den status",
            "ändern sie den status",
            "status von",
            "status auf",
        )
    ):
        return False
    return bool(_extract_inc_number(text))


_VOICE_LOG_PATH_HINTS = {
    "log path",
    "report path",
    "validation path",
    "full path",
    "path to the report",
    "path to the validation",
    "transcript path",
    "validation log",
    "log directory",
    "logs directory",
}


def _is_voice_log_path_request(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(hint in lower for hint in _VOICE_LOG_PATH_HINTS)


def _wants_validation_status_only(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    hints = (
        "final result",
        "just result",
        "only result",
        "only status",
        "status only",
        "just status",
        "compliance status",
        "just the result",
    )
    return any(hint in lower for hint in hints)


def _wants_validation_full_details(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(
        hint in lower
        for hint in (
            "full details",
            "full report",
            "full validation",
            "detailed report",
            "complete report",
            "with details",
        )
    )


def _is_last_transcript_request(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return "last transcript" in lower or "previous transcript" in lower


def _is_sop_info_request(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower or "sop" not in lower:
        return False
    hints = (
        "latest",
        "current",
        "active",
        "which sop",
        "which document",
        "what document",
        "sop document",
        "being validated",
    )
    return any(hint in lower for hint in hints)


def _format_sop_info_for_voice(info: dict[str, Any]) -> str:
    title = info.get("sop_title") or info.get("sop_id") or "SOP"
    sop_id = info.get("sop_id") or ""
    raw_path = info.get("sop_raw_path") or ""
    filename = os.path.basename(raw_path) if raw_path else ""
    parts = [f"Active SOP: {title}."]
    if sop_id:
        parts.append(f"ID: {sop_id}.")
    if filename:
        parts.append(f"Source document: {filename}.")
    return " ".join(parts).strip()


def _remember_voice_turn(session: dict, role: str, text: str, max_turns: int = 10) -> None:
    if not text:
        return
    turns = session.get("voice_turns") or []
    turns.append({"role": role, "text": text})
    if len(turns) > max_turns:
        turns = turns[-max_turns:]
    session["voice_turns"] = turns


def _voice_context_snippet(session: dict, max_turns: int = 6) -> str:
    turns = session.get("voice_turns") or []
    if not turns:
        return ""
    selected = turns[-max_turns:]
    lines = []
    for turn in selected:
        role = turn.get("role", "user")
        text = (turn.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _should_attach_context(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    if lower.startswith(("and ", "but ", "then ", "so ", "also ")):
        return True
    if lower.startswith(("i thought", "i think", "what about", "isn't it", "wasn't it")):
        return True
    if len(lower.split()) <= 6:
        return True
    return False
setup_log_export()
app.add_middleware(TelemetryMiddleware)

# Helpful boot logs
log.info(
    "AUTH CHECK → "
    f"MicrosoftAppId={os.getenv('MicrosoftAppId')} | "
    f"MicrosoftAppType={os.getenv('MicrosoftAppType')} | "
    f"TenantId={os.getenv('MicrosoftAppTenantId')} | "
    f"PasswordLen={len(os.getenv('MicrosoftAppPassword') or '')}"
)
log.info(
    "BOT_* MAP → "
    f"BOT_MICROSOFT_APP_ID={os.getenv('BOT_MICROSOFT_APP_ID')} | "
    f"BOT_MICROSOFT_APP_TENANT_ID={os.getenv('BOT_MICROSOFT_APP_TENANT_ID')} | "
    f"BOT_MICROSOFT_APP_PASSWORD_LEN={len(os.getenv('BOT_MICROSOFT_APP_PASSWORD') or '')}"
)
log.info(
    f"PUBLIC_BASE_URL={PUBLIC_BASE_URL or '(unset)'} | "
    f"CALLS_FORWARD_TO={CALLS_FORWARD_TO or '(unset)'} | "
    f"CALLS_AUTO_ANSWER={CALLS_AUTO_ANSWER}"
)

# ====== Utilities ======
def _should_answer(call_id: str, ttl_sec: int = 180) -> bool:
    """Avoid answering the same call more than once across bursty created/updated events."""
    now = time.time()
    for k, t in list(_ANSWERED.items()):
        if now - t > ttl_sec:
            _ANSWERED.pop(k, None)
    if call_id in _ANSWERED:
        return False
    _ANSWERED[call_id] = now
    return True

def get_graph_token() -> str:
    data = {
        "client_id": BOT_ID,
        "client_secret": BOT_SECRET,
        "grant_type": "client_credentials",
        "scope": GRAPH_SCOPE,
    }
    r = requests.post(GRAPH_TOKEN_URL, data=data, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

def answer_call(call_id: str) -> int:
    """Fire-and-forget /answer; prints HTTP status for visibility."""
    if not PUBLIC_BASE_URL:
        print(f"[CALL][ERROR] Cannot answer {call_id}: PUBLIC_BASE_URL not set.")
        return 0

    try:
        token = get_graph_token()
    except Exception as e:
        print(f"[CALL][ERROR] Token acquisition failed for {call_id}: {e}")
        return 0

    url = f"https://graph.microsoft.com/v1.0/communications/calls/{call_id}/answer"
    body = {
        "callbackUri": f"{PUBLIC_BASE_URL}/calls/notifications",
        "acceptedModalities": ["audio"],
        # NOTE: For app-hosted, you use appHostedMediaConfig from Windows side.
        # Here we keep your existing serviceHostedMediaConfig behavior as-is.
        "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"},
    }
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        print(f"[CALL] Answered {call_id} -> {resp.status_code} {resp.text}")
        return resp.status_code
    except Exception as e:
        print(f"[CALL][ERROR] /answer call failed for {call_id}: {e}")
        return 0

def _log_notification_summary(payload: Dict[str, Any]) -> None:
    """
    Extra debug: for each Graph notification, log changeType/state/direction/callId/terminationReason.
    This does NOT change behavior – purely observability.
    """
    try:
        values = payload.get("value") or []
        for n in values:
            change = (n.get("changeType") or "").lower()
            rd = (n.get("resourceData") or {}) or {}
            state = (rd.get("state") or "").lower()
            direction = (rd.get("direction") or "").lower()
            call_id = rd.get("id") or (n.get("resource", "/").split("/")[-1])
            term_reason = rd.get("terminationReason") or ""
            media_region = rd.get("mediaHostedRegion") or ""
            log.info(
                "CALL EVT → changeType=%s state=%s direction=%s callId=%s term=%s mediaRegion=%s",
                change,
                state,
                direction,
                call_id,
                term_reason,
                media_region,
            )
    except Exception as e:
        log.error("CALL EVT summary logging failed: %s", e)

# ====== Health & metrics ======
@app.get("/healthz")
def health():
    return {"status": "ok"}

@app.get("/metrics")
def metrics():
    return METRICS.snapshot()

# ====== Routers ======
app.include_router(bot_router, prefix="")            # /api/messages (+OPTIONS)
app.include_router(directory_router, prefix="/mock") # /mock/directory/reset-password
app.include_router(dev_router, prefix="")            # /dev/messages (local testing)
app.include_router(calls_router, prefix="")

# ====== Request logging ======
@app.middleware("http")
async def log_requests(request: Request, call_next):
    run_id = str(uuid.uuid4())
    start = time.perf_counter()
    logger.bind(runId=run_id, path=str(request.url), method=request.method).info("REQ")
    resp: Response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.bind(runId=run_id, status=resp.status_code, latencyMs=elapsed_ms).info("RES")
    return resp

# ====== Graph calling webhooks ======
@app.post("/calls/notifications")
async def calls_notifications(request: Request):
    """
    Graph posts here for call lifecycle events.
    We 202-ACK immediately, forward to Windows (if configured),
    and (optionally) answer the call in a background thread.
    """
    try:
        print("Inside Call")
        payload = await request.json()
    except Exception:
        payload = {}
    print("CALLS are NOTIFied", json.dumps(payload))

    # Extra structured summary per notification (purely logging)
    _log_notification_summary(payload)

    # Simple metric hook (non-breaking)
    try:
        METRICS.inc("calls_notifications_total")
    except Exception:
        pass

    # 1) ACK IMMEDIATELY to avoid Graph timeout
    ack = Response(status_code=202)

    # 2) Forward to Windows sidecar (Kestrel) if configured
    if CALLS_FORWARD_TO:
        try:
            r = requests.post(CALLS_FORWARD_TO, json=payload, timeout=10)
            print(f"[CALL] Forwarded to sidecar -> {r.status_code}")
            log.info(
                "CALL FWD → status=%s url=%s",
                r.status_code,
                CALLS_FORWARD_TO,
            )
        except Exception as e:
            print(f"[CALL] Forward FAILED: {e}")
            log.error("CALL FWD FAILED → url=%s error=%s", CALLS_FORWARD_TO, e)

    # 3) Optionally auto-answer locally (kept as-is; usually disabled for app-hosted)
    if CALLS_AUTO_ANSWER:
        try:
            for n in payload.get("value", []):
                change = (n.get("changeType") or "").lower()
                rd = (n.get("resourceData") or {}) or {}
                state = (rd.get("state") or "").lower()
                direction = (rd.get("direction") or "").lower()
                call_id = rd.get("id") or (n.get("resource", "/").split("/")[-1])

                # Consider both 'created' and 'updated' while still ringing/establishing
                if (
                    direction == "incoming"
                    and change in {"created", "updated"}
                    and state in {"incoming", "establishing", "ringing"}
                ):
                    if _should_answer(call_id):
                        threading.Thread(
                            target=answer_call,
                            args=(call_id,),
                            daemon=True,
                        ).start()
                        print(
                            f"[CALL] Answer queued for {call_id} "
                            f"(state={state}, change={change})"
                        )
                        log.info(
                            "CALL AUTO-ANSWER QUEUED → callId=%s state=%s change=%s",
                            call_id,
                            state,
                            change,
                        )
                elif change == "deleted":
                    term = rd.get("terminationReason") or "unknown"
                    initiator = rd.get("terminationReasonCode") or rd.get("terminationSource") or "unspecified"
                    print(
                        f"[CALL][END] Terminated call={call_id} reason={term} initiator={initiator}"
                    )
                    log.info(
                        "CALL TERMINATED → callId=%s reason=%s initiator=%s",
                        call_id,
                        term,
                        initiator,
                    )
        except Exception as e:
            log.error("CALL AUTO-ANSWER block failed: %s", e)

    return ack

# ====== Optional voice endpoints used by your Windows sidecar ======
@app.post("/voice/call-event")
async def voice_call_event(payload: dict, request: Request):
    call_id = payload.get("callId", "")
    evt = payload.get("event", "")
    status = payload.get("status", "")
    if call_id and evt.lower() == "connected":
        session = session_for({"call_id": call_id})
        session["voice_lang"] = "en-US"
    print(
        f"[VOICE][{request.client.host}] {evt} "
        f"callId={call_id} status={status} — Connected to Call Server (Windows)."
    )
    log.info(
        "VOICE EVENT → host=%s event=%s callId=%s status=%s",
        request.client.host,
        evt,
        call_id,
        status,
    )
    return {"ok": True}

@app.post("/voice/stt")
async def voice_stt(payload: dict, request: Request):
    text = (payload.get("text") or "").strip()
    text_lc = text.lower()
    call_id = (payload.get("callId") or "").strip()
    user_hint = (payload.get("userId") or call_id or request.client.host or "voice")
    if not text:
        print(f"[VOICE][{request.client.host}] (empty STT payload)")
        return {"ok": True}
    print(f"[VOICE][{request.client.host}] STT: {text}")
    log.info(
        "VOICE STT → host=%s text=%s",
        request.client.host,
        text,
    )
    session = session_for({"call_id": call_id})
    status_only = _wants_validation_status_only(text) and not _wants_validation_full_details(text)
    _remember_voice_turn(session, "user", text)
    pending = session.get("pending_labnote")
    if pending:
        if text_lc in {"yes", "confirm", "validate", "validate sop", "validate with sop", "please validate"}:
            sop = load_sop_json()
            if not sop:
                return {
                    "ok": True,
                    "result": {
                        "ok": True,
                        "action": "direct_reply",
                        "text": "I don't have an SOP configured yet. Please upload the SOP document first.",
                    },
                }
            try:
                validation_result = await validate_transcript_against_sop(
                    sop=sop,
                    transcript=pending.get("transcript", ""),
                )
                validation_path = await asyncio.to_thread(
                    store_validation_result,
                    result=validation_result,
                    transcript=pending.get("transcript", ""),
                    user=pending.get("user", ""),
                    duration_seconds=int(pending.get("duration", 0) or 0),
                    timestamp_utc=pending.get("timestamp", ""),
                )
                session["last_validation_path"] = validation_path
                voice_response = (
                    f"Validation result: {validation_result.status}."
                    if status_only
                    else build_voice_response(validation_result, validation_path)
                )
                session.pop("pending_labnote", None)
                _remember_voice_turn(session, "assistant", voice_response)
                return {
                    "ok": True,
                    "result": {
                        "ok": True,
                        "action": "direct_reply",
                        "text": voice_response,
                    },
                }
            except Exception as exc:
                log.warning("VOICE LABNOTE → validation failed: %s", exc)
                return {
                    "ok": True,
                    "result": {
                        "ok": True,
                        "action": "direct_reply",
                        "text": "I couldn't validate this recording right now. Please try again.",
                    },
                }
        if text_lc in {"no", "cancel", "discard"}:
            session.pop("pending_labnote", None)
            _remember_voice_turn(session, "assistant", "Okay. I won't validate that recording.")
            return {
                "ok": True,
                "result": {"ok": True, "action": "direct_reply", "text": "Okay. I won't validate that recording."},
            }

    if _is_sop_info_request(text):
        info = get_latest_sop_info()
        if not info.get("ok"):
            reply = "I don't have an active SOP yet. Upload one by saying 'upload SOP'."
        else:
            reply = _format_sop_info_for_voice(info)
        _remember_voice_turn(session, "assistant", reply)
        return {"ok": True, "result": {"ok": True, "action": "direct_reply", "text": reply}}

    if _is_voice_log_path_request(text):
        path = session.get("last_validation_path")
        if path:
            reply = f"The full report is saved at: {path}"
        else:
            reply = "I don't have a recent validation report yet."
        _remember_voice_turn(session, "assistant", reply)
        return {"ok": True, "result": {"ok": True, "action": "direct_reply", "text": reply}}

    if _is_voice_password_reset_request(text):
        reply = (
            "For security, password reset must be initiated only in chat. "
            "I can help with other Entra ID info like user count or tenant details."
        )
        _remember_voice_turn(session, "assistant", reply)
        return {"ok": True, "result": {"ok": True, "action": "direct_reply", "text": reply}}

    if "validate" in text_lc and "sop" in text_lc and _is_last_transcript_request(text):
        sop = load_sop_json()
        transcript = session.get("last_transcript") or ""
        if not sop or not transcript:
            reply = "I don't have a recent transcript to validate yet."
            _remember_voice_turn(session, "assistant", reply)
            return {"ok": True, "result": {"ok": True, "action": "direct_reply", "text": reply}}
        try:
            validation_result = await validate_transcript_against_sop(
                sop=sop,
                transcript=transcript,
            )
            validation_path = await asyncio.to_thread(
                store_validation_result,
                result=validation_result,
                transcript=transcript,
                user=payload.get("userName") or "",
                duration_seconds=0,
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            session["last_validation_path"] = validation_path
            voice_response = (
                f"Validation result: {validation_result.status}."
                if status_only
                else build_voice_response(validation_result, validation_path)
            )
            _remember_voice_turn(session, "assistant", voice_response)
            return {"ok": True, "result": {"ok": True, "action": "direct_reply", "text": voice_response}}
        except Exception as exc:
            log.warning("VOICE LABNOTE → validation failed: %s", exc)
            reply = "I couldn't validate the last transcript right now. Please try again."
            _remember_voice_turn(session, "assistant", reply)
            return {"ok": True, "result": {"ok": True, "action": "direct_reply", "text": reply}}

    lang_code, lang_label = _detect_voice_lang_switch(text)
    if lang_code:
        session["voice_lang"] = lang_code
        confirm = _VOICE_LANG_CONFIRM.get(lang_code) or f"Speech language set to {lang_label}."
        _remember_voice_turn(session, "assistant", confirm)
        return {
            "ok": True,
            "result": {
                "ok": True,
                "action": "direct_reply",
                "text": confirm,
                "voice_lang": lang_code,
            },
        }

    # Voice orchestrator bridge – mirrors Teams chat intent handling for STT input.
    try:
        voice_lang = session.get("voice_lang")
        text_for_orchestrator = text
        if voice_lang and voice_lang != "en-US":
            src_lang = _voice_locale_to_lang(voice_lang)
            text_for_orchestrator = translate(text, src_lang, "en", banner=False)
            if text_for_orchestrator and text_for_orchestrator != text:
                log.info(
                    "VOICE STT → translated input from %s to en: %s",
                    src_lang,
                    text_for_orchestrator,
                )
        context = _voice_context_snippet(session)
        if context:
            word_count = len((text_for_orchestrator or "").split())
            explicit_create, _ = detect_ticket_create(text_for_orchestrator)
            explicit_update = _is_status_update_request(text_for_orchestrator) or _is_status_update_request(text)
            if not explicit_create and not explicit_update and (_should_attach_context(text) or word_count <= 10):
                text_for_orchestrator = f"Context:\n{context}\n\nUser: {text_for_orchestrator}"
                log.info("VOICE STT → context attached turns=%s", len(session.get("voice_turns") or []))
        log.info(
            "VOICE STT → route_debug original=%s | routed=%s | intune_count=%s | user_count=%s",
            text,
            text_for_orchestrator,
            _is_intune_count_request(text_for_orchestrator),
            _is_user_count_request(text_for_orchestrator),
        )
        result = await voice_orchestrator.handle(
            text_for_orchestrator,
            {
                "user_id": user_hint,
                "user_name": payload.get("userName") or "",
                "user_email": payload.get("userEmail") or "",
                "channel": "voice",
                "call_id": call_id,
            },
        )
    except Exception as exc:
        log.exception("VOICE STT orchestrator failure: %s", exc)
        return {"ok": False, "error": "orchestrator_failed"}
    action = (result or {}).get("action") or ""
    if _is_status_update_request(text_for_orchestrator) or _is_status_update_request(text):
        source_text = text_for_orchestrator if _is_status_update_request(text_for_orchestrator) else text
        inc_number = _extract_inc_number(source_text)
        status = _extract_status_from_text(source_text)
        reason = _extract_update_reason(source_text, status)
        if not reason:
            reason = "User requested status update."
        result = await handle_ticket_update_status(
            inc_number=inc_number,
            status=status,
            reason=reason,
            user={
                "user_id": user_hint,
                "user_name": payload.get("userName") or "",
                "user_email": payload.get("userEmail") or "",
                "channel": "voice",
            },
        )
        action = (result or {}).get("action") or ""
    if action == "ticket_howto":
        explicit, extracted_reason = detect_ticket_create(text_for_orchestrator)
        if explicit:
            result = await handle_ticket_create(
                reason=(extracted_reason or "").strip(),
                user={
                    "user_id": user_hint,
                    "user_name": payload.get("userName") or "",
                    "user_email": payload.get("userEmail") or "",
                    "channel": "voice",
                },
            )
            action = (result or {}).get("action") or ""
        elif _is_status_update_request(text_for_orchestrator) or _is_status_update_request(text):
            source_text = text_for_orchestrator if _is_status_update_request(text_for_orchestrator) else text
            inc_number = _extract_inc_number(source_text)
            status = _extract_status_from_text(source_text)
            reason = _extract_update_reason(source_text, status)
            if not reason:
                reason = "User requested status update."
            result = await handle_ticket_update_status(
                inc_number=inc_number,
                status=status,
                reason=reason,
                user={
                    "user_id": user_hint,
                    "user_name": payload.get("userName") or "",
                    "user_email": payload.get("userEmail") or "",
                    "channel": "voice",
                },
            )
            action = (result or {}).get("action") or ""
    if action == "incident_intel":
        voice_text = (result or {}).get("voice_text") or (result or {}).get("text") or ""
        result = {
            "ok": True,
            "action": "direct_reply",
            "text": voice_text,
            "processing_hint": "Please hold while I check ServiceNow.",
            "long_running": True,
        }
    if action == "password_reset":
        result = {
            "ok": True,
            "action": "direct_reply",
            "text": "For security, password reset must be initiated in chat. Please open the chat and say “reset my password.”",
        }
    if action in {"help", "bot_profile", "ticket_howto"}:
        bot_name = settings.BOT_PERSONA_NAME or "Vox AI Service"
        if action == "bot_profile" and _asks_bot_name(text):
            text_out = f"I'm {bot_name}."
        elif action == "bot_profile":
            text_out = (
                "I can create ServiceNow tickets, check incident status, help with password resets, "
                "troubleshoot VPN issues, and show recurring incident insights. "
                "I can also record lab notes: say ‘Begin lab note’ and ‘Stop recording’, then confirm to save the transcript. "
                "I can speak English, Spanish, or German."
            )
        elif action == "ticket_howto":
            text_out = ticket_howto_playbook()
        else:
            text_out = (
                f"Hi, I'm {bot_name}. I can create ServiceNow tickets, check incident status, help with password "
                "resets, troubleshoot VPN issues, and show recurring incident insights. I can also record lab "
                "notes: say ‘Begin lab note’ and ‘Stop recording’, then confirm to save the transcript. "
                "I can speak English, Spanish, or German."
            )
        result = {
            "ok": True,
            "action": "direct_reply",
            "text": text_out,
        }
    if action == "repeat_last":
        last = session.get("last_voice_result")
        if last:
            if isinstance(last, dict):
                last = dict(last)
                last.pop("processing_hint", None)
                last.pop("long_running", None)
            return {"ok": True, "result": last}
        return {
            "ok": True,
            "result": {"ok": True, "action": "direct_reply", "text": "I don't have anything to repeat yet."},
        }

    if isinstance(result, dict):
        voice_lang = result.get("voice_lang") or session.get("voice_lang")
        if not result.get("text"):
            voice_text = _voice_action_to_text(result)
            if voice_text:
                result = {
                    "ok": True,
                    "action": "direct_reply",
                    "text": voice_text,
                    "voice_lang": voice_lang,
                }
        if voice_lang:
            result["voice_lang"] = voice_lang
            result["current_voice_lang"] = voice_lang
        if voice_lang and result.get("text"):
            result["text"] = await _translate_voice_text(result["text"], voice_lang)
        if voice_lang and result.get("processing_hint"):
            result["processing_hint"] = await _translate_voice_text(result["processing_hint"], voice_lang)

    if isinstance(result, dict) and result.get("voice_lang"):
        session["voice_lang"] = result.get("voice_lang")

    if isinstance(result, dict):
        reply_text = result.get("text") if isinstance(result.get("text"), str) else ""
        _remember_voice_turn(session, "assistant", reply_text)
    session["last_voice_result"] = result
    return {"ok": True, "result": result}


@app.post("/voice/labnote")
async def voice_labnote(payload: dict, request: Request):
    transcript = (payload.get("transcript") or "").strip()
    call_id = (payload.get("callId") or "").strip()
    user = (
        payload.get("user")
        or payload.get("userId")
        or payload.get("userEmail")
        or settings.LAB_NOTES_DEFAULT_USER
        or call_id
        or request.client.host
        or "voice"
    )
    duration = int(payload.get("durationSeconds") or 0)
    timestamp = (payload.get("timestampUtc") or payload.get("timestamp") or payload.get("startTimeUtc") or "").strip()

    if not transcript:
        log.warning("VOICE LABNOTE → empty transcript host=%s callId=%s", request.client.host, call_id)
        return {"ok": False, "error": "empty_transcript"}

    result = await asyncio.to_thread(
        upload_lab_transcript,
        transcript=transcript,
        user=user,
        duration_seconds=duration,
        timestamp_utc=timestamp,
    )

    session = session_for({"call_id": call_id})
    session["pending_labnote"] = {
        "transcript": transcript,
        "user": user,
        "duration": duration,
        "timestamp": timestamp,
    }
    session["last_transcript"] = transcript

    validation_blob = ""
    validation_result = None
    voice_response = ""
    should_validate = bool(payload.get("validate") or payload.get("confirmValidation"))
    sop = load_sop_json()
    if should_validate:
        if sop:
            try:
                validation_result = await validate_transcript_against_sop(
                    sop=sop,
                    transcript=transcript,
                )
                voice_response = build_voice_response(validation_result, validation_blob)
                validation_blob = await asyncio.to_thread(
                    store_validation_result,
                    result=validation_result,
                    transcript=transcript,
                    user=user,
                    duration_seconds=duration,
                    timestamp_utc=timestamp,
                )
                session["last_validation_path"] = validation_blob
                session.pop("pending_labnote", None)
            except Exception as exc:
                log.warning("VOICE LABNOTE → validation failed: %s", exc)
        else:
            log.info("VOICE LABNOTE → SOP not configured; skipping validation")

    log.info(
        "VOICE LABNOTE → host=%s callId=%s ok=%s blob=%s validation_blob=%s",
        request.client.host,
        call_id,
        result.ok,
        result.blob_path,
        validation_blob,
    )
    response = {"ok": result.ok, "blob_path": result.blob_path, "error": result.error}
    if not should_validate:
        response["validation_prompt"] = "Would you like me to validate this recording against the SOP?"
        response["awaiting_validation"] = True
    if validation_result:
        response["validation"] = {
            "status": validation_result.status,
            "issues": [asdict(issue) for issue in validation_result.issues],
            "sop_id": validation_result.sop_id,
            "sop_title": validation_result.sop_title,
            "validation_blob_path": validation_blob,
        }
        response["voice_response"] = voice_response
    return response


class _IssueMap(BaseModel):
    mapping: dict[str, str] = {}


def _extract_mapping_pairs(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    pairs = re.findall(r'"([^"\\]+)"\s*:\s*"([^"\\]+)"', raw)
    return {k: v for k, v in pairs if k and v}


def _parse_mapping(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw.strip())
        if isinstance(obj, dict):
            if "mapping" in obj and isinstance(obj["mapping"], dict):
                return {str(k): str(v) for k, v in obj["mapping"].items() if k and v}
            return {str(k): str(v) for k, v in obj.items() if k and v}
    except Exception:
        pass
    return _extract_mapping_pairs(raw)


class _VoiceTranslation(BaseModel):
    text: str = ""


_VOICE_TRANSLATE_PROMPT = (
    "Translate the following English text to {target_label} (locale {target_locale}). "
    "Keep ticket numbers like INC1234567, URLs, emails, and product names unchanged. "
    "Keep it concise and natural for spoken voice. Return ONLY JSON: {{\"text\": \"...\"}}."
)


def _voice_action_to_text(result: Dict[str, Any]) -> str:
    if not isinstance(result, dict):
        return ""
    action = (result.get("action") or "").strip().lower()
    bot_name = settings.BOT_PERSONA_NAME or "Vox AI Service"
    bot_role = settings.BOT_PERSONA_ROLE or "your virtual Service Desk assistant"
    if action == "ticket_create":
        inc = (result.get("inc_number") or "").strip()
        reason = (result.get("reason") or "").strip() or "your issue"
        if inc:
            return f"I created ticket {inc} for {reason}."
        if reason:
            return f"I created a ticket for {reason}."
        return "I created a ticket for your issue."
    if action == "ticket_status":
        inc = (result.get("inc_number") or "").strip() or "the incident"
        state = (result.get("state") or "").strip()
        extra = (result.get("short_description") or "").strip()
        if state and extra:
            return f"Status for {inc} is {state}. {extra}"
        if state:
            return f"Status for {inc} is {state}."
        return f"Status for {inc} is not available yet."
    if action == "ticket_update_status":
        inc = (result.get("inc_number") or "").strip() or "the incident"
        status = (result.get("status") or "").strip() or "updated"
        reason = (result.get("reason") or "").strip()
        if reason:
            return f"Updated {inc} to {status}. Reason: {reason}."
        return f"Updated {inc} to {status}."
    if action == "propose_ticket":
        reason = (result.get("reason") or "").strip() or "your issue"
        tips = (result.get("tips") or "").strip()
        if tips:
            return f"I can create a ticket for {reason}. {tips}"
        return f"I can create a ticket for {reason}. Should I go ahead?"
    if action == "greeting":
        return (
            f"Hi, I'm {bot_name}, {bot_role}. "
            "I can speak English, Spanish, or German. How can I help you?"
        )
    return ""


async def _translate_voice_text(text: str, target_locale: str) -> str:
    locale = normalize_locale(target_locale or "")
    if not text or locale == "en" or not is_enabled_locale(locale):
        return text
    try:
        llm = LLM.auto()
    except Exception:
        return text
    prompt = _VOICE_TRANSLATE_PROMPT.format(
        target_label=label_for(locale),
        target_locale=locale,
    )
    try:
        result = await llm.chat_json(prompt, text, schema=_VoiceTranslation)
    except Exception:
        return text
    translated = (getattr(result, "text", "") or "").strip()
    return translated or text


@app.get("/api/incident-intel")
async def incident_intel_api(days: int = 30):
    cache_key = f"incident-intel:{days}"
    cached = get_cached_response(cache_key)
    if cached:
        return cached
    active_only = bool(getattr(settings, "INCIDENT_INTEL_ACTIVE_ONLY", True))
    threshold = int(getattr(settings, "INCIDENT_INTEL_THRESHOLD", 2) or 2)
    days = int(days or getattr(settings, "INCIDENT_INTEL_DAYS", 30) or 30)

    query_now = build_stats_query(days_start=days, active_only=active_only)
    query_prev = build_stats_query(days_start=days * 2, days_end=days, active_only=active_only)

    client = get_snow()
    current = await client.get_incident_stats(query=query_now, group_by="short_description")
    previous = await client.get_incident_stats(query=query_prev, group_by="short_description")
    total = await client.get_incident_total(query=query_now)

    if getattr(settings, "FEATURE_LLM_INCIDENT_NORMALIZE", False):
        try:
            counts = raw_issue_counts(current)
            for k, v in raw_issue_counts(previous).items():
                counts[k] = counts.get(k, 0) + v
            ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            max_issues = int(getattr(settings, "INCIDENT_INTEL_LLM_MAX_ISSUES", 50) or 50)
            issues = [i for i, _ in ranked[:max_issues]]
            if issues:
                cache_key = "|".join(issues)
                now = time.time()
                cached_key = _INCIDENT_INTEL_CACHE.get("key")
                cached_ts = float(_INCIDENT_INTEL_CACHE.get("ts") or 0.0)
                cached_map = _INCIDENT_INTEL_CACHE.get("mapping") or {}
                if cache_key == cached_key and (now - cached_ts) < 600 and cached_map:
                    mapping = cached_map
                else:
                    llm = LLM.auto()
                    prompt = (
                        "Return ONLY JSON with shape {\"mapping\": {\"<original>\": \"<canonical>\"}}. "
                        "Canonical labels must be short (<=5 words) in Title Case. "
                        "Only map provided strings; if unsure, map to itself."
                    )
                    user_text = json.dumps(issues, ensure_ascii=False)
                    result = await llm.chat_json(prompt, user_text, schema=_IssueMap)
                    mapping = getattr(result, "mapping", {}) or {}
                    if not mapping:
                        raw = llm._adapter.call(prompt, user_text)  # type: ignore[attr-defined]
                        content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
                        mapping = _parse_mapping(content or "")
                    _INCIDENT_INTEL_CACHE["key"] = cache_key
                    _INCIDENT_INTEL_CACHE["mapping"] = mapping
                    _INCIDENT_INTEL_CACHE["ts"] = now
                apply_issue_map(current, mapping)
                apply_issue_map(previous, mapping)
        except Exception:
            pass

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
    repeated = sum(i.count for i in insights)
    repeat_rate = round((repeated / total) * 100, 1) if total else 0.0
    candidates = [i for i in insights if i.is_problem_candidate]

    response = {
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
    }

    set_cached_response(cache_key, response)
    return response


@app.get("/dashboard/incident-intel")
async def incident_intel_dashboard():
    html = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Recurring Incident Intelligence</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Fraunces:wght@500;700&display=swap" rel="stylesheet" />
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
      :root {
        --bg: #0b1020;
        --bg-alt: #111834;
        --card: rgba(255,255,255,0.06);
        --card-strong: rgba(255,255,255,0.12);
        --text: #f5f6fb;
        --muted: #a9b0c8;
        --accent: #00d1b2;
        --accent-2: #ffb703;
        --danger: #ff5d5d;
        --ink: #12182a;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Space Grotesk", sans-serif;
        color: var(--text);
        background: radial-gradient(1200px 600px at 10% 10%, #1b2550 0%, transparent 60%),
                    radial-gradient(900px 500px at 90% 0%, #1a3b3a 0%, transparent 55%),
                    linear-gradient(160deg, var(--bg) 0%, var(--bg-alt) 100%);
        min-height: 100vh;
      }
      header {
        padding: 36px 6vw 12px;
      }
      h1 {
        font-family: "Fraunces", serif;
        font-size: clamp(28px, 4vw, 44px);
        margin: 0 0 8px;
      }
      p.sub {
        margin: 0;
        color: var(--muted);
        max-width: 680px;
      }
      .grid {
        display: grid;
        gap: 18px;
        padding: 24px 6vw 40px;
      }
      .kpis {
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      }
      .card {
        background: var(--card);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 16px;
        padding: 18px;
        backdrop-filter: blur(8px);
        box-shadow: 0 12px 30px rgba(0,0,0,0.25);
      }
      .kpi-title {
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.12em;
      }
      .kpi-value {
        font-size: 28px;
        font-weight: 700;
        margin-top: 8px;
      }
      .layout {
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      }
      .chart-card canvas {
        width: 100%;
        height: 280px;
      }
      table {
        width: 100%;
        border-collapse: collapse;
      }
      th, td {
        padding: 10px 8px;
        text-align: left;
        border-bottom: 1px solid rgba(255,255,255,0.08);
        font-size: 14px;
      }
      th { color: var(--muted); font-weight: 600; }
      .trend.up { color: var(--accent); }
      .trend.down { color: var(--danger); }
      .trend.flat { color: var(--muted); }
      .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        background: rgba(0,209,178,0.15);
        color: var(--accent);
        font-size: 12px;
      }
      .badge.warn { background: rgba(255,183,3,0.2); color: var(--accent-2); }
      .footer {
        color: var(--muted);
        font-size: 12px;
        padding: 0 6vw 30px;
      }
    </style>
  </head>
  <body>
    <header>
      <h1>Recurring Incident Intelligence</h1>
      <p class="sub">Live view of repeated operational issues, trend signals, and problem-ticket candidates for the last 30 days.</p>
    </header>
    <section class="grid kpis" id="kpi-grid"></section>
    <section class="grid layout">
      <div class="card">
        <h3>Problem Management Recommendations</h3>
        <div id="recommendations"></div>
      </div>
      <div class="card">
        <h3>Voice / Chat Insight</h3>
        <p id="voiceSummary" class="sub"></p>
      </div>
    </section>
    <section class="grid layout">
      <div class="card chart-card">
        <h3>Top Recurring Issues</h3>
        <canvas id="barChart"></canvas>
      </div>
      <div class="card">
        <h3>Repeated Incidents</h3>
        <table>
          <thead>
            <tr>
              <th>Issue</th>
              <th>Occurrences</th>
              <th>Trend</th>
              <th>Group</th>
            </tr>
          </thead>
          <tbody id="issueTable"></tbody>
        </table>
      </div>
    </section>
    <section class="grid layout">
      <div class="card chart-card">
        <h3>Trend Pulse (% change vs previous period)</h3>
        <canvas id="trendChart"></canvas>
      </div>
      <div class="card">
        <h3>Top Movers</h3>
        <div id="trendList" class="sub"></div>
      </div>
    </section>
    <section class="grid layout">
      <div class="card chart-card">
        <h3>Assignment Group Load</h3>
        <canvas id="groupChart"></canvas>
      </div>
      <div class="card">
        <h3>Group Summary</h3>
        <div id="groupSummary" class="sub"></div>
      </div>
    </section>
    <div class="footer">Data source: ServiceNow Stats API • Updated on load</div>

    <script>
      async function loadData() {
        const res = await fetch("/api/incident-intel");
        const data = await res.json();
        const kpiGrid = document.getElementById("kpi-grid");
        kpiGrid.innerHTML = [
          { label: "Total Incidents (30d)", value: data.total_incidents || 0 },
          { label: "Repeated Incidents", value: data.repeated_incidents || 0 },
          { label: "Repeat Rate", value: (data.repeat_rate || 0) + "%" },
          { label: "Problem Candidates", value: (data.problem_candidates || []).length }
        ].map(k => `
          <div class="card">
            <div class="kpi-title">${k.label}</div>
            <div class="kpi-value">${k.value}</div>
          </div>
        `).join("");

        const issues = data.insights || [];
        const labels = issues.slice(0, 8).map(i => i.issue);
        const counts = issues.slice(0, 8).map(i => i.count);
        const ctx = document.getElementById("barChart").getContext("2d");
        new Chart(ctx, {
          type: "bar",
          data: {
            labels,
            datasets: [{
              label: "Occurrences",
              data: counts,
              backgroundColor: "rgba(0, 209, 178, 0.6)",
              borderColor: "rgba(0, 209, 178, 1)",
              borderWidth: 1
            }]
          },
          options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: {
              x: { ticks: { color: "#cbd2ea" }, grid: { display: false } },
              y: { ticks: { color: "#cbd2ea" }, grid: { color: "rgba(255,255,255,0.08)" } }
            }
          }
        });

        const tbody = document.getElementById("issueTable");
        tbody.innerHTML = issues.slice(0, 10).map(i => {
          let trendClass = "flat";
          let trendLabel = "stable";
          if (i.trend_percent > 5) { trendClass = "up"; trendLabel = `+${i.trend_percent}%`; }
          if (i.trend_percent < -5) { trendClass = "down"; trendLabel = `${i.trend_percent}%`; }
          return `
            <tr>
              <td>${i.issue}</td>
              <td>${i.count}</td>
              <td class="trend ${trendClass}">${trendLabel}</td>
              <td>${i.assignment_group}</td>
            </tr>
          `;
        }).join("");

        const rec = document.getElementById("recommendations");
        if ((data.problem_candidates || []).length === 0) {
          rec.innerHTML = "<p class='sub'>No issues meet the problem-ticket threshold yet.</p>";
        } else {
          rec.innerHTML = data.problem_candidates.map(i =>
            `<p><span class="badge warn">Problem Candidate</span> ${i}</p>`
          ).join("");
        }

        const voiceSummary = document.getElementById("voiceSummary");
        const top = issues[0];
        if (top) {
          voiceSummary.textContent = `${top.issue} occurred ${top.count} times in the last 30 days. Would you like me to create a Problem ticket?`;
        } else {
          voiceSummary.textContent = "No repeated incidents detected in the last 30 days.";
        }

        const trendCtx = document.getElementById("trendChart").getContext("2d");
        const trendLabels = issues.slice(0, 8).map(i => i.issue);
        const trendValues = issues.slice(0, 8).map(i => i.trend_percent);
        new Chart(trendCtx, {
          type: "bar",
          data: {
            labels: trendLabels,
            datasets: [{
              label: "Trend %",
              data: trendValues,
              backgroundColor: trendValues.map(v => v >= 0 ? "rgba(0, 209, 178, 0.6)" : "rgba(255, 93, 93, 0.6)"),
              borderWidth: 0
            }]
          },
          options: {
            responsive: true,
            plugins: { legend: { display: false } },
            scales: {
              x: { ticks: { color: "#cbd2ea" }, grid: { display: false } },
              y: { ticks: { color: "#cbd2ea" }, grid: { color: "rgba(255,255,255,0.08)" } }
            }
          }
        });

        const trendList = document.getElementById("trendList");
        trendList.innerHTML = issues.slice(0, 5).map(i => {
          const dir = i.trend_percent > 5 ? "up" : (i.trend_percent < -5 ? "down" : "flat");
          const label = i.trend_percent > 5 ? `+${i.trend_percent}%` : (i.trend_percent < -5 ? `${i.trend_percent}%` : "stable");
          return `<p class="trend ${dir}">${i.issue}: ${label}</p>`;
        }).join("");

        const groupMap = {};
        issues.forEach(i => {
          const key = i.assignment_group || "Unassigned";
          groupMap[key] = (groupMap[key] || 0) + i.count;
        });
        const groupLabels = Object.keys(groupMap);
        const groupCounts = groupLabels.map(k => groupMap[k]);
        const groupCtx = document.getElementById("groupChart").getContext("2d");
        new Chart(groupCtx, {
          type: "doughnut",
          data: {
            labels: groupLabels,
            datasets: [{
              data: groupCounts,
              backgroundColor: ["#00d1b2","#3a86ff","#ffb703","#fb5607","#8338ec","#06d6a0"]
            }]
          },
          options: {
            responsive: true,
            plugins: { legend: { labels: { color: "#cbd2ea" } } }
          }
        });

        const groupSummary = document.getElementById("groupSummary");
        groupSummary.innerHTML = groupLabels.map((g, idx) =>
          `<p>${g}: ${groupCounts[idx]} repeats</p>`
        ).join("");
      }
      loadData();
    </script>
  </body>
</html>
    """
    return PlainTextResponse(html, media_type="text/html")
