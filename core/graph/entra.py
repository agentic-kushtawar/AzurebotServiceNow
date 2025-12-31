from __future__ import annotations

import time
import base64
import json
from typing import Optional
from urllib.parse import quote
import logging

import httpx

from config.settings import settings

log = logging.getLogger("app")


class EntraGraphClient:
    def __init__(self) -> None:
        self._tenant_id = (settings.GRAPH_TENANT_ID or settings.BOT_MICROSOFT_APP_TENANT_ID or "").strip()
        self._client_id = (settings.GRAPH_CLIENT_ID or settings.BOT_MICROSOFT_APP_ID or "").strip()
        self._client_secret = (settings.GRAPH_CLIENT_SECRET or settings.BOT_MICROSOFT_APP_PASSWORD or "").strip()
        if not self._tenant_id or not self._client_id or not self._client_secret:
            raise ValueError("Missing Graph credentials (GRAPH_TENANT_ID/GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET).")
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._roles: list[str] = []

    @staticmethod
    def _b64url_decode(data: str) -> bytes:
        pad = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + pad)

    def _extract_roles(self, token: str) -> list[str]:
        """
        Decode JWT payload without verification to read app roles.
        We only log role names, never the token itself.
        """
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return []
            payload = json.loads(self._b64url_decode(parts[1]).decode("utf-8"))
            roles = payload.get("roles") or []
            return [str(r) for r in roles if r]
        except Exception:
            return []

    def _log_missing_roles(self, required: list[str], context: str) -> None:
        if not required:
            return
        have = set(self._roles or [])
        missing = [r for r in required if r not in have]
        if missing:
            log.warning(
                "Graph app roles missing for %s: missing=%s present=%s",
                context,
                ",".join(missing),
                ",".join(sorted(have)),
            )

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < (self._token_expires_at - 60):
            return self._token

        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, data=data)
            resp.raise_for_status()
            payload = resp.json()

        self._token = payload.get("access_token", "")
        expires_in = int(payload.get("expires_in") or 0)
        self._token_expires_at = now + expires_in
        self._roles = self._extract_roles(self._token)
        if self._roles:
            log.info("Graph app roles in token: %s", ",".join(sorted(self._roles)))
        return self._token

    async def get_user_by_upn(self, upn: str) -> Optional[dict]:
        upn = (upn or "").strip()
        if not upn:
            return None
        token = await self._get_token()
        url = f"https://graph.microsoft.com/v1.0/users/{quote(upn)}?$select=id,displayName,userPrincipalName,mail"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            if resp.status_code in {401, 403}:
                self._log_missing_roles(["User.Read.All"], "GET /users/{upn}")
            resp.raise_for_status()
            return resp.json()

    async def get_recovery_email(self, user_id: str) -> Optional[str]:
        user_id = (user_id or "").strip()
        if not user_id:
            return None
        token = await self._get_token()
        url = f"https://graph.microsoft.com/v1.0/users/{quote(user_id)}/authentication/emailMethods"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            if resp.status_code in {401, 403}:
                log.warning("Graph auth methods access denied for user_id=%s status=%s", user_id, resp.status_code)
                self._log_missing_roles(["UserAuthenticationMethod.Read.All"], "GET /users/{id}/authentication/emailMethods")
                return None
            resp.raise_for_status()
            data = resp.json() or {}
        for method in data.get("value", []) or []:
            email = (method.get("emailAddress") or "").strip()
            if email:
                return email
        return None

    async def count_users(self) -> int:
        token = await self._get_token()
        url = "https://graph.microsoft.com/v1.0/users"
        headers = {
            "Authorization": f"Bearer {token}",
            "ConsistencyLevel": "eventual",
        }
        params = {
            "$top": "1",
            "$count": "true",
            "$select": "id",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in {401, 403}:
                self._log_missing_roles(["User.Read.All"], "GET /users ($count)")
            resp.raise_for_status()
            data = resp.json() or {}
        count = data.get("@odata.count")
        if isinstance(count, int):
            return count
        return len(data.get("value") or [])

    async def get_managed_devices_by_name(self, device_name: str) -> list[dict]:
        device_name = (device_name or "").strip()
        if not device_name:
            return []
        token = await self._get_token()
        url = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "$filter": f"deviceName eq '{device_name}'",
            "$select": (
                "id,deviceName,complianceState,lastSyncDateTime,operatingSystem,osVersion,"
                "model,managementState,deviceRegistrationState"
            ),
            "$top": "5",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in {401, 403}:
                self._log_missing_roles(["DeviceManagementManagedDevices.Read.All"], "GET /deviceManagement/managedDevices")
            resp.raise_for_status()
            data = resp.json() or {}
        return list(data.get("value") or [])

    async def list_managed_devices(self, top: int = 10) -> list[dict]:
        token = await self._get_token()
        url = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "$select": (
                "id,deviceName,complianceState,lastSyncDateTime,operatingSystem,osVersion,"
                "model,managementState,deviceRegistrationState"
            ),
            "$top": str(max(1, min(top, 50))),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in {401, 403}:
                self._log_missing_roles(["DeviceManagementManagedDevices.Read.All"], "GET /deviceManagement/managedDevices (list)")
            resp.raise_for_status()
            data = resp.json() or {}
        return list(data.get("value") or [])

    async def count_managed_devices(self) -> int:
        token = await self._get_token()
        url = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"
        headers = {
            "Authorization": f"Bearer {token}",
            "ConsistencyLevel": "eventual",
        }
        params = {
            "$top": "1",
            "$count": "true",
            "$select": "id",
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in {401, 403}:
                self._log_missing_roles(["DeviceManagementManagedDevices.Read.All"], "GET /deviceManagement/managedDevices ($count)")
            resp.raise_for_status()
            data = resp.json() or {}
        count = data.get("@odata.count")
        if isinstance(count, int):
            return count
        return len(data.get("value") or [])

    async def count_managed_devices_by_os(self) -> tuple[int, dict[str, int]]:
        token = await self._get_token()
        url = "https://graph.microsoft.com/v1.0/deviceManagement/managedDevices"
        headers = {
            "Authorization": f"Bearer {token}",
            "ConsistencyLevel": "eventual",
        }
        params = {
            "$select": "operatingSystem",
            "$top": "100",
        }
        total = 0
        by_os: dict[str, int] = {}
        async with httpx.AsyncClient(timeout=20) as client:
            next_url = url
            next_params = params
            while next_url:
                resp = await client.get(next_url, headers=headers, params=next_params)
                if resp.status_code in {401, 403}:
                    self._log_missing_roles(
                        ["DeviceManagementManagedDevices.Read.All"],
                        "GET /deviceManagement/managedDevices (OS count)",
                    )
                resp.raise_for_status()
                data = resp.json() or {}
                items = data.get("value") or []
                for item in items:
                    os_name = (item.get("operatingSystem") or "unknown").strip() or "unknown"
                    by_os[os_name] = by_os.get(os_name, 0) + 1
                total += len(items)
                next_url = data.get("@odata.nextLink") or ""
                next_params = None
        return total, by_os

    async def list_detected_apps(self, device_id: str, top: int = 50) -> list[dict]:
        device_id = (device_id or "").strip()
        if not device_id:
            return []
        token = await self._get_token()
        url = f"https://graph.microsoft.com/beta/deviceManagement/managedDevices/{quote(device_id)}/detectedApps"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"$top": str(max(1, min(top, 200)))}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers, params=params)
            if resp.status_code in {401, 403}:
                self._log_missing_roles(
                    ["DeviceManagementManagedDevices.Read.All"],
                    "GET /deviceManagement/managedDevices/{id}/detectedApps",
                )
            if resp.status_code in {400, 404}:
                try:
                    data = resp.json() or {}
                    message = (data.get("error") or {}).get("message") or ""
                except Exception:
                    message = ""
                if "detectedApps" in message or resp.status_code == 404:
                    raise RuntimeError(
                        "Per-device detected apps requires Microsoft Graph beta "
                        "and may not be available in this tenant."
                    )
            resp.raise_for_status()
            data = resp.json() or {}
        return list(data.get("value") or [])

    async def restart_managed_device(self, device_id: str) -> tuple[bool, str | None]:
        device_id = (device_id or "").strip()
        if not device_id:
            return False, "Missing device ID."
        token = await self._get_token()
        url = f"https://graph.microsoft.com/v1.0/deviceManagement/managedDevices/{quote(device_id)}/rebootNow"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers)
            if resp.status_code in {401, 403}:
                self._log_missing_roles(
                    ["DeviceManagementManagedDevices.PrivilegedOperations.All"],
                    "POST /deviceManagement/managedDevices/{id}/rebootNow",
                )
            if resp.status_code in {200, 202, 204}:
                return True, None
            try:
                data = resp.json() or {}
                err = (data.get("error") or {}).get("message") or ""
            except Exception:
                err = ""
            if not err:
                err = (resp.text or "").strip()
            return False, err or f"Intune returned {resp.status_code}."
        return False, "Intune restart failed."
