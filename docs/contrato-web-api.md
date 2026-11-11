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
  "maps_url": "https://maps.google.com/?cid=...",
  "attribution": "Google Maps"
}
```

- `label`: `Bien` (85 o más), `Mejorable` (60 a 84), `Necesita atención` (menos de 60).
- `impact`: `alto`, `medio`, `bajo`. Hasta 5 `findings`, ya ordenados.
- `locked_checks`: lo que solo se puede comprobar conectando la ficha. Es el gancho hacia "Conectar con Google".
- `attribution`: origen de los datos. Consulta [Atribución de Google Maps](#atribución-de-google-maps-cambio-del-2026-11-11).
- Errores: 404 (no encontrado), 422, 429 (límite), 503 (Google no responde).

#### Atribución de Google Maps (cambio del 2026-11-11)

Nombre, dirección, categoría, enlace y las cifras de las mejoras (fotos, nota, reseñas) vienen de Google Maps. Las políticas de Google exigen decirlo.

- La web **muestra el texto de `attribution`** ("Google Maps") junto a esos datos, en la misma pantalla y sin que haga falta desplazarse o pulsar para verlo. Por ejemplo: "Datos de Google Maps", debajo del nombre y la dirección.
- No lo traduzcas, no lo abrevies y no lo partas en dos líneas. Usa el texto del campo; no lo escribas a mano en la web.
- Vale para el diagnóstico público y para `GET /api/v1/me/diagnosis`. También debe verse en las capturas y los vídeos que enseñen el diagnóstico.

#### Qué no puede guardar la web (cambio del 2026-11-11)

Los términos de Google solo permiten guardar el `place_id`. La API ya no guarda la respuesta del diagnóstico: pide los datos a Google cada vez.

- La web tampoco guarda la respuesta: ni en `localStorage`, ni en `sessionStorage`, ni en IndexedDB, ni en una caché de un service worker. Tenerla en memoria mientras la pantalla está abierta es correcto.
- La web no envía a la API datos del diagnóstico para que los guarde, salvo `place_id` y `score` en la lista de espera.

#### El límite cuenta todos los diagnósticos (cambio del 2026-11-11)

Antes, repetir una búsqueda salía de una caché y no contaba. Ahora **cada diagnóstico cuenta**, también los repetidos: 10 por hora por IP en el público y 10 por hora por cuenta en `GET /api/v1/me/diagnosis`. Cada uno es una petición de pago a Google.

- No vuelvas a pedir el diagnóstico en cada pintado, al cambiar de pestaña o al recuperar el foco. Pídelo una vez al entrar y cuando el usuario lo pida.
- Con un 429, muestra `detail` y conserva en pantalla el último diagnóstico si lo hay.

### `POST /api/v1/public/waitlist`

```json
{
  "channel": "whatsapp",
  "contact": "600 11 22 33",
  "consent": true,
  "consent_version": "waitlist-2026-11-11",
  "place_id": "ChIJ...",
  "business_query": "bar la parra granada",
  "score": 68,
  "source": "tiktok_v1"
}
```

> **Importante (cambio del 2026-11-11): `business_name` se sustituye por `business_query`.**
>
> - `business_query` es el **texto que escribió el usuario** en el buscador: el mismo valor que se envió en `query` a `POST /api/v1/public/diagnosis`. Opcional, 200 caracteres como máximo. Es lo que se guarda como nombre del negocio.
> - **No envíes en ningún campo el `name` de la respuesta del diagnóstico.** Ese nombre es contenido de Google Maps y los términos prohíben guardarlo.
> - `business_name` queda obsoleto. La API lo sigue aceptando para no romper a quien lo envíe, pero **lo ignora y no lo guarda**. Quien solo envíe `business_name` deja el alta sin nombre de negocio.
> - Pendiente en la web: `web/site/app.js` envía hoy `business_name: d.name`. Cámbialo por `business_query` con el texto del buscador.

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

Elegir ficha pide además la sincronización de esa ficha, para que `GET /api/v1/me/summary` tenga datos cuanto antes.

Errores: 401, 403 (falta la cabecera), 404 (la ficha no es de esa cuenta o no existe; la respuesta es la misma en los dos casos), 422.

### `GET /api/v1/me/diagnosis`

Diagnóstico de la ficha elegida, con la misma forma que el diagnóstico público, incluido `attribution`. Aplican las mismas reglas de atribución, de no guardar la respuesta y de límite.

La API busca la ficha por su `place_id`. Si la ficha no tiene `place_id`, o Google ya no lo reconoce, la busca por su título.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha. Lleva a la lista de fichas |
| 404 | No se ha encontrado la ficha en Google Maps ni por `place_id` ni por título. `detail` trae el mensaje |
| 429 | Demasiados diagnósticos de esa cuenta en la última hora. Cuentan todos, también los repetidos |
| 503 | Google no responde |

### `GET /api/v1/me/summary`

Resumen de la ficha elegida: reseñas sin responder, horarios y cambios hechos por Google. La API no llama a Google en esta ruta: lee lo que el worker guardó en la última sincronización. Por eso responde rápido y no tiene límite de uso.

Respuesta 200:

```json
{
  "status": "ok",
  "synced_at": "2026-11-11T16:58:39Z",
  "review_count": 13,
  "average_rating": 4.7,
  "unanswered_count": 13,
  "unanswered_low_count": 1,
  "last_review_at": "2025-02-06",
  "has_regular_hours": true,
  "upcoming_special_hours": 0,
  "google_changed": true,
  "google_changed_fields": ["specialHours"],
  "google_changed_labels": ["Horarios especiales"],
  "stale": false
}
```

| Campo | Tipo | Qué es |
|---|---|---|
| `status` | texto | `ok`, `needs_reconnect`, `error` o `pendiente`. Consulta [Estados del resumen](#estados-del-resumen) |
| `synced_at` | fecha y hora UTC, o `null` | Última lectura correcta de Google. Las cifras son de ese momento |
| `review_count` | entero o `null` | Número de reseñas |
| `average_rating` | decimal o `null` | Nota media, de 0 a 5, con dos decimales como máximo. `null` si no hay reseñas |
| `unanswered_count` | entero o `null` | Reseñas sin responder |
| `unanswered_low_count` | entero o `null` | Reseñas sin responder de 1 o 2 estrellas |
| `last_review_at` | fecha (`AAAA-MM-DD`, UTC) o `null` | Día de la última reseña. `null` si no hay reseñas |
| `has_regular_hours` | booleano o `null` | Si la ficha tiene horario |
| `upcoming_special_hours` | entero o `null` | Días con horario especial de hoy en adelante |
| `google_changed` | booleano o `null` | Si Google ha cambiado algo en la ficha |
| `google_changed_fields` | lista de textos, o `null` | Campos que Google ha cambiado, con el nombre que les da Google |
| `google_changed_labels` | lista de textos, o `null` | Los mismos campos en español, en el mismo orden y con la misma longitud. Muestra estos |
| `stale` | booleano | `true` si las cifras tienen más de 6 horas. La API ya ha pedido una actualización |

Etiquetas de `google_changed_labels`:

| Campo de Google | Etiqueta |
|---|---|
| `title` | Nombre |
| `phoneNumbers` | Teléfono |
| `websiteUri` | Web |
| `categories` | Categorías |
| `regularHours` | Horario |
| `specialHours` | Horarios especiales |
| `moreHours` | Otros horarios |
| `storefrontAddress` | Dirección |
| `serviceArea` | Zona de servicio |
| `latlng` | Ubicación en el mapa |
| `openInfo` | Abierto o cerrado |
| `profile` | Descripción |
| `serviceItems` | Servicios |
| `labels` | Etiquetas |

Un subcampo, como `phoneNumbers.primaryPhone`, lleva la etiqueta de su campo. Un campo que la API no conoce se devuelve tal cual.

#### Estados del resumen

| `status` | Qué significa | Qué hace la web |
|---|---|---|
| `pendiente` | Todavía no hay resumen: la sincronización se acaba de pedir. Todos los demás campos van a `null` y `stale` a `false` | Muestra las tarjetas cargando y vuelve a preguntar |
| `ok` | Cifras de la última sincronización | Muestra las tarjetas |
| `needs_reconnect` | Google ha retirado el permiso. Las cifras son las de la última lectura correcta, o `null` si nunca la hubo | Pide volver a conectar con Google. Si hay cifras, muéstralas con su fecha |
| `error` | La última sincronización falló. Las cifras son las de la última lectura correcta, o `null` si nunca la hubo | Si hay cifras, muéstralas con su fecha. Si no, "No hemos podido leer tu ficha. Prueba más tarde" |

Con `needs_reconnect` y `error`, `google_changed_fields` y `google_changed_labels` son listas, vacías si nunca hubo una lectura correcta.

#### Cuándo se sincroniza

- Al elegir ficha con `POST /api/v1/me/location`.
- Al pedir el resumen, si no existe, si tiene más de 6 horas o si el último intento falló hace más de 15 minutos.
- Como máximo, una sincronización por ficha cada 15 minutos. Pedir el resumen muchas veces no lanza más.

#### Sondeo

- Con `pendiente`, vuelve a pedir el resumen **cada 3 segundos, 10 veces como máximo**. Lo normal es que llegue en menos de 10 segundos.
- Si tras 10 intentos sigue en `pendiente`, deja de preguntar y muestra "Estamos leyendo tu ficha. Vuelve en unos minutos", con un botón para reintentar.
- Con `stale: true`, muestra las cifras que hay. Para refrescarlas sin que el usuario recargue, vuelve a pedir el resumen una sola vez pasados 10 segundos.
- Con `ok`, `needs_reconnect` y `error` no sondees.
- Puedes tener la respuesta en memoria mientras la pantalla está abierta. No la guardes en `localStorage` ni similares: caduca.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha, o Google ha dejado de listar la elegida. Lleva a la lista de fichas |

Cada cuenta ve solo el resumen de su ficha elegida. Dos cuentas que administran la misma ficha de Google tienen resúmenes separados.

### `POST /api/v1/auth/logout`

204 sin cuerpo. Cierra la sesión en el servidor y borra la cookie. Lleva la cabecera `X-Requested-With: fichaitor`; sin ella, 403. Responde 204 también si no había sesión.

## Pantallas mínimas para tener algo visible

1. **Inicio** (`/`): diagnóstico público, resultado, lista de espera y botón "Conectar con Google".
2. **App** (`/app`): lee `?google=`; sin sesión, invita a conectar; con sesión y sin ficha elegida, lista de fichas; con ficha elegida, panel con el diagnóstico y el resumen de esa ficha y botón de salir.
3. **Privacidad** (`/privacidad/`) y **condiciones** (`/condiciones-de-uso/`).

## Siguiente versión del contrato

El resumen de la ficha (`GET /api/v1/me/summary`) ya existe. Lo siguiente es la lista de reseñas con respuesta propuesta y las fechas clave. Se añadirán aquí antes de implementarse.
