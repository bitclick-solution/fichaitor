#!/usr/bin/env bash
# deploy.sh — Despliegue de Fichaitor en el servidor. Se explica en docs/despliegue.md.
#
# Uso (en el servidor; lo lanza .github/workflows/deploy.yml por SSH):
#   deploy.sh <ETIQUETA> <PROPIETARIO> [CARPETA]            desplegar
#   deploy.sh rollback <ETIQUETA> <PROPIETARIO> [CARPETA]   volver a una etiqueta anterior
#
#   ETIQUETA     etiqueta de las imágenes en GHCR: el commit completo (40 caracteres)
#   PROPIETARIO  usuario u organización de GitHub dueño de las imágenes, en minúsculas
#   CARPETA      carpeta del despliegue (por defecto, /opt/bitclick/fichaitor)
#
# Qué hace al desplegar, en este orden:
#   1. Comprueba los archivos de entorno (nombres de variables, nunca valores).
#   2. Descarga las dos imágenes. Si falla, no se ha tocado nada.
#   3. Arranca la base de datos y hace una copia de seguridad.
#   4. Aplica db/schema.sql, db/schema_worker.sql y db/schema_channels.sql en una
#      sola transacción.
#      Si falla, no se aplica nada y la versión anterior sigue en marcha.
#   5. Crea los usuarios de base de datos que falten (init-roles.sh).
#   6. Recrea la API, el worker, la conversación y la web con la etiqueta nueva.
#   7. Comprueba la salud. Si falla, vuelve a la etiqueta anterior y termina con error.
#
# La vuelta atrás cambia las imágenes, no el esquema: el esquema solo añade, así
# que el código anterior sigue funcionando. Para deshacer datos: deploy/restore.md.
#
# Es idempotente: repetirlo con la misma etiqueta no cambia nada.
# No imprime secretos ni el registro de los contenedores (salvo FICHAITOR_MOSTRAR_LOGS=1).
set -euo pipefail
umask 077

# ── Constantes ───────────────────────────────────────────────────────────────
REGISTRY="ghcr.io"
MAX_VERSIONS=5
DB_CONTAINER="fichaitor-db"
API_CONTAINER="fichaitor-api"
WORKER_CONTAINER="fichaitor-worker"
CONVERSATION_CONTAINER="fichaitor-conversation"
WEB_CONTAINER="fichaitor-web"
DB_USER="fichaitor"
DB_NAME="fichaitor"
RED_EXTERNA="bitclick-network"
# Segundos que el worker tiene que aguantar en marcha sin reiniciarse.
WORKER_OBSERVACION=20

# ── Argumentos ───────────────────────────────────────────────────────────────
USO="Uso: deploy.sh <ETIQUETA> <PROPIETARIO> [CARPETA] | deploy.sh rollback <ETIQUETA> <PROPIETARIO> [CARPETA]"
MODE="deploy"
if [[ "${1:-}" == "rollback" ]]; then
    MODE="rollback"
    shift
fi
TAG="${1:?${USO}}"
OWNER="${2:?${USO}}"
DEPLOY_DIR="${3:-/opt/bitclick/fichaitor}"

COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
VERSIONS_FILE="${DEPLOY_DIR}/.versions"
IMAGES_ENV="${DEPLOY_DIR}/.env"
API_ENV="${DEPLOY_DIR}/api.env"
WORKER_ENV="${DEPLOY_DIR}/worker.env"
CONVERSATION_ENV="${DEPLOY_DIR}/conversation.env"
DB_ENV="${DEPLOY_DIR}/db.env"
API_IMAGE_BASE="${REGISTRY}/${OWNER}/fichaitor-api"
WEB_IMAGE_BASE="${REGISTRY}/${OWNER}/fichaitor-web"

# ── Utilidades ───────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*"; }
err() { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; }
die() {
    err "$*"
    exit 1
}

compose() {
    docker compose --project-directory "${DEPLOY_DIR}" -f "${COMPOSE_FILE}" "$@"
}

# Valor de una variable en un archivo de entorno, sin ejecutarlo. Solo se usa
# para comprobar; el valor nunca se imprime.
valor_env() {
    local archivo="$1" variable="$2" linea
    linea="$(grep -E "^${variable}=" "${archivo}" | tail -n 1 || true)"
    linea="${linea#*=}"
    linea="${linea%$'\r'}"
    linea="${linea#[\"\']}"
    linea="${linea%[\"\']}"
    printf '%s' "${linea}"
}

exigir_variables() {
    local archivo="$1" variable faltan=()
    shift
    for variable in "$@"; do
        [[ -n "$(valor_env "${archivo}" "${variable}")" ]] || faltan+=("${variable}")
    done
    if ((${#faltan[@]} > 0)); then
        die "En $(basename "${archivo}") faltan o están vacías: ${faltan[*]}"
    fi
}

# Cualquier FICHAITOR_FAKE_* que no esté vacío ni apagado, también los que se añadan
# después (B13: FICHAITOR_FAKE_MAIL escribía en el registro el código de la baja y el
# enlace de restauración). docker-compose.prod.yml los fija además a "false".
prohibir_modos_falsos() {
    local archivo="$1" variable valor
    for variable in $(grep -oE '^[[:space:]]*(export[[:space:]]+)?FICHAITOR_FAKE_[A-Z0-9_]+=' "${archivo}" |
        sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | sort -u); do
        valor="$(valor_env "${archivo}" "${variable}")"
        case "${valor,,}" in
            "" | false | 0 | no | off) ;;
            *) die "$(basename "${archivo}") activa ${variable}: en producción no hay modos de ejemplo." ;;
        esac
    done
}

# I10: los secretos de Telegram, con longitud mínima. El pimiento de los códigos de
# vinculación no puede faltar (dos vacíos «coinciden»), y sin sondeo largo (el de
# producción) el secreto del webhook tampoco: es lo único que protege la ruta.
# Solo se miden: el valor nunca se imprime.
comprobar_secretos_telegram() {
    local pimienta secreto sondeo
    pimienta="$(valor_env "${API_ENV}" FICHAITOR_LINK_CODE_PEPPER)"
    ((${#pimienta} >= 32)) ||
        die "FICHAITOR_LINK_CODE_PEPPER falta o tiene menos de 32 caracteres en api.env (openssl rand -hex 32)."
    [[ "${pimienta}" == "$(valor_env "${CONVERSATION_ENV}" FICHAITOR_LINK_CODE_PEPPER)" ]] ||
        die "FICHAITOR_LINK_CODE_PEPPER no coincide entre api.env y conversation.env."
    sondeo="$(valor_env "${CONVERSATION_ENV}" FICHAITOR_TELEGRAM_POLLING)"
    case "${sondeo,,}" in
        true | 1 | yes | on) return 0 ;;
    esac
    secreto="$(valor_env "${API_ENV}" FICHAITOR_TELEGRAM_WEBHOOK_SECRET)"
    ((${#secreto} >= 32)) ||
        die "FICHAITOR_TELEGRAM_WEBHOOK_SECRET falta o tiene menos de 32 caracteres en api.env (openssl rand -hex 32)."
}

exigir_privado() {
    local archivo="$1" modo
    [[ -f "${archivo}" ]] || die "No existe ${archivo}. Créalo a partir del ejemplo (docs/despliegue.md)."
    modo="$(stat -c '%a' "${archivo}")"
    [[ "${modo}" == "600" || "${modo}" == "400" ]] ||
        die "${archivo} tiene modo ${modo}. Debe ser 600: chmod 600 ${archivo}"
}

# Comprobaciones previas. Todo lo que falle aquí falla antes de tocar nada.
comprobar_entorno() {
    [[ "${TAG}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || die "Etiqueta no válida."
    [[ "${OWNER}" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "Propietario no válido: en minúsculas, como en GHCR."
    [[ -f "${COMPOSE_FILE}" ]] || die "No existe ${COMPOSE_FILE}."
    docker network inspect "${RED_EXTERNA}" >/dev/null 2>&1 ||
        die "No existe la red ${RED_EXTERNA}. Es la del proxy de Bitclick: no la crees sin comprobar por qué falta."

    exigir_privado "${DB_ENV}"
    exigir_privado "${API_ENV}"
    exigir_privado "${WORKER_ENV}"
    exigir_privado "${CONVERSATION_ENV}"

    exigir_variables "${DB_ENV}" POSTGRES_PASSWORD
    # FICHAITOR_DATABASE_URL, FICHAITOR_WORKER_DATABASE_URL y
    # FICHAITOR_CONVERSATION_DATABASE_URL se comprueban después:
    # en la primera instalación las escribe init-roles.sh.
    exigir_variables "${API_ENV}" \
        FICHAITOR_PLACES_API_KEY FICHAITOR_GOOGLE_CLIENT_ID FICHAITOR_GOOGLE_CLIENT_SECRET \
        FICHAITOR_OAUTH_STATE_SECRET FICHAITOR_TOKEN_PUBLIC_KEY
    exigir_variables "${WORKER_ENV}" \
        FICHAITOR_TOKEN_PRIVATE_KEY FICHAITOR_GOOGLE_CLIENT_ID FICHAITOR_GOOGLE_CLIENT_SECRET

    # La clave privada solo puede existir en el entorno del worker.
    [[ -z "$(valor_env "${API_ENV}" FICHAITOR_TOKEN_PRIVATE_KEY)" ]] ||
        die "api.env contiene FICHAITOR_TOKEN_PRIVATE_KEY. Quítala: solo va en worker.env."
    [[ -z "$(valor_env "${CONVERSATION_ENV}" FICHAITOR_TOKEN_PRIVATE_KEY)" ]] ||
        die "conversation.env contiene FICHAITOR_TOKEN_PRIVATE_KEY. Quítala: solo va en worker.env."
    # Cada proceso, solo su usuario de base de datos.
    [[ -z "$(valor_env "${API_ENV}" FICHAITOR_WORKER_DATABASE_URL)" ]] ||
        die "api.env contiene FICHAITOR_WORKER_DATABASE_URL. Quítala: solo va en worker.env."
    [[ -z "$(valor_env "${API_ENV}" FICHAITOR_CONVERSATION_DATABASE_URL)" ]] ||
        die "api.env contiene FICHAITOR_CONVERSATION_DATABASE_URL. Solo va en conversation.env."
    # Los códigos de vinculación los crea la API y los comprueba la conversación: el
    # secreto de su huella tiene que ser el mismo en los dos, y largo.
    comprobar_secretos_telegram
    [[ "$(valor_env "${API_ENV}" FICHAITOR_TOKEN_PUBLIC_KEY)" != "$(valor_env "${WORKER_ENV}" FICHAITOR_TOKEN_PRIVATE_KEY)" ]] ||
        die "La clave pública de api.env y la privada de worker.env son el mismo valor."
    [[ "$(valor_env "${API_ENV}" FICHAITOR_GOOGLE_CLIENT_ID)" == "$(valor_env "${WORKER_ENV}" FICHAITOR_GOOGLE_CLIENT_ID)" ]] ||
        die "FICHAITOR_GOOGLE_CLIENT_ID no coincide entre api.env y worker.env."

    prohibir_modos_falsos "${API_ENV}"
    prohibir_modos_falsos "${WORKER_ENV}"
    prohibir_modos_falsos "${CONVERSATION_ENV}"
}

# ── Versiones ────────────────────────────────────────────────────────────────
# .versions guarda las últimas etiquetas desplegadas con éxito, la más nueva primero.
read_versions() {
    if [[ -f "${VERSIONS_FILE}" ]]; then
        cat "${VERSIONS_FILE}"
    fi
}

write_versions() {
    local new_tag="$1" current
    current="$(read_versions)"
    {
        echo "${new_tag}"
        echo "${current}" | grep -vx -- "${new_tag}" || true
    } | grep -v '^$' | head -n "${MAX_VERSIONS}" >"${VERSIONS_FILE}.tmp"
    mv "${VERSIONS_FILE}.tmp" "${VERSIONS_FILE}"
    chmod 644 "${VERSIONS_FILE}"
}

version_at() {
    read_versions | sed -n "${1}p"
}

# Fija las dos imágenes: en el entorno de este script y en .env, para que
# "docker compose ps" o "logs" funcionen a mano en la carpeta. .env no lleva secretos.
fijar_imagenes() {
    local tag="$1"
    export FICHAITOR_API_IMAGE="${API_IMAGE_BASE}:${tag}"
    export FICHAITOR_WEB_IMAGE="${WEB_IMAGE_BASE}:${tag}"
    {
        echo "# Lo escribe deploy.sh. No lo edites: para cambiar de versión, usa deploy.sh."
        echo "FICHAITOR_API_IMAGE=${FICHAITOR_API_IMAGE}"
        echo "FICHAITOR_WEB_IMAGE=${FICHAITOR_WEB_IMAGE}"
    } >"${IMAGES_ENV}.tmp"
    mv "${IMAGES_ENV}.tmp" "${IMAGES_ENV}"
    chmod 644 "${IMAGES_ENV}"
}

pull_images() {
    local tag="$1"
    log "Descarga de ${API_IMAGE_BASE}:${tag} ..."
    # "|| return 1" a propósito: esta función se llama con "|| die", y ahí
    # "set -e" no corta la función en la primera orden que falla.
    docker pull --quiet "${API_IMAGE_BASE}:${tag}" >/dev/null || return 1
    log "Descarga de ${WEB_IMAGE_BASE}:${tag} ..."
    docker pull --quiet "${WEB_IMAGE_BASE}:${tag}" >/dev/null || return 1
}

# ── Base de datos ────────────────────────────────────────────────────────────
esperar_salud() {
    local contenedor="$1" segundos="$2" estado="missing" i
    for ((i = 1; i <= segundos; i++)); do
        estado="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}sin-comprobacion{{end}}' "${contenedor}" 2>/dev/null || echo missing)"
        if [[ "${estado}" == "healthy" ]]; then
            log "${contenedor}: healthy (${i} s)."
            return 0
        fi
        sleep 1
    done
    err "${contenedor}: no llegó a healthy en ${segundos} s (último estado: ${estado})."
    return 1
}

arrancar_db() {
    log "Base de datos ..."
    compose up -d "${DB_CONTAINER}"
    esperar_salud "${DB_CONTAINER}" 90 || die "La base de datos no arranca. Revisa: docker logs ${DB_CONTAINER}"
}

aplicar_esquema() {
    local archivo
    for archivo in "${DEPLOY_DIR}/db/schema.sql" "${DEPLOY_DIR}/db/schema_worker.sql" \
        "${DEPLOY_DIR}/db/schema_channels.sql"; do
        [[ -s "${archivo}" ]] || die "Falta ${archivo}."
    done
    log "Esquema: db/schema.sql, db/schema_worker.sql y db/schema_channels.sql, en una transacción ..."
    # Los tres archivos entran juntos por la entrada estándar: o se aplican
    # todos o ninguno. schema_channels.sql necesita las tablas de los otros dos.
    # Con el usuario propietario, por el socket local del contenedor: no hace
    # falta su contraseña ni un puerto abierto.
    # lock_timeout: si la versión en marcha retiene un bloqueo, el despliegue
    # falla a los 30 s en vez de quedarse colgado.
    cat "${DEPLOY_DIR}/db/schema.sql" "${DEPLOY_DIR}/db/schema_worker.sql" \
        "${DEPLOY_DIR}/db/schema_channels.sql" |
        docker exec -i -e PGOPTIONS="-c lock_timeout=30s -c client_min_messages=warning" "${DB_CONTAINER}" \
            psql -U "${DB_USER}" -d "${DB_NAME}" \
            --no-psqlrc --quiet --single-transaction --set ON_ERROR_STOP=1 ||
        return 1
    log "Esquema aplicado."
}

# ── Aplicación ───────────────────────────────────────────────────────────────
mostrar_logs() {
    local contenedor="$1"
    if [[ "${FICHAITOR_MOSTRAR_LOGS:-0}" == "1" ]]; then
        docker logs --tail 40 "${contenedor}" 2>&1 || true
    else
        err "Para ver el motivo, en el servidor: docker logs --tail 80 ${contenedor}"
    fi
}

imagen_en_uso() {
    docker inspect --format '{{.Config.Image}}' "$1" 2>/dev/null || true
}

# El worker no tiene puerto ni endpoint. Se da por bueno si aguanta en marcha
# sin reiniciarse: con un error de configuración o sin base de datos termina
# enseguida y Docker lo reinicia en bucle. La imagen se comprueba en health_check.
comprobar_worker() {
    local contenedor="${1:-${WORKER_CONTAINER}}" reinicios_antes reinicios_despues en_marcha arrancado
    reinicios_antes="$(docker inspect --format '{{.RestartCount}}' "${contenedor}" 2>/dev/null || echo x)"
    sleep "${WORKER_OBSERVACION}"
    reinicios_despues="$(docker inspect --format '{{.RestartCount}}' "${contenedor}" 2>/dev/null || echo y)"
    en_marcha="$(docker inspect --format '{{.State.Running}}/{{.State.Restarting}}' "${contenedor}" 2>/dev/null || echo missing)"

    if [[ "${en_marcha}" != "true/false" || "${reinicios_antes}" != "${reinicios_despues}" ]]; then
        err "${contenedor}: no se mantiene en marcha (un código de salida 2 es un error de configuración)."
        return 1
    fi
    arrancado="$(docker logs "${contenedor}" 2>&1 | grep -c 'worker_started' || true)"
    if [[ "${arrancado}" == "0" ]]; then
        # Solo aviso: tras muchos días en marcha, la línea puede haber salido del registro.
        log "AVISO: ${contenedor} no tiene worker_started en su registro."
    fi
    log "${contenedor}: en marcha y estable durante ${WORKER_OBSERVACION} s."
}

# La web tiene que servir la portada y llegar a la API por /api/. Una ruta que
# la API no conoce devuelve 404; si nginx no llega a la API, devuelve 502 o 504.
comprobar_web() {
    local codigo
    codigo="$(docker exec "${WEB_CONTAINER}" wget -S -q -O /dev/null "http://127.0.0.1:8080/api/comprobacion-despliegue" 2>&1 |
        grep -oE 'HTTP/[0-9.]+ [0-9]{3}' | tail -n 1 | grep -oE '[0-9]{3}$' || true)"
    if [[ -z "${codigo}" || "${codigo}" -ge 500 ]]; then
        err "${WEB_CONTAINER}: /api/ no llega a la API (código: ${codigo:-ninguno})."
        return 1
    fi
    log "${WEB_CONTAINER}: portada y reenvío a la API correctos (código de /api/: ${codigo})."
}

health_check() {
    local tag="$1" fallo=0
    esperar_salud "${API_CONTAINER}" 120 || { mostrar_logs "${API_CONTAINER}"; fallo=1; }
    if [[ "${fallo}" == "0" ]]; then
        esperar_salud "${WEB_CONTAINER}" 60 || { mostrar_logs "${WEB_CONTAINER}"; fallo=1; }
    fi
    if [[ "${fallo}" == "0" ]]; then
        comprobar_web || fallo=1
    fi
    if [[ "${fallo}" == "0" ]]; then
        comprobar_worker || { mostrar_logs "${WORKER_CONTAINER}"; fallo=1; }
    fi
    if [[ "${fallo}" == "0" ]]; then
        comprobar_worker "${CONVERSATION_CONTAINER}" ||
            { mostrar_logs "${CONVERSATION_CONTAINER}"; fallo=1; }
    fi
    if [[ "${fallo}" == "0" ]]; then
        if [[ "$(imagen_en_uso "${API_CONTAINER}")" != "${API_IMAGE_BASE}:${tag}" ||
            "$(imagen_en_uso "${WORKER_CONTAINER}")" != "${API_IMAGE_BASE}:${tag}" ||
            "$(imagen_en_uso "${CONVERSATION_CONTAINER}")" != "${API_IMAGE_BASE}:${tag}" ||
            "$(imagen_en_uso "${WEB_CONTAINER}")" != "${WEB_IMAGE_BASE}:${tag}" ]]; then
            err "Algún contenedor no usa la etiqueta ${tag}."
            fallo=1
        fi
    fi
    return "${fallo}"
}

# Recrea los servicios con la etiqueta indicada y comprueba la salud.
deploy_tag() {
    local tag="$1"
    log "Servicios con la etiqueta ${tag} ..."
    fijar_imagenes "${tag}"
    # "up" espera a que la API esté sana antes de crear la web (depends_on):
    # si la API no arranca, falla aquí.
    if ! compose up -d --remove-orphans; then
        err "docker compose up ha fallado."
        mostrar_logs "${API_CONTAINER}"
        return 1
    fi
    health_check "${tag}"
}

# Borra las imágenes de Fichaitor cuyas etiquetas han salido de .versions.
# Solo toca las de Fichaitor: en este servidor hay más servicios.
remove_old_images() {
    local kept repo_tag tag
    kept="$(read_versions)"
    while IFS= read -r repo_tag; do
        tag="${repo_tag##*:}"
        if ! echo "${kept}" | grep -qx -- "${tag}"; then
            log "Imagen antigua borrada: ${repo_tag}"
            docker rmi "${repo_tag}" >/dev/null 2>&1 || true
        fi
    done < <(docker images --format '{{.Repository}}:{{.Tag}}' |
        grep -E "^(${API_IMAGE_BASE}|${WEB_IMAGE_BASE}):" || true)
}

print_rollback_hint() {
    local prev
    prev="$(version_at 2)"
    if [[ -n "${prev}" ]]; then
        echo ""
        echo "  Para volver a la versión anterior:"
        echo "  ${DEPLOY_DIR}/deploy.sh rollback ${prev} ${OWNER} ${DEPLOY_DIR}"
        echo ""
    fi
}

# ── Vuelta atrás manual ──────────────────────────────────────────────────────
do_rollback() {
    log "=== VUELTA ATRÁS a ${TAG} ==="
    comprobar_entorno
    exigir_variables "${API_ENV}" FICHAITOR_DATABASE_URL
    exigir_variables "${WORKER_ENV}" FICHAITOR_WORKER_DATABASE_URL
    exigir_variables "${CONVERSATION_ENV}" FICHAITOR_CONVERSATION_DATABASE_URL
    pull_images "${TAG}" || die "No se pueden descargar las imágenes de ${TAG}."
    arrancar_db
    # No se aplica el esquema: el del servidor ya es igual o más nuevo.
    if deploy_tag "${TAG}"; then
        write_versions "${TAG}"
        log "Vuelta atrás a ${TAG} completada."
    else
        die "La vuelta atrás ha fallado. Hace falta intervenir a mano."
    fi
}

# ── Despliegue ───────────────────────────────────────────────────────────────
do_deploy() {
    log "=== DESPLIEGUE de ${TAG} ==="
    comprobar_entorno

    local previous_tag
    previous_tag="$(version_at 1)"

    # Hasta aquí no se ha tocado nada. Si la descarga falla, tampoco.
    pull_images "${TAG}" || die "No se pueden descargar las imágenes de ${TAG}."

    # Para arrancar solo la base de datos, Compose necesita las dos imágenes
    # definidas. Aún no se escribe .env: los servicios siguen con la versión anterior.
    export FICHAITOR_API_IMAGE="${API_IMAGE_BASE}:${TAG}"
    export FICHAITOR_WEB_IMAGE="${WEB_IMAGE_BASE}:${TAG}"
    arrancar_db

    # La copia va ANTES del esquema. Si no se puede copiar, no se despliega.
    "${DEPLOY_DIR}/backup.sh" "pre-${TAG:0:12}" || die "La copia de seguridad ha fallado: no se aplica el esquema."

    # La conversación se para antes del esquema: schema_channels.sql mueve la propuesta
    # abierta de cada hilo a su carril, y el código anterior podría volver a escribir en
    # el hilo después del relleno. Se arranca con la versión nueva más abajo (o con la
    # anterior si hay vuelta atrás).
    compose stop fichaitor-conversation >/dev/null 2>&1 || true

    if ! aplicar_esquema; then
        compose start fichaitor-conversation >/dev/null 2>&1 || true
        die "El esquema no se ha aplicado (la transacción se ha deshecho). La versión anterior sigue en marcha."
    fi

    "${DEPLOY_DIR}/init-roles.sh" || die "No se han podido preparar los usuarios de la base de datos."
    exigir_variables "${API_ENV}" FICHAITOR_DATABASE_URL
    exigir_variables "${WORKER_ENV}" FICHAITOR_WORKER_DATABASE_URL
    exigir_variables "${CONVERSATION_ENV}" FICHAITOR_CONVERSATION_DATABASE_URL

    if deploy_tag "${TAG}"; then
        write_versions "${TAG}"
        log "Despliegue de ${TAG} completado."
        remove_old_images
        print_rollback_hint
        return 0
    fi

    err "La comprobación de salud ha fallado."
    if [[ -z "${previous_tag}" || "${previous_tag}" == "${TAG}" ]]; then
        die "No hay una versión anterior a la que volver. Hace falta intervenir a mano."
    fi
    log "Vuelta automática a ${previous_tag} ..."
    if deploy_tag "${previous_tag}"; then
        echo ""
        echo "  Despliegue fallido:  ${TAG}"
        echo "  En marcha ahora:     ${previous_tag}"
        echo "  El esquema nuevo sigue aplicado (solo añade). Copia previa: ${DEPLOY_DIR}/backups/"
        echo ""
    else
        die "La vuelta automática también ha fallado. Hace falta intervenir a mano."
    fi
    exit 1
}

# ── Principal ────────────────────────────────────────────────────────────────
command -v docker >/dev/null || die "Falta docker."
command -v flock >/dev/null || die "Falta flock (paquete util-linux)."
[[ -d "${DEPLOY_DIR}" ]] || die "No existe ${DEPLOY_DIR}."

# Un solo despliegue a la vez en este servidor.
exec 9>"${DEPLOY_DIR}/.deploy.lock"
flock -n 9 || die "Ya hay otro despliegue de Fichaitor en marcha."

case "${MODE}" in
    rollback) do_rollback ;;
    *) do_deploy ;;
esac
