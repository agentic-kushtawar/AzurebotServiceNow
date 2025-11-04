# core/llm/client.py
from __future__ import annotations
import os, json, re, asyncio
from typing import Literal, Optional, Dict, Any
from tenacity import retry, wait_exponential_jitter, stop_after_attempt
from pydantic import BaseModel, Field, ValidationError
from openai import OpenAI, AzureOpenAI

ProviderName = Literal["openai", "azure"]

class JsonResult(BaseModel):
    """Generic JSON result. Routers can pass their own Pydantic schema."""
    intent: str = "other"
    reason: str = ""
    inc_number: str = ""

def _redact(s: str) -> str:
    s = re.sub(r"sk-[A-Za-z0-9]{10,}", "sk-***", s)
    s = re.sub(r"INC\d{7}", "INC*******", s)
    return s

class _BaseAdapter:
    def __init__(self, temperature: float, max_tokens: int):
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _messages(self, system_prompt: str, user_text: str):
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{user_text}\nReturn ONLY a JSON object."},
        ]

    def call(self, system_prompt: str, user_text: str) -> Dict[str, Any]:
        raise NotImplementedError

class _OpenAIAdapter(_BaseAdapter):
    def __init__(self, **kw):
        super().__init__(kw["temperature"], kw["max_tokens"])
        api_key = os.environ["OPENAI_API_KEY"]
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini")
        # set a client-level timeout (seconds)
        self.client = OpenAI(api_key=api_key, timeout=float(os.getenv("LLM_TIMEOUT_SECS", "10")))

    def call(self, system_prompt: str, user_text: str) -> Dict[str, Any]:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=self._messages(system_prompt, user_text),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        return resp.model_dump()

class _AzureAdapter(_BaseAdapter):
    def __init__(self, **kw):
        super().__init__(kw["temperature"], kw["max_tokens"])
        self.client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01"),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            timeout=float(os.getenv("LLM_TIMEOUT_SECS", "10")),
        )
        # In Azure, 'model' is the DEPLOYMENT NAME (not a raw model id)
        self.deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    def call(self, system_prompt: str, user_text: str) -> Dict[str, Any]:
        resp = self.client.chat.completions.create(
            model=self.deployment,
            messages=self._messages(system_prompt, user_text),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        return resp.model_dump()

class LLM:
    """
    Stable, provider-agnostic wrapper.

    Usage:
        llm = LLM.auto()             # env-driven (LLM_PROVIDER=openai|azure)
        # or explicit one-line switch:
        # llm = LLM.use("openai")
        # llm = LLM.use("azure")
    """
    def __init__(self, provider: ProviderName, adapter: _BaseAdapter):
        self.provider = provider
        self._adapter = adapter

    @staticmethod
    def _build_adapter(provider: ProviderName) -> _BaseAdapter:
        temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "300"))
        if provider == "azure":
            return _AzureAdapter(temperature=temperature, max_tokens=max_tokens)
        return _OpenAIAdapter(temperature=temperature, max_tokens=max_tokens)

    @classmethod
    def use(cls, provider: ProviderName) -> "LLM":
        return cls(provider, cls._build_adapter(provider))

    @classmethod
    def auto(cls) -> "LLM":
        provider = os.getenv("LLM_PROVIDER", "openai").lower()
        return cls.use("azure" if provider == "azure" else "openai")

    @retry(wait=wait_exponential_jitter(1, 3), stop=stop_after_attempt(3))
    async def chat_json(
        self,
        system_prompt: str,
        user_text: str,
        schema: Optional[type[BaseModel]] = None,
    ) -> JsonResult | BaseModel:
        """
        Invoke the underlying provider and return a validated JSON object.
        If 'schema' (Pydantic model) is provided, validate to that; else return JsonResult.
        """
        # Safety—bound cost:
        user_text = (user_text or "")[:2000]

        # Call sync SDK in a worker thread; enforce timeout via client-level timeouts.
        def _invoke():
            return self._adapter.call(system_prompt, user_text)

        resp = await asyncio.to_thread(_invoke)
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "{}")

        try:
            data = json.loads(content or "{}")
        except json.JSONDecodeError:
            data = {"intent": "other", "reason": "", "inc_number": ""}

        Model = schema or JsonResult
        try:
            return Model(**data)
        except ValidationError:
            # minimal safe fallback
            return Model()
