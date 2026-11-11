"""Public-key encryption for Google refresh tokens (libsodium sealed boxes).

The API holds only the public key: it can encrypt a token but can never read
one back. The private key lives only in the worker.
"""

import base64

from nacl.public import PrivateKey, PublicKey, SealedBox


def generate_keypair() -> tuple[str, str]:
    """Return (public_key, private_key), both base64."""
    private = PrivateKey.generate()
    return (
        base64.b64encode(bytes(private.public_key)).decode(),
        base64.b64encode(bytes(private)).decode(),
    )


def seal(public_key_b64: str, plaintext: str) -> str:
    box = SealedBox(PublicKey(base64.b64decode(public_key_b64)))
    return base64.b64encode(box.encrypt(plaintext.encode())).decode()


def unseal(private_key_b64: str, ciphertext_b64: str) -> str:
    """Worker side only."""
    box = SealedBox(PrivateKey(base64.b64decode(private_key_b64)))
    return box.decrypt(base64.b64decode(ciphertext_b64)).decode()
