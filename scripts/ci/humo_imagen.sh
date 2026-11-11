#!/usr/bin/env bash
# Construye la imagen de la API, la arranca en modo de ejemplo (sin Google y
# sin base de datos) y comprueba que GET /healthz responde 200.
#
# El contenedor se arranca con el sistema de archivos de solo lectura, igual
# que en docker-compose.yml, para detectar pronto cualquier escritura en disco.
#
# Variables (todas opcionales):
#   IMAGEN      Etiqueta de la imagen que se construye   (fichaitor-api:ci)
#   CONTENEDOR  Nombre del contenedor de prueba          (fichaitor-api-ci)
#   PUERTO      Puerto local donde se publica la API     (8000)
#   ESPERA      Segundos máximos hasta que responda      (60)
#
# Uso, desde la raíz del repositorio:
#   bash scripts/ci/humo_imagen.sh
#   PUERTO=18000 bash scripts/ci/humo_imagen.sh   # si el 8000 está ocupado
set -euo pipefail

IMAGEN="${IMAGEN:-fichaitor-api:ci}"
CONTENEDOR="${CONTENEDOR:-fichaitor-api-ci}"
PUERTO="${PUERTO:-8000}"
ESPERA="${ESPERA:-60}"
URL="http://127.0.0.1:${PUERTO}/healthz"

limpiar() {
    docker rm --force "${CONTENEDOR}" >/dev/null 2>&1 || true
}
trap limpiar EXIT

echo "== Construcción de la imagen ${IMAGEN}"
docker build --tag "${IMAGEN}" .

echo "== Arranque en modo de ejemplo"
limpiar
docker run --detach --name "${CONTENEDOR}" \
    --read-only --tmpfs /tmp \
    --publish "127.0.0.1:${PUERTO}:8000" \
    --env FICHAITOR_FAKE_PLACES=true \
    "${IMAGEN}" >/dev/null

echo "== Espera a ${URL}"
codigo="000"
for ((i = 1; i <= ESPERA; i++)); do
    codigo="$(curl --silent --output /dev/null --max-time 2 --write-out '%{http_code}' "${URL}" || true)"
    if [[ "${codigo}" == "200" ]]; then
        echo "GET /healthz respondió 200 tras ${i} s."
        break
    fi
    # Si el contenedor ya ha terminado, no tiene sentido seguir esperando.
    if [[ "$(docker inspect --format '{{.State.Running}}' "${CONTENEDOR}")" != "true" ]]; then
        echo "El contenedor se ha detenido antes de responder."
        break
    fi
    sleep 1
done

if [[ "${codigo}" != "200" ]]; then
    echo "::error::GET /healthz no respondió 200 (último código: ${codigo})"
    echo "== Registro del contenedor"
    docker logs "${CONTENEDOR}" || true
    exit 1
fi

# La imagen no debe ejecutarse como root.
usuario="$(docker exec "${CONTENEDOR}" id -u)"
if [[ "${usuario}" == "0" ]]; then
    echo "::error::El contenedor se ejecuta como root"
    exit 1
fi
echo "El contenedor se ejecuta con el usuario ${usuario} (sin privilegios)."
