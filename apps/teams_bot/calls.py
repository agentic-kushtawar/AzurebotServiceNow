from fastapi import APIRouter, Request, Response
import requests
import os
import json

router = APIRouter()

# Your real handler in main.py
NOTIFY_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") + "/calls/notifications"

@router.post("/api/calls")
async def api_calls_root(request: Request):
    """
    This endpoint is required because Microsoft Graph will POST here.
    We forward the payload to /calls/notifications which is your real processor.
    """
    try:
        payload = await request.json()
    except:
        payload = {}

    print("🔥 /api/calls RECEIVED:", json.dumps(payload))

    if NOTIFY_URL:
        try:
            r = requests.post(NOTIFY_URL, json=payload, timeout=10)
            print(f"Forwarded → {NOTIFY_URL} [{r.status_code}]")
        except Exception as e:
            print("Forwarding failed:", e)

    # Graph requires a 202 response
    return Response(status_code=202)
