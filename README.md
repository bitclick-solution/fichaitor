# Fichaitor

Nombre comercial decidido el 2026-11-11. Dominio: `fichaitor.es` (registrar también `fichaitor.com`). La carpeta conserva el nombre provisional `agente-gbp`.

Agente conversacional que gestiona la ficha de Google Business Profile de un negocio desde WhatsApp. Es experto en la ficha y en SEO local, se adelanta a las fechas clave y nunca cambia nada sin la confirmación del dueño.

Estado: **en construcción**. La API, el worker, la base de datos y la web funcionan en local con Docker Compose. Todavía no hay agente conversacional ni WhatsApp. Fecha: 2026-11-12.

## Estado

| Pieza | Estado |
|---|---|
| API (`app/api`, FastAPI) | Funciona: diagnóstico público, lista de espera, conexión con Google, cuentas y sesiones, resumen y diagnóstico de la ficha conectada, y reseñas con borrador y publicación de la respuesta |
| Worker (`app/worker`) | Funciona: cola de jobs en PostgreSQL, job `sync_profile`, que lee la ficha en Google y guarda sus cifras, los hechos del diagnóstico y las reseñas, y job `reply_review`, que envía a Google la respuesta que el dueño confirma |
| Base de datos (PostgreSQL 16) | Funciona: `db/schema.sql` y `db/schema_worker.sql`, idempotentes, con un rol de mínimo privilegio para la API y otro para el worker |
| Web (`web/`) | Funciona: sitio público con el diagnóstico y panel en `/app` (React + Vite), servidos por nginx en `http://127.0.0.1:8080` |
| Calendario de fechas clave | Datos y reglas listos ([docs/calendario.md](docs/calendario.md)). El diagnóstico del panel usa los festivos nacionales; las propuestas de fechas clave aún no las usa ningún servicio |
| Integración continua | Formato, análisis estático y pruebas contra PostgreSQL en cada push ([docs/ci.md](docs/ci.md)) |

Lo que funciona hoy de principio a fin: un negocio consulta el diagnóstico público de su ficha, se apunta a la lista de espera, conecta su cuenta de Google, elige ficha y ve en el panel las reseñas sin responder, los horarios, los cambios hechos por Google y qué mejorar, todo con los datos de su ficha conectada. Sin credenciales de Google, todo el recorrido funciona con datos de ejemplo.

Lo que falta del MVP ([docs/05-roadmap.md](docs/05-roadmap.md), fase 3):

- La vinculación de WhatsApp y el agente conversacional (motor de propuestas, confirmación y ejecutor).
- De las reseñas: el aviso de reseña nueva (Pub/Sub), la respuesta por WhatsApp y el modo automático. Ver, redactar y publicar desde el panel ya funciona en el backend (sexto bloque); falta la pantalla en la web y la prueba contra Google real.
- Las fechas clave y "hoy cierro" dentro del producto.
- La facturación.
- En el panel: borrar la cuenta. En la base: la limpieza periódica de sesiones caducadas.
- El diagnóstico por pantallazo, que está en construcción.

Desde el 2026-11-12 hay **una escritura en Google**: la respuesta a una reseña, que solo envía el worker y solo cuando el dueño la ha confirmado desde su sesión. Todo lo demás sigue siendo lectura.

## Qué hay construido

Primer servicio: el **diagnóstico público** de una ficha (`POST /api/v1/public/diagnosis`). Puntúa de 0 a 100 con reglas fijas sobre datos públicos de Google Places y devuelve hasta cinco mejoras. Las reglas están en `app/domain/diagnosis.py` y sus pruebas en `tests/`.

Segundo bloque: la **lista de espera** (`POST /api/v1/public/waitlist`) y el **embudo** del experimento, sobre PostgreSQL. El esquema está en `db/schema.sql`. El usuario de la API solo puede insertar: no puede leer, cambiar ni borrar contactos.

Para ver la lista de espera y el embudo, con el usuario propietario:

```bash
docker compose exec -T db psql -U fichaitor -d fichaitor -c "select created_at, channel, contact, business_name, score, source from waitlist_entry order by created_at desc"
```

```bash
docker compose exec -T db psql -U fichaitor -d fichaitor -c "select kind, source, count(*) from funnel_event group by 1, 2 order by 1, 2"
```

Tercer bloque: la **conexión con Google** (`GET /api/v1/google/oauth/start` y `/callback`). Guarda el token de renovación cifrado con clave pública: la API puede cifrarlo pero no leerlo; la clave privada es solo del worker. Las rutas solo existen si el `.env` tiene los datos del cliente OAuth. Para cambiar de proyecto de Google Cloud se cambian tres variables.

Puesta en marcha de la conexión:

1. Genera las claves de cifrado y guarda la privada fuera del repositorio:

   ```bash
   uv run python scripts/gen_token_keys.py
   ```

2. Rellena en `.env` el bloque "Conexión con Google" de `.env.example`.
3. Da de alta la URI de redirección en el cliente OAuth de Google, exactamente igual que en el `.env`.
4. Reinicia la API y abre `http://localhost:8000/api/v1/google/oauth/start`.

Cuarto bloque: **cuentas y sesiones**. La cuenta es la identidad de Google: no hay contraseñas. Al conectar, la API crea o recupera la cuenta, guarda sus fichas, abre una sesión de 30 días y pone la cookie `fichaitor_session` (HttpOnly, `SameSite=Lax`). En la base solo se guarda el SHA-256 del token de la cookie.

| Ruta | Qué hace |
|---|---|
| `GET /api/v1/me` | Correo, fichas de la cuenta y ficha elegida |
| `POST /api/v1/me/location` | Elige una ficha. 404 si no es de esa cuenta |
| `GET /api/v1/me/diagnosis` | Diagnóstico de la ficha elegida, calculado con lo que el worker lee de la ficha conectada. No usa Google Places |
| `GET /api/v1/me/summary` | Resumen de la ficha elegida, leído de lo que guarda el worker |
| `GET /api/v1/me/reviews` | Reseñas de la ficha elegida, las que están sin responder primero |
| `POST /api/v1/me/reviews/{id}/draft` | Respuesta redactada por el modelo para esa reseña. No publica nada |
| `POST /api/v1/me/reviews/{id}/reply` | Publica la respuesta que el dueño confirma: crea la acción y encola su envío |
| `GET /api/v1/me/actions/{id}` | Estado del envío, con un mensaje listo para mostrar |
| `POST /api/v1/auth/logout` | Cierra la sesión y borra la cookie |

- Sin sesión, o con la sesión caducada o cerrada, responden 401.
- Las rutas que cambian algo exigen la cabecera `X-Requested-With: fichaitor`. Sin ella, 403.
- Cerrar sesión marca la sesión como revocada; no borra la fila.
- El usuario de la API lee y escribe en `account`, `location` y `session` solo las columnas que usan estas rutas, y no puede borrar. Sigue sin poder leer contactos ni tokens de Google.
- El contrato completo con la web está en [docs/contrato-web-api.md](docs/contrato-web-api.md).

Tras actualizar el código, vuelve a aplicar el esquema (se puede aplicar las veces que haga falta):

```bash
docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema.sql
```

Pruebas de integración contra PostgreSQL (sin estas variables se omiten):

```bash
FICHAITOR_TEST_DATABASE_URL=postgresql://fichaitor_api_dev:dev@127.0.0.1:5433/fichaitor FICHAITOR_TEST_OWNER_URL=postgresql://fichaitor:dev@127.0.0.1:5433/fichaitor FICHAITOR_TEST_WORKER_URL=postgresql://fichaitor_worker_dev:dev@127.0.0.1:5433/fichaitor uv run pytest -q
```

Quinto bloque: el **worker**, el **resumen de la ficha** y el **diagnóstico de la ficha conectada**. El worker toma jobs de una cola en PostgreSQL; el job `sync_profile` lee la ficha en Google y guarda en `profile_summary` sus cifras y los hechos con los que se calcula el diagnóstico. Está documentado en [docs/worker.md](docs/worker.md).

La API conecta el panel con esos datos:

- **Encola la sincronización** cuando el dueño elige ficha y cuando pide el resumen y este no existe, tiene más de `FICHAITOR_SUMMARY_STALE_HOURS` horas (6 por defecto) o su último intento falló.
- **Al volver a conectar con Google**, si la cuenta ya tenía ficha elegida, encola su sincronización al momento, sin esperar a la ventana. Como máximo, una por ficha y minuto.
- **Una sincronización por ficha y ventana** de `FICHAITOR_SYNC_WINDOW_SECONDS` (15 minutos por defecto). La ventana va en la clave de idempotencia del job.
- **`GET /api/v1/me/summary`** devuelve las cifras, o `status: "pendiente"` mientras el worker no ha terminado la primera lectura. La web sondea cada 3 segundos.
- **`GET /api/v1/me/diagnosis`** calcula el diagnóstico ("Qué mejorar") con los hechos guardados y el calendario de festivos nacionales (`app/domain/profile_diagnosis.py`). No llama a Google Places, no cuesta dinero y no tiene límite. Mientras no hay datos responde 404 con `status: "pendiente"`. Añade a las reglas del diagnóstico público las que solo se saben con la ficha conectada: reseñas sin responder, festivos próximos sin horario, descripción y cambios hechos por Google.
- **Todo fallo de la sincronización deja un estado** en el resumen (`needs_reconnect` o `error`), también cuando no hay conexión guardada o la ficha ya no está en ella. `pendiente` solo dura lo que tarda el worker en tomar el job.
- Si encolar falla, la ruta del usuario responde igual y el fallo queda en el log, sin identificadores.
- El resumen se busca por la identidad de Google de la cuenta de la sesión y su ficha elegida. Una cuenta no lee el resumen de otra, aunque administren la misma ficha.
- La API sigue sin poder leer la cola ni escribir resúmenes: solo inserta jobs y lee `profile_summary`.

Para ver el recorrido completo sin Google (conectar, elegir ficha y ver el resumen):

1. Pon `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_PLACES=true` en `.env`, junto con `FICHAITOR_OAUTH_STATE_SECRET` y `FICHAITOR_TOKEN_PUBLIC_KEY`.
2. Comprueba que el worker recibe la clave privada pareja de esa clave pública. En local llega desde `../fichaitor-secretos/worker.env`.
3. Levanta todo:

   ```bash
   docker compose up -d --build
   ```

4. Abre `http://127.0.0.1:8080/app`, conecta y elige la ficha de ejemplo. El worker usa su ficha de demostración y el resumen y el diagnóstico aparecen en unos segundos.

> **Nota:** Sin base de datos (`FICHAITOR_DATABASE_URL` vacío y `FICHAITOR_FAKE_PLACES=true`) no hay cola ni worker. En ese modo, con `FICHAITOR_FAKE_GOOGLE=true`, la API escribe ella misma el resumen, los hechos y las reseñas de demostración en memoria, y da por enviada al momento la respuesta a una reseña.

Sexto bloque: las **reseñas y sus respuestas**, la primera acción real sobre Google. El contrato con la web está en [docs/contrato-web-api.md](docs/contrato-web-api.md#reseñas-y-respuestas-2026-11-12) y el ejecutor, en [docs/worker.md](docs/worker.md#el-job-reply_review).

- **El worker guarda las reseñas** en cada sincronización: identificador de Google, estrellas, texto original, fecha y respuesta del propietario. No guarda el nombre ni la foto de quien la escribió. Las que Google deja de devolver se marcan como retiradas.
- **El borrador lo redacta el modelo** de la demostración por captura (Gemini), que recibe solo las estrellas y el texto. El texto de la reseña es un dato: si contiene instrucciones, no se obedecen. El borrador pasa la misma validación en código que la demostración (`app/domain/review_reply.py`); si no la pasa, la API responde 422 y el dueño escribe la suya. Se guarda el último borrador de cada reseña y hay un límite por cuenta (`FICHAITOR_REVIEW_DRAFT_RATE_LIMIT`, 30 cada 24 horas).
- **Publicar es confirmar.** `POST /api/v1/me/reviews/{id}/reply` crea una acción en estado pendiente con el texto exacto, escribe la auditoría y encola el job `reply_review`, todo en una transacción. La API no llama a Google.
- **El worker ejecuta la acción** con `PUT …/reviews/{reviewId}/reply`. El texto que llega a Google es el que el dueño confirmó: nadie tiene permiso para cambiarlo en la base. Reintentar o duplicar el job no publica dos veces.
- **El límite de Google es de 4.096 bytes** por respuesta ([referencia](https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews#ReviewReply)). La API lo comprueba antes de crear la acción.
- **Responder a una reseña ya respondida edita la respuesta**: Google usa la misma operación.
- **Auditoría solo de inserción** (`audit_log`): quién confirmó, qué texto, sobre qué reseña, cuándo, y qué respondió Google. Ni la API ni el worker pueden leerla, cambiarla ni borrarla.
- **El modelo nunca dispara una publicación.** Pedir un borrador no crea ninguna acción, diga lo que diga la reseña o el modelo.

Para ver el recorrido de reseñas sin Google, añade `FICHAITOR_FAKE_SCREENSHOT=true` a los pasos anteriores: el borrador devuelve respuestas de ejemplo sin clave ni coste. Sin esa variable y sin `FICHAITOR_GEMINI_API_KEY`, el borrador responde 503 y el resto funciona.

> **Precaución:** La escritura no se ha probado contra Google real. Una respuesta a una reseña es pública y avisa a quien la escribió. Antes de usarla con una ficha real, haz la prueba pendiente de `docs/fase0/matriz-api.md`: reenviar el mismo texto sobre una reseña ya respondida.

Séptimo bloque: el **sentimiento del establecimiento** (`GET /api/v1/me/sentiment`), un indicador propio del panel: cómo ve la gente el negocio, con una nota de 0 a 100 y su historia mensual.

- **El modelo clasifica cada reseña una sola vez** (positiva, neutra o negativa, y hasta 3 temas de una lista cerrada). Las reseñas sin texto se clasifican por sus estrellas, sin modelo. Al modelo va solo el texto y las estrellas, nunca un autor.
- **La nota, los recuentos por tema y la decisión de recalcular los hace el código.** Con las mismas reseñas, la nota es la misma.
- **El resumen y la sugerencia los redacta el modelo y los valida el código.** Si citan una cifra que no está en los datos, se descartan y se usan textos construidos por reglas fijas.
- **El primer análisis sale nada más conectar**: se encadena a la primera sincronización. Después, como máximo uno al día: con una reseña de 1 o 2 estrellas, con 3 reseñas nuevas o a los 30 días.
- **Con una reseña ya hay nota.** Con menos de 5 reseñas con texto, el análisis se marca como provisional.
- Lo calcula el worker (job `analyze_sentiment`); la API solo lo lee. El detalle, el coste estimado y lo que se guarda están en [docs/sentimiento.md](docs/sentimiento.md).

> **Precaución:** El análisis no se ha probado contra Gemini real. El coste de `docs/sentimiento.md` es una estimación: corrígela con las primeras cifras de la tabla `sentiment_usage`.

Falta en estos bloques: borrar la cuenta desde el panel, la limpieza periódica de sesiones caducadas y las pruebas automáticas contra Google real.

### Ejecutarlo

Con el negocio de ejemplo, sin llamar a Google:

```bash
docker compose up -d --build
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/public/diagnosis -H "content-type: application/json" -d '{"query":"demo bar granada"}'
```

Con Google real: copia `.env.example` a `.env`, pon `FICHAITOR_PLACES_API_KEY` y `FICHAITOR_FAKE_PLACES=false`.

Pruebas y formato:

```bash
uv run pytest -q
```

```bash
uv run ruff check .
```

## Documentos

| Documento | Contenido |
|---|---|
| [docs/01-flujos.md](docs/01-flujos.md) | Recorridos del usuario, flujo conversacional, estados y excepciones |
| [docs/02-hld.md](docs/02-hld.md) | Arquitectura general, componentes y comunicación entre servicios |
| [docs/03-lld.md](docs/03-lld.md) | Esquema de datos, agentes, prompts y contratos de API |
| [docs/04-ux-conversacional.md](docs/04-ux-conversacional.md) | Tono, patrones de mensaje, botones y pantallas del panel |
| [docs/05-roadmap.md](docs/05-roadmap.md) | Fases, qué hay que investigar, qué hay que construir y criterios de salida |
| [docs/06-estandares.md](docs/06-estandares.md) | Stack, separación de responsabilidades, Docker y estrategia de pruebas |
| [docs/sentimiento.md](docs/sentimiento.md) | Sentimiento del establecimiento: fórmula de la nota, temas, disparadores, coste y qué se guarda |

## Decisiones tomadas

1. **Venta directa**, negocio a negocio, con captación por TikTok y TikTok Ads. El alta es autoservicio.
2. **WhatsApp es la interfaz principal.** El panel web es secundario. Telegram y email son canales alternativos.
3. **Repo nuevo, desde cero.** No se toca nada de lo existente (GBP Autopilot en n8n sigue como banco de pruebas; Bitclick Social sigue su plan).
4. **Se reutilizan decisiones, no código.** El stack y los patrones vienen de la evaluación de arquitectura de Bitclick Social (FastAPI, Postgres con RLS, cola en Postgres, React + Vite).
5. **El modelo de lenguaje nunca escribe en Google.** Propone una acción estructurada; un ejecutor determinista la aplica solo tras la confirmación del dueño.
6. **MVP:** diagnóstico público, alta, reseñas con aprobación, fechas clave (festivos y fechas comerciales) y "hoy cierro". Posts, fotos y competencia van en la versión Pro.

## Decisiones abiertas

| Decisión | Cuándo se cierra |
|---|---|
| Precios definitivos. Hipótesis de lanzamiento en [docs/fase0/precios.md](docs/fase0/precios.md): gratis, Pro a 35 €/año y Pro WhatsApp a 59 €/año, sin monedero | Tras la beta |
| Proveedor del modelo de lenguaje (se decide con el conjunto de evaluación, no por preferencia) | Fase 1 |
| Acceso a WhatsApp: Cloud API directa o a través de un proveedor | Fase 0 |
| Relación futura con el módulo 009 de Bitclick Social (lo sustituye, lo alimenta o conviven) | Tras la beta |
| Precios definitivos | Tras el experimento de TikTok |
