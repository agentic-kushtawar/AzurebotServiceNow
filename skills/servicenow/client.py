"""
ServiceNow Table API client (MVP)
- Create incident
- Get incident by number
- Add a public comment

Auth: basic auth (dev instance). For prod, swap to OAuth and scoped token.
"""

from __future__ import annotations
from typing import Any, Dict, Optional
import base64
import time
import httpx
from loguru import logger
from typing import Optional


class ServiceNowClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 10.0):
        # Ensure base_url has no trailing slash
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout

        # Prebuild Basic auth header (dev)
        basic = f"{username}:{password}".encode("utf-8")
        self._auth_header = "Basic " + base64.b64encode(basic).decode("ascii")

        # Single pooled client (we'll reuse it in every call)
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": self._auth_header,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

    async def get_user_sys_id(self, user_name: str) -> Optional[str]:
        """
        Resolve a sys_user by user_name (User ID). Returns sys_id or None.
        """
        if not user_name:
            return None
        resp = await self._request(
            "GET",
            f"/api/now/table/sys_user"
            f"?sysparm_query=user_name={user_name}"
            f"&sysparm_fields=sys_id"
            f"&sysparm_limit=1"
        )
        results = resp.json().get("result") or []
        return (results[0] or {}).get("sys_id") if results else None



    async def close(self):
        await self._client.aclose()

    # --- Helpers -------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
    ) -> httpx.Response:
        """
        Thin wrapper with light retry on 429/5xx using backoff.
        Pass `url` as a relative path (e.g., "/api/now/table/incident?...").
        """
        backoff = 0.6
        for attempt in range(max_retries + 1):
            resp = await self._client.request(method, url, json=json)
            if resp.status_code < 400:
                return resp

            # Retry on Too Many Requests or server error
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(backoff)
                backoff *= 1.7
                continue

            # Give up
            raise httpx.HTTPStatusError(
                f"ServiceNow error {resp.status_code}: {resp.text[:300]}",
                request=resp.request,
                response=resp,
            )

    # --- Incidents -----------------------------------------------------------

    async def create_incident(
        self,
        *,
        short_description: str,
        description: str,
        category: str,
        subcategory: str,
        impact: str = "3",
        urgency: str = "3",
        caller_id: str | None = None,
    ) -> str:
        """
        Creates an incident and returns the friendly incident number (e.g., INC0010003).
        """
        payload = {
            "short_description": short_description,
            "description": description,
            "category": category,
            "subcategory": subcategory,
            "impact": impact,
            "urgency": urgency,
        }
        if caller_id:
            payload["caller_id"] = caller_id

        try:
            resp = await self._request(
                "POST",
                "/api/now/table/incident?sysparm_display_value=true",
                json=payload,
            )
        except httpx.HTTPStatusError as he:
            logger.error(
                "SNOW HTTP error: status={} url={} body={}",
                he.response.status_code,
                str(he.request.url),
                he.response.text,
            )
            raise
        except Exception:
            logger.exception("SNOW network/unknown error during create_incident")
            raise

        data = resp.json().get("result", {})
        number = data.get("number") or data.get("display_value") or ""
        if not number:
            logger.error("SNOW create_incident returned no number. Body={}", resp.text)
            raise RuntimeError("ServiceNow response missing number")

        return number

    async def get_incident(self, number: str) -> Optional[Dict[str, Any]]:
        """
        Lookup by friendly incident number (e.g., INC0010002).
        """
        q = f"number={number}"
        resp = await self._request(
            "GET",
            f"/api/now/table/incident?sysparm_query={q}&sysparm_display_value=true",
        )
        results = resp.json().get("result") or []
        return results[0] if results else None

    async def add_comment(self, number: str, comment: str) -> bool:
        """
        Adds a public comment to an incident.
        """
        inc = await self.get_incident(number)
        if not inc:
            return False
        sys_id = inc["sys_id"]
        payload = {"comments": comment}
        await self._request("PATCH", f"/api/now/table/incident/{sys_id}", json=payload)
        return True
