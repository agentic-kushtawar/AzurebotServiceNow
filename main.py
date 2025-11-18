# main.py
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from dotenv import load_dotenv
import os, json, time, uuid, threading, requests, logging

# --- Telemetry & middleware ---
from core.telemetry.otel import setup_tracing, setup_log_export
from middleware.telemetry import TelemetryMiddleware
from core.metrics import METRICS

# --- Routers (import BEFORE include) ---
from apps.teams_bot.routes import router as bot_router
from skills.directory_mock import router as directory_router
from dev.dev_routes import router as dev_router


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

setup_tracing(app)
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

    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/communications/calls/{call_id}/answer"
    body = {
        "callbackUri": f"{PUBLIC_BASE_URL}/calls/notifications",
        "acceptedModalities": ["audio"],
        "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"},
    }
    resp = requests.post(url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    print(f"[CALL] Answered {call_id} -> {resp.status_code} {resp.text}")
    return resp.status_code

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
        payload = await request.json()
    except Exception:
        payload = {}
    print("CALLS are NOTIFied", json.dumps(payload))

    # 1) ACK IMMEDIATELY to avoid Graph timeout
    ack = Response(status_code=202)

    # 2) Forward to Windows sidecar (Kestrel) if configured
    if CALLS_FORWARD_TO:
        try:
            r = requests.post(CALLS_FORWARD_TO, json=payload, timeout=10)
            print(f"[CALL] Forwarded to sidecar -> {r.status_code}")
        except Exception as e:
            print(f"[CALL] Forward FAILED: {e}")

    # 3) Optionally auto-answer locally
    if CALLS_AUTO_ANSWER:
        for n in payload.get("value", []):
            change = (n.get("changeType") or "").lower()
            rd = n.get("resourceData", {}) or {}
            state = (rd.get("state") or "").lower()
            direction = (rd.get("direction") or "").lower()
            call_id = rd.get("id") or (n.get("resource", "/").split("/")[-1])

            # Consider both 'created' and 'updated' while still ringing/establishing
            if direction == "incoming" and change in {"created", "updated"} and state in {"incoming", "establishing", "ringing"}:
                if _should_answer(call_id):
                    threading.Thread(target=answer_call, args=(call_id,), daemon=True).start()
                    print(f"[CALL] Answer queued for {call_id} (state={state}, change={change})")
            elif change == "deleted":
                print(f"[CALL] Deleted/terminated {call_id} ({rd.get('terminationReason')})")

    return ack

# ====== Optional voice endpoints used by your Windows sidecar ======
@app.post("/voice/call-event")
async def voice_call_event(payload: dict, request: Request):
    call_id = payload.get("callId", "")
    evt = payload.get("event", "")
    status = payload.get("status", "")
    print(f"[VOICE][{request.client.host}] {evt} callId={call_id} status={status} — Connected to Call Server (Windows).")
    return {"ok": True}

@app.post("/voice/stt")
async def voice_stt(payload: dict, request: Request):
    text = (payload.get("text") or "").strip()
    if not text:
        print(f"[VOICE][{request.client.host}] (empty STT payload)")
        return {"ok": True}
    print(f"[VOICE][{request.client.host}] STT: {text}")
    # If you use an orchestrator, keep your existing import/usage.
    try:
        from core.orchestrator import orchestrator  # your project’s module
        await orchestrator.handle(text, {"user_id": "", "channel_id": "msteams"})
    except Exception:
        pass
    return {"ok": True}
