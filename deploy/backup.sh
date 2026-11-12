#!/usr/bin/env bash
# backup.sh — Copia de seguridad de la base de datos de Fichaitor.
#
# Deja dos archivos en la carpeta de copias, con modo 600:
#   fichaitor-<fecha>[-<etiqueta>].dump       base de datos completa (pg_dump, formato custom)
#   fichaitor-<fecha>[-<etiqueta>].roles.sql  roles y sus contraseñas cifradas (pg_dumpall)
#
# La copia contiene datos personales y los tokens de Google CIFRADOS. No contiene
# la clave privada que los descifra (FICHAITOR_TOKEN_PRIVATE_KEY): esa se guarda
# aparte. Cómo restaurar: deploy/restore.md.
#
# Uso (en el servidor):
#   ./backup.sh              copia manual o programada (cron)
#   ./backup.sh pre-abc123   deploy.sh la llama así antes de aplicar el esquema
#
# Variables opcionales:
#   FICHAITOR_DEPLOY_DIR             carpeta del despliegue (por defecto, la de este script)
#   FICHAITOR_BACKUP_DIR             carpeta de copias (por defecto, <despliegue>/backups)
#   FICHAITOR_BACKUP_RETENCION_DIAS  días que se conservan (por defecto, 14; 0 = no borrar)
set -euo pipefail
umask 077

DEPLOY_DIR="${FICHAITOR_DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BACKUP_DIR="${FICHAITOR_BACKUP_DIR:-${DEPLOY_DIR}/backups}"
RETENCION_DIAS="${FICHAITOR_BACKUP_RETENCION_DIAS:-14}"
DB_CONTAINER="fichaitor-db"
DB_USER="fichaitor"
DB_NAME="fichaitor"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() {
    echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2
    exit 1
}

ETIQUETA="${1:-}"
if [[ -n "${ETIQUETA}" && ! "${ETIQUETA}" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
    die "Etiqueta no válida: solo letras, números, punto, guion y guion bajo."
fi
[[ "${RETENCION_DIAS}" =~ ^[0-9]+$ ]] || die "FICHAITOR_BACKUP_RETENCION_DIAS debe ser un número de días."

[[ "$(docker inspect --format '{{.State.Running}}' "${DB_CONTAINER}" 2>/dev/null || true)" == "true" ]] ||
    die "El contenedor ${DB_CONTAINER} no está en marcha."

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

BASE="${BACKUP_DIR}/fichaitor-$(date -u '+%Y%m%dT%H%M%SZ')${ETIQUETA:+-${ETIQUETA}}"
VOLCADO="${BASE}.dump"
ROLES="${BASE}.roles.sql"

# Se escribe con otro nombre y se renombra al final: una copia a medias nunca
# se confunde con una buena.
limpiar() { rm -f "${VOLCADO}.parcial" "${ROLES}.parcial"; }
trap limpiar EXIT

log "Copia de la base de datos ${DB_NAME} ..."
# Sin -t: la salida es binaria y un terminal la estropearía.
docker exec "${DB_CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" \
    --format=custom --compress=6 >"${VOLCADO}.parcial"

[[ -s "${VOLCADO}.parcial" ]] || die "La copia ha salido vacía."

# pg_restore --list lee el índice del archivo: falla si la copia está truncada.
docker exec -i "${DB_CONTAINER}" pg_restore --list <"${VOLCADO}.parcial" >/dev/null ||
    die "La copia no se puede leer con pg_restore."

log "Copia de los roles ..."
docker exec "${DB_CONTAINER}" pg_dumpall -U "${DB_USER}" --roles-only >"${ROLES}.parcial"
[[ -s "${ROLES}.parcial" ]] || die "La copia de los roles ha salido vacía."

mv "${VOLCADO}.parcial" "${VOLCADO}"
mv "${ROLES}.parcial" "${ROLES}"

log "Copia terminada: ${VOLCADO} ($(du -h "${VOLCADO}" | cut -f1))"
log "SHA-256: $(sha256sum "${VOLCADO}" | cut -d' ' -f1)"

if [[ "${RETENCION_DIAS}" -gt 0 ]]; then
    # Solo se borran archivos con el nombre que pone este script.
    BORRADAS="$(find "${BACKUP_DIR}" -maxdepth 1 -type f \
        \( -name 'fichaitor-*.dump' -o -name 'fichaitor-*.roles.sql' \) \
        -mtime "+${RETENCION_DIAS}" -print -delete | wc -l)"
    log "Retención de ${RETENCION_DIAS} días: ${BORRADAS} archivos antiguos borrados."
fi
