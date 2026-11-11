# Worker

El worker ejecuta el trabajo asíncrono de Fichaitor. Toma jobs de una cola en PostgreSQL y los ejecuta. Es el único proceso que tiene la clave privada de los tokens y las credenciales de Google.

El primer job es `sync_profile`: lee una ficha en Google y guarda dos cosas. La primera es el resumen del panel (reseñas sin responder, horarios especiales y cambios hechos por Google). La segunda, desde el 2026-11-11, son los hechos de la ficha con los que la API calcula el diagnóstico (`GET /api/v1/me/diagnosis`), que ya no depende de Google Places.

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
- **Worker caído.** Un job `running` con un bloqueo más antiguo que `WORKER_LOCK_TIMEOUT_SECONDS` lo recupera otro worker. Si ya no le quedan intentos, queda `failed` y el manejador puede dejar constancia con `on_lost`. Consulta [Worker caído en el último intento](#worker-caído-en-el-último-intento).
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

La API usa dos claves para `sync_profile` (`app/application/profile_sync.py`). Las dos llevan un hash de la ficha, no sus identificadores:

| Cuándo | Clave | Límite |
|---|---|---|
| Al elegir ficha y al pedir el resumen | `sync_profile:<hash>:<ventana>` | Una por ficha y ventana de `FICHAITOR_SYNC_WINDOW_SECONDS` (15 minutos) |
| Al volver a conectar con Google, si ya hay ficha elegida | `sync_profile:reconnect:<hash>:<minuto>` | Una por ficha y minuto |

La clave de reconexión no depende de la ventana: la sincronización se encola aunque la ventana siga abierta. El prefijo `reconnect` impide que coincida con una clave de ventana, y el minuto impide encolar en bucle conectando una y otra vez. Si la reconexión no pudo listar las fichas (`fichas=pendiente`), no se encola nada: esa conexión no tiene ninguna ficha que el worker pueda leer.

## El job `sync_profile`

Carga: `{"google_sub": "...", "location": "locations/123"}`.

1. Busca la conexión más reciente de ese `google_sub` en `google_connection`.
2. Comprueba que la ficha pertenece a esa conexión. La cuenta y la ficha que se envían a Google salen de la conexión guardada, no de la carga.
3. Descifra el token de renovación con la clave privada.
4. Pide a Google un token de acceso y hace las lecturas de la tabla siguiente, en ese orden.
5. Calcula el resumen y reúne los hechos de la ficha con `app/domain/profile_summary.py`, y lo guarda todo en `profile_summary`.

Lecturas de cada sincronización:

| Lectura | Endpoint | Qué se usa | Si Google la rechaza (4xx) |
|---|---|---|---|
| Reseñas | My Business v4 `GET {account}/{location}/reviews`, todas las páginas | Número, nota media, sin responder, fecha de la última | La sincronización falla |
| Ficha | Business Information `GET locations/{id}?readMask=name,title,storefrontAddress,categories,profile,phoneNumbers,websiteUri,regularHours,specialHours,serviceItems,openInfo,metadata` | Horario, días con horario especial, nombre, dirección, categoría principal y número de adicionales, longitud de la descripción, si hay teléfono y web, número de servicios, si figura cerrada, `placeId` y `mapsUri` | La sincronización falla |
| Cambios de Google | Business Information `GET locations/{id}:getGoogleUpdated` | `diffMask` | La sincronización falla |
| Atributos | Business Information `GET locations/{id}/attributes` | Cuántos atributos hay declarados | El dato queda sin valor y la sincronización sigue |
| Fotos | My Business v4 `GET {account}/{location}/media?pageSize=100`, hasta 5 páginas | `totalMediaItemCount` y la fecha (`createTime`) del elemento más reciente | El dato queda sin valor y la sincronización sigue |

- **Atributos y fotos son opcionales.** Si Google rechaza una de esas dos lecturas, el worker escribe el evento `optional_read_rejected` en el log y guarda el dato como desconocido. El diagnóstico no dice nada de lo que no se pudo leer. Un 401 o un fallo temporal en esas lecturas se tratan como en las demás.
- **La fecha de la foto más reciente** solo se guarda si se han leído todos los elementos: Google no documenta en qué orden los devuelve. Con más de 500, queda sin valor.
- **Un periodo de horario especial de varios días** (`startDate` y `endDate`) cuenta cada uno de sus días, hasta 31.
- **El recuento de fotos** es de lo que ha subido el dueño. Las fotos de clientes están en otro endpoint y no se leen.

| Qué pasa | Job | `profile_summary.status` |
|---|---|---|
| Todo va bien | `done` | `ok`, con cifras nuevas |
| Google responde 5xx, 429 o no responde | Se reintenta | Sin cambios hasta agotar intentos; entonces `error` |
| Google rechaza el token de renovación (`invalid_grant` o 401) | `failed`, sin reintentos | `needs_reconnect` |
| Google rechaza la lectura (4xx) | `failed`, sin reintentos | `error` |
| El token no se puede descifrar | `failed`, sin reintentos | `error` |
| No hay ninguna conexión guardada para ese `google_sub` | `failed`, sin reintentos y sin llamar a Google | `needs_reconnect` |
| La ficha no está en la conexión más reciente | `failed`, sin reintentos y sin llamar a Google | `error` |
| El worker muere durante el último intento | `failed` al caducar el bloqueo | `error`, como mucho un minuto después |
| La carga no es válida (falta `google_sub` o `location`) | `failed`, sin reintentos | No se escribe nada: no hay ficha a la que apuntar |

Cuando el estado no es `ok`, las cifras y los hechos de la última lectura correcta se conservan. `synced_at` indica de cuándo son.

Todo job con una carga válida deja un estado en `profile_summary`. La API no puede leer la cola, así que el resumen es el único sitio donde el panel ve un fallo. Sin ese estado, la API respondería `pendiente` sin fin.

### Worker caído en el último intento

Si un worker muere mientras ejecuta el último intento de un job, nadie llama a `on_exhausted`. La cola cierra el job como `failed` cuando caduca su bloqueo, con el error "el worker que lo ejecutaba dejó de responder".

Para que el resumen no se quede sin estado, cada worker busca esos jobs después de consultar la cola, como mucho una vez por minuto, y llama al método `on_lost` de cada manejador. El de `sync_profile` pone en `error` el resumen de las fichas afectadas (`PostgresWorkerStore.mark_lost_syncs`):

- El estado lleva la fecha en que se cerró el job y solo se escribe sobre un estado más antiguo. Una sincronización posterior nunca queda tapada.
- Repetirlo no cambia nada. Varios workers pueden hacerlo a la vez.
- Solo mira los jobs cerrados en las últimas 24 horas.
- No llama a Google ni toca la cola.

Qué ve el usuario mientras tanto: hasta que caduca el bloqueo (`WORKER_LOCK_TIMEOUT_SECONDS`, 5 minutos) el resumen sigue como estaba, es decir, `pendiente` o las cifras anteriores. Después pasa a `error` y la API vuelve a pedir la sincronización cuando se cumple la ventana.

> **Nota:** Hace falta al menos un worker en marcha para que esto ocurra. Sin ninguno, los jobs no se cierran y el resumen no cambia.

De las reseñas con el formato `(Translated by Google) … (Original) …` se conserva solo el texto original. El resumen no guarda textos ni autores, solo cifras.

### Los hechos de la ficha y el diagnóstico

El worker guarda hechos; el diagnóstico lo calcula la API en cada petición con `app/domain/profile_diagnosis.py`. Motivos:

- **Los festivos dependen del día.** "Hay un festivo en los próximos 30 días sin horario especial" cambia de un día a otro sin que la ficha cambie. Un diagnóstico guardado quedaría viejo; los hechos, no.
- **Las reglas pueden cambiar sin volver a leer Google.** Un texto o un peso nuevo se aplica a todas las fichas en el siguiente despliegue de la API.
- **El worker no necesita el calendario** ni conocer las reglas.

Los hechos van en la columna `facts` (jsonb) de `profile_summary`, no en columnas sueltas:

- La API los lee siempre enteros y nunca filtra ni ordena por ellos.
- La lista va a crecer (comunidad autónoma, publicaciones, carta) y cada hecho nuevo sería una migración.
- Llevan su versión en el campo `v`. Si la API encuentra una versión que no conoce o un formato roto, trata la ficha como "todavía sin diagnóstico" y pide una sincronización que los reescribe.

Qué contiene `facts`:

| Campo | Qué es |
|---|---|
| `v` | Versión del formato. Hoy, 1 |
| `title`, `address`, `category` | Nombre, dirección y categoría principal de la ficha |
| `additional_categories` | Número de categorías adicionales |
| `description_length` | Caracteres de la descripción del dueño. El texto no se guarda |
| `has_phone`, `has_website` | Si la ficha tiene teléfono y web. Los valores no se guardan |
| `service_count` | Número de servicios declarados |
| `is_open` | `false` si la ficha figura como cerrada temporal o definitivamente |
| `place_id`, `maps_url` | Identificador y enlace de Google Maps, si Google los da |
| `special_dates` | Días con horario especial desde el día de la sincronización (hasta 400) |
| `photo_count`, `latest_photo_at` | Fotos subidas por el dueño y fecha de la más reciente. `null` si no se pudieron leer |
| `attribute_count` | Atributos declarados. `null` si no se pudieron leer |

Las cifras de reseñas, el horario regular y los cambios de Google no se repiten en `facts`: el diagnóstico los toma de las columnas del resumen.

> **Nota:** Una fila guardada antes del 2026-11-11 tiene `facts` a `NULL`. Su resumen sigue valiendo. La API pide una sincronización en cuanto alguien abre el panel, y el diagnóstico responde `pendiente` hasta que termina. Hace falta que el worker en marcha tenga ya este código: un worker anterior sincroniza sin escribir `facts`.

> **Nota:** "Horarios especiales futuros" cuenta días distintos con fecha de hoy en adelante. "Hoy" se calcula en UTC.

## Tablas

Se definen en `db/schema_worker.sql`, que es idempotente y se aplica después de `db/schema.sql`. Sobre una base ya creada, volver a aplicarlo añade lo que falte (por ejemplo, la columna `facts`) sin tocar las filas.

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
| `facts` | Hechos de la ficha para el diagnóstico (jsonb). Consulta [Los hechos de la ficha y el diagnóstico](#los-hechos-de-la-ficha-y-el-diagnóstico) |

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

El worker escribe una línea JSON por evento: `worker_started`, `job_started`, `job_done`, `job_retry`, `job_failed`, `job_lost`, `lost_jobs_recorded`, `optional_read_rejected`, `lost_hook_error`, `exhausted_hook_error`, `queue_error`, `stop_requested` y `worker_stopped`. Los logs llevan el identificador, el tipo y el intento del job. No llevan cargas, tokens ni cadenas de conexión.

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
4. Si el job deja un estado que alguien lee fuera de la cola, añade el método `on_lost(kind)`. Se llama cada minuto para los jobs que murieron con su worker en el último intento. Debe poder repetirse sin efectos y devuelve cuántos ha anotado.
5. Regístralo en `build_registry` (`app/worker/__main__.py`).
