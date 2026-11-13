"""Baja manual: soporte tramita una baja pedida por correo.

Hace lo mismo que la baja del panel (docs/retencion.md), sin código: la persona la ha
pedido por correo desde la dirección de la cuenta. Igual que la del panel, queda
PROGRAMADA 48 horas y a la dirección de la cuenta le llega el correo de «baja programada»
con su enlace de restauración (B15 de docs/seguridad/revision-2026-11-12-c.md): si el
correo no lo mandó el dueño, la restaura con un clic y no se ha borrado nada. Pasado el
plazo, el worker la hace efectiva: revoca el acceso en Google, borra los tokens y empiezan
los plazos de borrado. El procedimiento está en docs/despliegue.md, «Tareas habituales».

--inmediata: solo para casos excepcionales y con el visto bueno del responsable (por
ejemplo, lo ordena una autoridad, o el dueño no puede recibir correo en esa dirección y lo
ha confirmado por otra vía verificable). Efectiva al momento, sin correo ni enlace: no
tiene vuelta atrás.

Se ejecuta en el servidor, con el rol PROPIETARIO de la base de datos, en un contenedor de
un solo uso (servicio fichaitor-baja de docker-compose.prod.yml, I20), nunca dentro del
worker. La URL va en la variable FICHAITOR_OWNER_DATABASE_URL o, en ese contenedor, se
compone con POSTGRES_PASSWORD de db.env; nunca en la línea de órdenes. Por defecto solo
enseña lo que haría; con --confirmar lo aplica.

    python scripts/baja_manual.py --email dueno@example.com --scope ficha \\
        --motivo "Ticket 1234" [--location loc_...] [--inmediata] [--confirmar]

Cómo encaja con las reglas de la base (disparador offboarding_guard, M5):
- Una baja normal se programa con al menos 47 horas, y el worker no hace efectiva una que
  no las tenga. Este script la escribe con reason = 'email_request' y delete_at a 48
  horas (con --inmediata, delete_at = confirmed_at). Solo el propietario puede escribir
  esa razón: el disparador le exime (fichaitor_is_owner) y la rechaza a cualquier otro
  rol, y la API no tiene privilegio sobre esa columna. El worker acepta sin las 47 horas
  solo las que llevan esa razón.
- Deja una línea en audit_log (evento 'offboarding_email_request', written_by = el
  usuario de la base, el motivo en text) y otra en la salida estándar.

Nada de lo que imprime es un secreto: ni tokens ni el correo completo.

Códigos de salida: 0 hecho (o simulación correcta); 1 no se puede tramitar así;
2 uso o configuración (rol que no es el propietario, falta la URL).
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import asyncpg

# Ejecutado como archivo (python scripts/baja_manual.py), la raíz del proyecto no está
# en la ruta de importación.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.offboarding import (  # noqa: E402
    APPLY_KIND,
    APPLY_MAX_ATTEMPTS,
    MAIL_MAX_ATTEMPTS,
    MAIL_SCHEDULED,
    MIN_GRACE_HOURS,
    REASON_EMAIL_REQUEST,
    SCOPE_ACCOUNT,
    SCOPE_PROFILE,
    SCOPES,
    SEND_MAIL_KIND,
    mask_email,
    new_restore_token,
    restore_token_hash,
)
from app.domain.proposals import (  # noqa: E402
    CANCELLED_BY_LEAVE,
    OUTCOME_OFFBOARDING,
    ProposalState,
)

URL_VARIABLE = "FICHAITOR_OWNER_DATABASE_URL"
# I20: en el contenedor de un solo uso (servicio fichaitor-baja), la URL se compone con la
# contraseña de db.env y el servidor que fija el Compose.
HOST_VARIABLE = "FICHAITOR_OWNER_DB_HOST"
# Como en el panel: 48 horas para restaurarla desde el enlace del correo.
GRACE = timedelta(hours=MIN_GRACE_HOURS)
AUDIT_EVENT = "offboarding_email_request"
MOTIVO_MIN, MOTIVO_MAX = 3, 500
_LOCATION_ID = re.compile(r"loc_[0-9a-z]{4,32}")
_EMAIL_LIKE = re.compile(r"\S+@\S+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

OK, REFUSED, USAGE = 0, 1, 2


class Refused(Exception):
    """No se puede tramitar así. El mensaje es para quien lo ejecuta."""

    def __init__(self, message: str, code: int = REFUSED) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Plan:
    account_id: str
    email: str
    google_sub: str
    scope: str
    locations: tuple[tuple[str, str], ...]  # (loc_..., título)
    named_location: str | None
    pending: tuple[str, str, datetime] | None  # (id, alcance, delete_at) de la del panel


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="baja_manual.py",
        description="Tramita una baja pedida por correo. Sin --confirmar, solo la enseña.",
    )
    parser.add_argument("--email", required=True, help="Correo de la cuenta")
    parser.add_argument("--scope", required=True, choices=SCOPES)
    parser.add_argument(
        "--location", help="Ficha (loc_...). Obligatoria con ficha si la cuenta tiene más de una"
    )
    parser.add_argument(
        "--motivo", required=True, help="Referencia de la petición, sin datos personales"
    )
    parser.add_argument(
        "--inmediata",
        action="store_true",
        help="Solo casos excepcionales: efectiva al momento, sin plazo ni enlace de restauración",
    )
    parser.add_argument("--confirmar", action="store_true", help="Aplica la baja")
    return parser.parse_args(argv)


def checked_motivo(motivo: str) -> str:
    motivo = motivo.strip()
    if not MOTIVO_MIN <= len(motivo) <= MOTIVO_MAX or _CONTROL.search(motivo):
        raise Refused(
            f"El motivo debe tener entre {MOTIVO_MIN} y {MOTIVO_MAX} caracteres, en una línea."
        )
    if _EMAIL_LIKE.search(motivo):
        # Se guarda 12 meses en la auditoría: una referencia, no el correo de nadie.
        raise Refused("El motivo no puede llevar una dirección de correo: usa la referencia.")
    return motivo


async def make_plan(conn: asyncpg.Connection, args: argparse.Namespace) -> Plan:
    if args.scope == SCOPE_ACCOUNT and args.location:
        raise Refused("--location solo vale con --scope ficha.")
    if args.location and not _LOCATION_ID.fullmatch(args.location):
        raise Refused("--location no es un identificador de ficha (loc_...).")
    accounts = await conn.fetch(
        """
        SELECT id::text AS id, email, google_sub, disconnected_at FROM account
        WHERE lower(email) = lower($1) AND deleted_at IS NULL
        ORDER BY created_at
        """,
        args.email.strip(),
    )
    if not accounts:
        raise Refused("No hay ninguna cuenta activa con ese correo.")
    if len(accounts) > 1:
        raise Refused(
            f"Hay {len(accounts)} cuentas activas con ese correo: no se tramita con este "
            "script. Escala el caso."
        )
    account = accounts[0]
    if args.scope == SCOPE_PROFILE and account["disconnected_at"] is not None:
        raise Refused("La cuenta ya está desconectada de Google: no hay ficha que dar de baja.")
    rows = await conn.fetch(
        "SELECT id, title FROM location WHERE account_id = $1::uuid AND removed_at IS NULL"
        " ORDER BY id",
        account["id"],
    )
    locations = tuple((r["id"], r["title"]) for r in rows)
    if args.scope == SCOPE_PROFILE:
        ids = [loc for loc, _ in locations]
        listed = ", ".join(f"{loc} «{title}»" for loc, title in locations)
        if args.location and args.location not in ids:
            raise Refused(f"Esa ficha no es de esta cuenta. Sus fichas: {listed or 'ninguna'}.")
        if len(locations) > 1 and not args.location:
            raise Refused(
                f"La cuenta tiene {len(locations)} fichas: indica con --location cuál nombra "
                f"el correo. Sus fichas: {listed}."
            )
    pending = await conn.fetchrow(
        """
        SELECT id::text AS id, scope, delete_at FROM offboarding
        WHERE account_id = $1::uuid AND restored_at IS NULL AND effective_at IS NULL
        """,
        account["id"],
    )
    return Plan(
        account_id=account["id"],
        email=account["email"],
        google_sub=account["google_sub"],
        scope=args.scope,
        locations=locations,
        named_location=args.location,
        pending=(pending["id"], pending["scope"], pending["delete_at"]) if pending else None,
    )


def describe(plan: Plan, out: Callable[[str], None], immediate: bool = False) -> None:
    out(f"Cuenta: {plan.account_id} ({mask_email(plan.email)})")
    if plan.scope == SCOPE_PROFILE:
        out("Alcance: ficha (desconectar Google; la cuenta sigue existiendo, sin fichas)")
    else:
        out("Alcance: cuenta (borrar la cuenta)")
    if plan.locations:
        out(f"Fichas de la cuenta ({len(plan.locations)}):")
        for loc, title in plan.locations:
            mark = " (la del correo)" if loc == plan.named_location else ""
            out(f"  - {loc} «{title}»{mark}")
    else:
        out("Fichas de la cuenta: ninguna")
    if plan.scope == SCOPE_PROFILE and len(plan.locations) > 1:
        out(
            f"Aviso: Google concede el acceso por usuario, no por ficha. Se desconectan las "
            f"{len(plan.locations)} fichas de la cuenta. Si solo quiere dejar una, que la "
            "desactive en el panel: eso no es una baja."
        )
    if plan.pending:
        _, scope, delete_at = plan.pending
        out(
            f"Ya tiene una baja programada desde el panel ({scope}, {delete_at.isoformat()}): "
            "esta la sustituye."
        )
    out("Qué se hará:")
    if immediate:
        out(
            "  - INMEDIATA: efectiva al momento, sin plazo ni enlace de restauración. No tiene "
            "vuelta atrás (reason = email_request)."
        )
    else:
        out(
            f"  - Baja programada {MIN_GRACE_HOURS} horas, como en el panel "
            "(reason = email_request)."
        )
        out(
            f"  - Correo de «baja programada» a {mask_email(plan.email)}, con el enlace para "
            "restaurarla hasta entonces."
        )
    out("  - Las rutas de la ficha dejan de servir datos y se cancelan las propuestas.")
    if plan.scope == SCOPE_ACCOUNT:
        out("  - Se cierran todas las sesiones y se desvinculan los chats.")
    if immediate:
        out("  - El worker revoca el acceso en Google y borra los tokens (job apply_offboarding).")
    else:
        out(
            "  - Pasado el plazo, el worker revoca el acceso en Google y borra los tokens "
            "(job apply_offboarding)."
        )
    out("  - Lo obtenido de Google se borra en 30 días; el registro de respuestas, en 12 meses.")


async def apply(
    conn: asyncpg.Connection,
    plan: Plan,
    motivo: str,
    now: datetime,
    *,
    immediate: bool = False,
) -> tuple[str, datetime]:
    """Escribe la baja, su línea de auditoría, el correo de baja programada (salvo con
    immediate) y el job que la hace efectiva, en una transacción. Devuelve el id de la
    baja y cuándo se hace efectiva."""
    delete_at = now if immediate else now + GRACE
    token = None if immediate else new_restore_token()
    async with conn.transaction():
        still = await conn.fetchval(
            "SELECT google_sub FROM account WHERE id = $1::uuid AND deleted_at IS NULL FOR UPDATE",
            plan.account_id,
        )
        if still != plan.google_sub:
            raise Refused("La cuenta ha cambiado mientras tanto. Vuelve a ejecutarlo.")
        # Una baja del panel todavía programada queda sustituida, como hace la API.
        await conn.execute(
            """
            UPDATE offboarding SET restored_at = $2, restore_token_hash = NULL
            WHERE account_id = $1::uuid AND restored_at IS NULL AND effective_at IS NULL
            """,
            plan.account_id,
            now,
        )
        offboarding_id = await conn.fetchval(
            """
            INSERT INTO offboarding
                (account_id, scope, google_sub, confirmed_at, delete_at, reason,
                 restore_token_hash)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
            RETURNING id::text
            """,
            plan.account_id,
            plan.scope,
            plan.google_sub,
            now,
            delete_at,
            REASON_EMAIL_REQUEST,
            restore_token_hash(token) if token else None,
        )
        # Lo mismo que al confirmar en el panel (B12): lo que esperaba en el chat no sale.
        await conn.execute(
            """
            UPDATE proposal SET state = $3, outcome_reason = $4, step_token = $5,
                                message_refs = '{}'::jsonb
            WHERE account_id = $1::uuid AND state = ANY($2::text[])
            """,
            plan.account_id,
            [state.value for state in CANCELLED_BY_LEAVE],
            ProposalState.CANCELLED.value,
            OUTCOME_OFFBOARDING,
            secrets.token_hex(3),
        )
        await conn.execute(
            """
            UPDATE conversation_lane SET active_proposal_id = NULL, updated_at = now()
            WHERE account_id = $1::uuid AND active_proposal_id IS NOT NULL
            """,
            plan.account_id,
        )
        if plan.scope == SCOPE_ACCOUNT:
            await conn.execute(
                "UPDATE session SET revoked_at = $2 WHERE account_id = $1::uuid"
                " AND revoked_at IS NULL",
                plan.account_id,
                now,
            )
            await conn.execute(
                "UPDATE channel_identity SET unlinked_at = $2 WHERE account_id = $1::uuid"
                " AND unlinked_at IS NULL",
                plan.account_id,
                now,
            )
            await conn.execute(
                "UPDATE link_code SET used_at = $2 WHERE account_id = $1::uuid AND used_at IS NULL",
                plan.account_id,
                now,
            )
        await conn.execute(
            """
            INSERT INTO audit_log
                (event, account_id, action_id, action_type, location, review_id,
                 gbp_review_id, text, confirmed_at, reason)
            VALUES ($1, $2::uuid, $3, $4, $5, '', '', $6, $7, $8)
            """,
            AUDIT_EVENT,
            plan.account_id,
            offboarding_id,
            f"offboarding_{plan.scope}",
            plan.named_location or "",
            motivo,
            now,
            REASON_EMAIL_REQUEST,
        )
        if token is not None:
            # El mismo correo que manda el panel al confirmar: la fecha y el enlace
            # #restaurar=. Va a la dirección de la cuenta, no a la que escribió: si el
            # correo era falso, el dueño de verdad se entera y la restaura.
            await conn.execute(
                "INSERT INTO job (kind, payload, max_attempts) VALUES ($1, $2::jsonb, $3)",
                SEND_MAIL_KIND,
                json.dumps(
                    {
                        "template": MAIL_SCHEDULED,
                        "to": plan.email,
                        "scope": plan.scope,
                        "delete_at": delete_at.isoformat(),
                        "token": token,
                    }
                ),
                MAIL_MAX_ATTEMPTS,
            )
        # El job que la hace efectiva espera a la fecha y decide otra vez al correr: una
        # baja restaurada se deja como está.
        await conn.execute(
            """
            INSERT INTO job (kind, payload, run_at, idempotency_key, max_attempts)
            VALUES ($1, $2::jsonb, $3, $4, $5)
            """,
            APPLY_KIND,
            json.dumps({"offboarding_id": offboarding_id}),
            delete_at,
            f"{APPLY_KIND}:{offboarding_id}",
            APPLY_MAX_ATTEMPTS,
        )
    return offboarding_id, delete_at


async def wait_effective(
    conn: asyncpg.Connection, offboarding_id: str, seconds: float
) -> asyncpg.Record | None:
    """La baja ya efectiva y revocada, o None si el worker no ha terminado en ese tiempo."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        row = await conn.fetchrow(
            "SELECT effective_at, revoke_state FROM offboarding WHERE id = $1::uuid",
            offboarding_id,
        )
        if row is not None and row["revoke_state"] is not None:
            return row
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(1)


async def run(
    argv: list[str],
    *,
    url: str,
    now: datetime | None = None,
    wait_seconds: float = 60,
    out: Callable[[str], None] = print,
) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return USAGE if exc.code else OK
    conn = await asyncpg.connect(url)
    try:
        if not await conn.fetchval("SELECT fichaitor_is_owner('offboarding'::regclass)"):
            raise Refused("Ejecútalo con el rol propietario de la base de datos.", USAGE)
        motivo = checked_motivo(args.motivo)
        plan = await make_plan(conn, args)
        describe(plan, out, args.inmediata)
        if not args.confirmar:
            out("Simulación: no se ha cambiado nada. Repite con --confirmar para aplicarla.")
            return OK
        when = now or datetime.now(UTC)
        offboarding_id, delete_at = await apply(conn, plan, motivo, when, immediate=args.inmediata)
        who = await conn.fetchval("SELECT session_user")
        out(f"Baja registrada: {offboarding_id}.")
        # La línea de auditoría también en la salida, para el registro de quien lo ejecuta.
        out(
            json.dumps(
                {
                    "event": "baja_manual",
                    "at": when.isoformat(),
                    "written_by": who,
                    "offboarding_id": offboarding_id,
                    "account_id": plan.account_id,
                    "scope": plan.scope,
                    "location": plan.named_location,
                    "reason": REASON_EMAIL_REQUEST,
                    "inmediata": args.inmediata,
                    "delete_at": delete_at.isoformat(),
                },
                ensure_ascii=False,
            )
        )
        if not args.inmediata:
            out(
                f"Programada: se hace efectiva el {delete_at.isoformat()}. El correo de baja "
                f"programada, con el enlace de restauración, sale hacia {mask_email(plan.email)} "
                "en cuanto el worker tome el job."
            )
            return OK
        done = await wait_effective(conn, offboarding_id, wait_seconds)
        if done is None:
            out(
                "El worker todavía no la ha hecho efectiva: lo hará en cuanto tome el job "
                f"apply_offboarding:{offboarding_id}. Los datos ya no se sirven."
            )
        else:
            out(
                f"Efectiva: {done['effective_at'].isoformat()}. "
                f"Revocación en Google: {done['revoke_state']}."
            )
        return OK
    except Refused as exc:
        out(f"No se ha tramitado: {exc}")
        return exc.code
    finally:
        await conn.close()


def owner_url(env: Mapping[str, str]) -> str | None:
    """La URL del propietario: FICHAITOR_OWNER_DATABASE_URL o, en el contenedor de un solo
    uso (I20), la que se compone con POSTGRES_PASSWORD de db.env y el servidor, el usuario
    y la base que fija el Compose. None si no hay ninguna."""
    url = env.get(URL_VARIABLE)
    if url:
        return url
    host, password = env.get(HOST_VARIABLE), env.get("POSTGRES_PASSWORD")
    if not (host and password):
        return None
    user = env.get("POSTGRES_USER") or "fichaitor"
    database = env.get("POSTGRES_DB") or user
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:5432/{quote(database, safe='')}"
    )


def main() -> int:
    url = owner_url(os.environ)
    if not url:
        print(
            f"Falta {URL_VARIABLE} (URL del rol propietario), o {HOST_VARIABLE} y "
            "POSTGRES_PASSWORD.",
            file=sys.stderr,
        )
        return USAGE
    return asyncio.run(run(sys.argv[1:], url=url))


if __name__ == "__main__":
    sys.exit(main())
