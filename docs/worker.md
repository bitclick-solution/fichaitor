# Worker

El worker ejecuta el trabajo asíncrono de Fichaitor. Toma jobs de una cola en PostgreSQL y los ejecuta. Es el único proceso que tiene la clave privada de los tokens y las credenciales de Google.

Tiene tres jobs:

- `sync_profile` lee una ficha en Google y guarda tres cosas: el resumen del panel (reseñas sin responder, horarios especiales y cambios hechos por Google); desde el 2026-11-11, los hechos de la ficha con los que la API calcula el diagnóstico (`GET /api/v1/me/diagnosis`); y desde el 2026-11-12, las reseñas de la ficha.
- `reply_review` es **el ejecutor**: envía a Google la respuesta a una reseña que el dueño ha confirmado. Es el único código del producto que escribe en Google. Consulta [El job `reply_review`](#el-job-reply_review).
- `analyze_sentiment` calcula el sentimiento del establecimiento a partir de las reseñas guardadas. Lo encola el propio worker al terminar una sincronización. Es el único job que llama al modelo. Consulta [El job `analyze_sentiment`](#el-job-analyze_sentiment).

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
6. Guarda las reseñas en `review`. Consulta [Las reseñas guardadas](#las-reseñas-guardadas).

Lecturas de cada sincronización:

| Lectura | Endpoint | Qué se usa | Si Google la rechaza (4xx) |
|---|---|---|---|
| Reseñas | My Business v4 `GET {account}/{location}/reviews`, todas las páginas | Número, nota media, sin responder, fecha de la última. De cada reseña: identificador, estrellas, texto original, fecha y respuesta del propietario | La sincronización falla |
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

### Las reseñas guardadas

Cada sincronización deja en `review` las reseñas que Google devuelve para esa ficha:

| Se guarda | No se guarda |
|---|---|
| Identificador de Google (`reviewId`), estrellas, texto original, fecha, y el texto y la fecha de la respuesta del propietario si la hay | **El nombre y la foto de quien escribió la reseña** (campo `reviewer` de Google). El worker no lee ese campo y la tabla no tiene columna para él. Tampoco la traducción automática |

- **Una reseña conserva su identificador** (`rev_…`) entre sincronizaciones. Se actualizan sus estrellas, su texto y su respuesta.
- **Las reseñas que Google deja de devolver se marcan con `removed_at`.** No se borran. Si Google vuelve a devolverlas, dejan de estar retiradas.
- **Una reseña sin `reviewId` válido no se guarda**: no se podría responder. Sigue contando en las cifras del resumen.
- **Una fila por reseña y conexión** (`google_sub`, `location`), igual que el resumen: dos cuentas que administran la misma ficha tienen filas distintas con identificadores distintos.
- **El texto de una reseña es un dato.** Nada de lo que diga una reseña crea una acción, encola un job ni cambia lo que hace el worker.
- **Respuesta recién enviada.** Durante 24 horas después de que Fichaitor envíe una respuesta, una sincronización en la que Google todavía no la devuelve no la borra ni vuelve a contar la reseña como "sin responder". Pasado ese plazo, vale lo que Google devuelva.
- `profile_summary.reviews_synced_at` guarda cuándo se guardaron las reseñas por última vez. Con `NULL`, la API responde `pendiente` en `GET /api/v1/me/reviews` y pide una sincronización.

> **Nota:** Una ficha sincronizada antes del 2026-11-12 no tiene reseñas guardadas. La API pide la sincronización la primera vez que alguien abre la lista de reseñas. Hace falta que el worker en marcha tenga ya este código.

## El job `analyze_sentiment`

Carga: `{"google_sub": "...", "location": "locations/123"}`, la misma que `sync_profile`.

Nadie lo encola desde la API. Al terminar `sync_profile`, cuando las reseñas ya están guardadas, el worker decide si toca un análisis y, si toca, encola este job con ejecución inmediata. Así, tras la primera sincronización de una ficha, el análisis existe unos segundos después del resumen.

Qué hace, en orden:

1. Vuelve a decidir si toca. Si no, termina sin hacer nada: ejecutarlo dos veces no cuesta nada.
2. Clasifica por sus estrellas las reseñas nuevas sin texto. Sin modelo.
3. Envía al modelo, por lotes, las reseñas con texto nuevas, cambiadas o pendientes: estrellas y texto, nada más. Guarda cada clasificación válida; la que no vale queda pendiente.
4. Calcula en código la nota, la valoración y los recuentos por tema.
5. Si hay suficientes reseñas con texto, pide al modelo el resumen y la sugerencia, y los valida. Si no pasan, usa los textos por reglas.
6. Guarda el análisis, la nota del mes y el uso del modelo.

Reglas:

- **El primer análisis no tiene tope.** Después, como máximo uno cada 24 horas por ficha: con una reseña nueva de 1 o 2 estrellas, con 3 reseñas nuevas, o a los 30 días si hay al menos una nueva.
- **Un fallo del modelo no hace fallar el job.** El lote queda pendiente para el siguiente análisis y la nota se calcula con las estrellas.
- **Un fallo de la decisión o del encolado no hace fallar la sincronización.** Se registra `sentiment_not_enqueued` y la siguiente sincronización lo vuelve a intentar.
- **Tres intentos** (`max_attempts`), para fallos de la base de datos. Si se agotan y la ficha no tenía análisis, queda `error`; si lo tenía, se conserva.
- **El texto de una reseña es un dato.** Nada de lo que diga cambia lo que hace el job.
- **Sin textos en los logs.** `sentiment_analyzed` lleva contadores: reseñas, llamadas, tokens y nota.

El modelo sale de las mismas variables que la captura y los borradores: `FICHAITOR_GEMINI_API_KEY` y `FICHAITOR_GEMINI_MODEL`, o `FICHAITOR_FAKE_SCREENSHOT=true`. Sin ninguna, el worker registra `sentiment_model_missing` al arrancar y el análisis cuenta todas las reseñas por sus estrellas.

La fórmula, los temas, los disparadores, el coste y lo que se guarda están en [sentimiento.md](sentimiento.md).

> **Nota:** Una ficha sincronizada antes del 2026-11-12 no tiene análisis. La API pide una sincronización la primera vez que alguien abre el panel. Hace falta que el worker en marcha tenga ya este código.

## El job `reply_review`

Carga: `{"action_id": "act_..."}`. Nada más. El job no lleva el texto ni la reseña: los lee de la acción.

Una **acción** es una fila de `action` que la API crea, en estado `pending`, cuando el dueño confirma una respuesta desde su sesión (`POST /api/v1/me/reviews/{id}/reply`). Guarda la cuenta, la ficha, la reseña y el texto exacto confirmado. En la misma transacción, la API escribe la fila de auditoría `reply_confirmed` y encola este job con la clave `reply_review:<action_id>` y 3 intentos como máximo.

Qué hace el job:

1. **Toma la acción.** La pasa a `running` y anota el identificador del job. Si la acción no existe, ya terminó o la tiene otro job, no hace nada y el job acaba `done` (evento `action_skipped`).
2. Busca la conexión más reciente de ese `google_sub` y comprueba que la ficha está en ella. La cuenta y la ficha que van en la URL de Google salen de la conexión guardada.
3. Descifra el token de renovación y pide a Google un token de acceso.
4. **Si es un reintento**, lee primero la reseña en Google (`GET {account}/{location}/reviews/{reviewId}`). Si la respuesta que hay ya es exactamente el texto confirmado, no vuelve a escribir.
5. **Escribe**: `PUT https://mybusiness.googleapis.com/v4/{account}/{location}/reviews/{reviewId}/reply` con el cuerpo `{"comment": "<texto>"}`. Una sola petición por intento, sin reintentos dentro del adaptador.
6. Cierra la acción y escribe la fila de auditoría, en una transacción. Si ha ido bien, marca la reseña guardada como respondida (texto y fecha) y resta uno a `unanswered_count` del resumen (y a `unanswered_low_count` si era de 1 o 2 estrellas), sin esperar a otra sincronización.

La operación de Google es `accounts.locations.reviews.updateReply` ([referencia](https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews/updateReply)): crea la respuesta si no existe y la sustituye si existe. Por eso responder a una reseña ya respondida es editar su respuesta. Google solo la admite en fichas verificadas. El texto admite 4.096 bytes como máximo ([`ReviewReply.comment`](https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews#ReviewReply)); la API lo comprueba antes de crear la acción.

> **Importante:** Esta escritura no se ha probado contra Google real. Una respuesta es pública y avisa a quien escribió la reseña. El adaptador se prueba solo con transporte HTTP simulado, y sigue pendiente la prueba de `docs/fase0/matriz-api.md` (reenviar el mismo texto sobre una reseña ya respondida).

| Qué pasa | Job | Acción | Auditoría |
|---|---|---|---|
| Google acepta la respuesta (200) | `done` | `done` | `reply_sent`, código 200 |
| Reintento: Google ya tiene ese texto | `done`, sin escribir | `done` | `reply_sent`, código 200, motivo `already_applied` |
| Google rechaza el token de renovación (`invalid_grant`) o responde 401 | `failed`, sin reintentos | `failed`, `reconnect`: "Tu acceso a Google ha caducado; vuelve a conectar." | `reply_failed`, código 401, motivo `reconnect` |
| Google responde 404 | `failed`, sin reintentos | `failed`, `review_gone` | `reply_failed`, código 404 |
| Google responde otro 4xx | `failed`, sin reintentos | `failed`, `rejected` | `reply_failed`, con el código |
| Google responde 5xx o 429, o no responde | Se reintenta (30 s, 60 s) | Sigue `running` hasta agotar los 3 intentos; entonces `failed`, `unavailable` | Solo al final: `reply_failed`, motivo `unavailable` |
| No hay conexión guardada para ese `google_sub` | `failed`, sin llamar a Google | `failed`, `reconnect` | `reply_failed`, motivo `reconnect` |
| La ficha no está en la conexión más reciente, o el token no se puede descifrar | `failed`, sin llamar a Google | `failed`, `internal` | `reply_failed`, motivo `internal` |
| El worker muere durante el último intento | `failed` al caducar el bloqueo | `failed`, `unavailable`, como mucho un minuto después (`on_lost`) | `reply_failed`, motivo `unavailable` |
| La carga no es válida o la acción no existe | `failed` o `done`, sin llamar a Google | No hay acción | Nada |

### Garantías del ejecutor

- **Sin acción no hay escritura.** El worker no puede crear acciones: su rol no tiene `INSERT` en `action`. Solo la API las crea, y solo desde una sesión con la cabecera de protección.
- **El texto es el confirmado.** El worker lee `action.text` y lo envía tal cual. Ni la API ni el worker tienen `UPDATE` sobre esa columna: nadie puede cambiarlo después de la confirmación. La carga del job no puede elegir el texto ni la reseña.
- **Una sola respuesta por acción.** Una acción terminada no se vuelve a ejecutar, aunque el job se repita o haya dos jobs para la misma acción. Una acción `running` solo la retoma el job que ya la tiene. Y un reintento mira antes lo que hay en Google.
- **Como mucho una acción viva por reseña.** Un índice único lo impide en la base; la API responde 409 a la segunda confirmación.
- **Un 4xx no se reintenta. Un 5xx, un 429 o un fallo de red, sí**, con la espera de la cola.
- **Nada sensible en los logs ni en `job.last_error`**: ni tokens, ni el texto de la reseña, ni la respuesta. De un fallo de Google solo queda el código de estado.

> **Nota:** Si Google aplica la respuesta pero su contestación se pierde en los tres intentos, la acción queda `failed` con `unavailable` aunque la respuesta esté en Google. La siguiente sincronización la trae y la reseña sale respondida.

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

### `review`

Una fila por reseña, ficha y conexión. La escribe el worker; la API solo la lee. Consulta [Las reseñas guardadas](#las-reseñas-guardadas).

| Columna | Uso |
|---|---|
| `id` | `rev_` y 16 caracteres. El identificador que ve la web |
| `google_sub`, `location` | De qué conexión y de qué ficha es |
| `gbp_review_id` | `reviewId` de Google. Va en la URL de la respuesta |
| `rating`, `comment`, `created_at` | Estrellas, texto original y fecha |
| `reply_text`, `reply_at` | Respuesta del propietario. `reply_text` a `NULL`: sin responder |
| `sent_at` | Cuándo envió Fichaitor la respuesta, si la envió |
| `first_seen_at`, `last_seen_at`, `removed_at` | Primera y última sincronización que la trajo, y cuándo dejó de venir |

No hay columna para el autor.

### `review_draft`

Último borrador que el modelo redactó para cada reseña (`review_id`, `source_hash`, `draft`, `created_at`). La escribe la API, que es quien llama al modelo; el worker no tiene acceso. `source_hash` cambia si cambian las estrellas, el texto de la reseña o la versión del prompt.

### `review_sentiment`, `sentiment_analysis`, `sentiment_history` y `sentiment_usage`

Las cuatro tablas del sentimiento del establecimiento. Las escribe el worker.

| Tabla | Una fila por | Contenido |
|---|---|---|
| `review_sentiment` | Reseña | Clasificación: huella de las estrellas y el texto, sentimiento, hasta 3 temas con polaridad, método (`model`, `stars`, `stars_fallback` o `pending`) e intentos. Ningún texto |
| `sentiment_analysis` | Ficha y conexión | Último análisis: estado (`ok`, `insuficiente` o `error`), fecha, nota, valoración, temas con recuentos, resumen, sugerencia y disparador |
| `sentiment_history` | Ficha, conexión y mes | Nota del mes. El mes en curso se actualiza con cada análisis |
| `sentiment_usage` | Análisis | Contadores de llamadas, reseñas enviadas y tokens. Solo se añaden filas |

El detalle está en [sentimiento.md](sentimiento.md#qué-se-guarda).

### `action`

Acciones sobre Google confirmadas por el dueño. Hoy solo hay un tipo, `reply_review`.

| Columna | Uso | Quién la escribe |
|---|---|---|
| `id` | `act_` y 16 caracteres | La base, al insertar |
| `account_id`, `google_sub`, `location`, `review_id`, `type` | Quién confirma y sobre qué | La API, una vez |
| `text` | El texto confirmado, de 1 a 4.096 bytes | La API, una vez |
| `confirmed_at` | Cuándo confirmó el dueño | La API, una vez |
| `state` | `pending`, `running`, `done` o `failed` | El worker |
| `job_id`, `attempts`, `started_at`, `finished_at` | Qué job la tiene, intentos y fechas | El worker |
| `google_status`, `error_code` | Código HTTP de Google y motivo del fallo (lista cerrada) | El worker |

### `audit_log`

Registro de auditoría. **Solo se añaden filas**: nadie las cambia ni las borra.

| Columna | Uso |
|---|---|
| `at`, `written_by` | Cuándo y con qué usuario de la base se escribió. Los pone la base: quien inserta no puede fijarlos |
| `event` | `reply_confirmed` (la API, cuando el dueño confirma), `reply_sent` o `reply_failed` (el worker, con el resultado) |
| `account_id` | Quién: la cuenta que confirmó |
| `action_id`, `action_type` | Qué acción |
| `location`, `review_id`, `gbp_review_id` | Sobre qué: la ficha y la reseña, con nuestro identificador y el de Google |
| `text` | El texto confirmado. En la fila del worker, el que se envió: se copia de la acción dentro de la base |
| `confirmed_at` | Cuándo se confirmó |
| `google_status`, `reason` | Resultado de Google: código HTTP y, si falla, el motivo. El motivo es un código de una lista cerrada (`reconnect`, `rejected`, `review_gone`, `unavailable`, `internal`, `already_applied`), nunca texto de Google |

Cada acción deja dos filas: la confirmación y el resultado. Una acción que sigue en curso tiene solo la primera.

La tabla tiene seguridad a nivel de fila: la API solo puede insertar `reply_confirmed` y el worker solo `reply_sent` y `reply_failed`. No hay ninguna política de lectura, cambio ni borrado, así que esas operaciones quedan negadas para los dos roles aunque alguien concediera el privilegio por error. Para consultarla hay que conectarse como propietario.

### Roles

| Rol | Puede | No puede |
|---|---|---|
| `fichaitor_api` | Insertar en `job` (solo `kind`, `payload`, `run_at`, `idempotency_key`, `max_attempts`); leer `profile_summary` y `review`; leer y escribir `review_draft`; insertar y leer `action` (al insertar, solo cuenta, ficha, reseña, tipo, texto y fecha de confirmación); insertar en `audit_log` el evento `reply_confirmed`; leer `sentiment_analysis` y `sentiment_history` | Leer `review_sentiment` o `sentiment_usage`, o escribir en las tablas del sentimiento; leer, cambiar o borrar jobs; fijar estado o intentos; escribir resúmenes o reseñas; cambiar o borrar una acción, o crearla con estado o resultado; leer, cambiar o borrar la auditoría |
| `fichaitor_worker` | Leer, insertar y actualizar `job`, `profile_summary` y `review`; leer de `google_connection` las columnas que usa; leer `action` y actualizar su estado, intentos, fechas y resultado; insertar en `audit_log` los eventos `reply_sent` y `reply_failed`; leer, insertar y actualizar `review_sentiment`, `sentiment_analysis` y `sentiment_history`; añadir filas a `sentiment_usage` | Leer o cambiar `sentiment_usage`; leer `waitlist_entry`, `funnel_event` o el email de las conexiones; escribir en `google_connection`; borrar nada; **crear una acción o cambiar su texto, su reseña o su cuenta**; leer `review_draft`; leer, cambiar o borrar la auditoría |

## Variables de entorno

Todas llevan el prefijo `FICHAITOR_`. El worker las valida al arrancar y, si falta alguna, termina con código 2 e indica cuál.

| Variable | Obligatoria | Valor por defecto | Uso |
|---|---|---|---|
| `FICHAITOR_WORKER_DATABASE_URL` | Sí | — | Conexión con un usuario que hereda de `fichaitor_worker` |
| `FICHAITOR_TOKEN_PRIVATE_KEY` | Sí | — | Clave privada (base64) que descifra los tokens de renovación |
| `FICHAITOR_GOOGLE_CLIENT_ID` | Sí, salvo con `FAKE_GOOGLE` | — | Cliente OAuth, el mismo de la API |
| `FICHAITOR_GOOGLE_CLIENT_SECRET` | Sí, salvo con `FAKE_GOOGLE` | — | Secreto del cliente OAuth |
| `FICHAITOR_FAKE_GOOGLE` | No | `false` | Usa una ficha de demostración y no llama a Google, ni para leer ni para responder a una reseña: la respuesta se queda en la memoria del worker |
| `FICHAITOR_WORKER_POLL_INTERVAL_SECONDS` | No | `2` | Espera entre consultas cuando la cola está vacía |
| `FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS` | No | `120` | Tiempo máximo de un job. Debe ser menor que el siguiente |
| `FICHAITOR_WORKER_LOCK_TIMEOUT_SECONDS` | No | `300` | Antigüedad del bloqueo a partir de la cual un job se da por abandonado |
| `FICHAITOR_WORKER_ID` | No | `<host>-<pid>` | Nombre del worker en `locked_by` y en los logs |
| `FICHAITOR_GEMINI_API_KEY` y `FICHAITOR_GEMINI_MODEL` | No | — y `gemini-3.6-flash` | Modelo del sentimiento del establecimiento. Las mismas variables que usa la API |
| `FICHAITOR_FAKE_SCREENSHOT` | No | `false` | Modelo de ejemplo para el sentimiento, sin clave y sin coste |
| `FICHAITOR_SENTIMENT_*` | No | Consulta [sentimiento.md](sentimiento.md#variables-de-entorno) | Umbrales, disparadores y límites de coste del sentimiento |

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

El worker escribe una línea JSON por evento: `worker_started`, `job_started`, `job_done`, `job_retry`, `job_failed`, `job_lost`, `lost_jobs_recorded`, `optional_read_rejected`, `reply_sent`, `action_skipped`, `action_lost`, `lost_hook_error`, `exhausted_hook_error`, `queue_error`, `stop_requested` y `worker_stopped`. Los logs llevan el identificador, el tipo y el intento del job, y el identificador de la acción en los eventos del ejecutor. No llevan cargas, tokens, cadenas de conexión, textos de reseñas ni respuestas.

> **Precaución:** Con `FICHAITOR_FAKE_GOOGLE=false`, un job `reply_review` publica de verdad. No encoles a mano un job de este tipo ni insertes filas en `action` contra una ficha real para probar: la respuesta es pública y avisa a quien escribió la reseña.

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
uv run pytest -q tests/test_worker_*.py tests/test_reviews_*.py tests/test_sentiment_*.py
```

Las pruebas de integración (`tests/test_worker_postgres.py`, `tests/test_reviews_postgres.py` y `tests/test_sentiment_postgres.py`) se saltan si faltan estas variables:

```bash
export FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor
export FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor
export FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor
```

Crean y borran solo filas marcadas con el prefijo `pytest-`. Los jobs de las pruebas llevan un tipo propio (`pytest-…`), así que un worker en marcha sobre la misma base no los toma. Ninguna prueba llama a Google ni a Gemini: la escritura del adaptador real se prueba con transporte HTTP simulado (`tests/test_reviews_google.py`), igual que el modelo del sentimiento (`tests/test_sentiment_gemini.py`).

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
