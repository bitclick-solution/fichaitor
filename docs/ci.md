# Integración continua

El flujo `.github/workflows/ci.yml` se ejecuta en cada push y en cada pull request. No necesita ningún secreto y el `GITHUB_TOKEN` solo tiene permiso de lectura (`contents: read`). Un push nuevo a la misma rama cancela la ejecución anterior.

Desarrolla las puertas de calidad de [06-estandares.md](06-estandares.md), sección 4.

## Trabajos

| Trabajo | Qué comprueba | Cómo |
|---|---|---|
| **Calidad** | Formato y análisis estático | `uv sync --frozen`, `uv run ruff format --check .`, `uv run ruff check .` |
| **Pruebas** | Unitarias e integración contra PostgreSQL 16 | `scripts/ci/aplicar_esquema.sh` y `uv run pytest -q` |
| **Imagen** | La imagen de la API se construye y arranca | `scripts/ci/humo_imagen.sh` |
| **Seguridad** | Secretos en el historial y dependencias con vulnerabilidades | `scripts/ci/buscar_secretos.sh` (gitleaks) y `pip-audit` |
| **Detectar web** | Si existe `web/package.json` | Decide si se ejecuta el trabajo Web |
| **Web** | Tipos, lint y compilación de la web | `npm ci`, `npm run typecheck`, `npm run lint`, `npm run build` |

### Pruebas

1. Arranca un servicio `postgres:16-alpine` con el usuario propietario `fichaitor`.
2. `scripts/ci/aplicar_esquema.sh` aplica `db/schema.sql` y, si existe, `db/schema_worker.sql`; crea los usuarios de desarrollo con `db/dev-init.sql`; y aplica el esquema una segunda vez. Si el esquema deja de ser idempotente, el trabajo falla aquí.
3. Define `FICHAITOR_TEST_DATABASE_URL` y `FICHAITOR_TEST_OWNER_URL`, así que las pruebas de integración no se saltan.

### Imagen

`scripts/ci/humo_imagen.sh` construye la imagen, la arranca con `FICHAITOR_FAKE_PLACES=true` (sin Google y sin base de datos), con el sistema de archivos de solo lectura como en `docker-compose.yml`, espera a que `GET /healthz` responda 200 y comprueba que el contenedor no se ejecuta como root.

### Seguridad

- **gitleaks** revisa todo el historial de git. Se descarga el binario oficial de una versión fija y se verifica su suma SHA-256. No se usa `gitleaks-action` porque exige una licencia en repositorios de organizaciones.
- **pip-audit** audita las versiones exactas de `uv.lock`, incluidas las de desarrollo.

### Web

El trabajo se omite mientras no exista `web/package.json`. Los scripts `typecheck`, `lint` y `build` se ejecutan con `--if-present`: si alguno no está definido en `package.json`, ese paso no hace nada.

> **Importante:** cuando exista la web con npm, define los tres scripts en `web/package.json` y activa el bloque de npm en `.github/dependabot.yml`.

## Versiones fijadas

| Qué | Versión | Dónde se cambia |
|---|---|---|
| Acciones de GitHub | Por commit, con la versión en un comentario | `ci.yml` (lo actualiza Dependabot) |
| uv | 0.8.13, la misma que el `Dockerfile` | `UV_VERSION` en `ci.yml` |
| Python | 3.12 | `UV_PYTHON` en `ci.yml` |
| gitleaks | 8.30.1 | `scripts/ci/buscar_secretos.sh` (versión y sumas) |
| pip-audit | 2.10.1 | `ci.yml`, trabajo Seguridad |
| Node | 24 | `ci.yml`, trabajo Web |

> **Nota:** Dependabot no actualiza `UV_VERSION`, gitleaks ni pip-audit. Revísalos a mano cuando actualices uv en el `Dockerfile`.

## Dependabot

`.github/dependabot.yml` abre pull requests semanales, los lunes, para las acciones de GitHub, las dependencias de Python (`uv`), las imágenes del `Dockerfile` y las de `docker-compose.yml`.

## Ejecutar lo mismo en local

Desde la raíz del repositorio:

```bash
# Calidad
uv sync --frozen
uv run ruff format --check .
uv run ruff check .

# Pruebas, con la base de datos de desarrollo (docker compose up -d db)
export FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor_test
export FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor_test
bash scripts/ci/aplicar_esquema.sh
uv run pytest -q

# Imagen (cambia el puerto si el 8000 está ocupado)
PUERTO=18000 bash scripts/ci/humo_imagen.sh

# Auditoría de dependencias
uv export --frozen --no-emit-project --format requirements-txt --output-file requirements.audit.txt --quiet
uvx pip-audit@2.10.1 --strict --require-hashes --disable-pip --requirement requirements.audit.txt
```

En Windows con Git Bash:

- Sin `psql` instalado, usa el de un contenedor y apunta la URL del propietario a `host.docker.internal`:

  ```bash
  PSQL="docker run --rm -i postgres:16-alpine psql" \
  FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@host.docker.internal:5433/fichaitor_test \
  bash scripts/ci/aplicar_esquema.sh
  ```

- Antepón `MSYS_NO_PATHCONV=1` a `humo_imagen.sh` para que Git Bash no convierta la ruta `/tmp`.
- `buscar_secretos.sh` descarga un binario de Linux. Ejecútalo dentro de un contenedor:

  ```bash
  MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/repo:ro" -w /repo buildpack-deps:bookworm-scm \
    bash -c "git config --global --add safe.directory /repo && bash scripts/ci/buscar_secretos.sh"
  ```

## Lo que aún no cubre

De las puertas de [06-estandares.md](06-estandares.md) faltan, porque todavía no existen en el proyecto: mypy estricto, pruebas de contrato (schemathesis), conversacionales y E2E en Compose, evaluación del modelo y el despliegue desde la rama principal.
