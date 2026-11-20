from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ollama_base_url: str = "http://127.0.0.1:11434"
    default_model: str = "qwen2.5:0.5b"
    default_temperature: float = Field(default=0.7, ge=0, le=2)
    default_system_prompt: str = "You are a concise, helpful assistant."
    database_path: str = "./data/chat.db"
    max_history_messages: int = Field(default=20, ge=0)
    connect_timeout_seconds: float = Field(default=5, gt=0)
    read_timeout_seconds: float = Field(default=60, gt=0)
    total_timeout_seconds: float = Field(default=150, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=0.5, ge=0)
    input_cost_per_million_usd: float = Field(default=0, ge=0)
    output_cost_per_million_usd: float = Field(default=0, ge=0)
