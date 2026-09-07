"""Central settings for Phase A: thresholds, model name, feature flags."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App-wide settings, read from environment variables prefixed `BOT_`."""

    model_config = SettingsConfigDict(env_prefix="BOT_", env_file=".env", extra="ignore")

    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = "gpt-5-mini"
    azure_openai_api_version: str = "v1"
    azure_openai_api_key: str = ""
    confidence_floor: float = 0.6
    fuzzy_match_floor: float = 0.82
    max_clarification_turns: int = 2


settings = Settings()
