#!/usr/bin/env bash
# deploy-web.sh — Despliegue de Fichaitor en modo «solo web». Se explica en docs/despliegue.md.
#
# Uso (en el servidor; lo lanza .github/workflows/deploy.yml por SSH con solo_web=1):
#   deploy-web.sh <ETIQUETA> <PROPIETARIO> [CARPETA]
#
#   ETIQUETA     commit completo (40 caracteres): etiqueta de la imagen en GHCR
#   PROPIETARIO  usuario u organización de GitHub dueño de la imagen, en minúsculas
#   CARPETA      carpeta del despliegue (por defecto, /opt/bitclick/fichaitor)
#
# Qué hace:
#   1. Descarga la imagen de la web. Si falla, no se ha tocado nada.
#   2. Recrea fichaitor-web con docker-compose.web.yml.
#   3. Comprueba la salud. Si falla, vuelve a la imagen anterior y termina con error.
#
# No toca la base de datos ni ningún archivo de secretos. Es idempotente.
set -euo pipefail

ETIQUETA="${1:-}"
PROPIETARIO="${2:-}"
DEPLOY_DIR="${3:-/opt/bitclick/fichaitor}"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.web.yml"
ESTADO="${DEPLOY_DIR}/.web-version"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

[[ "${ETIQUETA}" =~ ^[0-9a-f]{40}$ ]] || die "La etiqueta debe ser un commit completo (40 caracteres hexadecimales)."
[[ "${PROPIETARIO}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "Propietario no válido (minúsculas, cifras y guiones)."
[[ -f "${COMPOSE_FILE}" ]] || die "No existe ${COMPOSE_FILE}."
docker network inspect bitclick-network >/dev/null 2>&1 || die "No existe la red bitclick-network (la de Nginx Proxy Manager)."

IMAGEN="ghcr.io/${PROPIETARIO}/fichaitor-web:${ETIQUETA}"
ANTERIOR="$(cat "${ESTADO}" 2>/dev/null || true)"

levantar() {
    FICHAITOR_WEB_IMAGE="$1" docker compose --project-directory "${DEPLOY_DIR}" -f "${COMPOSE_FILE}" \
        up -d --no-deps --force-recreate fichaitor-web
}

sano() {
    local estado
    for _ in $(seq 1 30); do
        estado="$(docker inspect -f '{{.State.Health.Status}}' fichaitor-web 2>/dev/null || echo ausente)"
        [[ "${estado}" == "healthy" ]] && return 0
        sleep 2
    done
    return 1
}

log "Descargando ${IMAGEN}"
docker pull --quiet "${IMAGEN}" >/dev/null || die "No se pudo descargar la imagen. No se ha tocado nada."

log "Recreando fichaitor-web"
levantar "${IMAGEN}"

if sano; then
    echo "${IMAGEN}" >"${ESTADO}"
    log "fichaitor-web en marcha con ${ETIQUETA}"
    exit 0
fi

if [[ -n "${ANTERIOR}" ]]; then
    log "La web nueva no está sana: vuelvo a ${ANTERIOR}"
    levantar "${ANTERIOR}" || true
fi
die "fichaitor-web no ha pasado la comprobación de salud."
