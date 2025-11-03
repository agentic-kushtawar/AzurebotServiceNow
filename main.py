# main.py
from fastapi import FastAPI, Request, Response
from loguru import logger
import time, uuid

# Routers (import BEFORE including)
from apps.teams_bot.routes import router as bot_router
from skills.directory_mock import router as directory_router
from dev.dev_routes import router as dev_router

from core.metrics import METRICS

app = FastAPI(title="Teams AI Service Desk (MVP)")

# Health
@app.get("/healthz")
def health():
    return {"status": "ok"}

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
