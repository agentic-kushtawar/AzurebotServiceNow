# apps/teams_bot/bootstrap.py
from __future__ import annotations
import os
from core.llm.client import LLM
from core.orchestrator.llm_router import LLMIntentRouter
from core.orchestrator.engine import Engine

# Single place to construct and share the engine across the bot app.

def build_engine() -> Engine:
    # Env-driven provider selection. You can override with LLM.use("azure") here if desired.
    # Example explicit one-line switch:
    # llm = LLM.use("azure")  # or LLM.use("openai")
    llm = LLM.auto()
    router = LLMIntentRouter(llm)  # kept for future DI if needed
    eng = Engine()                 # Engine reads FEATURE_LLM_ROUTER internally

    # If you prefer explicit injection instead of Engine creating its own:
    # eng.router = router
    # eng.use_llm = os.getenv("FEATURE_LLM_ROUTER", "false").lower() == "true"

    return eng

# Export a module-level singleton for easy import elsewhere
engine = build_engine()
