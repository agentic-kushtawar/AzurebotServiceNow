# skills/directory_mock.py
from fastapi import APIRouter, Body

router = APIRouter()

@router.post("/directory/reset-password")
def reset_password(username: str = Body(..., embed=True)):
    return {
        "ok": True,
        "username": username,
        "message": f"Mock reset initiated for {username}. In real AD, a reset link would be sent."
    }

@router.get("/directory/reset-password")
def dev_hint():
    return {"hint": "POST { username: 'user@contoso.com' } to /mock/directory/reset-password"}
