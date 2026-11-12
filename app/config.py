from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FICHAITOR_", env_file=".env", extra="ignore")

    places_api_key: str = ""
    fake_places: bool = False
    # Every public diagnosis is a paid request to Google and counts, repeated ones
    # included. The diagnosis behind a session reads stored facts and has no limit.
    diagnosis_rate_limit: int = 10  # per client IP
    diagnosis_rate_window_seconds: int = 3600
    # All clients together, per UTC day: what bounds the bill when someone rotates
    # addresses. Each diagnosis is paid to Places (docs/fase0/places-coste.md).
    diagnosis_daily_limit: int = Field(default=500, ge=0)
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

    # Panel summary. The worker writes it; the API only asks for a new sync.
    # A summary older than this is served with stale=true and a sync is enqueued.
    summary_stale_hours: float = 6
    # At most one sync per profile in each window of this many seconds.
    sync_window_seconds: int = 900

    # Replies to reviews. A draft is a paid call to the model: at most this many per
    # account in each window. A draft served from the stored one does not count.
    review_draft_rate_limit: int = 30
    review_draft_rate_window_seconds: int = 86400
    review_draft_timeout_seconds: float = 20.0

    # Leaving (docs/contrato-web-api.md, "Baja"). A leave is confirmed with a code
    # sent by email. The secret keys the fingerprint of the codes; empty: the one of
    # the OAuth state is used.
    offboarding_secret: str = ""
    offboarding_code_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    offboarding_code_max_attempts: int = Field(default=5, ge=1, le=20)
    offboarding_code_rate_limit: int = Field(default=5, ge=1)  # per account
    offboarding_code_rate_window_seconds: int = Field(default=3600, ge=1)
    # Hours a confirmed leave stays scheduled, and can be restored, before it is effective.
    # At least 48: the database refuses a leave of less than 47 hours (M5).
    offboarding_grace_hours: float = Field(default=48.0, ge=48)
    offboarding_restore_rate_limit: int = Field(default=20, ge=1)  # per client IP and hour
