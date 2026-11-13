"""Which Gemini provider to call, from the settings. The only place that reads
FICHAITOR_GEMINI_PROVIDER: whoever uses the model gets what `GeminiClient` takes and
does not know which provider is behind it."""

from app.adapters.gemini_auth import application_default, from_file
from app.adapters.gemini_client import GeminiBackend, VertexApiKeyBackend, VertexBackend
from app.screenshot_config import ScreenshotSettings


def gemini_credentials(settings: ScreenshotSettings) -> str | GeminiBackend | None:
    """The key of AI Studio, a Vertex AI backend, or None when the model is not
    configured (the callers then go on without it, as they always did).

    With Vertex, an API key (FICHAITOR_VERTEX_API_KEY) is used when there is one;
    otherwise the credentials file is read here, at start-up: a missing or broken
    file stops the process with a fixed message (gemini_auth.VertexAuthError), never
    with its content."""
    if settings.gemini_provider == "vertex":
        api_key = settings.vertex_api_key.get_secret_value().strip()
        if api_key:  # the key wins: no file nor ADC is read
            return VertexApiKeyBackend(
                api_key, settings.vertex_project.strip(), settings.vertex_location.strip()
            )
        path = settings.vertex_credentials_file.strip()
        tokens = from_file(path) if path else application_default()
        return VertexBackend(
            settings.vertex_project.strip(), settings.vertex_location.strip(), tokens
        )
    api_key = settings.gemini_api_key.get_secret_value()
    return api_key or None
