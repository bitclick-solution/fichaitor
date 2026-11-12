from typing import Protocol


class ReplyWriterUnavailable(Exception):
    """The model failed, timed out or answered something unusable. The message is a
    fixed string: never the review, the draft or anything the provider said."""


class ReplyWriter(Protocol):
    """Drafts the owner's reply to one review.

    The writer gets the stars and the text of the review and nothing else: no author,
    no business, no identifiers. The review is data written by a third party, and so
    is the draft that comes back: callers must pass it through `validate_reply` in
    `app.domain.review_reply`. A writer has no way to publish anything.
    """

    async def write_reply(self, stars: int, review_text: str) -> str:
        """Draft the reply. `review_text` may be empty."""
        ...
