import pytest

from core.llm.client import LLM
from core.orchestrator.llm_router import LLMIntentRouter, IntentResult

class FakeAdapter:
    def __init__(self, payload_json):
        self.payload_json = payload_json

    def call(self, system_prompt, user_text):
        # Mimic the SDK response shape
        return {"choices": [{"message": {"content": self.payload_json}}]}

@pytest.mark.asyncio
async def test_router_parses_intent_and_normalizes_inc(monkeypatch):
    # Stub the adapter factory BEFORE constructing LLM
    monkeypatch.setattr(LLM, "_build_adapter", lambda *_args, **_kw: FakeAdapter(
        '{"intent":"ticket_status","reason":"status check","inc_number":"inc0012345"}'
    ))

    llm = LLM.use("openai")   # does not build real SDK now
    router = LLMIntentRouter(llm)

    res: IntentResult = await router.classify("what is the status of inc0012345?")
    assert res.intent == "ticket_status"
    assert res.reason == "status check"
    assert res.inc_number == "INC0012345"

@pytest.mark.asyncio
async def test_router_fallbacks_on_bad_json(monkeypatch):
    monkeypatch.setattr(LLM, "_build_adapter", lambda *_args, **_kw: FakeAdapter("not-json"))
    llm = LLM.use("azure")
    router = LLMIntentRouter(llm)
    res = await router.classify("hello")
    # schema ensures a valid object even on bad JSON
    assert res.intent in {"ticket_create","ticket_status","password_reset","vpn","help","other"}
