# middleware/telemetry.py
from __future__ import annotations
import time, uuid
from typing import Callable
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from core.telemetry.logger import set_context, event, redact

class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        req_id = str(uuid.uuid4())
        # try to pick conversation/user info if available in Teams posts
        conv_id = request.headers.get("Conversation-Id") or "-"
        user_id = request.headers.get("From-Id") or "-"
        user_email = request.headers.get("From-Email") or None

        set_context(request_id=req_id, conv_id=conv_id, user_id=user_id, user_email=user_email)

        body_preview = ""
        try:
            body_bytes = await request.body()
            body_preview = redact(body_bytes.decode("utf-8")[:500])
        except Exception:
            pass

        start = time.perf_counter()
        event("http.req", path=request.url.path, method=request.method, body=body_preview)

        resp = await call_next(request)
        dur_ms = int(1000 * (time.perf_counter() - start))
        event("http.res", path=request.url.path, status=resp.status_code, duration_ms=dur_ms)
        return resp
