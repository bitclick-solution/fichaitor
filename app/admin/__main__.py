"""`python -m app.admin`: the backoffice on port 8090 of its container.

Compose publishes it only on 127.0.0.1:8090 of the server (deploy/docker-compose.prod.yml)
and it is reached through an SSH tunnel. Exit code 2 is a configuration error (the
message names the variables, never their values).
"""

import logging
import sys

import uvicorn

from app.admin.app import create_app
from app.admin.config import AdminConfigError, AdminSettings


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = AdminSettings()
    try:
        app = create_app(settings)
    except AdminConfigError as exc:
        logging.getLogger("fichaitor.admin").error("admin_config_error: %s", exc)
        return 2
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        access_log=False,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
