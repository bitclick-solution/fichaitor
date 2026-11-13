"""Settings of the screenshot diagnosis. Kept apart from `app.config` on purpose."""

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# An API key of the express mode of Agent Platform only has the projectless endpoint,
# which is global: "use the global endpoint without a project ID or location"
# (docs/despliegue.md, «Gemini en Vertex AI»).
VERTEX_EXPRESS_LOCATION = "global"
EXPRESS_NEEDS_GLOBAL = (
    "FICHAITOR_VERTEX_API_KEY sin FICHAITOR_VERTEX_PROJECT es una clave del modo exprés,"
    " que solo admite la ubicación global (endpoint sin proyecto, procesamiento en"
    " cualquier región del mundo). Para procesar en la UE, pon FICHAITOR_VERTEX_PROJECT"
    " y usa una clave vinculada a una cuenta de servicio; para aceptar global, pon"
    " FICHAITOR_VERTEX_LOCATION=global."
)


class ScreenshotSettings(BaseSettings):
    # hide_input_in_errors: a ValidationError quotes the whole input by default, and the
    # input holds the keys of Gemini and Vertex in clear.
    model_config = SettingsConfigDict(
        env_prefix="FICHAITOR_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    # Who serves Gemini: AI Studio (the key below; the beta) or Vertex AI (a project of
    # Google Cloud with an API key of Agent Platform or a service account; production).
    # See docs/despliegue.md.
    gemini_provider: Literal["aistudio", "vertex"] = "aistudio"
    # Key of the Gemini API (AI Studio only). SecretStr: never in a repr or a traceback.
    gemini_api_key: SecretStr = SecretStr("")
    # With or without the "models/" prefix. Same name for both providers.
    gemini_model: str = "gemini-3.6-flash"
    # Vertex AI only. The multi-region "eu" keeps the ML processing inside the EU and is
    # where the Gemini Flash models are offered in Europe (docs/despliegue.md).
    vertex_project: str = ""
    vertex_location: str = "eu"
    # Service account key (JSON) mounted read-only. Empty: Application Default
    # Credentials (GOOGLE_APPLICATION_CREDENTIALS, gcloud, or the metadata server).
    vertex_credentials_file: str = ""
    # API key of Agent Platform. When set it wins: no service account file nor ADC is
    # read. With a project, a key bound to a service account (any location, "eu" too);
    # without one, a key of the express mode, which only has the global endpoint.
    vertex_api_key: SecretStr = SecretStr("")
    # No model: canned answers, for development and for laying out the web.
    fake_screenshot: bool = False
    # Gemini takes up to 20 MB per request with inline data, base64 and prompts included.
    screenshot_max_bytes: int = Field(default=6_000_000, gt=0, le=12_000_000)
    # Stricter than the text diagnosis (10 per hour): every capture is up to two paid calls.
    screenshot_rate_limit: int = Field(default=3, ge=0)  # per client IP
    screenshot_rate_window_seconds: int = Field(default=3600, gt=0)
    # All clients together, per 24 hours. Caps the bill if someone rotates addresses.
    screenshot_daily_limit: int = Field(default=300, ge=0)
    # Reading and suggested reply together. If the reading does not fit, the route
    # answers 503; if only the reply does not fit, it is left out.
    screenshot_timeout_seconds: float = Field(default=30.0, gt=0)
    # Below this confidence about which screen the image shows, the capture is rejected.
    screenshot_min_confidence: float = Field(default=0.6, ge=0, le=1)

    @model_validator(mode="after")
    def _vertex_needs_a_project(self) -> "ScreenshotSettings":
        # Asked for Vertex but not said where: fail at start-up, not at the first call.
        if self.gemini_provider == "vertex" and not self.fake_screenshot:
            if not self.vertex_location.strip():
                raise ValueError("FICHAITOR_VERTEX_LOCATION no puede estar vacía")
            if self.vertex_api_key.get_secret_value().strip():
                # The express endpoint has no project and is only global: say so now,
                # never move the processing out of the EU without being asked.
                if (
                    not self.vertex_project.strip()
                    and self.vertex_location.strip() != VERTEX_EXPRESS_LOCATION
                ):
                    raise ValueError(EXPRESS_NEEDS_GLOBAL)
            elif not self.vertex_project.strip():
                raise ValueError(
                    "FICHAITOR_GEMINI_PROVIDER=vertex exige FICHAITOR_VERTEX_PROJECT"
                    " (o FICHAITOR_VERTEX_API_KEY con FICHAITOR_VERTEX_LOCATION=global)"
                )
        return self
