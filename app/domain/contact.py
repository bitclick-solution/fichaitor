"""Contact normalization for the waitlist. Pure functions."""

import re

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
_E164 = re.compile(r"^\+\d{8,15}$")
_SPANISH_MOBILE = re.compile(r"^[67]\d{8}$")


class InvalidContact(ValueError):
    pass


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise InvalidContact("Ese correo no parece válido.")
    return email


def normalize_phone(value: str) -> str:
    """Return the number in E.164. A bare Spanish mobile gets the +34 prefix."""
    digits = re.sub(r"[\s\-().]", "", value.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if _SPANISH_MOBILE.match(digits):
        digits = "+34" + digits
    if not _E164.match(digits):
        raise InvalidContact(
            "Ese número no parece válido. Escríbelo con el prefijo, como +34 600 000 000."
        )
    return digits


def normalize_contact(channel: str, value: str) -> str:
    if channel == "email":
        return normalize_email(value)
    if channel == "whatsapp":
        return normalize_phone(value)
    raise InvalidContact("Canal no admitido.")
