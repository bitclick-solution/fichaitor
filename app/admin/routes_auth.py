"""Sign-in, enrollment and the person's own password and TOTP (docs/admin-facturacion.md,
6.2; contract in docs/contrato-backoffice.md, «Sesión» and «Alta»).

Sign-in in two steps: user and password (same answer and the same time whether the user
exists or not), then a TOTP code or a recovery code. 5 failures lock the user (15
minutes, doubling); 20 failures in an hour stop the whole backoffice for the rest of the
hour. All of it is counted and audited by the database.
"""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.admin.auth import (
    AuthKit,
    new_recovery_codes,
    new_token,
    new_totp_secret,
    normalize_recovery_code,
    password_problem,
    provisioning_uri,
    token_hash,
    totp_step,
)
from app.admin.session import (
    RawSession,
    clear_session_cookie,
    current_actor,
    raw_session,
    set_session_cookie,
)
from app.admin.store import AdminError, AdminStore

log = logging.getLogger("fichaitor.admin")

FRESH_TOTP = timedelta(minutes=5)

_PASSWORD_MESSAGES = {
    "password_too_short": "La contraseña necesita al menos 12 caracteres.",
    "password_too_long": "La contraseña admite como mucho 128 caracteres.",
    "password_common": "Esa contraseña es demasiado común o previsible.",
    "password_has_username": "La contraseña no puede contener tu usuario.",
}


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=1024)


class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)


class EnrollmentStartIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class EnrollmentFinishIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=1, max_length=1024)
    state: str = Field(min_length=1, max_length=1000)
    code: str = Field(min_length=1, max_length=40)


class TotpFinishIn(BaseModel):
    state: str = Field(min_length=1, max_length=1000)
    code: str = Field(min_length=1, max_length=40)


class PasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


def _invalid_credentials() -> AdminError:
    return AdminError(401, "invalid_credentials", "Usuario o contraseña incorrectos.")


def _too_many() -> AdminError:
    return AdminError(
        429, "too_many_attempts", "Demasiados intentos fallidos en la última hora. Espera."
    )


def _check_password(username: str, password: str) -> None:
    problem = password_problem(password, username)
    if problem:
        raise AdminError(422, problem, _PASSWORD_MESSAGES[problem])


def session_view(row: dict, kit: AuthKit, token: str) -> dict:
    verified = row["totp_verified_at"]
    fresh_until = verified + FRESH_TOTP if verified is not None else None
    return {
        "stage": "ok",
        "username": row["username"],
        "role": row["role"],
        "totp_fresh_until": fresh_until.isoformat() if fresh_until else None,
        "csrf_token": kit.csrf(token),
    }


def build_router(kit: AuthKit, store: AdminStore) -> APIRouter:
    router = APIRouter(prefix="/admin/api")

    def _raw(request: Request) -> RawSession:
        return raw_session(request, kit)

    async def _secret_of(raw: RawSession) -> tuple[str, str]:
        pending = await store.pending_user(raw.hash)
        if pending is None:
            raise AdminError(401, "session_invalid", "La sesión no vale: vuelve a entrar.")
        secret = kit.box.open_totp(pending["totp_secret_enc"], pending["username"])
        if secret is None:
            # The key of admin.env changed, or the row was moved: nobody can enter.
            log.error("admin_totp_secret_unreadable")
            raise AdminError(500, "totp_unreadable", "No se puede leer el secreto TOTP.")
        return pending["username"], secret

    # --- sign-in -----------------------------------------------------------------------

    @router.post("/login")
    async def login(body: LoginIn, response: Response) -> dict:
        if not await store.login_allowed():
            raise _too_many()
        username = body.username.strip().lower()
        row = await store.login_lookup(username)
        stored = row["password_hash"] if row else None
        if not kit.passwords.verify(stored, body.password):
            await store.login_failed(username, "password")
            raise _invalid_credentials()
        token = new_token()
        hashed = token_hash(token)
        # False if locked: the same answer as a wrong password.
        if not await store.session_open(username, hashed):
            raise _invalid_credentials()
        if stored and kit.passwords.needs_rehash(stored):
            await store.rehash_password(hashed, kit.passwords.hash(body.password))
        set_session_cookie(response, token)
        return {"stage": "totp", "csrf_token": kit.csrf(token)}

    @router.post("/session/totp")
    async def session_totp(body: CodeIn, request: Request) -> dict:
        """The second step of the sign-in, and the new code a sensitive action asks for
        when the last one is more than 5 minutes old."""
        raw = _raw(request)
        if not await store.login_allowed():
            raise _too_many()
        username, secret = await _secret_of(raw)
        step = totp_step(secret, body.code, kit.clock())
        if step is None:
            await store.login_failed(username, "totp")
            raise AdminError(401, "totp_invalid", "El código no vale.")
        if not await store.session_totp(raw.hash, step):
            # A code already used, or the session closed meanwhile: counted by the base.
            raise AdminError(401, "totp_invalid", "El código no vale o ya se ha usado.")
        row = await store.session_check(raw.hash)
        if row is None:
            raise AdminError(401, "session_invalid", "La sesión no vale: vuelve a entrar.")
        return session_view(row, kit, raw.token)

    @router.post("/login/recovery")
    async def login_recovery(body: CodeIn, request: Request) -> dict:
        """A recovery code instead of the TOTP (the phone is lost): it is spent and the
        person has to enroll a new TOTP right away (POST /totp/start and /totp/finish)."""
        raw = _raw(request)
        if not await store.login_allowed():
            raise _too_many()
        pending = await store.pending_user(raw.hash)
        if pending is None:
            raise AdminError(401, "session_invalid", "La sesión no vale: vuelve a entrar.")
        typed = normalize_recovery_code(body.code)
        for candidate in await store.recovery_candidates(raw.hash):
            if kit.passwords.verify(candidate["code_hash"], typed):
                if await store.session_recovery(raw.hash, candidate["code_id"]):
                    return {"stage": "reenroll", "csrf_token": kit.csrf(raw.token)}
                break
        await store.login_failed(pending["username"], "recovery")
        raise AdminError(401, "recovery_invalid", "El código de recuperación no vale.")

    @router.get("/session")
    async def get_session(request: Request) -> dict:
        raw = _raw(request)
        row = await store.session_check(raw.hash)
        if row is not None:
            return session_view(row, kit, raw.token)
        if await store.session_must_enroll(raw.hash):
            return {"stage": "reenroll", "csrf_token": kit.csrf(raw.token)}
        raise AdminError(401, "session_invalid", "La sesión no vale: vuelve a entrar.")

    @router.post("/logout")
    async def logout(request: Request, response: Response) -> dict:
        raw = _raw(request)
        await store.session_close(raw.hash)
        clear_session_cookie(response)
        return {"ok": True}

    # --- enrollment (link printed by scripts/admin_usuario.py) --------------------------

    @router.post("/enrollment/start")
    async def enrollment_start(body: EnrollmentStartIn) -> dict:
        hashed = token_hash(body.token)
        who = await store.enrollment_check(hashed)
        if who is None:
            raise AdminError(404, "enrollment_invalid", "El enlace de alta no vale o ha caducado.")
        secret = new_totp_secret()
        return {
            "username": who["username"],
            "role": who["role"],
            "otpauth_uri": provisioning_uri(secret, who["username"]),
            "secret": secret,
            "state": kit.box.seal_state(secret, hashed, kit.clock()),
        }

    @router.post("/enrollment/finish")
    async def enrollment_finish(body: EnrollmentFinishIn) -> dict:
        hashed = token_hash(body.token)
        who = await store.enrollment_check(hashed)
        if who is None:
            raise AdminError(404, "enrollment_invalid", "El enlace de alta no vale o ha caducado.")
        username = who["username"]
        _check_password(username, body.password)
        now = kit.clock()
        secret = kit.box.open_state(body.state, hashed, now)
        if secret is None:
            raise AdminError(409, "enrollment_expired", "Vuelve a empezar el alta del TOTP.")
        step = totp_step(secret, body.code, now)
        if step is None:
            raise AdminError(422, "totp_invalid", "El código no vale: revisa la hora del móvil.")
        codes = new_recovery_codes()
        ok = await store.enroll(
            hashed,
            kit.passwords.hash(body.password),
            kit.box.seal_totp(secret, username),
            step,
            [kit.passwords.hash(normalize_recovery_code(c)) for c in codes],
        )
        if not ok:
            raise AdminError(409, "enrollment_invalid", "El enlace de alta no vale o ha caducado.")
        return {"username": username, "recovery_codes": codes}

    # --- the person's own TOTP and password ---------------------------------------------

    @router.post("/totp/start")
    async def totp_start(request: Request) -> dict:
        """A new TOTP: after a recovery code (compulsory) or by choice, with a code of the
        last 5 minutes."""
        raw = _raw(request)
        username = await store.session_username(raw.hash)
        if username is None:
            raise AdminError(403, "totp_required", "Confirma con un código TOTP nuevo.")
        secret = new_totp_secret()
        return {
            "otpauth_uri": provisioning_uri(secret, username),
            "secret": secret,
            "state": kit.box.seal_state(secret, raw.hash, kit.clock()),
        }

    @router.post("/totp/finish")
    async def totp_finish(body: TotpFinishIn, request: Request) -> dict:
        raw = _raw(request)
        username = await store.session_username(raw.hash)
        if username is None:
            raise AdminError(403, "totp_required", "Confirma con un código TOTP nuevo.")
        now = kit.clock()
        secret = kit.box.open_state(body.state, raw.hash, now)
        if secret is None:
            raise AdminError(409, "enrollment_expired", "Vuelve a empezar el alta del TOTP.")
        step = totp_step(secret, body.code, now)
        if step is None:
            raise AdminError(422, "totp_invalid", "El código no vale: revisa la hora del móvil.")
        codes = new_recovery_codes()
        ok = await store.set_totp(
            raw.hash,
            kit.box.seal_totp(secret, username),
            step,
            [kit.passwords.hash(normalize_recovery_code(c)) for c in codes],
        )
        if not ok:
            raise AdminError(403, "totp_required", "Confirma con un código TOTP nuevo.")
        return {"recovery_codes": codes}

    @router.post("/password")
    async def change_password(body: PasswordIn, request: Request) -> dict:
        actor = await current_actor(request, kit, store)
        row = await store.login_lookup(actor.username)
        if not kit.passwords.verify(row["password_hash"] if row else None, body.current_password):
            await store.login_failed(actor.username, "password")
            raise _invalid_credentials()
        _check_password(actor.username, body.new_password)
        await store.change_password(actor.hash, kit.passwords.hash(body.new_password))
        return {"ok": True, "changed_at": datetime.now(UTC).isoformat()}

    return router
