#!/usr/bin/env bash
# init-roles.sh — Crea los usuarios de inicio de sesión de PRODUCCIÓN de Fichaitor.
#
# Sustituye a db/dev-init.sql, que es solo para desarrollo. Crea tres usuarios:
#   fichaitor_api_prod           hereda de fichaitor_api          -> FICHAITOR_DATABASE_URL
#                                                                    en api.env
#   fichaitor_worker_prod        hereda de fichaitor_worker       -> FICHAITOR_WORKER_DATABASE_URL
#                                                                    en worker.env
#   fichaitor_conversation_prod  hereda de fichaitor_conversation -> FICHAITOR_CONVERSATION_DATABASE_URL
#                                                                    en conversation.env
#   fichaitor_billing_prod       hereda de fichaitor_billing      -> FICHAITOR_BILLING_DATABASE_URL
#                                                                    en billing.env (solo si existe)
#   fichaitor_admin_prod         hereda de fichaitor_admin        -> FICHAITOR_ADMIN_DATABASE_URL
#                                                                    en admin.env (solo si existe)
# Los dos últimos, sin TEMPORARY (M6): PUBLIC ya no lo da, y db/schema_billing.sql y
# db/schema_admin.sql se lo quitan también a los miembros de sus roles.
#
# Cada contraseña se genera aquí, en el servidor, y va directa de openssl a
# PostgreSQL y al archivo de entorno: no se muestra, no pasa por GitHub y no
# aparece en la lista de procesos.
#
# Es idempotente: si el usuario existe y su archivo ya tiene la URL, no toca nada.
# deploy.sh lo ejecuta en cada despliegue, después de aplicar el esquema.
#
# Uso (en el servidor):
#   ./init-roles.sh            crea lo que falte
#   ./init-roles.sh --rotar    genera contraseñas nuevas para los tres usuarios;
#                              después hay que recrear la API, el worker y la conversación
#
# Requisitos: el contenedor fichaitor-db en marcha, el esquema aplicado (crea los
# roles fichaitor_api, fichaitor_worker y fichaitor_conversation) y los archivos
# api.env, worker.env y conversation.env.
#
# Variables opcionales:
#   FICHAITOR_DEPLOY_DIR   carpeta del despliegue (por defecto, la de este script)
set -euo pipefail
umask 077

DEPLOY_DIR="${FICHAITOR_DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
DB_CONTAINER="fichaitor-db"
DB_USER="fichaitor"
DB_NAME="fichaitor"
DB_HOST="fichaitor-db"

ROTAR=0
case "${1:-}" in
    "") ;;
    --rotar) ROTAR=1 ;;
    *)
        echo "Uso: init-roles.sh [--rotar]" >&2
        exit 64
        ;;
esac

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() {
    echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2
    exit 1
}

# Las órdenes SQL entran por la entrada estándar: lo que va en la línea de
# órdenes lo ve cualquier usuario del servidor con "ps".
psql_db() {
    docker exec -i "${DB_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" \
        --no-psqlrc --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 "$@"
}

existe_rol() {
    local rol="$1"
    [[ "$(psql_db --command "SELECT 1 FROM pg_roles WHERE rolname = '${rol}'")" == "1" ]]
}

# ¿Tiene la variable un valor no vacío en el archivo? No imprime el valor.
tiene_valor() {
    local archivo="$1" variable="$2"
    grep -Eq "^${variable}=.+" "${archivo}"
}

# Sustituye la línea VARIABLE=... (o la añade al final) sin tocar el resto.
# El valor viaja por el entorno de awk, no por sus argumentos.
fijar_variable() {
    local archivo="$1" variable="$2" valor="$3" temporal
    temporal="$(mktemp "${archivo}.XXXXXX")"
    FICHAITOR_NUEVO_VALOR="${valor}" awk -v clave="${variable}" '
        BEGIN { valor = ENVIRON["FICHAITOR_NUEVO_VALOR"] }
        index($0, clave "=") == 1 { if (!hecho) print clave "=" valor; hecho = 1; next }
        { print }
        END { if (!hecho) print clave "=" valor }
    ' "${archivo}" >"${temporal}"
    chmod 600 "${temporal}"
    mv "${temporal}" "${archivo}"
}

# Crea el usuario si falta, le pone una contraseña nueva y escribe la URL.
preparar_usuario() {
    local usuario="$1" rol_padre="$2" archivo="$3" variable="$4" clave

    [[ -f "${archivo}" ]] || die "No existe ${archivo}. Créalo a partir del ejemplo (docs/despliegue.md)."
    existe_rol "${rol_padre}" || die "No existe el rol ${rol_padre}: aplica antes el esquema (deploy.sh lo hace)."

    if [[ "${ROTAR}" == "0" ]] && existe_rol "${usuario}" && tiene_valor "${archivo}" "${variable}"; then
        log "${usuario}: ya existe y ${variable} está definida. Sin cambios."
        return 0
    fi

    # Hexadecimal: 64 caracteres sin símbolos, así que la URL no necesita escapes
    # y Docker Compose no encuentra ningún "$" que interpretar.
    command -v openssl >/dev/null || die "Falta openssl en el servidor."
    clave="$(openssl rand -hex 32)"
    [[ "${#clave}" -eq 64 ]] || die "openssl no ha generado la contraseña."

    # \gexec ejecuta el CREATE solo si el usuario no existe. La contraseña se
    # pone aparte, con una variable de psql, que la entrecomilla de forma segura.
    psql_db <<SQL
\set clave '${clave}'
SELECT 'CREATE ROLE ${usuario} LOGIN IN ROLE ${rol_padre}'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${usuario}') \gexec
ALTER ROLE ${usuario} WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'clave';
SQL

    fijar_variable "${archivo}" "${variable}" \
        "postgresql://${usuario}:${clave}@${DB_HOST}:5432/${DB_NAME}"
    log "${usuario}: contraseña nueva guardada en ${archivo} (${variable})."
    CAMBIOS=1
}

[[ "$(docker inspect --format '{{.State.Running}}' "${DB_CONTAINER}" 2>/dev/null || true)" == "true" ]] ||
    die "El contenedor ${DB_CONTAINER} no está en marcha."

CAMBIOS=0
preparar_usuario fichaitor_api_prod fichaitor_api "${DEPLOY_DIR}/api.env" FICHAITOR_DATABASE_URL
preparar_usuario fichaitor_worker_prod fichaitor_worker "${DEPLOY_DIR}/worker.env" FICHAITOR_WORKER_DATABASE_URL
preparar_usuario fichaitor_conversation_prod fichaitor_conversation "${DEPLOY_DIR}/conversation.env" \
    FICHAITOR_CONVERSATION_DATABASE_URL
# Cobro y backoffice: hasta que sus procesos existan en el servidor (paso 11 de
# docs/admin-facturacion.md), sin su archivo de entorno no se crea nada.
if [[ -f "${DEPLOY_DIR}/billing.env" ]]; then
    preparar_usuario fichaitor_billing_prod fichaitor_billing "${DEPLOY_DIR}/billing.env" \
        FICHAITOR_BILLING_DATABASE_URL
fi
if [[ -f "${DEPLOY_DIR}/admin.env" ]]; then
    preparar_usuario fichaitor_admin_prod fichaitor_admin "${DEPLOY_DIR}/admin.env" \
        FICHAITOR_ADMIN_DATABASE_URL
fi

if [[ "${CAMBIOS}" == "1" && "${ROTAR}" == "1" ]]; then
    log "Contraseñas rotadas. Recrea la API, el worker y la conversación para que las lean:"
    log "  cd ${DEPLOY_DIR} && docker compose up -d --force-recreate fichaitor-api fichaitor-worker fichaitor-conversation"
fi
