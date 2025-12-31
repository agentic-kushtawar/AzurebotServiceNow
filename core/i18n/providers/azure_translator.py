# core/i18n/providers/azure_translator.py
from __future__ import annotations
import os
import httpx
from core.telemetry.logger import get_logger

log = get_logger("app")

class AzureTranslator:
    """
    Thin wrapper over Azure Translator Text API v3.0 using httpx (sync).
    Env:
      AZURE_TRANSLATOR_ENDPOINT=https://api.cognitive.microsofttranslator.com
      AZURE_TRANSLATOR_KEY=<key>
      AZURE_TRANSLATOR_REGION=<region>
    """

    def __init__(self) -> None:
        self.endpoint = (os.getenv("AZURE_TRANSLATOR_ENDPOINT") or "").rstrip("/")
        self.key = (
            os.getenv("Azure_Text_Translator_Key")
            or os.getenv("AZURE_TEXT_TRANSLATOR_KEY")
            or os.getenv("AZURE_TRANSLATOR_KEY")
            or ""
        )
        self.region = os.getenv("AZURE_TRANSLATOR_REGION") or ""

        # Normalize to resource-style path if they gave a resource endpoint
        if self.endpoint.endswith(".cognitiveservices.azure.com") and "/translator" not in self.endpoint:
            self.endpoint = f"{self.endpoint}/translator/text/v3.0"

        # Do not raise here; adapter handles graceful fallback if config is missing.
        key_suffix = self.key[-4:] if self.key else ""
        log.info(
            "Translator config endpoint=%s region=%s key_present=%s key_suffix=%s",
            self.endpoint or "<empty>",
            self.region or "<empty>",
            bool(self.key),
            key_suffix or "<none>",
        )

    def _headers(self) -> dict:
        h = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/json",
        }
        if self.region:
            h["Ocp-Apim-Subscription-Region"] = self.region
        return h

    def detect(self, text: str) -> str:
        if not (self.endpoint and self.key) or not text:
            return "en"
        url = f"{self.endpoint}/detect?api-version=3.0"
        with httpx.Client(timeout=15.0) as client:
            try:
                r = client.post(url, headers=self._headers(), json=[{"Text": text}])
                r.raise_for_status()
                data = r.json()
                return (data and data[0].get("language")) or "en"
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                body = ""
                if getattr(exc, "response", None) is not None:
                    try:
                        body = exc.response.text
                    except Exception:
                        body = ""
                log.warning("Translator detect failed status=%s body=%s", status, body or "<empty>")
                raise

    def translate(self, text: str, src: str, dst: str) -> str:
        if not (self.endpoint and self.key) or not text:
            return text
        url = f"{self.endpoint}/translate?api-version=3.0&from={src}&to={dst}"
        with httpx.Client(timeout=20.0) as client:
            try:
                r = client.post(url, headers=self._headers(), json=[{"Text": text}])
                r.raise_for_status()
                data = r.json()
                return data[0]["translations"][0]["text"]
            except httpx.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                body = ""
                if getattr(exc, "response", None) is not None:
                    try:
                        body = exc.response.text
                    except Exception:
                        body = ""
                log.warning("Translator translate failed status=%s body=%s", status, body or "<empty>")
                raise
