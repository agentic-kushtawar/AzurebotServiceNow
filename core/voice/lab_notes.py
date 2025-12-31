from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Optional

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

from config.settings import settings


@dataclass
class UploadResult:
    ok: bool
    blob_path: str = ""
    error: str = ""


def _parse_timestamp(ts: str) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _safe_user_tag(user: str) -> str:
    if not user:
        return "unknown"
    base = user.split("@")[0]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned or "unknown"


def _build_blob_path(user: str, ts: datetime) -> str:
    date_path = ts.strftime("%Y/%m/%d")
    file_name = ts.strftime("%Y%m%dT%H%M%S") + ".txt"
    prefix = (settings.LAB_NOTES_BLOB_PREFIX or "").strip("/")
    base_path = f"{date_path}/{_safe_user_tag(user)}/{file_name}"
    return f"{prefix}/{base_path}" if prefix else base_path


def upload_lab_transcript(
    *,
    transcript: str,
    user: str,
    duration_seconds: int,
    timestamp_utc: Optional[str] = None,
) -> UploadResult:
    if not transcript:
        return UploadResult(ok=False, error="empty_transcript")

    conn = settings.LAB_NOTES_BLOB_CONNECTION_STRING or ""
    if not conn:
        return UploadResult(ok=False, error="missing_connection_string")

    container = settings.LAB_NOTES_BLOB_CONTAINER or "lab-transcripts"
    source = settings.LAB_NOTES_SOURCE or "Teams EchoBot Voice"
    environment = settings.LAB_NOTES_ENVIRONMENT or "Lab"

    ts = _parse_timestamp(timestamp_utc or "")
    blob_path = _build_blob_path(user, ts)

    service = BlobServiceClient.from_connection_string(conn)
    container_client = service.get_container_client(container)
    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    blob = container_client.get_blob_client(blob_path)
    data = transcript.strip().encode("utf-8")

    metadata = {
        "user": user or "unknown",
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "durationSeconds": str(max(duration_seconds, 0)),
        "source": source,
        "environment": environment,
        "confirmedByUser": "true",
        "contentType": "transcription",
    }

    blob.upload_blob(data, overwrite=True, metadata=metadata, content_type="text/plain")

    return UploadResult(ok=True, blob_path=blob_path)
