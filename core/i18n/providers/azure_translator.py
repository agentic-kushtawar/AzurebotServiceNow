# core/i18n/providers/azure_translator.py
from __future__ import annotations
import os
import httpx

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
        self.key = os.getenv("AZURE_TRANSLATOR_KEY") or ""
        self.region = os.getenv("AZURE_TRANSLATOR_REGION") or ""

        # Normalize to resource-style path if they gave a resource endpoint
        if self.endpoint.endswith(".cognitiveservices.azure.com") and "/translator" not in self.endpoint:
            self.endpoint = f"{self.endpoint}/translator/text/v3.0"

        # Do not raise here; adapter handles graceful fallback if config is missing.

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
            r = client.post(url, headers=self._headers(), json=[{"Text": text}])
            r.raise_for_status()
            data = r.json()
            return (data and data[0].get("language")) or "en"

    def translate(self, text: str, src: str, dst: str) -> str:
        if not (self.endpoint and self.key) or not text:
            return text
        url = f"{self.endpoint}/translate?api-version=3.0&from={src}&to={dst}"
        with httpx.Client(timeout=20.0) as client:
            r = client.post(url, headers=self._headers(), json=[{"Text": text}])
            r.raise_for_status()
            data = r.json()
            return data[0]["translations"][0]["text"]
