from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    telegram_bot_token: str
    telegram_webhook_secret: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    redis_url: str = "redis://localhost:6379/1"
    voice_max_duration_seconds: int = 45
    scheduler_interval_seconds: int = 60
    reminder_precompute_days: int = 7
    refill_alert_days_before: int = 5
    pending_action_ttl_minutes: int = 30
    user_timezone: str = "Asia/Kolkata"
    debug: bool = False
    # Logs full request bodies, which contain user health data. Opt in explicitly.
    log_requests: bool = False


settings = Settings()
