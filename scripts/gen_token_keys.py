"""Genera el par de claves que cifra los tokens de Google.

    uv run python scripts/gen_token_keys.py

La clave pública va en el .env de la API (FICHAITOR_TOKEN_PUBLIC_KEY).
La privada va SOLO en el entorno del worker y en una copia de seguridad aparte.
Si se pierde la privada, todos los clientes tienen que volver a conectar Google.
"""

from app.adapters.crypto.sealed import generate_keypair

public, private = generate_keypair()
print(f"FICHAITOR_TOKEN_PUBLIC_KEY={public}")
print(f"FICHAITOR_TOKEN_PRIVATE_KEY={private}   # solo worker; no la pongas en el .env de la API")
