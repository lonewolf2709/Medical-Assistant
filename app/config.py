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
    # Minimum gap between two "you are running low" alerts for the same
    # medication, so the daily recompute cannot nag someone at zero stock.
    refill_realert_cooldown_days: int = 3
    pending_action_ttl_minutes: int = 30
    # Telegram HTTP budgets. A tight connect budget turns an event-loop stall
    # into a spurious ConnectTimeout even on a healthy network.
    telegram_timeout_seconds: float = 10.0
    telegram_connect_timeout_seconds: float = 10.0
    # A dose reminder later than this is retired undelivered rather than sent
    # as though it were due now.
    reminder_max_lateness_minutes: int = 120
    # Per-Telegram-user request budget.
    user_rate_limit_per_minute: int = 20
    user_timezone: str = "Asia/Kolkata"
    debug: bool = False
    # Logs full request bodies, which contain user health data. Opt in explicitly.
    log_requests: bool = False


settings = Settings()
