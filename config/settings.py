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

# --- make sure the Bot Framework SDK sees single-tenant env keys ---
os.environ["MicrosoftAppId"] = settings.BOT_MICROSOFT_APP_ID
os.environ["MicrosoftAppPassword"] = settings.BOT_MICROSOFT_APP_PASSWORD
os.environ["MicrosoftAppType"] = "SingleTenant"
os.environ["MicrosoftAppTenantId"] = settings.BOT_MICROSOFT_APP_TENANT_ID



# Who should be set as Caller for bot-created incidents

