from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    places_api_key: str = ""
    fake_places: bool = False
    diagnosis_rate_limit: int = 10  # per client IP
    diagnosis_rate_window_seconds: int = 3600
    diagnosis_cache_seconds: int = 86400
    trust_proxy_headers: bool = False
    database_url: str = ""
    waitlist_rate_limit: int = 5  # per client IP
    waitlist_rate_window_seconds: int = 3600
