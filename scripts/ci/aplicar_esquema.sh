#!/usr/bin/env bash
# Aplica el esquema de la base de datos como lo hará un despliegue y comprueba
# que es idempotente: el esquema se aplica dos veces y la segunda no puede fallar.
#
# Orden:
#   1. db/schema.sql y, si existen, db/schema_worker.sql y db/schema_channels.sql
#   2. db/dev-init.sql (usuarios de desarrollo y pruebas)
#   3. otra vez el paso 1
#
# Variables:
#   FICHAITOR_TEST_OWNER_URL  URL de conexión del rol propietario (obligatoria).
#   PSQL                      Orden para invocar psql (por defecto, "psql").
#                             En local, sin psql instalado, sirve por ejemplo:
#                             PSQL="docker run --rm -i postgres:16-alpine psql"
#
# Uso, desde la raíz del repositorio:
#   bash scripts/ci/aplicar_esquema.sh
set -euo pipefail

: "${FICHAITOR_TEST_OWNER_URL:?Falta FICHAITOR_TEST_OWNER_URL}"

# Se parte en palabras a propósito: PSQL puede ser una orden con argumentos.
read -r -a psql_cmd <<<"${PSQL:-psql}"

aplicar() {
    local archivo="$1"
    echo "--> ${archivo}"
    # El archivo entra por la entrada estándar para que funcione igual
    # con un psql local que con uno dentro de un contenedor.
    "${psql_cmd[@]}" "${FICHAITOR_TEST_OWNER_URL}" \
        --no-psqlrc --quiet --set ON_ERROR_STOP=1 <"${archivo}"
}

aplicar_esquema() {
    aplicar db/schema.sql
    if [[ -f db/schema_worker.sql ]]; then
        aplicar db/schema_worker.sql
    else
        echo "--> db/schema_worker.sql no existe todavía: se omite"
    fi
    # Necesita las tablas de los dos anteriores (docs/canales.md).
    if [[ -f db/schema_channels.sql ]]; then
        aplicar db/schema_channels.sql
    fi
    # Cobro y backoffice, en el mismo orden que deploy.sh y preparar_bd_pruebas.sh.
    if [[ -f db/schema_billing.sql ]]; then
        aplicar db/schema_billing.sql
    fi
    if [[ -f db/schema_admin.sql ]]; then
        aplicar db/schema_admin.sql
    fi
}

echo "== Primera aplicación del esquema"
aplicar_esquema

echo "== Usuarios de desarrollo"
aplicar db/dev-init.sql

echo "== Segunda aplicación del esquema (idempotencia)"
aplicar_esquema

echo "Esquema aplicado dos veces sin errores."
