"""Which sentiment model to use, from the same settings as the rest of the Gemini uses."""

from app.adapters.gemini_provider import gemini_credentials
from app.adapters.sentiment_model.fake import FakeSentimentModel
from app.adapters.sentiment_model.gemini import GeminiSentimentModel
from app.ports.sentiment_model import SentimentModel
from app.screenshot_config import ScreenshotSettings


def build_sentiment_model(
    settings: ScreenshotSettings, timeout_seconds: float
) -> SentimentModel | None:
    """The fake model (FICHAITOR_FAKE_SCREENSHOT), the real one (FICHAITOR_GEMINI_MODEL on
    AI Studio or on Vertex AI, see gemini_provider), or None when neither is set: the
    analysis then counts every review by its stars and words its texts by fixed rules."""
    if settings.fake_screenshot:
        return FakeSentimentModel()
    credentials = gemini_credentials(settings)
    if credentials:
        return GeminiSentimentModel(credentials, settings.gemini_model, timeout_seconds)
    return None
