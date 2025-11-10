# main.py
from fastapi import FastAPI, Request, Response
from loguru import logger
import time, uuid
from dotenv import load_dotenv
from core.telemetry.otel import setup_tracing
from middleware.telemetry import TelemetryMiddleware
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
import json
import logging



load_dotenv(override=True)
import threading, time



# Routers (import BEFORE including)
from apps.teams_bot.routes import router as bot_router
from skills.directory_mock import router as directory_router
from dev.dev_routes import router as dev_router

from core.telemetry.otel import setup_tracing, setup_log_export
from core.metrics import METRICS
import os, requests


""" 
GRAPH_TOKEN_URL = f"https://login.microsoftonline.com/{os.environ['BOT_MICROSOFT_APP_TENANT_ID']}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
BOT_ID = os.environ["BOT_MICROSOFT_APP_ID"]
BOT_SECRET = os.environ["BOT_MICROSOFT_APP_PASSWORD"]
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"]  # e.g., https://<your-ngrok>.ngrok-free.dev """

GRAPH_TENANT = os.getenv("GRAPH_TENANT_ID") or "organizations"   # safe fallback
GRAPH_TOKEN_URL = f"https://login.microsoftonline.com/{GRAPH_TENANT}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
BOT_ID = os.environ["BOT_MICROSOFT_APP_ID"]
BOT_SECRET = os.environ["BOT_MICROSOFT_APP_PASSWORD"]

_ANSWERED: dict[str, float] = {}


CALLS_AUTO_ANSWER = os.getenv("CALLS_AUTO_ANSWER", "true").lower() == "true"
CALLS_FORWARD_TO = (os.getenv("CALLS_FORWARD_TO", "") or "").rstrip("/")


app = FastAPI(title="Teams AI Service Desk (MVP)")
log = logging.getLogger("app")
setup_tracing(app)                 # enables App Insights traces if conn string present
setup_log_export()      # Python logs to App Insights 'traces' table
app.add_middleware(TelemetryMiddleware)   # structured REQ/RES logs

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")   # <-- add/ensure this line
if not PUBLIC_BASE_URL:
        print("[CALL] WARNING: PUBLIC_BASE_URL not set – call answering will fail.")



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

def _should_answer(call_id: str, ttl=180) -> bool:
    now = time.time()
    for k, t in list(_ANSWERED.items()):
        if now - t > ttl:
            _ANSWERED.pop(k, None)
    if call_id in _ANSWERED:
        return False
    _ANSWERED[call_id] = now
    return True


def get_graph_token():
    data = {
        "client_id": BOT_ID, "client_secret": BOT_SECRET,
        "grant_type": "client_credentials", "scope": GRAPH_SCOPE,
    }
    r = requests.post(GRAPH_TOKEN_URL, data=data, timeout=20)
    r.raise_for_status()
    return r.json()["access_token"]

    
def answer_call(call_id: str):
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")   # <-- add/ensure this line
    if not PUBLIC_BASE_URL:
        print("[CALL] WARNING: PUBLIC_BASE_URL not set – call answering will fail.")
    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/communications/calls/{call_id}/answer"
    body = {
        "callbackUri": f"{PUBLIC_BASE_URL}/calls/notifications",
        "acceptedModalities": ["audio"],
        "mediaConfig": {"@odata.type": "#microsoft.graph.serviceHostedMediaConfig"},
    }
    resp = requests.post(url, json=body,
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=20)
    print(f"[CALL] Answered {call_id} -> {resp.status_code} {resp.text}")  # <— add this
    return resp.status_code

# Health
@app.get("/healthz")
def health():
    return {"status": "okk"}

# Dev metrics (dev-only; don’t expose publicly without auth)
@app.get("/metrics")
def metrics():
    return METRICS.snapshot()

# Attach routers ONCE
app.include_router(bot_router, prefix="")           # /api/messages (+OPTIONS)
app.include_router(directory_router, prefix="/mock")# /mock/directory/reset-password
app.include_router(dev_router, prefix="")           # /dev/messages (local testing)

# Structured request logging with runId & latency
@app.middleware("http")
async def log_requests(request: Request, call_next):
    run_id = str(uuid.uuid4())
    start = time.perf_counter()
    logger.bind(runId=run_id, path=request.url.path, method=request.method).info("REQ")
    resp: Response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.bind(runId=run_id, status=resp.status_code, latencyMs=elapsed_ms).info("RES")
    return resp


@app.post("/calls/notifications")
async def calls_notifications(request: Request):
    payload = await request.json()
    print("CALLS are NOTIFied", json.dumps(payload))

    # Forward the exact payload to the Windows sidecar if configured
    if CALLS_FORWARD_TO:
        try:
            r = requests.post(CALLS_FORWARD_TO, json=payload, timeout=10)
            print(f"[CALL] Forwarded to sidecar -> {r.status_code}")
        except Exception as e:
            print(f"[CALL] Forward FAILED: {e}")

    # Only auto-answer locally if explicitly enabled
    if CALLS_AUTO_ANSWER:
        for n in payload.get("value", []):
            if n.get("changeType") == "created":
                rd = n.get("resourceData", {}) or {}
                if rd.get("state") == "incoming" and "id" in rd:
                    call_id = rd["id"]
                    try:
                        status = answer_call(call_id)
                        print(f"[CALL] LOCAL answer {call_id} -> {status}")
                    except Exception as e:
                        print(f"[CALL] answer failed for {call_id}: {e}")

    return Response(status_code=202)



# replace your POST handler body with this shape (keep your logging):
@app.post("/calls/notifications")
async def calls_notifications(request: Request):
    payload = await request.json()
    print("CALLS are NOTIFied", json.dumps(payload))
    # ACK first; answer in background to avoid timing out Graph
    ack = Response(status_code=202)

    for n in payload.get("value", []):
        rd = n.get("resourceData", {}) or {}
        change = (n.get("changeType") or "").lower()
        state = (rd.get("state") or "").lower()
        direction = (rd.get("direction") or "").lower()
        call_id = rd.get("id") or (n.get("resource","/").split("/")[-1])

        if direction == "incoming" and change in {"created","updated"} and state in {"incoming","establishing","ringing"}:
            if _should_answer(call_id):
                threading.Thread(target=answer_call, args=(call_id,), daemon=True).start()
                print(f"[CALL] Answering request queued for {call_id} (state={state}, change={change})")
        elif change == "deleted":
            print(f"[CALL] Deleted/terminated {call_id} ({rd.get('terminationReason')})")

    return ack

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
    result = await orchestrator.handle(text, {"user_id": "", "channel_id": "msteams"})
    # optional: send proactive Teams message here…
    return {"ok": True}