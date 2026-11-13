"""The FastAPI application of the backoffice (`python -m app.admin`).

A process apart from the public API, with its own settings (admin.env) and its own
database role (fichaitor_admin). No key of Stripe, Odoo, SMTP nor Google. It answers
only to Host 127.0.0.1:8090 or localhost:8090: it is reached through an SSH tunnel to
the loopback of the server (docs/admin-facturacion.md, 6.1).
"""

import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.admin import routes_auth, routes_communications, routes_screens
from app.admin.audit import audit_denied
from app.admin.auth import AuthKit, Passwords, SecretBox
from app.admin.config import AdminSettings
from app.admin.session import GuardMiddleware, allowed_origins, check_unsafe_request
from app.admin.store import AdminError, AdminStore

log = logging.getLogger("fichaitor.admin")

_PLACEHOLDER = (
    b'<!doctype html><html lang="es"><head><meta charset="utf-8">'
    b"<title>Backoffice de Fichaitor</title></head><body><h1>Backoffice de Fichaitor</h1>"
    b"<p>La interfaz (web/admin) no est\xc3\xa1 en esta imagen. La API responde en "
    b"/admin/api/.</p></body></html>"
)


class _LazyStore:
    """The pool is opened in the lifespan; the routers are built before. This forwards
    every attribute to the store once it exists."""

    def __init__(self) -> None:
        self.target: AdminStore | None = None

    def __getattr__(self, name: str):
        if self.target is None:
            raise AdminError(503, "starting", "El backoffice está arrancando.")
        return getattr(self.target, name)


def create_app(
    settings: AdminSettings | None = None,
    *,
    store: AdminStore | None = None,
    clock: Callable[[], float] = time.time,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    settings = settings or AdminSettings()
    settings.check(environ)
    kit = AuthKit(
        passwords=Passwords(
            settings.password_pepper.get_secret_value(),
            time_cost=settings.argon2_time_cost,
            memory_kib=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
        ),
        box=SecretBox(settings.totp_key.get_secret_value()),
        pepper=settings.password_pepper.get_secret_value(),
        clock=clock,
    )
    lazy = _LazyStore()
    if store is not None:
        lazy.target = store

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        opened = None
        if lazy.target is None:
            opened = await AdminStore.connect(settings.database_url.get_secret_value())
            lazy.target = opened
        log.info("admin_started")
        try:
            yield
        finally:
            if opened is not None:
                await opened.close()
                lazy.target = None

    origins = allowed_origins(settings.allowed_hosts)

    async def unsafe_guard(request: Request) -> None:
        check_unsafe_request(request, origins)

    app = FastAPI(
        title="Fichaitor backoffice",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(unsafe_guard)],
    )
    store_view: AdminStore = lazy  # type: ignore[assignment]
    app.include_router(routes_auth.build_router(kit, store_view))
    app.include_router(
        routes_screens.build_router(kit, store_view, settings.authserv_id, settings.erp_base_url)
    )
    app.include_router(
        routes_communications.build_router(kit, store_view, settings.communication_per_minute)
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    @app.exception_handler(AdminError)
    async def admin_error(request: Request, exc: AdminError) -> JSONResponse:
        if exc.code in ("role_not_allowed", "totp_required"):
            route = request.scope.get("route")
            name = getattr(route, "name", None) or "unknown"
            await audit_denied(store_view, getattr(request.state, "actor", None), name, exc.code)
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never the values sent: a password or a pasted message would come back.
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) for e in exc.errors()})
        return JSONResponse(
            {"code": "validation", "message": "Datos no válidos.", "fields": fields},
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse({"code": code, "message": ""}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("admin_unexpected_error %s", type(exc).__name__)
        return JSONResponse({"code": "internal", "message": "Error interno."}, status_code=500)

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        # Without the query string: a search can be an email address.
        response = await call_next(request)
        route = request.scope.get("route")
        log.info(
            "admin_request %s %s %s",
            request.method,
            getattr(route, "name", "-"),
            response.status_code,
        )
        return response

    _mount_frontend(app, settings.static_dir)
    app.add_middleware(GuardMiddleware, allowed_hosts=settings.allowed_hosts)
    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """web/admin, built by the interface session, on the same origin. Until it is in
    the image, a page that says so."""
    index = static_dir / "index.html"
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def home() -> Response:
        if index.is_file():
            return FileResponse(index, media_type="text/html")
        return Response(_PLACEHOLDER, media_type="text/html; charset=utf-8")
