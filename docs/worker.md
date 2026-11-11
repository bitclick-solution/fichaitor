# Worker

El worker ejecuta el trabajo asíncrono de Fichaitor. Toma jobs de una cola en PostgreSQL y los ejecuta. Es el único proceso que tiene la clave privada de los tokens y las credenciales de Google.

El primer job es `sync_profile`: lee una ficha en Google y guarda un resumen con lo que el panel muestra hoy como bloqueado (reseñas sin responder, horarios especiales y cambios hechos por Google).

## Cómo funciona

1. La API inserta una fila en `job`. No llama al worker: la cola es el único canal entre ambos.
2. El worker toma el siguiente job vencido con `FOR UPDATE SKIP LOCKED` y lo marca `running`.
3. El worker ejecuta el manejador registrado para ese `kind`.
4. Según el resultado, el job queda `done`, vuelve a `pending` con una espera, o queda `failed`.

Reglas de la cola:

- **Un job, un worker.** Dos workers nunca reciben el mismo job. Puedes arrancar varios procesos.
- **Reintentos.** Un fallo temporal reprograma el job con espera creciente: 30 s, 60 s, 120 s… hasta 1 hora.
- **Límite de intentos.** Al agotar `max_attempts` (5 por defecto), el job queda `failed` con su error en `last_error`.
- **Errores definitivos.** Un manejador lanza `PermanentJobError` cuando reintentar no sirve. El job queda `failed` en el primer intento.
- **Worker caído.** Un job `running` con un bloqueo más antiguo que `WORKER_LOCK_TIMEOUT_SECONDS` lo recupera otro worker. Si ya no le quedan intentos, queda `failed`.
- **Tiempo máximo.** Cada ejecución se corta a los `WORKER_JOB_TIMEOUT_SECONDS`, siempre antes de que caduque el bloqueo.
- **Idempotencia.** Encolar dos veces con la misma `idempotency_key` no crea un segundo job.
- **Tipos conocidos.** Un worker solo toma los `kind` para los que tiene manejador.

> **Importante:** La clave de idempotencia es única para siempre, también después de terminar el job. Para un trabajo periódico, incluye el periodo en la clave, por ejemplo `sync_profile:<google_sub>:<location>:2026-11-11`.

### Encolar desde la API

Usa `PostgresJobQueue.enqueue` con el pool de la API. Solo necesita permiso de `INSERT`:

```python
from app.adapters.job_queue.postgres import PostgresJobQueue

created = await PostgresJobQueue(pool).enqueue(
    "sync_profile",
    {"google_sub": google_sub, "location": "locations/123"},
    idempotency_key=f"sync_profile:{google_sub}:locations/123:{today}",
)
```

`enqueue` devuelve `True` si creó el job y `False` si la clave ya existía. No devuelve el identificador: la API no puede leer la cola.

## El job `sync_profile`

Carga: `{"google_sub": "...", "location": "locations/123"}`.

1. Busca la conexión más reciente de ese `google_sub` en `google_connection`.
2. Comprueba que la ficha pertenece a esa conexión. La cuenta y la ficha que se envían a Google salen de la conexión guardada, no de la carga.
3. Descifra el token de renovación con la clave privada.
4. Pide a Google un token de acceso y lee las reseñas (todas las páginas), los horarios y `getGoogleUpdated`.
5. Calcula el resumen con `app/domain/profile_summary.py` y lo guarda en `profile_summary`.

| Qué pasa | Job | `profile_summary.status` |
|---|---|---|
| Todo va bien | `done` | `ok`, con cifras nuevas |
| Google responde 5xx, 429 o no responde | Se reintenta | Sin cambios hasta agotar intentos; entonces `error` |
| Google rechaza el token de renovación (`invalid_grant` o 401) | `failed`, sin reintentos | `needs_reconnect` |
| Google rechaza la lectura (4xx) | `failed`, sin reintentos | `error` |
| El token no se puede descifrar | `failed`, sin reintentos | `error` |
| No hay conexión o la ficha no es de esa conexión | `failed`, sin reintentos | No se escribe nada |

Cuando el estado no es `ok`, las cifras de la última lectura correcta se conservan. `synced_at` indica de cuándo son.

De las reseñas con el formato `(Translated by Google) … (Original) …` se conserva solo el texto original. El resumen no guarda textos ni autores, solo cifras.

> **Nota:** "Horarios especiales futuros" cuenta días distintos con fecha de hoy en adelante. "Hoy" se calcula en UTC.

## Tablas

Se definen en `db/schema_worker.sql`, que es idempotente y se aplica después de `db/schema.sql`.

### `job`

| Columna | Uso |
|---|---|
| `id`, `kind`, `payload` | Identificador, tipo de trabajo y datos |
| `run_at` | Cuándo puede ejecutarse. Un reintento lo mueve al futuro |
| `state` | `pending`, `running`, `done` o `failed` |
| `attempts`, `max_attempts` | Intentos empezados y máximo |
| `locked_by`, `locked_at` | Qué worker lo tiene y desde cuándo. Solo en `running` |
| `last_error` | Último error, sin datos sensibles |
| `idempotency_key` | Única; opcional |
| `created_at`, `finished_at` | Fechas de alta y de cierre |

### `profile_summary`

Una fila por ficha y conexión (`google_sub`, `location`). Cada sincronización la actualiza.

| Columna | Uso |
|---|---|
| `status`, `status_at` | `ok`, `needs_reconnect` o `error`, y fecha del último intento |
| `synced_at` | Fecha de la última lectura correcta |
| `review_count`, `average_rating` | Número de reseñas y nota media |
| `unanswered_count`, `unanswered_low_count` | Reseñas sin responder: total y de 1 o 2 estrellas |
| `last_review_at` | Fecha de la última reseña |
| `has_regular_hours`, `upcoming_special_hours` | Si tiene horario regular y cuántos días con horario especial quedan |
| `google_changed`, `google_changed_fields` | Si Google ha cambiado algo (`diffMask` no vacío) y qué campos |

### Roles

| Rol | Puede | No puede |
|---|---|---|
| `fichaitor_api` | Insertar en `job` (solo `kind`, `payload`, `run_at`, `idempotency_key`, `max_attempts`); leer `profile_summary` | Leer, cambiar o borrar jobs; fijar estado o intentos; escribir resúmenes |
| `fichaitor_worker` | Leer, insertar y actualizar `job` y `profile_summary`; leer de `google_connection` las columnas que usa | Leer `waitlist_entry`, `funnel_event` o el email de las conexiones; escribir en `google_connection`; borrar |

## Variables de entorno

Todas llevan el prefijo `FICHAITOR_`. El worker las valida al arrancar y, si falta alguna, termina con código 2 e indica cuál.

| Variable | Obligatoria | Valor por defecto | Uso |
|---|---|---|---|
| `FICHAITOR_WORKER_DATABASE_URL` | Sí | — | Conexión con un usuario que hereda de `fichaitor_worker` |
| `FICHAITOR_TOKEN_PRIVATE_KEY` | Sí | — | Clave privada (base64) que descifra los tokens de renovación |
| `FICHAITOR_GOOGLE_CLIENT_ID` | Sí, salvo con `FAKE_GOOGLE` | — | Cliente OAuth, el mismo de la API |
| `FICHAITOR_GOOGLE_CLIENT_SECRET` | Sí, salvo con `FAKE_GOOGLE` | — | Secreto del cliente OAuth |
| `FICHAITOR_FAKE_GOOGLE` | No | `false` | Usa una ficha de demostración y no llama a Google |
| `FICHAITOR_WORKER_POLL_INTERVAL_SECONDS` | No | `2` | Espera entre consultas cuando la cola está vacía |
| `FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS` | No | `120` | Tiempo máximo de un job. Debe ser menor que el siguiente |
| `FICHAITOR_WORKER_LOCK_TIMEOUT_SECONDS` | No | `300` | Antigüedad del bloqueo a partir de la cual un job se da por abandonado |
| `FICHAITOR_WORKER_ID` | No | `<host>-<pid>` | Nombre del worker en `locked_by` y en los logs |

> **Precaución:** `FICHAITOR_TOKEN_PRIVATE_KEY` solo debe estar en el entorno del worker. No la pongas en el entorno de la API. La API cifra con la clave pública y no puede leer un token.

## Arrancar en local

1. Levanta la base de datos:

   ```bash
   docker compose up -d db
   ```

2. Aplica el esquema del worker:

   ```bash
   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_worker.sql
   ```

3. Crea el usuario de desarrollo del worker, si no existe. El bloque está al final de `db/schema_worker.sql`:

   ```bash
   docker compose exec -T db psql -U fichaitor -d fichaitor -c "CREATE ROLE fichaitor_worker_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_worker"
   ```

4. Define las variables. La clave privada es la pareja de `FICHAITOR_TOKEN_PUBLIC_KEY`; un par nuevo se genera con `uv run python scripts/gen_token_keys.py`:

   ```bash
   export FICHAITOR_WORKER_DATABASE_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor
   export FICHAITOR_TOKEN_PRIVATE_KEY=<clave privada>
   export FICHAITOR_FAKE_GOOGLE=true
   ```

5. Arranca el worker:

   ```bash
   uv run python -m app.worker
   ```

6. Para pararlo, pulsa `Ctrl+C`. El worker termina el job en curso y sale.

El worker escribe una línea JSON por evento: `worker_started`, `job_started`, `job_done`, `job_retry`, `job_failed`, `job_lost`, `queue_error`, `stop_requested` y `worker_stopped`. Los logs llevan el identificador, el tipo y el intento del job. No llevan cargas, tokens ni cadenas de conexión.

### Probar un job a mano

Con una conexión ya guardada en `google_connection`, encola un job como propietario:

```sql
INSERT INTO job (kind, payload)
VALUES ('sync_profile', '{"google_sub": "<sub>", "location": "locations/<id>"}');
```

Después consulta el resultado:

```sql
SELECT state, attempts, last_error FROM job ORDER BY created_at DESC LIMIT 1;
SELECT * FROM profile_summary;
```

## Pruebas

```bash
uv run pytest -q tests/test_worker_*.py
```

Las pruebas de integración (`tests/test_worker_postgres.py`) se saltan si faltan estas variables:

```bash
export FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor
export FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor
export FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor
```

Crean y borran solo filas marcadas con el prefijo `pytest-`. Ninguna prueba llama a Google.

## Servicio en Docker Compose

El worker usa la misma imagen que la API con otro comando. Añade este servicio a `docker-compose.yml`:

```yaml
  worker:
    build: .
    command: ["python", "-m", "app.worker"]
    env_file:
      - path: .env
        required: false
    environment:
      FICHAITOR_WORKER_DATABASE_URL: ${FICHAITOR_WORKER_DATABASE_URL:-postgresql://fichaitor_worker_dev:dev@db:5432/fichaitor}
      FICHAITOR_FAKE_GOOGLE: ${FICHAITOR_FAKE_GOOGLE:-true}
    depends_on:
      db:
        condition: service_healthy
    # La imagen trae la comprobación de salud HTTP de la API; el worker no escucha en ningún puerto.
    healthcheck:
      disable: true
    read_only: true
    tmpfs:
      - /tmp
    # Da tiempo a terminar el job en curso (FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS).
    stop_grace_period: 130s
    restart: on-failure:3
```

Y monta el esquema del worker en el servicio `db`, hasta que se fusione en `schema.sql`:

```yaml
      - ./db/schema_worker.sql:/docker-entrypoint-initdb.d/15-schema-worker.sql:ro
```

> **Nota:** `FICHAITOR_TOKEN_PRIVATE_KEY` no tiene valor por defecto. Si el servicio `api` y el servicio `worker` comparten `.env`, la API ignora la clave privada, pero la recibe en su entorno. En producción, usa un archivo de entorno distinto para cada servicio.

## Añadir un tipo de job

1. Escribe el manejador en `app/worker/`: una función o un objeto asíncrono que recibe un `Job`.
2. Lanza `PermanentJobError` si reintentar no sirve y `JobError` con un mensaje sin datos sensibles si el fallo es temporal. De cualquier otra excepción solo se guarda el tipo.
3. Si necesitas actuar cuando el job agota sus intentos, añade el método `on_exhausted(job)`.
4. Regístralo en `build_registry` (`app/worker/__main__.py`).
