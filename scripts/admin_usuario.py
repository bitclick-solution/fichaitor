"""Personas del backoffice: alta, restablecer y desactivar (docs/admin-facturacion.md, 6.2).

Lo ejecuta el propietario en el servidor, con el rol PROPIETARIO de la base de datos, en
un contenedor de un solo uso (servicio fichaitor-admin-usuario de
deploy/docker-compose.prod.yml, como fichaitor-baja). El backoffice no puede crear
personas ni cambiar roles: solo este script, con las funciones admin_owner_* de
db/schema_admin.sql, que comprueban que quien llama es el propietario.

    python scripts/admin_usuario.py alta --usuario ana --rol soporte
    python scripts/admin_usuario.py restablecer --usuario ana
    python scripts/admin_usuario.py desactivar --usuario ana
    python scripts/admin_usuario.py correo --usuario ana --correo ana@bitclick.es

- alta: crea a la persona y escribe un enlace de alta, válido 24 horas y de un solo uso:
  http://localhost:8090/#alta=<token>. La persona lo abre por el túnel, elige su
  contraseña, escanea el QR del TOTP y recibe sus 10 códigos de recuperación.
- restablecer: olvidó la contraseña o perdió el TOTP y los códigos. Comprueba antes su
  identidad en persona o por teléfono. Cierra sus sesiones, retira sus códigos y escribe
  un enlace de alta nuevo.
- desactivar: ya no entra. Cierra sus sesiones. Borra también su clave SSH del túnel.
- correo: la dirección a la que le llegan los envíos de prueba de las comunicaciones
  (docs/manual-backoffice.md, «Enviar una comunicación por correo»). --quitar la borra.

Nunca imprime ni pide contraseñas. El enlace es el único secreto que imprime: de la base
solo sale su huella (SHA-256). Dáselo a la persona por un canal privado.

La URL va en FICHAITOR_OWNER_DATABASE_URL o, en el contenedor, se compone con
POSTGRES_PASSWORD de db.env; nunca en la línea de órdenes.

Códigos de salida: 0 hecho; 1 no se puede hacer (usuario que no existe o ya existe);
2 uso o configuración (falta la URL, rol que no es el propietario).
"""

import argparse
import asyncio
import hashlib
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping
from urllib.parse import quote

import asyncpg

OK, REFUSED, USAGE = 0, 1, 2
HOST_VARIABLE = "FICHAITOR_OWNER_DB_HOST"
ROLES = ("soporte", "finanzas", "propietario")
USERNAME = re.compile(r"^[a-z][a-z0-9_.-]{1,39}$")
LINK = "http://localhost:8090/#alta={token}"
EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ScriptError(Exception):
    def __init__(self, message: str, code: int = REFUSED) -> None:
        super().__init__(message)
        self.code = code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="admin_usuario.py", description="Personas del backoffice de Fichaitor."
    )
    sub = parser.add_subparsers(dest="orden", required=True)
    alta = sub.add_parser("alta", help="Da de alta a una persona y escribe su enlace de alta.")
    alta.add_argument("--usuario", required=True)
    alta.add_argument("--rol", required=True, choices=ROLES)
    reset = sub.add_parser("restablecer", help="Enlace de alta nuevo; cierra sus sesiones.")
    reset.add_argument("--usuario", required=True)
    off = sub.add_parser("desactivar", help="La persona ya no puede entrar.")
    off.add_argument("--usuario", required=True)
    mail = sub.add_parser("correo", help="Dirección para las pruebas de las comunicaciones.")
    mail.add_argument("--usuario", required=True)
    which = mail.add_mutually_exclusive_group(required=True)
    which.add_argument("--correo")
    which.add_argument("--quitar", action="store_true")
    args = parser.parse_args(argv)
    args.usuario = args.usuario.strip().lower()
    if not USERNAME.match(args.usuario):
        parser.error("--usuario: de 2 a 40 caracteres, minúsculas, cifras, punto, guion o _.")
    if args.orden == "correo" and args.correo is not None:
        args.correo = args.correo.strip().lower()
        if not EMAIL.match(args.correo):
            parser.error("--correo: no es una dirección de correo.")
    return args


def owner_url(env: Mapping[str, str]) -> str | None:
    """La URL del propietario: FICHAITOR_OWNER_DATABASE_URL o, en el contenedor de un solo
    uso, la que se compone con POSTGRES_PASSWORD de db.env y el servidor."""
    if env.get("FICHAITOR_OWNER_DATABASE_URL"):
        return env["FICHAITOR_OWNER_DATABASE_URL"]
    host, password = env.get(HOST_VARIABLE), env.get("POSTGRES_PASSWORD")
    if not host or not password:
        return None
    user = env.get("POSTGRES_USER") or "fichaitor"
    database = env.get("POSTGRES_DB") or user
    return f"postgresql://{quote(user)}:{quote(password, safe='')}@{host}:5432/{quote(database)}"


def _new_link() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return LINK.format(token=token), hashlib.sha256(token.encode()).hexdigest()


async def run(
    args: argparse.Namespace, conn: asyncpg.Connection, out: Callable[[str], None]
) -> int:
    try:
        is_owner = await conn.fetchval("SELECT admin_caller_is_owner()")
    except asyncpg.InsufficientPrivilegeError:
        is_owner = False
    if not is_owner:
        raise ScriptError("Hay que ejecutarlo con el rol propietario de la base.", USAGE)
    try:
        if args.orden == "alta":
            link, hashed = _new_link()
            await conn.fetchval(
                "SELECT admin_owner_create_user($1, $2, $3)", args.usuario, args.rol, hashed
            )
            out(f"Alta de {args.usuario} ({args.rol}). Enlace, válido 24 horas y de un solo uso:")
            out(link)
            out("Dáselo por un canal privado. Lo abre con el túnel abierto (docs/despliegue.md).")
        elif args.orden == "restablecer":
            link, hashed = _new_link()
            await conn.fetchval("SELECT admin_owner_reset_user($1, $2)", args.usuario, hashed)
            out(f"{args.usuario}: sesiones cerradas y códigos retirados. Enlace de alta nuevo:")
            out(link)
        elif args.orden == "correo":
            address = None if args.quitar else args.correo
            await conn.fetchval("SELECT admin_owner_set_email($1, $2)", args.usuario, address)
            out(
                f"{args.usuario}: dirección de las pruebas "
                + ("quitada." if address is None else "guardada.")
            )
        else:
            await conn.fetchval("SELECT admin_owner_set_active($1, false)", args.usuario)
            out(f"{args.usuario} desactivado y sus sesiones cerradas.")
            out("Borra también su línea de authorized_keys del usuario fichaitor-tunel.")
    except asyncpg.UniqueViolationError:
        raise ScriptError(f"Ya existe una persona llamada {args.usuario}.") from None
    except asyncpg.CheckViolationError as exc:
        raise ScriptError(f"No se puede: {exc.message}") from None
    except asyncpg.InsufficientPrivilegeError:
        raise ScriptError("Hay que ejecutarlo con el rol propietario de la base.", USAGE) from None
    return OK


async def _main(args: argparse.Namespace, url: str) -> int:
    conn = await asyncpg.connect(url)
    try:
        return await run(args, conn, print)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    url = owner_url(os.environ)
    if not url:
        print(
            "Falta FICHAITOR_OWNER_DATABASE_URL (o FICHAITOR_OWNER_DB_HOST y POSTGRES_PASSWORD).",
            file=sys.stderr,
        )
        return USAGE
    try:
        return asyncio.run(_main(args, url))
    except ScriptError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
