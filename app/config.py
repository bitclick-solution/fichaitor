from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    places_api_key: str = ""
    fake_places: bool = False
    # Every diagnosis is a paid request to Google and counts, repeated ones included.
    diagnosis_rate_limit: int = 10  # per client IP, or per account behind a session
    diagnosis_rate_window_seconds: int = 3600
    # How long "search text -> place_id" is remembered. Only the identifier is kept:
    # Google's terms allow storing it; the content of the place is asked for every time.
    place_id_cache_seconds: int = 30 * 86400
    trust_proxy_headers: bool = False
    database_url: str = ""
    waitlist_rate_limit: int = 5  # per client IP
    waitlist_rate_window_seconds: int = 3600

    # Google OAuth. Changing Google Cloud project means changing the first three values.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    fake_google: bool = False
    oauth_state_secret: str = ""
    token_public_key: str = ""
    token_key_version: int = 1
    post_connect_url: str = "/app"
    cookie_secure: bool = True
    oauth_start_rate_limit: int = 20  # per client IP and hour
