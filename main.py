from fastapi import FastAPI, Request, Response
from loguru import logger
from skills.directory_mock import router as directory_router
from dev.dev_routes import router as dev_router


app = FastAPI(title="Teams AI Service Desk (MVP)")
app.include_router(directory_router, prefix="/mock")
app.include_router(dev_router, prefix="")

@app.get("/healthz")
def health():
    return {"status": "ok"}

# Include the Teams bot route (folder must be apps/teams_bot)
from apps.teams_bot.routes import router as bot_router
app.include_router(bot_router, prefix="")

# Optional: simple request logging
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.bind(path=request.url.path, method=request.method).info("REQ")
    resp: Response = await call_next(request)
    logger.bind(status=resp.status_code).info("RES")
    return resp
