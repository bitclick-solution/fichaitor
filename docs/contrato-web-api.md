# Contrato entre la web y la API (v1)

Acuerdo entre la sesión de backend y la sesión de interfaz. Si una de las dos necesita cambiarlo, se cambia aquí primero.

## Reparto

| Quién | Carpetas | No toca |
|---|---|---|
| Interfaz (sesión "Preparar marca y textos legales de Fichaitor") | `web/`, `brand/`, `docs/legal/` | `app/`, `db/`, `tests/`, `docker-compose.yml` |
| Backend (sesión principal) | `app/`, `db/`, `tests/`, `scripts/`, `docker-compose.yml`, `docs/` salvo `docs/legal/` | `web/`, `brand/` |

Las dos sesiones trabajan en la **misma carpeta y la misma rama** (`main`). Cada una hace `git add` solo de sus carpetas. Nadie usa `git add -A`.

## Cómo encajan

- La web es una aplicación estática (React 18, Vite, TypeScript estricto) en `web/`.
- En producción, un contenedor `web` con nginx sirve los estáticos y redirige `/api/` a la API. Mismo origen: sin CORS.
- La interfaz entrega `web/Dockerfile` (compilación + nginx sin privilegios, escuchando en el 8080) y `web/nginx.conf` con `location /api/ { proxy_pass http://api:8000; }`. El backend añade el servicio `web` a `docker-compose.yml`.
- En desarrollo, el servidor de Vite redirige `/api` a `http://localhost:8000`.
- Sin Google real: con `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_PLACES=true` todo el recorrido funciona con datos de ejemplo.

## Convenciones

- Prefijo `/api/v1`. JSON en UTF-8.
- Errores: `{"detail": "texto en español listo para mostrar"}` con el código HTTP correspondiente. En los 422 de validación automática, `detail` puede ser una lista: muestra un mensaje genérico.
- Sesión: cookie `fichaitor_session`, HttpOnly. La web no la lee ni la escribe; solo envía las peticiones con `credentials: "same-origin"`.
- Toda petición que cambie algo (`POST`, `PATCH`, `DELETE`) con sesión lleva la cabecera `X-Requested-With: fichaitor`. Sin ella, la API responde 403.
- 401 significa "sin sesión": la web lleva a la pantalla de conectar.

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
  "consent_version": "2026-11-v1",
  "place_id": "ChIJ...",
  "business_name": "Bar La Parra",
  "score": 68,
  "source": "tiktok_v1"
}
```

`channel`: `whatsapp` o `email`. `consent` debe ser `true`. `consent_version` es el identificador del texto legal mostrado; lo define la sesión de interfaz. Respuesta 201 `{"status": "ok"}`. Errores: 422 con mensaje, 429, 503.

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

Con `google=ok` la API ya ha puesto la cookie de sesión.

## Rutas con sesión (en construcción en el backend)

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

401 si no hay sesión.

### `POST /api/v1/me/location`

```json
{ "location_id": "loc_3f2a" }
```

Devuelve el mismo cuerpo que `GET /me`. 404 si la ficha no es de esa cuenta.

### `GET /api/v1/me/diagnosis`

Diagnóstico de la ficha elegida, con la misma forma que el diagnóstico público. 409 si aún no se ha elegido ficha. Si la ficha no tiene `place_id`, 404 con mensaje.

### `POST /api/v1/auth/logout`

204 y borra la cookie.

## Pantallas mínimas para tener algo visible

1. **Inicio** (`/`): diagnóstico público, resultado, lista de espera y botón "Conectar con Google".
2. **App** (`/app`): lee `?google=`; sin sesión, invita a conectar; con sesión y sin ficha elegida, lista de fichas; con ficha elegida, panel con el diagnóstico de esa ficha y botón de salir.
3. **Privacidad** (`/privacidad/`) y **condiciones** (`/condiciones-de-uso/`).

## Siguiente versión del contrato

Cuando exista el worker, el panel recibirá datos que hoy están bloqueados: reseñas sin responder, horarios de festivos y cambios de Google. Se añadirán aquí antes de implementarse.
