"""Central settings for Phase A: thresholds, model name, feature flags."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    llm_model: str = "claude-sonnet-5"
    confidence_floor: float = 0.6
    fuzzy_match_floor: float = 0.82
    max_clarification_turns: int = 2

    class Config:
        env_prefix = "BOT_"
        env_file = ".env"
        extra = "ignore"


settings = Settings()
