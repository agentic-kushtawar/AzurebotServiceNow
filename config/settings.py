from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    BOT_MICROSOFT_APP_ID: str = ""
    BOT_MICROSOFT_APP_PASSWORD: str = ""
    BOT_MICROSOFT_APP_TENANT_ID: str = ""

    SNOW_BASE_URL: str = ""
    SNOW_USERNAME: str = ""
    SNOW_PASSWORD: str = ""
    FEATURE_SNOW_ENABLED: bool = True
    # NEW: who should be set as Caller (ServiceNow user_name, e.g., "botuser")
    SNOW_CALLER_USER: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()

# --- make sure the Bot Framework SDK

# Force multi-tenant at runtime: no tenant hint
if (os.getenv("MicrosoftAppType") or "").strip().lower() == "multitenant":
    # Remove any tenant values that might be carried over from other env names
    os.environ.pop("MicrosoftAppTenantId", None)
    os.environ.pop("BOT_MICROSOFT_APP_TENANT_ID", None)



# Who should be set as Caller for bot-created incidents

