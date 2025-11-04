# Ensure project root is on sys.path and set safe default envs for tests.
import sys, pathlib, os

# Make 'core/...' imports work when running pytest from the repo root
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

def pytest_configure(config):
    # Default provider + behavior
    os.environ.setdefault("LLM_PROVIDER", "openai")
    os.environ.setdefault("FEATURE_LLM_ROUTER", "true")
    os.environ.setdefault("LLM_MODEL", "gpt-4o-mini")
    os.environ.setdefault("LLM_TEMPERATURE", "0.2")
    os.environ.setdefault("LLM_MAX_TOKENS", "300")
    os.environ.setdefault("LLM_TIMEOUT_SECS", "10")

    # Dummy keys so adapter construction doesn’t crash (tests stub the calls anyway)
    os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
    os.environ.setdefault("AZURE_OPENAI_KEY", "test-azure-key")
    os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    os.environ.setdefault("AZURE_OPENAI_API_VERSION", "2024-06-01")
