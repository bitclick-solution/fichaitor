#!/usr/bin/env bash
# Crea la base de datos de las pruebas de integración (si no existe) y le aplica los
# esquemas. Idempotente: se puede ejecutar las veces que haga falta.
#
# Las pruebas NUNCA se ejecutan contra la base de desarrollo: tests/conftest.py detiene
# la suite si alguna URL de prueba no apunta a una base cuyo nombre termine en "_test".
#
# Uso, con el servicio `db` de docker compose en marcha:
#   scripts/preparar_bd_pruebas.sh
#
# Variables opcionales:
#   FICHAITOR_TEST_DB      nombre de la base (por defecto fichaitor_test; debe acabar en _test)
#   FICHAITOR_DB_OWNER     rol propietario (por defecto fichaitor)
#   PSQL                   cómo llamar a psql como propietario, sin base de datos
#                          (por defecto: docker compose exec -T db psql -U <propietario>)
set -euo pipefail

cd "$(dirname "$0")/.."

DB="${FICHAITOR_TEST_DB:-fichaitor_test}"
OWNER="${FICHAITOR_DB_OWNER:-fichaitor}"
PSQL="${PSQL:-docker compose exec -T db psql -U $OWNER}"

case "$DB" in
  *_test) ;;
  *) echo "El nombre de la base de pruebas debe terminar en _test: $DB" >&2; exit 2 ;;
esac

exists=$($PSQL -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname = '$DB'")
if [ "$exists" != "1" ]; then
  $PSQL -d postgres -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$DB\" OWNER \"$OWNER\""
  echo "Base de datos $DB creada."
fi

# Los roles son del clúster: dev-init.sql crea los de desarrollo si faltan.
for file in db/dev-init.sql db/schema.sql db/schema_worker.sql db/schema_channels.sql; do
  $PSQL -d "$DB" -q -v ON_ERROR_STOP=1 < "$file"
done

echo "Esquemas aplicados en $DB. URL de las pruebas:"
echo "  FICHAITOR_TEST_OWNER_URL=postgresql://$OWNER:<clave>@127.0.0.1:5433/$DB"
echo "  FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/$DB"
echo "  FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/$DB"
echo "  FICHAITOR_TEST_CONVERSATION_URL=postgresql://fichaitor_conversation_dev:dev@127.0.0.1:5433/$DB"
