# apps/config/settings.py
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
import os, logging

log = logging.getLogger("app")


class Settings(BaseSettings):
    # --- Bot / Microsoft Identity ---
    BOT_MICROSOFT_APP_ID: str = ""
    BOT_MICROSOFT_APP_PASSWORD: str = ""
    BOT_MICROSOFT_APP_TENANT_ID: str = ""  # used only if single-tenant
    MicrosoftAppType: str = Field(default="MultiTenant", alias="MicrosoftAppType")

    # --- Teams App packaging / public URL ---
    TEAMS_APP_ID: str = ""
    PUBLIC_BASE_URL: str = ""
    PUBLIC_BASE_HOST: str = ""

    # --- ServiceNow ---
    SNOW_BASE_URL: str = ""
    SNOW_USERNAME: str = ""
    SNOW_PASSWORD: str = ""
    FEATURE_SNOW_ENABLED: bool = True
    SNOW_CALLER_USER: str | None = None

    # --- LLM / Router ---
    FEATURE_LLM_ROUTER: bool = True
    LLM_PROVIDER: str = "openai"          # openai | azure
    OPENAI_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT_SECS: float = 20.0
    LLM_TEMPERATURE: float = 0.0
    LLM_MAX_TOKENS: int = 300

    # --- Localization / Translator ---
    FEATURE_LANG_ES_ENABLED: bool = True
    AZURE_TRANSLATOR_ENDPOINT: str = ""
    AZURE_TRANSLATOR_KEY: str = ""
    AZURE_TRANSLATOR_REGION: str = ""

    # --- Voice / Speech ---
    FEATURE_VOICE_ENABLED: bool = False
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = ""

    # --- Calling (ACS / RTM placeholders) ---
    ACS_CONNECTION_STRING: str = ""
    ACS_PHONE_NUMBER: str = ""
    ACS_CALLBACK_URL: str = ""
    CALLS_AUTO_ANSWER: bool = False
    CALLS_FORWARD_TO: str = ""            # your RTM/forward URL if used

    # --- Logging / Telemetry ---
    APPINSIGHTS_CONNECTION_STRING: str = ""
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    LOG_RAW_TEXT: bool = False
    FEATURE_PII_REDACT: bool = True
    LOG_PII_SAMPLES: bool = False
    PII_HASH_SALT: str = "change_me_32+chars"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # -------- helpers ----------
    def as_dict_safe(self) -> dict:
        """For debug logs – hides secrets."""
        hidden = {"BOT_MICROSOFT_APP_PASSWORD", "SNOW_PASSWORD", "OPENAI_API_KEY",
                  "AZURE_TRANSLATOR_KEY", "AZURE_SPEECH_KEY", "APPINSIGHTS_CONNECTION_STRING",
                  "ACS_CONNECTION_STRING", "PII_HASH_SALT"}
        d = self.model_dump()
        for k in list(d.keys()):
            if k in hidden and d.get(k):
                d[k] = "***"
        return d


settings = Settings()


def apply_botframework_env_shim() -> None:
    """
    The Bot Framework SDK also reads these classic env names.
    We map ours so both styles are present.
    """
    os.environ["MicrosoftAppId"] = settings.BOT_MICROSOFT_APP_ID or ""
    os.environ["MicrosoftAppPassword"] = settings.BOT_MICROSOFT_APP_PASSWORD or ""
    os.environ["MicrosoftAppType"] = settings.MicrosoftAppType or "MultiTenant"

    # If MultiTenant, be explicit: remove any tenant hints so token acquisition won’t be constrained
    if (settings.MicrosoftAppType or "").strip().lower() == "multitenant":
        os.environ.pop("MicrosoftAppTenantId", None)
        os.environ.pop("BOT_MICROSOFT_APP_TENANT_ID", None)
    else:
        # single-tenant case – keep the tenant id for AAD
        if settings.BOT_MICROSOFT_APP_TENANT_ID:
            os.environ["MicrosoftAppTenantId"] = settings.BOT_MICROSOFT_APP_TENANT_ID


def dump_runtime_flags() -> None:
    """
    One concise line that tells you what the running process actually loaded.
    Put this after logging configuration and before the app starts serving.
    """
    log.info(
        "RUNTIME → SNOW=%s | LLM_ROUTER=%s | LLM_PROVIDER=%s | TIMEOUT=%.1fs | TEMP=%.2f | LANG_ES=%s | VOICE=%s | LOG_LEVEL=%s",
        settings.FEATURE_SNOW_ENABLED,
        settings.FEATURE_LLM_ROUTER,
        settings.LLM_PROVIDER,
        settings.LLM_TIMEOUT_SECS,
        settings.LLM_TEMPERATURE,
        settings.FEATURE_LANG_ES_ENABLED,
        settings.FEATURE_VOICE_ENABLED,
        settings.LOG_LEVEL,
    )


# Apply shims immediately so anything importing SDK sees consistent env
apply_botframework_env_shim()
