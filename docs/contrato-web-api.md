# Contrato entre la web y la API (v1)

Acuerdo entre la sesión de backend y la sesión de interfaz. Si una de las dos necesita cambiarlo, se cambia aquí primero.

## Reparto

| Quién | Carpetas | No toca |
|---|---|---|
| Interfaz (sesión "Preparar marca y textos legales de Fichaitor") | `web/`, `brand/`, `docs/legal/` | `app/`, `db/`, `tests/`, `docker-compose.yml` |
| Backend (sesión principal) | `app/`, `db/`, `tests/`, `scripts/`, `docker-compose.yml`, `docs/` salvo `docs/legal/` | `web/`, `brand/` |

Las dos sesiones trabajan en la **misma carpeta y la misma rama** (`main`). Cada una hace `git add` solo de sus carpetas. Nadie usa `git add -A`.

## Cómo encajan

- La web vive en `web/` y tiene dos partes, servidas por el mismo nginx:
  - **Sitio público** (inicio, `/privacidad/`, `/condiciones-de-uso/`): HTML estático generado por `web/build.py` a partir de `web/src` y `docs/legal`. No se rehace en React.
  - **Aplicación** (`/app`): React 18, Vite y TypeScript estricto, compilada con base `/app/`. Es la única parte con sesión.
- En producción, un contenedor `web` con nginx sirve los estáticos y redirige `/api/` a la API. Mismo origen: sin CORS.
- La interfaz entrega `web/Dockerfile` (compila las dos partes; nginx sin privilegios, escuchando en el 8080) y `web/nginx.conf` con `location /api/ { proxy_pass http://api:8000; }`. El respaldo a `index.html` se aplica **solo** a `/app`; las páginas legales y el inicio se sirven como archivos. El backend añade el servicio `web` a `docker-compose.yml`.
- En desarrollo, el servidor de Vite redirige `/api` a `http://localhost:8000`.
- Sin Google real: con `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_PLACES=true` todo el recorrido funciona con datos de ejemplo.

## Convenciones

- Prefijo `/api/v1`. JSON en UTF-8.
- Errores: `{"detail": "texto en español listo para mostrar"}` con el código HTTP correspondiente. En los 422 de validación automática, `detail` puede ser una lista: muestra un mensaje genérico.
- Sesión: cookie `fichaitor_session`, HttpOnly, `SameSite=Lax`, `Path=/`, 30 días. La web no la lee ni la escribe; solo envía las peticiones con `credentials: "same-origin"`.
- Toda petición que cambie algo (`POST`, `PATCH`, `DELETE`) con sesión lleva la cabecera `X-Requested-With: fichaitor`. Sin ella, la API responde 403, haya sesión o no: la cabecera se comprueba antes que la sesión.
- 401 significa "sin sesión" (no hay cookie, o la sesión ha caducado o se ha cerrado): la web lleva a la pantalla de conectar.

## Rutas públicas (ya existen)

### `POST /api/v1/public/diagnosis`

```json
{ "query": "Bar La Parra Granada", "source": "tiktok_v1" }
```

`source` es opcional (letras, números, `_`, `-`, `.`; 60 como máximo). Se toma del parámetro `?s=` de la URL de llegada.

Respuesta 200:

```json
{
  "place_id": "ChIJ...",
  "name": "Bar La Parra",
  "address": "Calle ..., Granada",
  "category": "Bar",
  "score": 68,
  "label": "Mejorable",
  "findings": [
    { "code": "no_website", "title": "No tienes web enlazada", "detail": "Pierdes los clics...", "impact": "medio" }
  ],
  "locked_checks": [
    { "code": "unanswered_reviews", "title": "Cuántas reseñas tienes sin responder" }
  ],
  "maps_url": "https://maps.google.com/?cid=..."
}
```

- `label`: `Bien` (85 o más), `Mejorable` (60 a 84), `Necesita atención` (menos de 60).
- `impact`: `alto`, `medio`, `bajo`. Hasta 5 `findings`, ya ordenados.
- `locked_checks`: lo que solo se puede comprobar conectando la ficha. Es el gancho hacia "Conectar con Google".
- Errores: 404 (no encontrado), 422, 429 (límite), 503 (Google no responde).

### `POST /api/v1/public/waitlist`

```json
{
  "channel": "whatsapp",
  "contact": "600 11 22 33",
  "consent": true,
  "consent_version": "waitlist-2026-11-11",
  "place_id": "ChIJ...",
  "business_name": "Bar La Parra",
  "score": 68,
  "source": "tiktok_v1"
}
```

`channel`: `whatsapp` o `email`. `consent` debe ser `true`. `consent_version` es el identificador del texto legal mostrado. El vigente es `waitlist-2026-11-11`; el texto y su historial están en `docs/legal/consentimiento-lista-espera.md`. Respuesta 201 `{"status": "ok"}`. Errores: 422 con mensaje, 429, 503.

## Conexión con Google (ya existe)

`GET /api/v1/google/oauth/start` es una **navegación del navegador**, no una llamada `fetch`: un enlace o `window.location`.

Al terminar, Google devuelve al usuario a `/app` con uno de estos parámetros:

| URL de vuelta | Qué mostrar |
|---|---|
| `/app?google=ok&fichas=2` | Conectado. Sigue con la elección de ficha |
| `/app?google=ok&fichas=pendiente` | Conectado, pero aún no se han podido leer las fichas. "Vuelve a intentarlo en unos minutos" |
| `/app?google=ok&fichas=0` | Conectado, pero esa cuenta no administra ninguna ficha |
| `/app?google=cancelado` | El usuario canceló |
| `/app?google=sin_permiso` | No marcó el permiso de la ficha. Explicar y ofrecer reintentar |
| `/app?google=error` | Fallo. Ofrecer reintentar |

Con `google=ok` la API ya ha puesto la cookie de sesión. La cuenta es la identidad de Google: no hay registro ni contraseña, y volver a conectar con la misma cuenta de Google entra en la misma cuenta de Fichaitor.

Al volver a conectar, las fichas se actualizan con lo que Google lista en ese momento. Una ficha conserva su `id` entre conexiones. Si Google deja de listar una ficha, desaparece de `locations`; si era la elegida, `selected_location_id` vuelve a `null`. Con `fichas=pendiente` se conservan las fichas que la cuenta ya tuviera.

## Rutas con sesión

### `GET /api/v1/me`

```json
{
  "email": "dueno@example.com",
  "locations": [
    { "id": "loc_3f2a", "title": "Bar La Parra", "place_id": "ChIJ...", "verified": true }
  ],
  "selected_location_id": null
}
```

- `locations` viene ordenado por `title`. Puede ser una lista vacía.
- `id`: `loc_` seguido de letras y números. Es propio de cada cuenta: no sirve en otra.
- `place_id` puede ser `null`: Google no siempre lo da (fichas sin verificar o sin ubicación en el mapa).
- 401 si no hay sesión.

### `POST /api/v1/me/location`

```json
{ "location_id": "loc_3f2a" }
```

Devuelve el mismo cuerpo que `GET /me`, ya con `selected_location_id`. Se puede llamar otra vez para cambiar de ficha.

Errores: 401, 403 (falta la cabecera), 404 (la ficha no es de esa cuenta o no existe; la respuesta es la misma en los dos casos), 422.

### `GET /api/v1/me/diagnosis`

Diagnóstico de la ficha elegida, con la misma forma que el diagnóstico público.

La API busca la ficha por su `place_id`. Si la ficha no tiene `place_id`, o Google ya no lo reconoce, la busca por su título.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha. Lleva a la lista de fichas |
| 404 | No se ha encontrado la ficha en Google Maps ni por `place_id` ni por título. `detail` trae el mensaje |
| 429 | Demasiados diagnósticos nuevos de esa cuenta. Los repetidos salen de la caché y no cuentan |
| 503 | Google no responde |

### `POST /api/v1/auth/logout`

204 sin cuerpo. Cierra la sesión en el servidor y borra la cookie. Lleva la cabecera `X-Requested-With: fichaitor`; sin ella, 403. Responde 204 también si no había sesión.

## Pantallas mínimas para tener algo visible

1. **Inicio** (`/`): diagnóstico público, resultado, lista de espera y botón "Conectar con Google".
2. **App** (`/app`): lee `?google=`; sin sesión, invita a conectar; con sesión y sin ficha elegida, lista de fichas; con ficha elegida, panel con el diagnóstico de esa ficha y botón de salir.
3. **Privacidad** (`/privacidad/`) y **condiciones** (`/condiciones-de-uso/`).

## Siguiente versión del contrato

Cuando exista el worker, el panel recibirá datos que hoy están bloqueados: reseñas sin responder, horarios de festivos y cambios de Google. Se añadirán aquí antes de implementarse.
