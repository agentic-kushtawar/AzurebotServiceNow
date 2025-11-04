import pytest
from core.llm.client import LLM

class FakeAdapter:
    def __init__(self): pass
    def call(self, system_prompt, user_text):
        return {"choices":[{"message":{"content": '{"intent":"other","reason":"","inc_number":""}'}}]}

@pytest.mark.parametrize("provider", ["openai", "azure"])
def test_one_line_switch(monkeypatch, provider):
    # Prevent real SDK from being constructed
    monkeypatch.setattr(LLM, "_build_adapter", lambda *_a, **_k: FakeAdapter())
    llm = LLM.use(provider)
    assert llm.provider == provider

@pytest.mark.parametrize("env_value,expected", [("openai","openai"), ("azure","azure"), ("weird","openai")])
def test_env_switch(monkeypatch, env_value, expected):
    monkeypatch.setenv("LLM_PROVIDER", env_value)
    monkeypatch.setattr(LLM, "_build_adapter", lambda *_a, **_k: FakeAdapter())
    llm = LLM.auto()
    assert llm.provider == expected
