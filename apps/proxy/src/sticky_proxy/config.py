from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    bot_username: str
    jwt_secret: str
    pair_code_ttl_sec: int = 300
    host: str = "127.0.0.1"
    port: int = 8088

    model_config = SettingsConfigDict(
        env_prefix="STICKY_PROXY_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
