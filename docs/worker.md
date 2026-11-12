# Worker

El worker ejecuta el trabajo asíncrono de Fichaitor. Toma jobs de una cola en PostgreSQL y los ejecuta. Es el único proceso que tiene la clave privada de los tokens y las credenciales de Google.

Tiene siete jobs. Los cuatro primeros trabajan sobre la ficha:

- `sync_profile` lee una ficha en Google y guarda tres cosas: el resumen del panel (reseñas sin responder, horarios especiales y cambios hechos por Google); desde el 2026-11-11, los hechos de la ficha con los que la API calcula el diagnóstico (`GET /api/v1/me/diagnosis`); y desde el 2026-11-12, las reseñas de la ficha.
- `reply_review` es **el ejecutor**: envía a Google la respuesta a una reseña que el dueño ha confirmado. Es el único código del producto que escribe en Google. Consulta [El job `reply_review`](#el-job-reply_review).
- `set_special_hours` es **el ejecutor de las fechas clave** (2026-11-12): pone en Google el horario especial de un día que el dueño ha confirmado, por Telegram o en el panel. Es la segunda escritura en Google. Consulta [El job `set_special_hours`](#el-job-set_special_hours).
- `analyze_sentiment` calcula el sentimiento del establecimiento a partir de las reseñas guardadas. Lo encola el propio worker al terminar una sincronización. Es el único job que llama al modelo. Consulta [El job `analyze_sentiment`](#el-job-analyze_sentiment).

Los otros tres son los de la baja, y están en [Baja, correo y retención](#baja-correo-y-retención):

- `send_mail` envía los correos de la baja: el código y el aviso con el enlace de restauración.
- `apply_offboarding` hace efectiva una baja cuando llega su fecha: revoca los tokens en Google y los borra.
- `retention` es la pasada diaria que borra lo que ha superado su plazo. El worker se lo encola a sí mismo.

Desde el 2026-11-12, dos jobs más enlazan con los canales de mensajería (`docs/canales.md`). Este worker solo los **encola**; los ejecuta otro proceso, el de la conversación:

- Al terminar una sincronización, `sync_profile` encola un `propose_review` por cada reseña guardada por primera vez, sin respuesta y escrita en los últimos 7 días. Nunca hace fallar la sincronización.
- Al cerrar cada acción, bien o mal, `reply_review` y `set_special_hours` encolan un `channel_action_result` con `{"action_id", "ok", "error"}` y la clave `channel_action_result:<acción>`, para que el dueño que confirmó por Telegram sepa cómo acabó.

En las tablas de los canales este worker solo **borra**: al hacer efectiva una baja y en la pasada de retención. Consulta [El proceso de la conversación](#el-proceso-de-la-conversación).

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
- **Baja programada.** `sync_profile`, `analyze_sentiment`, `reply_review` y `set_special_hours` no hacen nada para un usuario de Google con una baja programada: terminan `done` con el evento `account_suspended`. Una respuesta pendiente queda `failed` con el motivo `suspended`.

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

1. Busca la conexión más reciente de ese `google_sub` en `google_connection`, sin contar las revocadas.
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
| Google rechaza el token de renovación con `invalid_grant` | `failed`, sin reintentos. La conexión queda revocada y su token, borrado. Consulta [Acceso retirado desde Google](#acceso-retirado-desde-google) | `needs_reconnect` |
| Google rechaza el token de renovación con un 401 sin `invalid_grant` | `failed`, sin reintentos. La conexión se conserva | `needs_reconnect` |
| Google rechaza la lectura (4xx) | `failed`, sin reintentos | `error` |
| El token no se puede descifrar | `failed`, sin reintentos | `error` |
| No hay ninguna conexión válida (sin revocar) para ese `google_sub` | `failed`, sin reintentos y sin llamar a Google | `needs_reconnect` |
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
5. Si hay suficientes reseñas con texto, pide al modelo el resumen y una sugerencia, y los valida. Si no pasan, usa los textos por reglas. El código añade la frase de las valoraciones bajas sin comentario y elige la sugerencia por reglas: primero, las reseñas sin responder.
6. Guarda el análisis, la nota del mes y el uso del modelo.

Reglas:

- **El primer análisis no tiene tope.** Después, como máximo uno cada 24 horas por ficha: con una reseña nueva de 1 o 2 estrellas, con 3 reseñas nuevas, o a los 30 días si hay al menos una nueva.
- **Un fallo del modelo no hace fallar el job.** El lote queda pendiente para el siguiente análisis y la nota se calcula con las estrellas.
- **Un fallo de la decisión o del encolado no hace fallar la sincronización.** Se registra `sentiment_not_enqueued` y la siguiente sincronización lo vuelve a intentar.
- **Tres intentos** (`max_attempts`), para fallos de la base de datos. Si se agotan y la ficha no tenía análisis, queda `error`; si lo tenía, se conserva.
- **El texto de una reseña es un dato.** Nada de lo que diga cambia lo que hace el job.
- **Versión de la fórmula.** Un análisis guardado con una versión anterior (`sentiment_analysis.formula_version`) se rehace en la siguiente sincronización, con el disparador `formula_update` y sin el tope de 24 horas. No vuelve a clasificar reseñas: lo normal es una sola llamada, la del resumen.
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
| Google rechaza el token de renovación (`invalid_grant`) o responde 401 | `failed`, sin reintentos. Con `invalid_grant`, la conexión queda revocada y su token, borrado | `failed`, `reconnect`: "Tu acceso a Google ha caducado; vuelve a conectar." | `reply_failed`, código 401, motivo `reconnect` |
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

## El job `set_special_hours`

Carga: `{"action_id": "act_..."}`. Nada más. El día y el horario salen de la acción.

La acción la crea, en estado `pending`, quien recibe la confirmación del dueño: el proceso de la conversación (botón o resumen confirmado en Telegram) o la API (`POST /api/v1/me/key-dates/{id}/answer`). Las dos usan el mismo método, `PostgresStore.create_special_hours_action`: en una transacción, la acción (`type = 'set_special_hours'`, `special_date` y en `text` el cambio en forma canónica, por ejemplo `{"date":"2026-12-08","kind":"periods","periods":[["10:00","14:00"]]}`), la fila de auditoría `special_hours_confirmed` y este job, con la clave `set_special_hours:<action_id>` y 6 intentos. Solo para la ficha elegida de esa cuenta, sin baja programada.

Qué hace el job:

1. **Toma la acción** con `claim_action`, que comprueba en la misma sentencia que la cuenta tiene ese usuario de Google y esa ficha, no retirada (M1). Una acción incoherente falla con `internal` sin llamar a Google.
2. **Lee el cambio del texto** de la acción, de forma estricta: si no es exactamente la forma canónica, o su día no es `special_date`, falla con `internal`.
3. Si hay **baja programada**, falla con `suspended`. Si el día **ya ha pasado** (hora peninsular), falla con `date_passed` sin llamar a Google. El mismo día todavía se aplica.
4. Conexión, ficha y token como en `reply_review`.
5. **Lee** el horario de la ficha: `GET locations/{id}?readMask=regularHours,specialHours`.
6. **Fusiona**: quita los periodos que empiezan ese día, añade los nuevos y devuelve todos los demás tal cual, incluidos los que no sabe leer. Nunca borra otra fecha. "Horario normal" copia los periodos normales de ese día de la semana; si la ficha no tiene horario normal, falla con `no_regular_hours`. Un día de la semana sin periodos queda cerrado.
7. Si Google **ya tiene exactamente** ese horario ese día (un reintento cuyo intento anterior llegó), no escribe y cierra con el motivo `already_applied`.
8. **Escribe**: `PATCH locations/{id}?updateMask=specialHours` con `{"specialHours": {"specialHourPeriods": [...]}}`, la lista entera. Una petición por intento.
9. Cierra la acción con su fila `special_hours_sent` o `special_hours_failed`, en una transacción. Si ha ido bien, añade el día a `facts.special_dates` del resumen. Encola `channel_action_result`.

| Qué pasa | Job | Acción |
|---|---|---|
| Google acepta (200) | `done` | `done` |
| Reintento: Google ya tiene ese horario | `done`, sin escribir | `done`, motivo `already_applied` |
| `invalid_grant` o 401 | `failed`, sin reintentos. Con `invalid_grant`, la conexión queda revocada y su token, borrado | `failed`, `reconnect` |
| Otro 4xx | `failed`, sin reintentos | `failed`, `rejected` |
| 5xx, 429 o sin respuesta | Se reintenta (30 s, 60 s, 120 s, 240 s, 480 s) | `failed`, `unavailable` al agotar los 6 intentos |
| El día ya pasó | `failed`, sin llamar a Google | `failed`, `date_passed` |
| "Horario normal" sin horario normal en Google | `failed` | `failed`, `no_regular_hours` |
| Acción incoherente o texto ilegible | `failed`, sin llamar a Google | `failed`, `internal` |
| El worker muere en el último intento | `failed` al caducar el bloqueo | `failed`, `unavailable` (`on_lost`) |

> **Importante:** La escritura de horario especial se probó a mano en la fase 0 contra la ficha de Bitclick (`docs/fase0/matriz-api.md`), con un día cerrado. Este código solo se ha probado con transporte HTTP simulado. Leer, fusionar y escribir no es atómico: si el dueño cambia otro día en Google entre la lectura y la escritura (unos segundos), ese cambio se pierde. La API no ofrece una escritura condicionada para esto.

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
| `special_dates` | Días con horario especial desde el día de la sincronización (hasta 400). El ejecutor de fechas clave añade el día que aplica |
| `region` | Comunidad autónoma (`AN`) sacada del código postal de una dirección de España; vacía si no se sabe. Añadido el 2026-11-12 sin cambiar `v`: en filas anteriores falta y se lee como vacía |
| `regular_hours` | Horario normal como `[día, "HH:MM", "HH:MM"]`, lunes 0, para la opción «Horario normal» del panel. Añadido el 2026-11-12, igual que `region` |
| `photo_count`, `latest_photo_at` | Fotos subidas por el dueño y fecha de la más reciente. `null` si no se pudieron leer |
| `attribute_count` | Atributos declarados. `null` si no se pudieron leer |

Las cifras de reseñas, el horario regular y los cambios de Google no se repiten en `facts`: el diagnóstico los toma de las columnas del resumen.

> **Nota:** Una fila guardada antes del 2026-11-11 tiene `facts` a `NULL`. Su resumen sigue valiendo. La API pide una sincronización en cuanto alguien abre el panel, y el diagnóstico responde `pendiente` hasta que termina. Hace falta que el worker en marcha tenga ya este código: un worker anterior sincroniza sin escribir `facts`.

> **Nota:** "Horarios especiales futuros" cuenta días distintos con fecha de hoy en adelante. "Hoy" se calcula en UTC.

## Baja, correo y retención

El recorrido de una baja, los plazos y la tabla de qué se borra y cuándo están en [retencion.md](retencion.md). Las rutas están en [contrato-web-api.md](contrato-web-api.md#baja-desconectar-y-borrar-la-cuenta-2026-11-12). Aquí están los tres jobs.

### El job `send_mail`

Envía los correos de una baja. La API los encola; solo el worker habla con el servidor de correo.

| Plantilla | Carga | Cuándo |
|---|---|---|
| `offboarding_code` | `to`, `scope`, `code`, `minutes`, `code_id` | Al pedir el código |
| `offboarding_scheduled` | `to`, `scope`, `delete_at`, `token` | Al confirmar la baja |
| `offboarding_restored` | `to`, `scope` | Al restaurar, desde la sesión o desde el enlace, en la misma transacción que restaura. `to` es el correo que tiene la cuenta en ese momento; si ya no tiene, no se encola. Con `scope: cuenta` lleva el botón «Entrar en Fichaitor» a `<FICHAITOR_PUBLIC_BASE_URL>/app/` |

- **La carga del job es el único sitio donde el código y el token de restauración existen en claro.** El job la vacía (`{}`) al enviar el correo, al fallar para siempre y al agotar los intentos. Cada minuto, `on_lost` vacía la de los jobs terminados que la conserven porque su worker murió.
- **3 intentos.** Un fallo temporal del servidor (4xx de SMTP, red) se reintenta. Un rechazo (5xx de SMTP, credenciales, certificado no válido, servidor sin TLS) no.
- **El resultado del código se anota** en `offboarding_code.mail_state` (`sent` o `failed`). La web lo consulta con `GET /api/v1/me/offboarding/code`.
- **Nada sensible en los logs ni en `job.last_error`**: los errores del adaptador son textos fijos, sin dirección, código ni enlace.
- **Sin correo configurado**, el worker registra `mail_missing` al arrancar y todo job `send_mail` falla al primer intento. La API responde 503 al pedir un código.

El adaptador SMTP usa `smtplib`, de la biblioteca estándar, en un hilo. Basta para un correo por conversación y no añade dependencias.

- **TLS obligatorio y certificado comprobado.** En el puerto 465, TLS desde el primer byte. En cualquier otro, STARTTLS: si el servidor no lo ofrece, el correo no se envía.
- **Cabeceras**: `From` con el nombre de `FICHAITOR_MAIL_FROM_NAME` (en producción, `Fichaitor <avisos@fichaitor.es>`), `Message-ID` con el dominio del remitente, `Auto-Submitted: auto-generated` y `Reply-To` con `FICHAITOR_MAIL_REPLY_TO` (por defecto `soporte@fichaitor.es`: `avisos@` no se lee).
- **Cuerpo**: `multipart/alternative` con texto plano y HTML. El HTML usa las plantillas de marca de `brand/correo/` (textos y reglas en `brand/correo/LEEME.md`), copiadas en `app/mail_templates/` para que viajen en la imagen; una prueba falla si la copia difiere del original. Cada valor pasa por `html.escape`, salvo el cuerpo, que son bloques ya escapados. El código nunca va en el asunto ni en el preencabezado. Sin datos de reseñas.
- **El enlace de restauración** es `<FICHAITOR_PUBLIC_BASE_URL>/app#restaurar=<token>`, absoluto y montado solo con la configuración: nunca con la cabecera `Host` ni con datos de la petición. Si `FICHAITOR_PUBLIC_BASE_URL` no es una URL absoluta `http(s)` sin consulta ni fragmento, la configuración no arranca. El token va en el fragmento para que no llegue a los registros de acceso.

> **Nota:** La fecha de los correos sale en hora peninsular si la imagen tiene los datos de zonas horarias (`tzdata`). Si no, sale en UTC y lo dice.

### El job `apply_offboarding`

Carga: `{"offboarding_id": "<uuid>"}`. La API lo encola al confirmar la baja, para ejecutarse en su fecha (`run_at`), con la clave `apply_offboarding:<id>` y 5 intentos. Una baja por correo (`scripts/baja_manual.py`) lo encola igual, para ya: lleva `reason = 'email_request'`, que solo el propietario puede escribir, y es la única que el job hace efectiva sin las 47 horas ([retencion.md](retencion.md#baja-por-correo)).

1. **Hace efectiva la baja**, en una transacción. Solo si sigue programada y su fecha ha llegado: una baja restaurada, o un job que corre antes de tiempo, no hace nada (`offboarding_skipped`). Desde aquí ya no se puede restaurar.
2. **Revoca en Google todas las conexiones guardadas** de ese usuario, no solo la última: `POST https://oauth2.googleapis.com/revoke`, con el token de renovación en el cuerpo (`token=...`, `application/x-www-form-urlencoded`). Una petición por token.
3. **Borra cada token cifrado** en cuanto su revocación queda resuelta.
4. **Anota el resultado** en `offboarding.revoke_state`.

La revocación sigue la [documentación de Google](https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke): admite un token de acceso o de renovación, responde 200 si lo revoca y 400 con un código de error si no.

| Qué responde Google | Qué hace el job | `revoke_state` |
|---|---|---|
| 200 | Borra el token | `revoked` |
| 400 con `invalid_token` (ya caducado o revocado) | Lo da por revocado y borra el token | `revoked` |
| Otro 4xx | Borra el token y no reintenta | `failed` |
| 5xx, 429 o sin respuesta | Reintenta (30 s, 60 s, 120 s, 240 s). El token sigue guardado entre intentos. En el quinto, lo borra | `failed` si se agotan |
| El token no se puede descifrar | Borra el token | `failed` |
| No había ninguna conexión con token (tampoco una que el dueño retiró desde Google) | Nada | `none` |
| El dueño ya ha vuelto a conectar | No revoca nada y borra los tokens antiguos | `superseded` |

- **Con `failed` queda constancia** en `offboarding` y en el log (`offboarding_effective`, nivel `warning`, con `not_revoked`). El token ya no está en la base: no se puede reintentar. El dueño puede quitar el acceso desde `myaccount.google.com/connections`.
- **Repetirlo no revoca dos veces.** Una baja con `revoke_state` ya anotado no se vuelve a tomar.
- **El token en claro vive solo en variables locales.** Ningún error ni log lo lleva: de Google solo queda el código de estado.

> **Importante:** La revocación no se ha probado contra Google real. El adaptador se prueba con transporte HTTP simulado (`tests/test_offboarding_units.py`).

### El job `retention`

Carga: `{}`. La pasada diaria que borra lo que ha superado su plazo. Qué borra y cuándo está en [retencion.md](retencion.md#tabla-de-conservación).

- **Lo encola el propio worker.** No hay planificador aparte. Cada worker intenta encolar el job del día con la clave `retention:<fecha>` y `run_at` a las `FICHAITOR_RETENTION_HOUR_UTC`. La clave es única: con varias réplicas hay un solo job, y lo ejecuta un solo worker.
- **Un worker que arranca pasada la hora** encola el job del día ya vencido y lo ejecuta en el momento, una vez.
- **Idempotente.** Repetir la pasada no borra nada más. Dos pasadas a la vez no fallan.
- **Antes de borrar**, hace efectivas las bajas vencidas cuyo job `apply_offboarding` se perdió y termina las revocaciones que quedaron a medias. Pasado un día, sin insistir a Google.
- **Cada pasada deja constancia**: una fila en `retention_run` y el evento `retention_done`, con cuántas filas borró de cada tabla. Sin identificadores.
- **Si no corre, avisa**: con 36 horas sin fila nueva en `retention_run`, el worker escribe `retention_overdue` (nivel `warning`).
- **Jobs terminados**: borra los `done` y `failed` de más de `FICHAITOR_RETENTION_JOB_DAYS` días (14), salvo el que ejecutó una acción que se conserva (`job_finished` en los recuentos).
- **Fichas desactivadas** (multinegocio): borra lo obtenido de Google de una ficha que lleva `FICHAITOR_RETENTION_INACTIVE_DAYS` días (30) desactivada, contados desde `location.deactivated_at`, con las mismas sentencias que las fichas retiradas. La fila de `location` se queda. Una ficha activada otra vez no tiene `deactivated_at` y no se toca. Hasta el paso 9 de [multinegocio.md](multinegocio.md#paso-9-retirada-de-la-compatibilidad) tampoco se toca la ficha elegida en el panel.
- **Registro de activaciones y derechos de plan**: borra los de las cuentas con más de `FICHAITOR_RETENTION_AUDIT_MONTHS` meses (12) de baja efectiva, como el registro de respuestas. Los recuentos los nombran `location_activation_log` y `plan_entitlement`.
- **Acceso retirado desde Google**: borra lo obtenido de Google de todas las fichas de un usuario cuyas conexiones están todas revocadas, la última hace `FICHAITOR_RETENTION_GOOGLE_DATA_DAYS` días (30) o más. Lo decide otra vez con las conexiones bloqueadas: una conexión nueva lo cancela. Consulta [Acceso retirado desde Google](#acceso-retirado-desde-google).
- **Carriles**: no borra ninguna propuesta terminada que nombre un carril (`conversation_lane.active_proposal_id`), igual que las que nombra un hilo. La baja efectiva (`apply_offboarding`) borra los carriles de la cuenta antes que sus propuestas, porque la clave ajena del carril a su propuesta no lleva `ON DELETE`.

Para añadir otro trabajo diario, añade un `DailyJob` a la lista `daily` de `Worker` (`app/worker/__main__.py`).

### Acceso retirado desde Google

Si el dueño quita el acceso a Fichaitor en su cuenta de Google, la siguiente renovación del token responde `invalid_grant`. El adaptador lo distingue (`GrantRevoked`, que es un `ReconnectRequired`) de un 401 sin ese código, de un 5xx, de un 429 y de un fallo de red. Solo `refresh_access_token` lo lanza.

Lo tratan igual `sync_profile`, `reply_review` y `set_special_hours` (`app/worker/google_grant.py`):

1. El job falla como hoy con un acceso perdido: `needs_reconnect` en el resumen, o la acción `failed` con `reconnect`.
2. `PostgresWorkerStore.revoke_connection` marca la conexión con `revoked_at` y pone su token a `NULL`, en la misma escritura, y borra las conexiones más antiguas a las que sustituía. No llama a Google: ya está revocado.
3. Si el usuario de Google se queda sin ninguna conexión válida, el evento `google_grant_revoked` lleva `countdown_started: true`. A los 30 días la tarea de retención borra lo obtenido de Google de todas sus fichas. Una conexión nueva antes de ese plazo lo cancela.

Si la base falla al marcarla, el job se reintenta: Google vuelve a responder `invalid_grant` y el token se borra entonces. Los plazos y las tablas están en [retencion.md](retencion.md#retirar-el-acceso-desde-google).

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
| `sentiment_analysis` | Ficha y conexión | Último análisis: estado (`ok`, `insuficiente` o `error`), fecha, nota, valoración, temas con recuentos, resumen, sugerencia, disparador y versión de la fórmula |
| `sentiment_history` | Ficha, conexión y mes | Nota del mes. El mes en curso se actualiza con cada análisis |
| `sentiment_usage` | Análisis | Contadores de llamadas, reseñas enviadas y tokens. Solo se añaden filas |

El detalle está en [sentimiento.md](sentimiento.md#qué-se-guarda).

### `action`

Acciones sobre Google confirmadas por el dueño. Dos tipos: `reply_review` y, desde el 2026-11-12, `set_special_hours` (sin `review_id`, con `special_date`; la restricción `action_target_check` lo exige). Como mucho una acción viva por reseña (`action_one_live_per_review`) y una por ficha y día (`action_one_live_per_special_date`).

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

Registro de auditoría. **Solo se añaden filas**: nadie las cambia. Solo se borran, en la tarea de retención, las de una cuenta que lleva más de 12 meses de baja.

| Columna | Uso |
|---|---|
| `at`, `written_by` | Cuándo y con qué usuario de la base se escribió. Los pone la base: quien inserta no puede fijarlos |
| `event` | `reply_confirmed` (la API o la conversación, cuando el dueño confirma), `reply_sent` o `reply_failed` (el worker, con el resultado). Para el horario: `special_hours_confirmed`, `special_hours_sent` y `special_hours_failed`, con `review_id` y `gbp_review_id` vacíos y en `text` el cambio canónico |
| `account_id` | Quién: la cuenta que confirmó |
| `action_id`, `action_type` | Qué acción |
| `location`, `review_id`, `gbp_review_id` | Sobre qué: la ficha y la reseña, con nuestro identificador y el de Google |
| `text` | El texto confirmado. En la fila del worker, el que se envió: se copia de la acción dentro de la base |
| `confirmed_at` | Cuándo se confirmó |
| `google_status`, `reason` | Resultado de Google: código HTTP y, si falla, el motivo. El motivo es un código de una lista cerrada (`reconnect`, `rejected`, `review_gone`, `unavailable`, `internal`, `already_applied`), nunca texto de Google |

Cada acción deja dos filas: la confirmación y el resultado. Una acción que sigue en curso tiene solo la primera.

La tabla tiene seguridad a nivel de fila: la API solo puede insertar `reply_confirmed` y el worker solo `reply_sent` y `reply_failed`. No hay ninguna política de lectura ni de cambio, así que esas operaciones quedan negadas para los dos roles aunque alguien concediera el privilegio por error. La única política de borrado es la del worker, para las filas de cuentas con más de 12 meses de baja. Para consultarla hay que conectarse como propietario.

### Roles

| Rol | Puede | No puede |
|---|---|---|
| `fichaitor_api` | Insertar en `job` (solo `kind`, `payload`, `run_at`, `idempotency_key`, `max_attempts`); leer `profile_summary` y `review`; leer y escribir `review_draft`; insertar y leer `action` (al insertar, solo cuenta, ficha, reseña, tipo, texto y fecha de confirmación); insertar en `audit_log` el evento `reply_confirmed`; leer `sentiment_analysis` y `sentiment_history` | Leer `review_sentiment` o `sentiment_usage`, o escribir en las tablas del sentimiento; leer, cambiar o borrar jobs; fijar estado o intentos; escribir resúmenes o reseñas; cambiar o borrar una acción, o crearla con estado o resultado; leer, cambiar o borrar la auditoría |
| `fichaitor_worker` | Leer, insertar y actualizar `job`, `profile_summary` y `review`; leer de `google_connection` las columnas que usa y marcar una conexión como revocada, borrando su token (lo único que el disparador `google_connection_guard` le deja escribir ahí); leer `action` y actualizar su estado, intentos, fechas y resultado; insertar en `audit_log` los eventos `reply_sent` y `reply_failed`; leer, insertar y actualizar `review_sentiment`, `sentiment_analysis` y `sentiment_history`; añadir filas a `sentiment_usage` | Leer o cambiar `sentiment_usage`; leer `waitlist_entry`, `funnel_event` o el email de las conexiones; escribir en `google_connection` otra cosa que la revocación, o devolverle un token; borrar fuera de la baja y de la tarea de retención (consulta [retencion.md](retencion.md#privilegios)); vaciar una tabla; **crear una acción o cambiar su texto, su reseña o su cuenta**; leer `review_draft`; leer o cambiar la auditoría; crear o restaurar una baja; leer el correo de una cuenta, la huella de una sesión o un contacto de la lista de espera |

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
| `FICHAITOR_SMTP_HOST`, `FICHAITOR_SMTP_PORT`, `FICHAITOR_SMTP_USER`, `FICHAITOR_SMTP_PASSWORD` | No | —, `587`, —, — | Servidor de correo saliente. Consulta [El job `send_mail`](#el-job-send_mail) |
| `FICHAITOR_MAIL_FROM`, `FICHAITOR_MAIL_FROM_NAME`, `FICHAITOR_MAIL_REPLY_TO` | No | —, `Fichaitor`, `soporte@fichaitor.es` | Remitente (en producción, `avisos@fichaitor.es`), su nombre y la dirección de respuesta |
| `FICHAITOR_PUBLIC_BASE_URL` | No | `http://localhost:8080` | Base del enlace de restauración de los correos |
| `FICHAITOR_FAKE_MAIL` | No | `false` | No envía correo: lo escribe en el log con el prefijo `[CORREO SIMULADO]`, con la dirección enmascarada y sin el código ni el token del enlace (`[código]`, `[oculto]`). Prohibido en producción: `deploy.sh` lo rechaza y el Compose lo fija a `false` |
| `FICHAITOR_RETENTION_*` | No | Consulta [retencion.md](retencion.md#variables) | Plazos de conservación y hora de la pasada diaria |

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

El worker escribe una línea JSON por evento: `worker_started`, `job_started`, `job_done`, `job_retry`, `job_failed`, `job_lost`, `lost_jobs_recorded`, `optional_read_rejected`, `reply_sent`, `action_skipped`, `action_lost`, `lost_hook_error`, `exhausted_hook_error`, `queue_error`, `stop_requested` y `worker_stopped`. Los de la baja son `mail_sent`, `mail_failed`, `mail_missing`, `fake_mail_enabled`, `account_suspended`, `offboarding_effective`, `offboarding_skipped`, `offboarding_not_applied`, `leave_purged_on_reconnect`, `daily_job_enqueued`, `schedule_error`, `retention_done`, `retention_overdue` (aviso: 36 horas sin pasada de retención) y `watch_error`. Los logs llevan el identificador, el tipo y el intento del job, y el identificador de la acción en los eventos del ejecutor. No llevan cargas, tokens, cadenas de conexión, textos de reseñas ni respuestas. Tampoco códigos de baja, enlaces de restauración ni direcciones de correo, ni siquiera con `FICHAITOR_FAKE_MAIL=true`.

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
uv run pytest -q tests/test_worker_*.py tests/test_reviews_*.py tests/test_sentiment_*.py tests/test_offboarding_*.py
```

Las pruebas de integración (`tests/test_worker_postgres.py`, `tests/test_reviews_postgres.py`, `tests/test_sentiment_postgres.py` y `tests/test_offboarding_postgres.py`) se saltan si faltan estas variables. Deben apuntar a la base de pruebas, que se crea con `scripts/preparar_bd_pruebas.sh`:

```bash
export FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor_test
export FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor_test
export FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor_test
```

> **Precaución:** Nunca ejecutes las pruebas contra la base de desarrollo ni contra una con datos reales. `tests/conftest.py` detiene la suite si alguna de las tres URL no apunta a una base cuyo nombre termine en `_test`. Consulta [retencion.md](retencion.md#pruebas).

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

## El proceso de la conversación

`uv run python -m app.worker.conversation` (servicio `conversation` en Docker Compose, `fichaitor-conversation` en producción) es un segundo proceso con la misma cola y otro usuario de base de datos, `fichaitor_conversation`. Es la opción A del paso 0 de `docs/canales.md`:

- Habla con el dueño por Telegram y, cuando confirma, crea la acción por la misma vía que el panel (`ReviewReplies.confirm` y `Store.create_reply_action`): acción `pendiente`, fila `reply_confirmed` y job `reply_review`, todo o nada.
- No tiene la clave privada de los tokens (se niega a arrancar si la recibe) ni lee `google_connection`. Puede crear una acción, pero solo este worker la ejecuta, tras la comprobación de coherencia de `claim_action` (M1).
- Solo toma sus jobs:

| Job | Lo encola | Qué hace |
|---|---|---|
| `channel_inbound` | La API, desde el webhook de Telegram (`telegram:<update_id>`, un intento) | Pasa la actualización a la conversación. Vacía la carga al terminar, acabe como acabe |
| `conversation_tick` | Su planificador, cada 5 minutos (`conversation_tick:<franja>`) | Recordatorios, caducidades y lo que esperaba al fin del horario de silencio |
| `propose_review` | Este worker, tras una sincronización (`propose_review:<reseña>`, 3 intentos) | Pide el borrador como el panel (borrador guardado, tope por ficha, `validate_reply`) y propone la reseña por Telegram en el carril de cada ficha **activa** que la tiene: de una cuenta sin baja programada, con la última sincronización en `ok` y con un chat que ve esa ficha vinculado antes de que se escribiera |
| `channel_action_result` | Este worker, al cerrar una acción (`channel_action_result:<acción>`, 3 intentos) | Cuenta el resultado por el chat y cierra la propuesta. Una respuesta a reseña confirmada en el panel no tiene propuesta: no hace nada. Una fecha clave respondida en el panel sí la tiene: se cierra y se avisa por el chat |
| `plan_key_dates` | Su planificador, una vez al día a las 9:05 (`plan_key_dates:<fecha>`, 3 intentos) | Crea una propuesta por ficha activa y día de las fechas clave próximas, tenga o no chat vinculado. Consulta `docs/calendario.md`, "Preguntar por el horario" |

- **Planificador.** Cada minuto encola, con clave por franja, el `conversation_tick` y un `sync_profile` de cada ficha **activa** de una cuenta sin baja con un chat vinculado que la ve: cada 30 minutos de 8:30 a 21:00 (hora peninsular) y cada 3 horas de noche. Varios procesos no duplican nada. Cada sincronización lleva su `run_at` dentro de la franja: el inicio de la franja más un desplazamiento fijo de la ficha (`spread_run_at`), para que muchas fichas no llamen a Google en el mismo minuto (`docs/multinegocio.md`, sección 7). Los 30 minutos están por contrastar con la cuota de la API de reseñas (`docs/fase0/matriz-api.md`).
- **Carriles.** Un hilo por cuenta y un carril por ficha (`conversation_lane`): como mucho una propuesta activa por ficha, y el límite semanal se cuenta por ficha. `conversation_tick` recorre los carriles con trabajo; si la ficha de un carril ya no está activa, cancela sus propuestas sin confirmar con `location_inactive`, sin enviar nada. Consulta `docs/canales.md`, sección 2b.
- **Despliegue del paso 6b.** `db/schema_channels.sql` termina con `SELECT fichaitor_fill_conversation_lanes();`, que pasa la propuesta abierta de cada hilo a su carril. Detén este proceso antes de aplicar los esquemas y arráncalo con el código nuevo después: el código antiguo escribiría en el hilo y no en el carril.
- **Sondeo largo.** Con `FICHAITOR_TELEGRAM_POLLING=true` recibe por `getUpdates` en lugar del webhook (solo desarrollo; borra el webhook del bot).
- **Sin token.** Sin `FICHAITOR_TELEGRAM_BOT_TOKEN` arranca y no envía nada (`telegram_disabled` en el registro): las propuestas esperan.
- **Registrar el webhook:** `python -m app.worker.conversation --set-webhook <url>` (`docs/despliegue.md`, "Webhook de Telegram").

Variables propias: `FICHAITOR_CONVERSATION_DATABASE_URL` (obligatoria), `FICHAITOR_PLANNER_INTERVAL_SECONDS` (60), `FICHAITOR_KEY_DATES_ENABLED` (`true`; con `false` no se planifican fechas clave) y `FICHAITOR_KEY_DATES_LEAD_DAYS` (5, de 1 a 60: días de antelación de un festivo). Comparte con el worker `FICHAITOR_WORKER_POLL_INTERVAL_SECONDS`, `FICHAITOR_WORKER_JOB_TIMEOUT_SECONDS` (aquí 30 por defecto) y `FICHAITOR_WORKER_LOCK_TIMEOUT_SECONDS` (aquí 120). Las de Telegram, en `docs/canales.md`.

## Añadir un tipo de job

1. Escribe el manejador en `app/worker/`: una función o un objeto asíncrono que recibe un `Job`.
2. Lanza `PermanentJobError` si reintentar no sirve y `JobError` con un mensaje sin datos sensibles si el fallo es temporal. De cualquier otra excepción solo se guarda el tipo.
3. Si necesitas actuar cuando el job agota sus intentos, añade el método `on_exhausted(job)`.
4. Si el job deja un estado que alguien lee fuera de la cola, añade el método `on_lost(kind)`. Se llama cada minuto para los jobs que murieron con su worker en el último intento. Debe poder repetirse sin efectos y devuelve cuántos ha anotado.
5. Regístralo en `build_registry` (`app/worker/__main__.py`).
