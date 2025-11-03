import httpx
from typing import Dict, Any, Optional
from config.settings import settings

class ServiceNowClient:
    def __init__(self):
        self.base = (getattr(settings, "SNOW_BASE_URL", "") or "").rstrip("/")
        self.user = getattr(settings, "SNOW_USERNAME", "")
        self.pwd  = getattr(settings, "SNOW_PASSWORD", "")
        self.enabled = bool(getattr(settings, "FEATURE_SNOW_ENABLED", False))

    async def create_incident(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": True, "number": "INCSTUB001", "sys_id": "stub"}
        async with httpx.AsyncClient(auth=(self.user, self.pwd), timeout=20.0) as c:
            r = await c.post(f"{self.base}/api/now/table/incident", json=payload)
            r.raise_for_status()
            res = r.json()["result"]
            return {"ok": True, "number": res["number"], "sys_id": res["sys_id"]}

    async def get_incident(self, number: str) -> Dict[str, Any]:
        if not self.enabled:
            return {"ok": True, "result": {"number": number, "state": "New", "short_description": "stub"}}
        async with httpx.AsyncClient(auth=(self.user, self.pwd), timeout=20.0) as c:
            r = await c.get(f"{self.base}/api/now/table/incident",
                            params={"sysparm_query": f"number={number}"})
            r.raise_for_status()
            items = r.json()["result"]
            return {"ok": True, "result": items[0] if items else None}
