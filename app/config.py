from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    jwt_secret: str = Field(min_length=32)
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 30
    claim_minutes: int = 15
    cors_origins: str = "https://validation-frontend-phi.vercel.app"
    auth_rate_limit: int = 10
    frontend_url: str = "https://validation-frontend-phi.vercel.app"
    password_reset_ttl_minutes: int = 30
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    resend_api_key: str | None = None
    resend_from_email: str = "Ekyama <onboarding@resend.dev>"

    @property
    def origins(self) -> list[str]:
        raw = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        for local in ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000", "http://127.0.0.1:3000"):
            if local not in raw:
                raw.append(local)
        return raw


@lru_cache
def get_settings() -> Settings:
    return Settings()
