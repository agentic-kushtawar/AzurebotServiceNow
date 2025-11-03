# core/snow.py
from typing import Optional
from threading import Lock

from config.settings import settings
from skills.servicenow import ServiceNowClient

_client: Optional[ServiceNowClient] = None
_lock = Lock()

def get_snow() -> ServiceNowClient:
    """
    Lazily create a single ServiceNowClient using env-backed settings.
    Thread-safe and import-friendly.
    """
    global _client
    if _client is None:
        with _lock:
            if _client is None:  # double-checked locking
                _client = ServiceNowClient(
                    base_url=settings.SNOW_BASE_URL,
                    username=settings.SNOW_USERNAME,
                    password=settings.SNOW_PASSWORD,
                )
    return _client
