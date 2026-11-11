# Fichaitor

Nombre comercial decidido el 2026-11-11. Dominio: `fichaitor.es` (registrar también `fichaitor.com`). La carpeta conserva el nombre provisional `agente-gbp`.

Agente conversacional que gestiona la ficha de Google Business Profile de un negocio desde WhatsApp. Es experto en la ficha y en SEO local, se adelanta a las fechas clave y nunca cambia nada sin la confirmación del dueño.

Estado: **fase de diseño**. No hay código. Fecha: 2026-11-11.

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
| `GET /api/v1/me/diagnosis` | Diagnóstico de la ficha elegida, buscada por su `place_id` |
| `GET /api/v1/me/summary` | Resumen de la ficha elegida, leído de lo que guarda el worker |
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

Quinto bloque: el **worker** y el **resumen de la ficha**. El worker toma jobs de una cola en PostgreSQL; el job `sync_profile` lee la ficha en Google y guarda sus cifras en `profile_summary`. Está documentado en [docs/worker.md](docs/worker.md).

La API conecta el panel con esos datos:

- **Encola la sincronización** cuando el dueño elige ficha y cuando pide el resumen y este no existe, tiene más de `FICHAITOR_SUMMARY_STALE_HOURS` horas (6 por defecto) o su último intento falló.
- **Una sincronización por ficha y ventana** de `FICHAITOR_SYNC_WINDOW_SECONDS` (15 minutos por defecto). La ventana va en la clave de idempotencia del job.
- **`GET /api/v1/me/summary`** devuelve las cifras, o `status: "pendiente"` mientras el worker no ha terminado la primera lectura. La web sondea cada 3 segundos.
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

4. Abre `http://127.0.0.1:8080/app`, conecta y elige la ficha de ejemplo. El worker usa su ficha de demostración y el resumen aparece en unos segundos.

> **Nota:** Sin base de datos (`FICHAITOR_DATABASE_URL` vacío y `FICHAITOR_FAKE_PLACES=true`) no hay cola ni worker. En ese modo, con `FICHAITOR_FAKE_GOOGLE=true`, la API escribe ella misma el resumen de demostración en memoria.

Falta: borrar la cuenta desde el panel, la limpieza periódica de sesiones caducadas y las pruebas contra Google real.

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
