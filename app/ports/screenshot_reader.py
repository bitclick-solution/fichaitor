from collections.abc import Mapping
from typing import Protocol

from app.ports.reply_writer import ReplyWriterUnavailable


class ScreenshotReaderUnavailable(ReplyWriterUnavailable):
    """The model failed, timed out or answered something unusable."""


class ScreenshotReader(Protocol):
    """Reads the reviews tab of a Google listing from a screenshot and drafts one reply.

    Both answers are untrusted, because the text inside the image is written by third
    parties: callers must pass them through `parse_facts` and `validate_reply` in
    `app.domain.screenshot_diagnosis`.

    Implementations must not keep the image or the review, or write them anywhere.
    """

    async def read(self, image: bytes, media_type: str) -> Mapping[str, object]:
        """Return what is visible: `screen`, `confidence`, `business_name` and `reviews`
        (each with `stars`, `text`, `age` and `has_owner_reply`). None means "not
        visible". The names of the people who wrote the reviews are never returned."""
        ...

    async def write_reply(self, stars: int, review_text: str) -> str:
        """Draft the owner's reply to one review. `review_text` may be empty."""
        ...
