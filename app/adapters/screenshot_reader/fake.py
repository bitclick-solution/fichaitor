"""Screenshot reader without a model, for tests and local development.

With nothing injected, the answer depends on the image format, so that the web can lay
out every screen with `FICHAITOR_FAKE_SCREENSHOT=true`:

- PNG: a reviews tab with four reviews, three of them unanswered, and a suggested reply.
- JPEG: not a Google listing (422).
- WebP: a Google listing, but its overview instead of the reviews (422).
"""

import asyncio
from collections.abc import Mapping

from app.ports.screenshot_reader import ScreenshotReaderUnavailable

DEMO: Mapping[str, object] = {
    "screen": "reviews_tab",
    "confidence": 0.97,
    "business_name": "Bar La Parra (ejemplo)",
    "reviews": [
        {
            "stars": 5,
            "text": "Las mejores tapas del barrio y un trato estupendo.",
            "age": "hace 2 semanas",
            "has_owner_reply": True,
        },
        {
            "stars": 4,
            "text": "Buen sitio para tomar algo. La terraza se llena pronto.",
            "age": "hace 1 mes",
            "has_owner_reply": False,
        },
        {
            "stars": 1,
            "text": "Esperamos cuarenta minutos y la comida llegó fría.",
            "age": "hace 2 meses",
            "has_owner_reply": False,
        },
        {"stars": 5, "text": None, "age": "hace 3 meses", "has_owner_reply": False},
    ],
}
NOT_A_PROFILE: Mapping[str, object] = {"screen": "other", "confidence": 0.95, "reviews": []}
OVERVIEW: Mapping[str, object] = {
    "screen": "profile_overview",
    "confidence": 0.95,
    "business_name": "Bar La Parra (ejemplo)",
    "reviews": [],
}
_BY_FORMAT = {"image/png": DEMO, "image/jpeg": NOT_A_PROFILE, "image/webp": OVERVIEW}

REPLY_LOW = (
    "Sentimos mucho que tu visita no fuera como esperabas. "
    "Tomamos nota de lo que cuentas para mejorar. "
    "Nos gustaría hablarlo contigo en privado para entender bien qué pasó."
)
REPLY_MID = (
    "Gracias por contarnos tu experiencia. Tomamos nota de lo que comentas para seguir mejorando."
)
REPLY_HIGH = (
    "Muchas gracias por tu reseña. "
    "Nos alegra mucho que hayas estado a gusto y esperamos verte pronto por aquí."
)


class FakeScreenshotReader:
    def __init__(
        self,
        facts: object = None,
        reply: object = None,
        fail: bool = False,
        fail_reply: bool = False,
        delay_seconds: float = 0.0,
        reply_delay_seconds: float = 0.0,
    ) -> None:
        self._facts = facts
        self._reply = reply
        self._fail = fail
        self._fail_reply = fail_reply
        self._delay = delay_seconds
        self._reply_delay = reply_delay_seconds
        # Only what was asked, never the image itself.
        self.calls: list[tuple[int, str]] = []
        self.reply_calls: list[tuple[int, str]] = []

    async def read(self, image: bytes, media_type: str) -> Mapping[str, object]:
        self.calls.append((len(image), media_type))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise ScreenshotReaderUnavailable("fallo simulado")
        if self._facts is not None:
            return self._facts
        return _BY_FORMAT.get(media_type, DEMO)

    async def write_reply(self, stars: int, review_text: str) -> str:
        self.reply_calls.append((stars, review_text))
        if self._reply_delay:
            await asyncio.sleep(self._reply_delay)
        if self._fail_reply:
            raise ScreenshotReaderUnavailable("fallo simulado")
        if self._reply is not None:
            return self._reply
        if stars <= 2:
            return REPLY_LOW
        return REPLY_MID if stars == 3 else REPLY_HIGH
