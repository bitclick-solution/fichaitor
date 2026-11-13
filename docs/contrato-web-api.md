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
- Sin Google real: con `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_PLACES=true` todo el recorrido funciona con datos de ejemplo. Las reseñas y sus respuestas también; el borrador necesita además `FICHAITOR_FAKE_SCREENSHOT=true`. El diagnóstico del panel no depende de `FICHAITOR_FAKE_PLACES`: sale de la ficha de demostración que lee el worker.

## Convenciones

- Prefijo `/api/v1`. JSON en UTF-8.
- Errores: `{"detail": "texto en español listo para mostrar"}` con el código HTTP correspondiente. En los 422 de validación automática, `detail` puede ser una lista: muestra un mensaje genérico.
- Sesión: cookie `__Host-fichaitor_session`, HttpOnly, `Secure`, `SameSite=Lax`, `Path=/`, sin `Domain`, 30 días. La web no la lee ni la escribe; solo envía las peticiones con `credentials: "same-origin"`.
  - **Cambio del 2026-11-12 (B6 de la revisión de seguridad).** Antes se llamaba `fichaitor_session`. El prefijo `__Host-` impide que otro sitio bajo `fichaitor.es` la fije. La API ya no lee el nombre anterior: **al desplegar, todas las sesiones abiertas se cierran** y cada dueño ve la pantalla de conectar (un 401) una vez. No hay nada que cambiar en la web.
  - La cookie del `state` de OAuth pasa a `__Secure-fichaitor_oauth_state` (ruta `/api/v1/google/oauth`, 10 minutos). Una conexión a medias durante el despliegue responde 400 en la vuelta de Google: basta con volver a empezar.
  - En desarrollo por http (`FICHAITOR_COOKIE_SECURE=false`) los nombres van sin prefijo (`fichaitor_session`, `fichaitor_oauth_state`): los navegadores rechazan esos prefijos sin `Secure`.
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
- Errores: 404 (no encontrado), 422, 429 (límite), 503 (Google no responde, o se ha alcanzado el tope diario).
- **Tope diario (cambio del 2026-11-12).** Además del límite por IP, hay un tope de diagnósticos por día para todos los visitantes juntos (`FICHAITOR_DIAGNOSIS_DAILY_LIMIT`, 500 por defecto; el día es el de UTC). Al alcanzarlo, 503 con `detail` "Hoy hemos recibido muchas consultas y no podemos hacer más. Prueba mañana." Muestra `detail` tal cual y no reintentes.

#### Atribución de Google Maps (cambio del 2026-11-11)

Nombre, dirección, categoría, enlace y las cifras de las mejoras (fotos, nota, reseñas) vienen de Google Maps. Las políticas de Google exigen decirlo.

- La web **muestra el texto de `attribution`** ("Google Maps") junto a esos datos, en la misma pantalla y sin que haga falta desplazarse o pulsar para verlo. Por ejemplo: "Datos de Google Maps", debajo del nombre y la dirección.
- No lo traduzcas, no lo abrevies y no lo partas en dos líneas. Usa el texto del campo; no lo escribas a mano en la web.
- Vale para el diagnóstico público. `GET /api/v1/me/diagnosis` ya no usa datos de Google Maps: trae su propio texto en `attribution` ("Datos de tu Perfil de Empresa de Google"), que la web muestra igual, tal cual llega. También debe verse en las capturas y los vídeos que enseñen el diagnóstico.

#### Qué no puede guardar la web (cambio del 2026-11-11)

Los términos de Google solo permiten guardar el `place_id`. La API ya no guarda la respuesta del diagnóstico: pide los datos a Google cada vez.

- La web tampoco guarda la respuesta: ni en `localStorage`, ni en `sessionStorage`, ni en IndexedDB, ni en una caché de un service worker. Tenerla en memoria mientras la pantalla está abierta es correcto.
- La web no envía a la API datos del diagnóstico para que los guarde, salvo `place_id` y `score` en la lista de espera.

#### El límite cuenta todos los diagnósticos (cambio del 2026-11-11)

Antes, repetir una búsqueda salía de una caché y no contaba. Ahora **cada diagnóstico público cuenta**, también los repetidos: 10 por hora por IP. Cada uno es una petición de pago a Google. `GET /api/v1/me/diagnosis` no tiene límite: no llama a Google.

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

### `POST /api/v1/public/visit` (2026-11-13)

Cuenta una visita a la portada para el embudo del backoffice. Sin sesión y sin cookies. La portada la llama una vez al cargar, con `navigator.sendBeacon`:

```js
navigator.sendBeacon("/api/v1/public/visit", JSON.stringify({ source: origen() }));
```

**Cuerpo.** Cualquiera de estos, de 512 bytes como mucho:

- `text/plain` con un JSON: `{"source": "instagram"}`. Es lo que manda `sendBeacon` con una cadena. También vale `application/json`.
- `application/x-www-form-urlencoded`: `source=instagram`.
- Vacío.

**`source`.** El valor de `utm_source` o de `ref` de la URL de la portada. Se pasa a minúsculas y se guarda solo si son de 1 a 40 caracteres de `[a-z0-9_-]`. Si no cumple, o no viene, se guarda `null`. No da error.

**Respuesta.** Siempre `204` sin cuerpo, también cuando la visita no se cuenta. Así no da pistas a quien intente inflar el número.

**Qué se guarda.** Una fila de `funnel_event` con `kind = 'visit'`, `source` y la hora. **Nada más**: ni la dirección IP, ni el *user agent*, ni una cookie, ni ningún identificador del visitante. La tabla no tiene datos personales (`docs/retencion.md`).

**Cuándo no se cuenta** (y responde 204 igual):

- El *user agent* no empieza por `Mozilla/` (no es un navegador) o contiene `bot`, `crawl`, `spider` o `preview` (robots y vistas previas de enlaces).
- Más de 10 visitas en una hora desde la misma dirección. El límite vive solo en la memoria de la API y no guarda la dirección: usa una huella HMAC con una clave aleatoria que se crea al arrancar y no se escribe en ningún sitio. Al reiniciar la API, el contador vuelve a cero.
- El cuerpo pasa de 512 bytes.
- La base no responde (se registra un aviso, sin datos).

**Sin banner de cookies.** No escribe ni lee nada en el navegador del visitante (ni cookies, ni `localStorage`, ni huella del navegador), así que no le aplica el artículo 22.2 de la LSSI. Lo único del visitante que toca es su dirección IP, que llega con cualquier petición: se usa un instante, en memoria y transformada con una clave que se descarta, solo para el límite contra abusos, y no se guarda en ningún sitio. Lo que queda es un recuento agregado, sin nada que identifique a nadie.

**Modo «solo web».** No hay API (`/api/` responde 503) y las visitas no se cuentan.

## Conexión con Google (ya existe)

`GET /api/v1/google/oauth/start` es una **navegación del navegador**, no una llamada `fetch`: un enlace o `window.location`.

**Parámetro opcional `commercial_optout=1` (2026-11-13).** La casilla del alta para oponerse a las comunicaciones comerciales (artículo 21.2 de la LSSI). Si está marcada, navega a `/api/v1/google/oauth/start?commercial_optout=1`; si no, sin el parámetro. Consulta [Comunicaciones comerciales: en el alta y en Ajustes](#comunicaciones-comerciales-en-el-alta-y-en-ajustes-2026-11-13).

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
  "selected_location_id": null,
  "connected": true,
  "pending_deletion": null
}
```

- `connected` y `pending_deletion` (2026-11-12): el estado de la conexión con Google y la baja programada, si la hay. Consulta [Baja: desconectar y borrar la cuenta](#baja-desconectar-y-borrar-la-cuenta-2026-11-12).
- `locations` viene ordenado por `title`. Puede ser una lista vacía.
- `id`: `loc_` seguido de letras y números. Es propio de cada cuenta: no sirve en otra.
- `place_id` puede ser `null`: Google no siempre lo da (fichas sin verificar o sin ubicación en el mapa).
- 401 si no hay sesión.
- **Multinegocio (2026-11-12):** cada ficha trae además `active`, `plan`, `active_since`, `status` y `pending`, y el cuerpo trae `plans` y `pricing`. `selected_location_id` pasa a ser la ficha preferida del panel. Consulta [Multinegocio](#multinegocio-2026-11-12).
- **No trae la preferencia de comunicaciones comerciales** (2026-11-13): `GET /me` es también la respuesta de elegir, activar y desactivar fichas, y la preferencia solo se enseña en Ajustes. Léela con `GET /api/v1/me/communications`.

### `POST /api/v1/me/location`

```json
{ "location_id": "loc_3f2a" }
```

Guarda la **ficha preferida** del panel (`selected_location_id`): la que sirven las lecturas sin `?location=`. Solo admite una ficha **activa**: no activa nada. Para una ficha inactiva, activa antes con [`POST /api/v1/me/locations/{id}/activation`](#post-apiv1melocationsidactivation).

Devuelve el mismo cuerpo que `GET /me`, ya con `selected_location_id`. Se puede llamar otra vez para cambiar de ficha.

Elegir ficha pide además la sincronización de esa ficha, para que `GET /api/v1/me/summary` tenga datos cuanto antes.

Errores:

| Código | `code` | Cuándo | `detail` |
|---|---|---|---|
| 401 | — | Sin sesión | "No has iniciado sesión." |
| 403 | — | Falta la cabecera `X-Requested-With` | "Petición no permitida." |
| 404 | — | La ficha no es de esa cuenta, no existe o Google ya no la lista. La misma respuesta en todos los casos | "Esa ficha no está en tu cuenta." |
| 409 | `location_inactive` | La ficha es de la cuenta pero no está activa. No cambia nada | "Esa ficha no está activa. Actívala primero en «Tus negocios»." |
| 409 | — | La cuenta tiene una baja programada | "Tienes una baja programada. Restáurala para seguir usando Fichaitor." |
| 422 | — | Falta `location_id` | — |

> **Cambio del paso 9 de [multinegocio.md](multinegocio.md):** hasta ahora, elegir una ficha inactiva la activaba con el plan por defecto, y la ruta podía responder 402 y los 409 de la activación (`free_taken`, `offboarding_pending` y `location_removed`). Ya no: responde 409 `location_inactive` y no activa nada. La web actual no se ve afectada: solo elige fichas activas y, en «Activa tu primera ficha», activa antes de elegir.

### `GET /api/v1/me/diagnosis`

> **Importante (cambio del 2026-11-11): el diagnóstico del panel sale de la ficha conectada, no de Google Maps.**
>
> - La API ya no consulta Google Places en esta ruta. Calcula el diagnóstico con los datos que el worker lee del Perfil de Empresa del dueño: los mismos de `GET /api/v1/me/summary`, más descripción, categorías, teléfono, web, fotos y atributos.
> - `attribution` pasa a ser `"Datos de tu Perfil de Empresa de Google"`. Muéstralo tal cual. Ya no aplica la regla de "Datos de Google Maps" en esta ruta.
> - `locked_checks` va siempre vacío: con la ficha conectada no hay nada bloqueado.
> - La respuesta 200 gana tres campos: `status`, `synced_at` y `stale`.
> - Hay un estado nuevo, "todavía no hay diagnóstico": **404 con `status` y `detail`**. Consulta [Cuando todavía no hay diagnóstico](#cuando-todavía-no-hay-diagnóstico).
> - Desaparecen de esta ruta el 429 (ya no hay límite) y el 503 de Google. El 404 cambia de significado.
> - Aparecen mejoras nuevas (`code`): `unanswered_reviews`, `no_holiday_hours`, `no_description`, `short_description` y `google_changed`.

> **Multinegocio (2026-11-12):** admite `?location=loc_…` y la respuesta lleva `location_id`. Sin `?location=`, la ficha preferida (`selected_location_id`). Consulta [La ficha de cada lectura](#la-ficha-de-cada-lectura-location).

Diagnóstico de la ficha elegida. La API no llama a Google en esta ruta: lo calcula en el momento con lo que el worker guardó en la última sincronización. Responde rápido, no cuesta dinero y no tiene límite de uso.

Respuesta 200:

```json
{
  "place_id": "ChIJ...",
  "name": "Bar La Parra",
  "address": "Calle Elvira 1, 18010 Granada",
  "category": "Bar",
  "score": 69,
  "label": "Mejorable",
  "findings": [
    {
      "code": "unanswered_reviews",
      "title": "Tienes 13 reseñas sin responder",
      "detail": "Una es de 1 o 2 estrellas. Una crítica sin respuesta es lo que más frena a quien te compara.",
      "impact": "alto"
    },
    {
      "code": "no_holiday_hours",
      "title": "Sin horario para el festivo del 8 de diciembre",
      "detail": "El 8 de diciembre es festivo nacional y no has indicado si abres. Google avisa a quien te busca de que tu horario puede no ser el real.",
      "impact": "medio"
    }
  ],
  "locked_checks": [],
  "maps_url": "https://maps.google.com/?cid=...",
  "attribution": "Datos de tu Perfil de Empresa de Google",
  "status": "ok",
  "synced_at": "2026-11-11T16:58:39Z",
  "stale": false
}
```

Los diez primeros campos tienen la misma forma y el mismo significado que en el diagnóstico público (`label`, `impact`, hasta 5 `findings` ya ordenados). Lo que cambia:

| Campo | Tipo | Qué es |
|---|---|---|
| `name` | texto | Nombre de la ficha |
| `address`, `category` | texto | Dirección y categoría principal, como las tiene el dueño. Pueden ir vacíos |
| `place_id`, `maps_url` | texto | Se rellenan si Google los da para esa ficha. Si no, texto vacío |
| `locked_checks` | lista | Siempre vacía |
| `attribution` | texto | Siempre `"Datos de tu Perfil de Empresa de Google"` |
| `status` | texto | `ok`, `needs_reconnect` o `error`: cómo acabó la última sincronización. Los mismos valores y el mismo significado que en [Estados del resumen](#estados-del-resumen) |
| `synced_at` | fecha y hora UTC | Última lectura correcta de Google. El diagnóstico es de la ficha en ese momento |
| `stale` | booleano | `true` si los datos tienen más de 6 horas. La API ya ha pedido una actualización |
| `location_id` | texto | La ficha de la respuesta (2026-11-12). También va en el 404 de «todavía no hay diagnóstico» |

Con `needs_reconnect` y `error` la API sigue sirviendo el diagnóstico de la última lectura correcta. Muéstralo con su fecha; el aviso de reconectar o de error ya lo da el resumen.

Mejoras posibles (`code`). Las cinco primeras solo existen en esta ruta:

| `code` | Cuándo | `impact` |
|---|---|---|
| `unanswered_reviews` | Hay reseñas sin responder. El título dice cuántas | `alto` si alguna es de 1 o 2 estrellas; si no, `medio` |
| `no_holiday_hours` | Hay un festivo nacional en los próximos 30 días sin horario especial. El título o el detalle dicen cuáles | `alto` si el primero cae en 7 días o menos; si no, `medio` |
| `no_description` | La ficha no tiene descripción | `medio` |
| `short_description` | La descripción tiene menos de 150 caracteres | `bajo` |
| `google_changed` | Google ha cambiado datos de la ficha. El detalle dice cuáles, con las etiquetas de `google_changed_labels` | `medio` |
| `not_operational` | La ficha figura como cerrada | `alto` |
| `no_hours` | No tiene horario | `alto` |
| `no_photos`, `few_photos`, `some_photos` | El dueño ha subido 0, menos de 5 o menos de 10 fotos | `alto`, `medio`, `bajo` |
| `low_rating` | Nota media por debajo de 4 | `alto` |
| `few_reviews` | Menos de 10 reseñas | `medio` |
| `no_website`, `no_phone` | Sin web o sin teléfono | `medio` |
| `no_secondary_categories` | Una sola categoría | `medio` |
| `no_service_attributes` | Ningún atributo marcado | `bajo` |

- Los festivos son solo los nacionales: la API todavía no sabe la comunidad autónoma del negocio. Se calculan con la fecha del día de la petición, así que el diagnóstico puede cambiar de un día a otro sin una sincronización nueva.
- Las fotos son las que ha subido el dueño. Las de clientes no cuentan.
- Si Google no deja leer las fotos o los atributos de una ficha, esas mejoras no aparecen; el resto del diagnóstico sí.

#### Cuando todavía no hay diagnóstico

Si el worker aún no ha leído la ficha, la API responde **404** con este cuerpo:

```json
{ "status": "pendiente", "detail": "Estamos leyendo tu ficha. Vuelve a intentarlo en unos segundos." }
```

| `status` | Qué significa | Qué hace la web |
|---|---|---|
| `pendiente` | La sincronización está pedida y el worker no ha terminado. También sale con una ficha sincronizada antes de este cambio, hasta su siguiente sincronización | Muestra "Analizando tu ficha" y vuelve a preguntar, con el mismo ritmo que el resumen: cada 3 segundos, 10 veces como máximo |
| `needs_reconnect` | Nunca se ha podido leer la ficha y Google ha retirado el permiso | Muestra `detail`. El resumen ya pide volver a conectar |
| `error` | Nunca se ha podido leer la ficha y el último intento falló | Muestra `detail` y un botón para reintentar |

- Es un 404 y no un 2xx a propósito: `detail` llega como en cualquier error, así que una web que no conozca `status` muestra el mensaje y su botón de reintentar en vez de intentar pintar un diagnóstico que no existe.
- Distingue los casos por `status`, no por el texto de `detail`.
- `pendiente` del diagnóstico y `pendiente` del resumen casi siempre coinciden. La excepción es la ficha sincronizada antes del 2026-11-11: el resumen ya responde `ok` y el diagnóstico sigue `pendiente` hasta la siguiente sincronización, que la API pide al momento.
- Pedir el diagnóstico pide la sincronización con las mismas reglas que el resumen ([Cuándo se sincroniza](#cuándo-se-sincroniza)). Pedir las dos rutas a la vez no lanza dos.

Qué puede guardar la web: lo mismo que con el resumen. Tenlo en memoria mientras la pantalla está abierta; no lo guardes en `localStorage` ni similares, porque caduca.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha, o Google ha dejado de listar la elegida. Lleva a la lista de fichas |
| 404 | Todavía no hay diagnóstico. El cuerpo trae `status` y `detail` |

### `GET /api/v1/me/summary`

> **Multinegocio (2026-11-12):** admite `?location=loc_…` y la respuesta lleva `location_id`. Sin `?location=`, la ficha preferida (`selected_location_id`). Consulta [La ficha de cada lectura](#la-ficha-de-cada-lectura-location).

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
| `pendiente` | Todavía no hay resumen, o el último intento falló (`needs_reconnect` o `error`) y hay una sincronización de esa ficha pedida después, en cola o en curso: el worker la está leyendo otra vez. Todos los demás campos van a `null` y `stale` a `false` | Muestra las tarjetas cargando y vuelve a preguntar |
| `ok` | Cifras de la última sincronización | Muestra las tarjetas |
| `needs_reconnect` | Google ha retirado el permiso, o no hay ninguna conexión guardada para esa cuenta, o la cuenta ha perdido el acceso a esa ficha (ver abajo). Las cifras son las de la última lectura correcta, o `null` si nunca la hubo o se ha perdido el acceso | Pide volver a conectar con Google. Si hay cifras, muéstralas con su fecha |
| `error` | La última sincronización falló: Google no respondió o rechazó la lectura, o la ficha ya no está en la conexión más reciente. Las cifras son las de la última lectura correcta, o `null` si nunca la hubo | Si hay cifras, muéstralas con su fecha. Si no, "No hemos podido leer tu ficha. Prueba más tarde" |

Con `needs_reconnect` y `error`, `google_changed_fields` y `google_changed_labels` son listas, vacías si nunca hubo una lectura correcta.

#### Sin acceso a la ficha (cambio del 2026-11-12)

Si Google rechaza la lectura de la ficha (403 o 404) en **dos sincronizaciones seguidas**, el worker entiende que ese usuario de Google ya no tiene acceso: por ejemplo, el dueño le ha quitado de gestor. Desde ese momento, y hasta que vuelva a conectar:

- `GET /api/v1/me/summary` responde 200 con `status: "needs_reconnect"`, todas las cifras a `null` y `synced_at: null`. Ya no se sirven las cifras de la última lectura correcta.
- `GET /api/v1/me/reviews` responde `status: "needs_reconnect"`, `total: 0` y la lista vacía. El borrador y la respuesta de una reseña responden 404.
- `GET /api/v1/me/diagnosis` responde 404 con `status: "needs_reconnect"`.
- `GET /api/v1/me/sentiment` responde `status: "error"` con todo a `null`; el aviso del resumen basta.
- `GET /api/v1/me` sigue devolviendo `selected_location_id`, pero esa ficha ya no está en `locations`.
- No se pide ninguna sincronización. Lo guardado de esa ficha se borra a los 30 días, como el de las fichas que Google deja de listar.

Al volver a conectar, si Google vuelve a listar la ficha, se vuelve a ver lo guardado y se pide una lectura nueva. Si no la lista, deja de estar elegida (`selected_location_id: null`) y la web pide elegir otra.

`pendiente` no es un estado en el que una ficha se quede: toda sincronización que falla deja `needs_reconnect` o `error`. Si el worker está parado, `pendiente` dura hasta que vuelve a arrancar.

#### «Actualizando» al volver a conectar (cambio del 2026-11-12)

Al volver de conectar con Google (`google=ok`), la API pide al momento la sincronización de cada ficha activa. Mientras esa sincronización espera en la cola o está en curso, el resumen de una ficha que estaba en `needs_reconnect` o `error` responde **`pendiente`**, con todo a `null`, en lugar del estado de antes. La web ya sondea mientras el estado es `pendiente`, así que pasa sola a `ok` (o al estado nuevo) cuando el worker termina.

- Solo cuenta una sincronización pedida **después** del último intento, que ya toca ejecutar (en cola y vencida, o en curso). Un reintento con espera o una franja futura del planificador no la cuentan.
- Un resumen en `ok` no cambia: se siguen enseñando sus cifras mientras se refrescan, con `stale` si toca.
- Lo mismo vale para el `status` de cada ficha en `GET /api/v1/me` (consulta [`GET /api/v1/me` con varias fichas](#get-apiv1me-con-varias-fichas)).
- El diagnóstico, las reseñas y el sentimiento no cambian.

#### Cuándo se sincroniza

- Al elegir ficha con `POST /api/v1/me/location`.
- Al volver a conectar con Google (`google=ok`), si la cuenta ya tenía ficha elegida y Google ha listado las fichas. Se pide al momento, aunque no hayan pasado los 15 minutos, y como máximo una vez por minuto.
- Al pedir el resumen o el diagnóstico, si no existe, si tiene más de 6 horas, si el último intento falló hace más de 15 minutos o si se guardó antes de que existieran los datos del diagnóstico.
- Fuera de la reconexión, como máximo una sincronización por ficha cada 15 minutos. Pedir el resumen muchas veces no lanza más.

#### Sondeo

- Con `pendiente`, vuelve a pedir el resumen **cada 3 segundos, 10 veces como máximo**. Lo normal es que llegue en menos de 10 segundos.
- Si tras 10 intentos sigue en `pendiente`, deja de preguntar y muestra "Estamos leyendo tu ficha. Vuelve en unos minutos", con un botón para reintentar.
- Con `stale: true`, muestra las cifras que hay. Para refrescarlas sin que el usuario recargue, vuelve a pedir el resumen una sola vez pasados 10 segundos.
- Con `ok`, `needs_reconnect` y `error` no sondees.
- Al volver de conectar con `google=ok`, el resumen responde `pendiente` mientras el worker lee la ficha otra vez (consulta [«Actualizando» al volver a conectar](#actualizando-al-volver-a-conectar-cambio-del-2026-11-12)): sondea como con cualquier `pendiente`. Si aun así llega `needs_reconnect` o `error` (la cola no había recibido la petición todavía), vuelve a pedirlo una sola vez pasados 10 segundos.
- Puedes tener la respuesta en memoria mientras la pantalla está abierta. No la guardes en `localStorage` ni similares: caduca.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha, o Google ha dejado de listar la elegida. Lleva a la lista de fichas |

Cada cuenta ve solo el resumen de su ficha elegida. Dos cuentas que administran la misma ficha de Google tienen resúmenes separados.

### `POST /api/v1/auth/logout`

204 sin cuerpo. Cierra la sesión en el servidor y borra la cookie. Lleva la cabecera `X-Requested-With: fichaitor`; sin ella, 403. Responde 204 también si no había sesión.

## Baja: desconectar y borrar la cuenta (2026-11-12)

El dueño puede desconectar Google (`scope: "ficha"`) o borrar su cuenta (`scope: "cuenta"`). Las dos bajas siguen el mismo recorrido:

1. La web pide un código. Llega al correo de la cuenta, que es el de Google.
2. El dueño escribe el código. La baja queda **programada a 48 horas**.
3. Hasta esa fecha se puede **restaurar**: desde la sesión o desde el enlace del correo de aviso.
4. A las 48 horas la baja se hace efectiva: se revoca el acceso en Google y empieza el borrado. Lo hace el worker; ninguna ruta lo dispara.

Los plazos de borrado están en [retencion.md](retencion.md).

Reglas comunes:

- Los `POST` con sesión llevan la cabecera `X-Requested-With: fichaitor`. Sin ella, 403.
- Sin sesión, 401.
- Todos los errores traen `detail` con un texto en español listo para mostrar.
- El código y el token de restauración viajan siempre en el cuerpo del `POST`, nunca en la URL.

### `POST /api/v1/me/offboarding/code`

Pide el código. Petición:

```json
{ "scope": "ficha" }
```

`scope` es `"ficha"` o `"cuenta"`.

Respuesta 202:

```json
{ "sent_to": "d***o@example.com", "expires_in": 600 }
```

- `sent_to` es el correo de la cuenta, enmascarado.
- `expires_in` son los segundos de validez del código (10 minutos).
- El código tiene 6 cifras y sirve una sola vez. Vale solo para ese `scope` y desde la sesión que lo pidió.
- Pedir otro código invalida el anterior.
- La respuesta llega antes de que el correo salga: lo envía el worker. Para saber si salió, consulta `GET /api/v1/me/offboarding/code`.

| Código | Cuándo | `detail` |
|---|---|---|
| 409 | La cuenta ya tiene una baja programada | "Ya tienes una baja programada. Restáurala si quieres cambiarla." |
| 429 | Más de 5 códigos por cuenta en una hora | "Has pedido demasiados códigos. Prueba dentro de un rato." |
| 503 | El servidor no tiene correo configurado, o no pudo guardar el código. No se programa nada | "Ahora mismo no podemos enviarte el código por correo. Escríbenos a soporte@fichaitor.es." |

### `GET /api/v1/me/offboarding/code`

Estado del correo del último código vigente de la cuenta:

```json
{ "status": "enviado" }
```

| `status` | Qué significa | Qué hace la web |
|---|---|---|
| `pendiente` | El correo está en cola | Sigue esperando. Sondea cada 3 segundos, 30 segundos como máximo |
| `enviado` | El servidor de correo lo aceptó | Pide el código al dueño |
| `fallido` | No se pudo enviar tras 3 intentos | Avisa al dueño y ofrece pedir otro código |
| `ninguno` | No hay ningún código vigente | Ofrece pedir uno |

### `POST /api/v1/me/offboarding/confirm`

Confirma la baja con el código. Petición:

```json
{ "scope": "ficha", "code": "123456" }
```

Respuesta 200:

```json
{ "scope": "ficha", "delete_at": "2026-11-14T11:16:00Z" }
```

`delete_at` es cuándo se hace efectiva la baja: 48 horas después.

| Código | Cuándo | `detail` |
|---|---|---|
| 409 | La cuenta ya tiene una baja programada | "Ya tienes una baja programada. Restáurala si quieres cambiarla." |
| 410 | No hay código, ha caducado, ya se usó o se pidió otro después | "El código ha caducado o ya se ha usado. Pide otro." |
| 422 | El código no es correcto. También si es de otro `scope` o de otra sesión | "El código no es correcto. Te quedan 4 intentos." |
| 429 | Quinto intento fallido. El código queda invalidado: hay que pedir otro | "Demasiados intentos. Ese código ya no vale: pide otro." |
| 503 | No se pudo guardar | "No hemos podido hacerlo. Prueba otra vez en un minuto." |

Qué pasa al confirmar:

- **Se deja de leer la ficha y de enviar avisos.** No se revoca ni se borra nada todavía.
- **Las rutas de la ficha responden 409** con `detail` "Tienes una baja programada. Restáurala para seguir usando Fichaitor.": `POST /api/v1/me/location`, `GET /api/v1/me/summary`, `GET /api/v1/me/diagnosis`, `GET /api/v1/me/sentiment`, `GET /api/v1/me/reviews`, los borradores, las respuestas y `GET /api/v1/me/actions/{id}`.
- **Se envía un correo de aviso** con la fecha y el enlace de restauración.
- **Con `scope: "ficha"`** la sesión sigue abierta. `GET /api/v1/me` trae `pending_deletion`.
- **Con `scope: "cuenta"`** se cierran todas las sesiones de la cuenta, también la que confirma: la siguiente petición responde 401. La web lleva al dueño a la pantalla de inicio.

### `POST /api/v1/me/offboarding/restore`

Cancela la baja programada de la cuenta de la sesión. Sin cuerpo. Respuesta 204.

- 409 si la cuenta no tiene ninguna baja programada: "No tienes ninguna baja programada."
- 503 si no se pudo guardar: "No hemos podido hacerlo. Prueba otra vez en un minuto." La baja sigue programada.
- Tras restaurar, todo vuelve a como estaba y se pide una sincronización. El enlace del correo deja de valer.
- Se envía un correo que confirma la restauración. Con `scope: "cuenta"` recuerda que las sesiones siguen cerradas y lleva un botón «Entrar en Fichaitor» a `<FICHAITOR_PUBLIC_BASE_URL>/app/`.

### `POST /api/v1/public/offboarding/restore`

Cancela la baja desde el enlace del correo. **Sin sesión** y sin la cabecera de protección. Petición:

```json
{ "token": "..." }
```

Respuesta 204.

- El enlace del correo es `https://<dominio>/app#restaurar=<token>`. El token va en el **fragmento**, que el navegador no envía al servidor: no queda en los registros de acceso. La web lo lee del fragmento, lo quita de la barra de direcciones y lo envía en el cuerpo de esta ruta.
- El token sirve una sola vez y hasta `delete_at`. Solo se gasta si la restauración se guarda: con un **503** ("No hemos podido hacerlo. Prueba otra vez en un minuto.") el enlace sigue valiendo y la web puede ofrecer reintentar.
- Al restaurar se envía el mismo correo de confirmación que desde la sesión.
- **410** para un token desconocido, usado o caducado, siempre con el mismo `detail`: "Este enlace ya no vale: ha caducado o ya se ha usado." La respuesta no revela si la cuenta existe.
- **429** con más de 20 intentos por IP en una hora.
- Restaurar no abre sesión. Si el dueño no tiene una, la web le pide entrar con Google.

### `GET /api/v1/me` durante una baja

`GET /api/v1/me` trae dos campos más:

```json
{
  "email": "dueno@example.com",
  "locations": [],
  "selected_location_id": null,
  "connected": true,
  "pending_deletion": { "scope": "ficha", "delete_at": "2026-11-14T11:16:00Z" }
}
```

| Campo | Tipo | Qué es |
|---|---|---|
| `pending_deletion` | objeto o `null` | La baja programada. `null` si no hay ninguna |
| `connected` | booleano | `false` cuando la baja de la ficha ya es efectiva y el dueño no ha vuelto a conectar. La web ofrece "Conectar con Google" |

Con `pending_deletion`, la web enseña solo el aviso de la baja con su fecha y el botón de restaurar.

### Entrar con Google durante las 48 horas

Si alguien entra con Google en una cuenta con la baja programada, no se crea una cuenta nueva: es la misma. La sesión nueva solo puede ver `GET /api/v1/me` (con `pending_deletion`), restaurar y cerrar sesión. El resto responde 409.

### Cuando la baja ya es efectiva

- **`scope: "ficha"`**: la cuenta sigue existiendo, sin fichas. `GET /api/v1/me` responde con `locations: []`, `connected: false` y `pending_deletion: null`. Al volver a conectar, la ficha se lee desde cero: nada de lo anterior vuelve.
- **`scope: "cuenta"`**: la cuenta está dada de baja y sus sesiones, cerradas. Entrar después con la misma identidad de Google crea una cuenta nueva y vacía.
- Ya no se puede restaurar: las dos rutas responden como si no hubiera baja (409 y 410).

### Lo que la web debe mostrar

Antes de pedir el código, una confirmación explícita con lo que va a pasar:

- **Desconectar la ficha**: "Dejaremos de leer tu ficha. A las 48 horas revocaremos el acceso a tu cuenta de Google. En un máximo de 30 días borraremos todo lo obtenido de Google: los datos de la ficha, las reseñas, su clasificación, los resúmenes, el historial de notas y los borradores de respuesta. El registro de las respuestas que publicaste se conserva 12 meses. Tu cuenta sigue existiendo y podrás volver a conectar."
- **Borrar la cuenta**: lo mismo, y además: "Cerraremos tu sesión en todos tus dispositivos. A las 48 horas tu cuenta quedará borrada."
- En los dos casos: "Tienes 48 horas para cambiar de idea. Te enviaremos un correo con un enlace para restaurarla."

### Lo que la web NO debe hacer con la baja

- No pongas el código ni el token de restauración en una URL de la API, ni los guardes en `localStorage`.
- No dejes el token en la barra de direcciones después de leerlo.
- No envíes el código ni el token a herramientas de analítica o de registro de errores.
- No des la baja por hecha con el 202 del código: hace falta el 200 de la confirmación.

### Sin correo real

Con `FICHAITOR_FAKE_MAIL=true` no se envía nada. El worker escribe el correo completo en su registro, con el prefijo `[CORREO SIMULADO]`: ahí están el código y el enlace de restauración.

```bash
docker compose logs worker | grep -A12 "CORREO SIMULADO"
```

## Baja de las comunicaciones comerciales (2026-11-13)

Desde el backoffice se envían correos a los clientes (`docs/contrato-backoffice.md`, 10). Los **comerciales** (ofertas, novedades con intención de venta, boletín) llevan un enlace para darse de baja de ellos, como pide el artículo 21 de la LSSI. Los de **servicio** (cambios en las condiciones, incidencias) no lo llevan: llegan a todos.

El enlace es `https://fichaitor.es/baja-comunicaciones#<token>`. El token va en el **fragmento**: no llega a los registros del servidor. Es uno por cuenta, firmado por el backend, solo sirve para esto y no caduca pronto (un correo leído meses después tiene que funcionar).

La baja deja de enviar las comunicaciones **comerciales**. Los correos sobre la cuenta y el servicio (códigos, bajas, facturas, avisos de renovación, comunicaciones de servicio) siguen llegando.

### `POST /api/v1/public/unsubscribe`

Para la página `fichaitor.es/baja-comunicaciones`, que hace la sesión de interfaz. **Sin sesión, sin cookies y sin la cabecera `X-Requested-With`.**

```json
{ "token": "..." }
```

Respuesta **204** sin cuerpo.

- La página lee el token del fragmento (`location.hash`), lo quita de la barra de direcciones (`history.replaceState`) y lo envía en el cuerpo. Sin token en el fragmento, no llames a la API.
- **Siempre 204**, sea el token válido, ya usado, de una cuenta que se dio de baja, de otro secreto o no sea un token. La respuesta no dice nada de ninguna cuenta. Enseña siempre el mismo texto: «Hecho: no te enviaremos más comunicaciones comerciales. Seguirás recibiendo los correos necesarios de tu servicio». 429 → «Demasiados intentos…»; 503 o error de red → «Ahora mismo no podemos…», con reintento.
- **Idempotente:** repetirla no cambia nada.
- **429** con más de 20 intentos por dirección IP en una hora (`FICHAITOR_UNSUBSCRIBE_RATE_LIMIT`). Texto: «Demasiados intentos. Prueba dentro de un rato.»
- **503** si la base no responde o si falta `FICHAITOR_UNSUBSCRIBE_SECRET`: «Ahora mismo no podemos hacerlo. Prueba otra vez en un minuto.» Ofrece reintentar.
- La página **no** da de baja al abrirse: la baja ocurre al pulsar el botón «Darme de baja de las comunicaciones comerciales», para que los analizadores de enlaces que ejecutan JavaScript no den de baja a nadie. El clic único real (RFC 8058) es `POST /api/v1/public/unsubscribe/one-click`, desde la cabecera `List-Unsubscribe`. La página quita el token de la URL al cargar y llama sin cookies ni `X-Requested-With`. La página no ofrece «volver a darse de alta»: eso se hace con sesión, desde Ajustes (`PUT /api/v1/me/communications`). Puede enlazar a Ajustes con un texto como «¿Te has equivocado? Puedes volver a activarlas en Ajustes».

### `POST /api/v1/public/unsubscribe/one-click?token=<token>`

La URL de la cabecera `List-Unsubscribe` de cada correo comercial. **No es para la web:** la llama el cliente de correo (Gmail, Yahoo, Apple Mail…) cuando el destinatario pulsa «Cancelar suscripción», según el RFC 8058:

```http
List-Unsubscribe: <https://fichaitor.es/api/v1/public/unsubscribe/one-click?token=...>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

- El cliente hace un `POST` con el cuerpo `List-Unsubscribe=One-Click` (`application/x-www-form-urlencoded` o `multipart/form-data`). Sin cookies ni cabeceras nuestras: no pide sesión ni la cabecera de protección.
- Aquí el token tiene que ir en la URL (el cuerpo lo fija el RFC), así que puede quedar en el registro de acceso de nginx. Filtrado, lo único que permite es dar de baja de las comerciales a esa cuenta.
- Mismas respuestas que la ruta de la página: **204** siempre, **429** con más de 600 llamadas por IP en una hora (`FICHAITOR_UNSUBSCRIBE_ONE_CLICK_RATE_LIMIT`; los proveedores de correo llaman desde pocas direcciones) y **503**.
- Un `GET` responde **405** y no da de baja: los analizadores de enlaces de los servidores de correo abren las URL y no deben dar de baja a nadie.
- Sin `mailto:` en `List-Unsubscribe`: una baja por correo a soporte@ sería manual.

### Qué se guarda

Una fila por cuenta en `communication_optout`: el id de la cuenta, la fecha y el medio: la página (`enlace`), el cliente de correo (`un_clic`), la casilla del alta (`signup`) o Ajustes (`settings`). Ni el token, ni la IP. Se borra con la cuenta (`docs/retencion.md`).

### Lo que la web NO debe hacer con la baja de comunicaciones

- No envíes el token a herramientas de analítica o de registro de errores, ni lo guardes.
- No muestres un mensaje distinto si el token no vale: la API no lo dice a propósito.
- No pongas la página detrás del inicio de sesión.

## Comunicaciones comerciales: en el alta y en Ajustes (2026-11-13)

El artículo 21.2 de la LSSI pide ofrecer al cliente oponerse a las comunicaciones comerciales **al recoger su correo** y que pueda cambiarlo **en cualquier momento**. Hay dos sitios: una casilla antes de «Conectar con Google» y un interruptor en Ajustes. Los dos escriben en la misma tabla que el enlace de cada correo ([Baja de las comunicaciones comerciales](#baja-de-las-comunicaciones-comerciales-2026-11-13)), y el envío solo mira esa tabla.

Sin oposición, la cuenta recibe las comerciales por interés legítimo (artículo 21.2): no hace falta un consentimiento previo, así que **la casilla no viene marcada**. Los correos de servicio (códigos, bajas, facturas, avisos, comunicaciones de servicio) llegan siempre.

### En el alta: la casilla y `commercial_optout=1`

No hay formulario de registro: la cuenta se crea al volver de «Conectar con Google». Junto al botón, en la pantalla de conectar, pon una casilla **sin marcar** con este texto:

> No quiero recibir novedades ni ofertas de Fichaitor por correo. Los correos de servicio te llegarán igual.

- Marcada: el botón navega a `/api/v1/google/oauth/start?commercial_optout=1`. Sin marcar: a `/api/v1/google/oauth/start`, sin el parámetro (o con `commercial_optout=0`).
- El valor viaja **firmado dentro del `state`** de OAuth, el mismo que se guarda en la cookie `HttpOnly` del flujo. No hay otra cookie ni otro parámetro: cambiarlo a la vuelta de Google invalida el `state` (400, «La conexión ha caducado. Vuelve a empezar.»).
- **Solo cuenta si la vuelta crea la cuenta.** Se guarda en la misma escritura que crea la cuenta (medio `signup`). Si la cuenta ya existía (alguien que vuelve a entrar), la casilla se ignora y su preferencia no cambia, esté marcada o no.
- Un valor que no sea booleano (`commercial_optout=quizas`) responde **422** antes de ir a Google. Envía solo `1`.
- La vuelta no cambia (`/app?google=ok&fichas=…`): no dice si se guardó la oposición. Si quieres confirmarlo, lee `GET /api/v1/me/communications`.
- Muestra la casilla donde la persona conecta por primera vez (la pantalla de alta). En un botón que solo sirve para volver a entrar no hace falta: no tendría efecto.

### `GET /api/v1/me/communications`

```json
{ "commercial": false, "changed_at": "2026-11-13T10:42:07Z", "source": "signup" }
```

- `commercial`: `true` si la cuenta recibe las comunicaciones comerciales; `false` si se opuso.
- `changed_at` y `source`: el último cambio. Los dos `null` si nunca cambió (recibe las comerciales por interés legítimo).
- `source`: con `commercial: false`, por dónde se opuso: `signup` (la casilla del alta), `settings` (Ajustes), `enlace` (la página de baja de un correo) o `un_clic` (el botón «Cancelar suscripción» del cliente de correo). Con `commercial: true`, `settings` si la última vez la volvió a activar en Ajustes.
- **401** sin sesión. **503** si la base no responde: «Ahora mismo no podemos leerlo. Prueba otra vez en un minuto.»

### `PUT /api/v1/me/communications`

Con la cabecera `X-Requested-With: fichaitor`, como las demás rutas `/me` que escriben.

```json
{ "commercial": false }
```

- `false`: se opone (medio `settings`). `true`: retira la oposición y vuelve a recibirlas.
- Responde **200** con el mismo cuerpo que el `GET`, ya con el cambio.
- **Idempotente.** Oponerse estando ya opuesto no cambia nada: conserva la fecha y el medio de la primera vez (por ejemplo, `enlace`). Activarlas estando activas tampoco cambia nada.
- `commercial` tiene que ser un booleano JSON: `"false"`, `0`, `null`, un cuerpo vacío o un campo de más responden **422**.
- **401** sin sesión, **403** sin la cabecera, **503** si la base no responde: «Ahora mismo no podemos guardarlo. Prueba otra vez en un minuto.» Ofrece reintentar y deja el interruptor como estaba.
- **Quién.** Solo la cuenta de la sesión, y una sesión web solo se abre conectando con la cuenta de Google del titular. Los encargados de un negocio solo existen como canales de chat (Telegram, WhatsApp) y no llegan a estas rutas.
- Responde también con una baja programada y con la cuenta bloqueada por un pago (402 `account_blocked` en otras rutas): oponerse tiene que poder hacerse siempre.

### Lo que la web debe mostrar en Ajustes

Un interruptor en **Ajustes**, en un apartado propio (por ejemplo, «Correos de Fichaitor»), separado de Facturación y de la baja de la cuenta:

- Etiqueta: «Recibir novedades y ofertas de Fichaitor por correo». Encendido = `commercial: true`.
- Debajo: «Los correos de servicio (avisos de tu cuenta, facturas y cambios en el servicio) te llegarán siempre.»
- Con `commercial: false`, puedes decir cuándo y cómo se desactivó a partir de `changed_at` y `source` (por ejemplo, «Desactivado el 13 de noviembre desde un correo»).
- Al cambiarlo, haz el `PUT` y pinta lo que responda. Sin pedir confirmación.

### Lo que la web NO debe hacer con estas preferencias

- No marques la casilla del alta por defecto ni la escondas.
- No guardes la casilla en una cookie, en `localStorage` ni en otro parámetro: solo `commercial_optout=1` en la URL de inicio.
- No la uses para los correos de servicio: no se pueden desactivar.

## Reseñas y respuestas (2026-11-12)

Primera función que **escribe en Google**: el dueño ve las reseñas de su ficha, pide una respuesta redactada si quiere y **publica la que confirma**. Una respuesta es pública y Google avisa a quien escribió la reseña: no se puede deshacer, solo editar o borrar después.

Las cuatro rutas trabajan sobre la **ficha elegida de la cuenta de la sesión** y sobre ninguna otra. Los identificadores (`rev_…`, `act_…`) son propios de cada cuenta: no sirven en otra.

> **Multinegocio (2026-11-12):** la lista admite `?location=loc_…` y su respuesta lleva `location_id` (consulta [La ficha de cada lectura](#la-ficha-de-cada-lectura-location)). El borrador, la publicación y el estado del envío ya identifican la ficha por la reseña o la acción: valen para una reseña de **cualquier ficha activa** de la cuenta, elegida o no. Una reseña de una ficha inactiva da 404. El tope de borradores redactados es por ficha.

| Ruta | Qué hace | Llama a |
|---|---|---|
| `GET /api/v1/me/reviews` | Lista las reseñas guardadas | Nada |
| `POST /api/v1/me/reviews/{id}/draft` | Devuelve una respuesta redactada por el modelo | El modelo |
| `POST /api/v1/me/reviews/{id}/reply` | **Publica**: registra la confirmación del dueño | Nada: el worker la envía a Google |
| `GET /api/v1/me/actions/{id}` | Estado del envío | Nada |

Las dos rutas `POST` llevan la cabecera `X-Requested-With: fichaitor`. Sin ella, 403, haya sesión o no.

### `GET /api/v1/me/reviews`

Parámetros de consulta, los dos opcionales:

| Parámetro | Valores | Por defecto |
|---|---|---|
| `filter` | `unanswered` (solo las que no tienen respuesta) o `all` | `unanswered` |
| `limit` | De 1 a 200 | 50 |
| `location` | `loc_…`, una ficha activa de la cuenta (2026-11-12) | La ficha preferida |

Respuesta 200:

```json
{
  "status": "ok",
  "synced_at": "2026-11-12T09:58:39Z",
  "stale": false,
  "total": 13,
  "reviews": [
    {
      "id": "rev_3f2a9c1d7b6e5a40",
      "stars": 1,
      "text": "Esperamos cuarenta minutos y la comida llegó fría.",
      "created_at": "2026-10-03T18:21:07Z",
      "replied": false,
      "reply": null
    },
    {
      "id": "rev_9b1c2d3e4f5a6b7c",
      "stars": 5,
      "text": "",
      "created_at": "2026-09-14T11:02:44Z",
      "replied": true,
      "reply": { "text": "Gracias por tu visita.", "updated_at": "2026-09-15T08:30:00Z" }
    }
  ]
}
```

| Campo | Tipo | Qué es |
|---|---|---|
| `status` | texto | `ok`, `pendiente`, `needs_reconnect` o `error`. Los mismos valores y el mismo significado que en [Estados del resumen](#estados-del-resumen) |
| `synced_at` | fecha y hora UTC, o `null` | Cuándo se guardaron las reseñas por última vez |
| `stale` | booleano | `true` si tienen más de 6 horas. La API ya ha pedido una actualización |
| `total` | entero | Cuántas reseñas cumplen el filtro, antes de aplicar `limit` |
| `reviews` | lista | Como mucho `limit` reseñas, ya ordenadas |
| `reviews[].id` | texto | `rev_` seguido de letras y números. Es el que va en las rutas de borrador y de publicar |
| `reviews[].stars` | entero de 1 a 5, o `null` | `null` si Google no da las estrellas |
| `reviews[].text` | texto | Texto de la reseña en su idioma original, sin la traducción automática. Vacío si la reseña solo tiene estrellas |
| `reviews[].created_at` | fecha y hora UTC, o `null` | Cuándo se escribió la reseña |
| `reviews[].replied` | booleano | Si tiene respuesta del propietario |
| `reviews[].reply` | objeto o `null` | `text` y `updated_at` (puede ser `null`) de la respuesta. `null` si no hay respuesta |

- **Orden:** primero las que no tienen respuesta. Dentro de cada grupo, las de menos estrellas primero y, a igualdad de estrellas, las más recientes.
- **Sin autor:** la respuesta nunca trae el nombre ni la foto de quien escribió la reseña. La API no los guarda. No los pidas ni los muestres.
- Pinta `text` y `reply.text` como texto, nunca como HTML. Los escribe un tercero.
- Con `pendiente` la lista va vacía: el worker aún no ha guardado las reseñas. Sondea como el resumen: cada 3 segundos, 10 veces como máximo. También sale `pendiente` la primera vez que se abre la lista de una ficha sincronizada antes del 2026-11-12, hasta su siguiente sincronización, que la API pide al momento.
- Con `needs_reconnect` y `error`, la lista trae las reseñas de la última lectura correcta, o va vacía si nunca la hubo.
- No hay paginación: si `total` es mayor que `limit`, sube `limit` (200 como máximo).

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha, o Google ha dejado de listar la elegida |
| 422 | `filter` o `limit` no válidos |

### `POST /api/v1/me/reviews/{id}/draft`

Sin cuerpo. Devuelve una respuesta redactada por el modelo para esa reseña. **Pedir un borrador no publica nada.**

Respuesta 200:

```json
{
  "review_id": "rev_3f2a9c1d7b6e5a40",
  "text": "Sentimos mucho que tu visita no fuera como esperabas. Tomamos nota de lo que cuentas para mejorar. Nos gustaría hablarlo contigo en privado para entender bien qué pasó.",
  "cached": false
}
```

| Campo | Tipo | Qué es |
|---|---|---|
| `review_id` | texto | La reseña del borrador |
| `text` | texto | De 40 a 600 caracteres, un solo párrafo, sin formato |
| `cached` | booleano | `true` si es el borrador guardado de una petición anterior. No ha costado nada ni cuenta para el límite |

- El modelo recibe solo las estrellas y el texto de la reseña. El texto es un dato: si contiene instrucciones, no se obedecen.
- El borrador pasa una validación en código antes de salir (tono, longitud, sin datos de contacto, sin ofertas ni promesas). Si no la pasa, la API responde 422 y **el dueño escribe su respuesta**.
- La API guarda el último borrador de cada reseña. Pedirlo otra vez sin que la reseña haya cambiado devuelve el mismo, con `cached: true`.
- Funciona también con una reseña ya respondida, para proponer una respuesta nueva.
- Puede tardar hasta 20 segundos. No reintentes sola.
- Muestra el borrador en un campo que el dueño pueda editar. Es una propuesta, no la respuesta.

Errores, todos con `detail` en español listo para mostrar:

| Código | Cuándo | Qué hace la web |
|---|---|---|
| 401 | Sin sesión | Lleva a conectar |
| 403 | Falta la cabecera `X-Requested-With` | — |
| 404 | La reseña no está en la ficha elegida de esa cuenta: no existe, Google ya no la devuelve, es de otra ficha o de otra cuenta. La respuesta es la misma en todos los casos | Vuelve a pedir la lista |
| 422 | No hay borrador que proponer: el del modelo no ha pasado la validación, o la reseña no tiene estrellas | Deja el campo vacío para que el dueño escriba la suya |
| 429 | Más de 30 borradores nuevos en 24 horas en esa cuenta (`FICHAITOR_REVIEW_DRAFT_RATE_LIMIT`) | Deja escribir la respuesta a mano |
| 503 | El modelo no responde, o la redacción no está configurada en ese entorno | Deja escribir la respuesta a mano y ofrece reintentar el borrador |

### `POST /api/v1/me/reviews/{id}/reply`

**Publica.** Esta petición es la confirmación del dueño: el texto que lleva es el que se envía a Google, tal cual.

```json
{ "text": "Sentimos mucho la espera. Nos gustaría hablarlo contigo en privado." }
```

Respuesta **202**:

```json
{
  "id": "act_51b0c7e2a9d84f36",
  "type": "reply_review",
  "review_id": "rev_3f2a9c1d7b6e5a40",
  "status": "pendiente",
  "message": "Tu respuesta está en cola para enviarse a Google.",
  "error": null,
  "confirmed_at": "2026-11-12T10:04:11Z",
  "finished_at": null
}
```

El cuerpo tiene la misma forma que `GET /api/v1/me/actions/{id}`. `id` es el identificador de la acción: úsalo para seguir el envío.

- **202 no significa "publicada".** La API ha registrado la confirmación y ha encolado el envío. Quien escribe en Google es el worker.
- **El texto no se toca.** La API no recorta espacios, no corrige ni normaliza: lo que llega a Google es exactamente lo que se envió en `text`, saltos de línea incluidos. Si quieres quitar espacios sobrantes, hazlo antes de enseñárselo al dueño, no después de que confirme.
- **Límite de longitud: 4.096 bytes en UTF-8**, el de Google para `ReviewReply.comment` ([referencia de la API](https://developers.google.com/my-business/reference/rest/v4/accounts.locations.reviews#ReviewReply)). Son bytes, no caracteres: una letra con tilde o una eñe ocupan 2 y un emoji, 4. Un contador de caracteres con tope en 4.000 se queda del lado seguro; para ser exacto, cuenta `new TextEncoder().encode(texto).length`.
- No se admite un texto vacío o solo con espacios, ni caracteres de control (se admiten el salto de línea y el tabulador).
- **Responder a una reseña ya respondida edita la respuesta.** Google usa la misma operación para crear y para sustituir: la respuesta nueva reemplaza a la anterior. La web debe decirlo ("Vas a sustituir tu respuesta actual") cuando `replied` es `true`.
- **Una respuesta en vuelo por reseña.** Mientras una acción de esa reseña está `pendiente` o `en_curso`, otra petición responde 409. Desactiva el botón al pulsarlo.
- Google solo admite respuestas en fichas verificadas. Si la ficha no lo está, la acción acaba `fallida`.

Errores, todos con `detail` en español listo para mostrar, salvo el 422 de un cuerpo mal formado:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 403 | Falta la cabecera `X-Requested-With` |
| 404 | La reseña no está en la ficha elegida de esa cuenta. La misma respuesta para una reseña de otra cuenta y para una que no existe |
| 409 | Ya hay una respuesta enviándose para esa reseña |
| 422 | Texto vacío, de más de 4.096 bytes o con caracteres no admitidos. Si falta `text`, no es un texto o pasa de 16.384 caracteres, `detail` es una lista: muestra un mensaje genérico |
| 503 | No se ha podido registrar la confirmación. **No se ha creado ninguna acción**: el dueño puede volver a pulsar |

### `GET /api/v1/me/actions/{id}`

Estado de una acción de esa cuenta. Desde las fechas clave hay dos tipos: `reply_review` y `set_special_hours` (un cambio de horario de un día; entonces `review_id` es `null` y `message` habla del horario). Respuesta 200:

```json
{
  "id": "act_51b0c7e2a9d84f36",
  "type": "reply_review",
  "review_id": "rev_3f2a9c1d7b6e5a40",
  "status": "hecha",
  "message": "Enviada a Google. Puede tardar unos minutos en verse en tu ficha.",
  "error": null,
  "confirmed_at": "2026-11-12T10:04:11Z",
  "finished_at": "2026-11-12T10:04:14Z"
}
```

| `status` | Qué significa | `message` |
|---|---|---|
| `pendiente` | Confirmada; el worker aún no la ha tomado | "Tu respuesta está en cola para enviarse a Google." |
| `en_curso` | El worker la está enviando, o Google ha fallado y se va a reintentar | "Estamos enviando tu respuesta a Google." |
| `hecha` | Google ha aceptado la respuesta | "Enviada a Google. Puede tardar unos minutos en verse en tu ficha." |
| `fallida` | No se ha enviado y no se va a reintentar | Según `error` |

`error` solo tiene valor con `fallida`:

| `error` | Cuándo | `message` |
|---|---|---|
| `reconnect` | Google ha retirado el permiso (401 o `invalid_grant`) | "Tu acceso a Google ha caducado; vuelve a conectar." |
| `rejected` | Google ha rechazado la respuesta (4xx): por ejemplo, la ficha no está verificada | "Google no ha aceptado la respuesta. Revisa el texto y vuelve a enviarla." |
| `review_gone` | La reseña ya no existe en Google (404) | "Esa reseña ya no está en Google. No hay nada que responder." |
| `unavailable` | Google ha fallado en todos los intentos (5xx, 429 o sin respuesta) | "No hemos podido enviar tu respuesta a Google. Prueba otra vez en unos minutos." |
| `suspended` | La cuenta confirmó una baja antes de que la respuesta saliera. No se envía nada | "No se ha enviado: tienes una baja programada." |
| `internal` | Fallo de nuestro lado: por ejemplo, la ficha ya no está en la conexión | "No hemos podido enviar tu respuesta. Prueba otra vez en unos minutos." |

- Muestra `message` tal cual. Distingue los casos por `status` y `error`, no por el texto.
- **Di "enviada", nunca "publicada".** Google revisa las respuestas antes de mostrarlas y puede tardar. Con `hecha`, el texto correcto es el de `message`.
- Con `reconnect`, ofrece además el botón de conectar con Google.
- `confirmed_at`: cuándo confirmó el dueño. `finished_at`: cuándo acabó, o `null` si sigue en curso.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 404 | La acción no existe o es de otra cuenta. La misma respuesta en los dos casos |

#### Sondeo del estado de la acción

- Tras el 202, pide `GET /api/v1/me/actions/{id}` **cada 2 segundos, 15 veces como máximo** (30 segundos). Lo normal es que pase a `hecha` en menos de 5 segundos.
- Deja de preguntar en cuanto el estado sea `hecha` o `fallida`.
- Si a los 30 segundos sigue `pendiente` o `en_curso`, deja de sondear y muestra "Seguimos enviando tu respuesta a Google. Puedes seguir; te lo confirmamos aquí." Vuelve a preguntar una vez por minuto, 5 veces como máximo, o cuando el dueño vuelva a la pantalla. El worker reintenta solo durante minuto y medio como mucho; después la acción queda `fallida`.
- Tras `hecha`, vuelve a pedir `GET /api/v1/me/reviews` y `GET /api/v1/me/summary`: la reseña ya sale respondida y `unanswered_count` baja. No hace falta esperar a otra sincronización.
- Si el worker está parado, la acción se queda en `pendiente`. No es un error de la web.

### Lo que la web NO debe hacer

- **No publiques sin que el dueño vea y confirme el texto exacto.** `POST …/reply` solo se llama desde un botón que el dueño pulsa con el texto delante, tal como se va a enviar. Nunca encadenes borrador y publicación en un solo paso, ni publiques "todas a la vez" sin enseñar cada texto.
- **No envíes en `text` nada distinto de lo que el dueño tiene en pantalla.** Si el dueño edita el borrador, se envía lo editado.
- **No reintentes una publicación por tu cuenta.** Ni ante un 503, ni ante un error de red, ni ante una acción `fallida`, ni porque el sondeo tarde. El worker ya reintenta lo que se puede reintentar. Una publicación nueva la decide el dueño volviendo a pulsar.
- **No des por publicada una respuesta por haber recibido el 202.** El resultado lo dice la acción.
- **No uses el borrador como si fuera la respuesta.** Hasta que el dueño confirma, no hay nada enviado.
- No guardes reseñas, borradores ni respuestas en `localStorage` ni similares. Son contenido de terceros y caducan. Un borrador sin enviar puede vivir en memoria mientras la pantalla está abierta.
- No muestres ni pidas el nombre de quien escribió la reseña.
- No pintes como HTML ningún texto de estas rutas.

### Sin Google real

- Con `FICHAITOR_FAKE_GOOGLE=true`, listar, publicar y seguir la acción funcionan con la ficha de demostración (13 reseñas, 9 sin responder). Nada sale hacia Google.
- El borrador usa el mismo modelo que la demostración por captura. Con `FICHAITOR_FAKE_SCREENSHOT=true` devuelve respuestas de ejemplo, sin clave y sin coste. Sin esa variable y sin `FICHAITOR_GEMINI_API_KEY`, el borrador responde 503 y el resto funciona igual.
- Con base de datos, la API encola y el worker ejecuta: hacen falta los dos servicios en marcha y el worker con `FICHAITOR_FAKE_GOOGLE=true`. Sin base de datos, la API lo resuelve en memoria y la acción sale `hecha` en la misma respuesta del 202.

## Sentimiento del establecimiento (2026-11-12)

Cómo ve la gente el negocio, a partir de las reseñas de la ficha elegida: una nota de 0 a 100 con su historia, los temas que más se elogian y los que más quejas reciben, un resumen y una sugerencia. Es un indicador propio del panel, aparte del diagnóstico y del resumen.

La API no llama a Google ni al modelo en esta ruta: lee el análisis que el worker guardó. Por eso responde rápido y no tiene límite de uso. La fórmula, los temas y los disparadores están en [sentimiento.md](sentimiento.md).

### `GET /api/v1/me/sentiment`

Con sesión. Devuelve el análisis de una ficha activa de esa cuenta: la de `?location=loc_…` o, sin el parámetro, la preferida. Ningún otro parámetro elige la ficha. La respuesta lleva `location_id` (2026-11-12). Consulta [La ficha de cada lectura](#la-ficha-de-cada-lectura-location).

Respuesta 200:

```json
{
  "status": "ok",
  "based_on": 13,
  "with_text": 9,
  "period": "últimos 12 meses",
  "overall": "positivo",
  "score": 86,
  "previous_score": 82,
  "delta": 4,
  "history": [{ "month": "2026-10", "score": 82 }, { "month": "2026-11", "score": 86 }],
  "summary": "A tus clientes les gusta sobre todo el trato del personal. La espera es la queja que más se repite.",
  "praised": [{ "topic": "Trato del personal", "mentions": 7 }],
  "complaints": [{ "topic": "Tiempo de espera", "mentions": 2 }],
  "suggestion": "Avisa a cada cliente del tiempo de espera previsto en cuanto llegue.",
  "generated_at": "2026-11-12T10:00:00Z",
  "next_update": "Se actualiza con 3 reseñas nuevas, con una de 1 o 2 estrellas o a los 30 días.",
  "provisional": false
}
```

| Campo | Tipo | Qué es |
|---|---|---|
| `status` | texto | `ok`, `pendiente`, `insuficiente` o `error`. Consulta [Estados del sentimiento](#estados-del-sentimiento) |
| `based_on` | entero o `null` | Reseñas en las que se basa la nota. Con `insuficiente` vale `0` |
| `with_text` | entero o `null` | De esas, cuántas tienen texto. Las demás solo tienen estrellas |
| `period` | texto o `null` | Periodo del que salen los temas: `"últimos 12 meses"` o, si en ese periodo hay pocas reseñas con texto, `"todas las reseñas"`. Muéstralo tal cual |
| `overall` | texto o `null` | `positivo`, `mixto` o `negativo`, según la nota |
| `score` | entero o `null` | Nota de 0 a 100. La calcula el código, no el modelo |
| `previous_score` | entero o `null` | Nota del mes anterior con análisis. `null` si no hay ninguno en los últimos 12 meses |
| `delta` | entero o `null` | `score - previous_score`, en puntos. Puede ser negativo o `0`. `null` si no hay mes anterior |
| `history` | lista | Una nota por mes, de más antiguo a más reciente, con `month` en formato `"AAAA-MM"`. Como máximo los últimos 12 meses, el actual incluido. Solo aparecen los meses en los que hubo análisis. Vacía con `pendiente`, `insuficiente` y `error` |
| `summary` | texto o `null` | Dos frases como máximo, más una tercera fija cuando hay valoraciones de 1 o 2 estrellas sin comentario ("Hay 1 valoración de 1 o 2 estrellas sin comentario."). Texto sin formato, listo para mostrar |
| `praised` | lista | Hasta 5 temas elogiados, de más a menos menciones: `topic` (etiqueta en español de una lista cerrada) y `mentions` (número de reseñas que lo mencionan; nunca supera `based_on`). Puede ir vacía |
| `complaints` | lista | Lo mismo para los temas con quejas. Puede ir vacía |
| `suggestion` | texto o `null` | Una acción concreta para el dueño. La elige el código por reglas: si hay reseñas sin responder, propone responderlas (primero las de 1 o 2 estrellas) y dice cuántas. Puede tener dos frases. Texto sin formato |
| `generated_at` | fecha y hora UTC, o `null` | Cuándo se hizo el análisis |
| `next_update` | texto o `null` | Frase en español lista para mostrar: cuándo se vuelve a calcular |
| `provisional` | booleano | `true` cuando hay menos de 5 reseñas con texto: la nota existe, pero se apoya en poco. Consulta [Análisis provisional](#análisis-provisional) |

`status`, `based_on`, `overall`, `score`, `summary`, `praised`, `complaints`, `suggestion`, `generated_at` y `period` no cambian de nombre ni de tipo. `with_text`, `previous_score`, `delta`, `history`, `next_update` y `provisional` son añadidos del 2026-11-12.

#### Estados del sentimiento

| `status` | Qué significa | Qué trae | Qué hace la web |
|---|---|---|---|
| `ok` | Hay análisis. Basta con una reseña | Todos los campos. `praised`, `complaints`, `previous_score` y `delta` pueden ir vacíos o a `null` | Muestra el bloque. Si `provisional` es `true`, indícalo |
| `pendiente` | Todavía no hay análisis: el worker no ha terminado la primera sincronización o el análisis que va detrás | Todo a `null`, listas vacías y `provisional: false` | Muestra "Analizando tus reseñas" y sondea |
| `insuficiente` | La ficha no tiene ninguna reseña | `based_on: 0`, `with_text: 0`, `summary` y `suggestion` con un mensaje útil, `generated_at`, `next_update` y `provisional: true`. Sin nota: `score`, `overall` y `period` a `null`, y `history` vacía | Muestra `summary` y `suggestion`. No pintes nota ni tendencia |
| `error` | No se ha podido leer la ficha (el resumen está en `error` o `needs_reconnect`) o el análisis falló y nunca hubo uno bueno | Todo a `null` y listas vacías | Muestra "No hemos podido analizar tus reseñas" con un botón para reintentar. Si el resumen pide reconectar, basta con ese aviso |

Un análisis bueno no se sustituye por un error: si un recálculo falla, la ruta sigue devolviendo el último `ok`.

#### Análisis provisional

Con al menos una reseña ya hay nota. Las reseñas sin texto cuentan por sus estrellas: 4 y 5 positiva, 3 neutra, 1 y 2 negativa.

Con `provisional: true`:

- `score`, `overall` e `history` se calculan igual.
- `praised` y `complaints` traen lo que haya. Pueden ir vacías.
- `summary` es un texto fijo y honesto sobre la base, por ejemplo: "Basado en 3 valoraciones; aún hay pocas con texto para ver de qué habla la gente."
- `suggestion` propone conseguir más reseñas con el enlace de la ficha, salvo que haya reseñas sin responder: entonces propone responderlas.

#### Sondeo

- **Pide `GET /api/v1/me/sentiment` al entrar al panel**, también justo después de conectar con Google y elegir ficha, a la vez que el resumen. El primer análisis se lanza en cuanto termina la primera sincronización, sin esperas: lo normal es que llegue unos segundos después del resumen.
- Con `pendiente`, vuelve a pedirlo **cada 3 segundos, 10 veces como máximo**, igual que el resumen.
- Si tras 10 intentos sigue en `pendiente`, deja de preguntar y muestra "Estamos analizando tus reseñas. Vuelve en unos minutos", con un botón para reintentar.
- Con `ok`, `insuficiente` y `error` no sondees. El análisis cambia, como mucho, una vez al día.
- Pedir esta ruta mientras no hay análisis pide la sincronización con las mismas reglas que el resumen ([Cuándo se sincroniza](#cuándo-se-sincroniza)). Pedir las tres rutas a la vez no lanza tres.

> **Nota:** Una ficha sincronizada antes del 2026-11-12 no tiene análisis. La primera petición a esta ruta pide una sincronización y responde `pendiente` hasta que el worker termina.

#### Cuándo se vuelve a calcular

El primer análisis no tiene tope. Después, como máximo uno cada 24 horas, con lo primero que ocurra:

- Una reseña nueva de 1 o 2 estrellas.
- 3 reseñas nuevas desde el último análisis.
- 30 días desde el último análisis, si hay al menos una reseña nueva.

Se comprueba al terminar cada sincronización de la ficha. `next_update` lo dice en una frase; si cambian los umbrales, cambia la frase. No la escribas a mano en la web.

Errores:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 409 | Aún no se ha elegido ficha, o Google ha dejado de listar la elegida. Lleva a la lista de fichas |

Cada cuenta ve solo el sentimiento de su ficha elegida. Dos cuentas que administran la misma ficha de Google tienen análisis separados.

### Lo que la web NO debe hacer con el sentimiento

- No calcules la nota, los recuentos ni `delta` por tu cuenta. Muestra los que llegan.
- No pintes como HTML `summary`, `suggestion`, `topic`, `period` ni `next_update`.
- No guardes la respuesta en `localStorage` ni similares. Tenla en memoria mientras la pantalla está abierta.
- No esperes textos de reseñas ni autores en esta ruta: no los trae.

### Sin Google real ni modelo

- Con `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_SCREENSHOT=true`, el recorrido completo funciona con la ficha de demostración (13 reseñas, 10 con texto): clasificación, nota, temas, resumen e historia. Nada sale hacia Google ni hacia el modelo.
- Sin `FICHAITOR_FAKE_SCREENSHOT` y sin `FICHAITOR_GEMINI_API_KEY`, el análisis se hace igual, solo con las estrellas: hay nota, pero `praised` y `complaints` van vacíos y los textos salen de reglas fijas.
- Con base de datos, el análisis lo hace el worker: hacen falta los dos servicios en marcha, y el worker con esas mismas variables. Sin base de datos, la API lo resuelve en memoria al elegir la ficha.

## Canales de mensajería: Telegram (2026-11-12) y WhatsApp (2026-11-13)

El dueño vincula su Telegram o su WhatsApp desde el panel y, desde ese momento, recibe por ese canal las reseñas nuevas con una respuesta propuesta y la publica con un botón. Todo el detalle del lado del servidor está en `docs/canales.md`.

> **WhatsApp (2026-11-13):** un solo número de Fichaitor para todos los clientes. **Solo avisa de las fichas Pro+** (`plan: "pro_whatsapp"`); de las demás avisan Telegram o el panel. Hay **80 mensajes nuestros al mes por ficha Pro+**, sumados en una bolsa de la cuenta (`whatsapp` de `GET /api/v1/me`). Al agotarse, no sale nada más por WhatsApp hasta el día 1: los avisos llegan por Telegram, si está vinculado, o se quedan en el panel. Ofrece el botón de WhatsApp solo si `whatsapp_url` no es `null` y la cuenta tiene alguna ficha con `features.whatsapp: true`: sin Pro+, un WhatsApp vinculado no recibe nada (ni la bienvenida, porque su bolsa es 0).

| Ruta | Qué hace | Cabecera `X-Requested-With` |
|---|---|---|
| `POST /api/v1/channels/link-code` | Pide un código de vinculación, con las fichas que verá el chat | Sí |
| `GET /api/v1/me/channels` | Lista los canales vinculados, con las fichas que ve cada uno | No |
| `PUT /api/v1/me/channels/{id}/locations` | Cambia las fichas que ve un canal (multinegocio, 2026-11-12) | Sí |
| `DELETE /api/v1/me/channels/{id}` | Desvincula un canal | Sí |

Las rutas `POST /api/v1/webhooks/telegram` y `GET` y `POST /api/v1/webhooks/whatsapp` son de Telegram y de Meta, no de la web: la web no las llama nunca.

### `POST /api/v1/channels/link-code`

El cuerpo es opcional. Sin cuerpo, con `{}` o con `{"locations": null}`, el chat verá **todas** las fichas activas de la cuenta, también las que se activen después. Con una lista, solo esas (el caso del encargado de un negocio):

```json
{ "locations": ["loc_9b1c"] }
```

- Cada `id` tiene que ser una ficha **activa** de la cuenta. Entre 1 y 50; un `id` repetido cuenta una vez.
- El filtro se guarda con el código y pasa al chat en el momento de vincularlo: ese chat no recibe nada de las otras fichas, ni siquiera la bienvenida (que solo nombra sus fichas).
- Un filtro que no vale no gasta ninguno de los 5 códigos de la hora.

Respuesta 200:

```json
{
  "code": "042817",
  "expires_at": "2026-11-12T10:14:11Z",
  "telegram_url": "https://t.me/FichaitorBot?start=Qm3v0X5k1y2Zt8pW-aR4sLd9_fHj6cNe7uBoVgIiT0Q",
  "whatsapp_url": "https://wa.me/34600000000?text=Vincular%20Fichaitor%20Qm3v0X5k1y2Zt8pW-aR4sLd9_fHj6cNe7uBoVgIiT0Q"
}
```

- **Un código para los dos canales y de un solo uso.** Sirve abriendo el enlace o tecleando los 6 dígitos en el chat; lo que llegue primero lo gasta.
- **El enlace no lleva los 6 dígitos.** Lleva un testigo largo y aleatorio (43 caracteres de `A-Z`, `a-z`, `0-9`, `_` y `-`). Los 6 dígitos son solo para teclearlos. No construyas el enlace con `code`: usa `telegram_url` tal cual.
- **Caduca a los 10 minutos** (`expires_at`). Muestra la cuenta atrás y ofrece pedir otro al llegar a cero.
- **Pedir otro invalida el anterior,** enlace incluido.
- `whatsapp_url` abre un chat con el número de Fichaitor con el texto «Vincular Fichaitor <testigo>» ya escrito: el dueño solo pulsa **Enviar**, y ese teléfono queda vinculado. Lleva el mismo testigo que `telegram_url`, nunca los 6 dígitos. Es `null` mientras WhatsApp no esté configurado en el servidor (como hasta ahora). Úsalo tal cual: no lo construyas con `code`.
- **El mismo código sirve en los dos canales**, y el primero que lo usa lo gasta. Los 6 dígitos también se pueden escribir en el chat de WhatsApp.
- Ni la API ni la base de datos guardan el código ni el testigo: solo su huella. Si el dueño cierra la pantalla, hay que pedir otro.

Qué enseñar: un botón "Abrir Telegram" con `telegram_url` (en el móvil abre la app; en el ordenador, Telegram Web o un código QR del mismo enlace) y, debajo, "o envía este código al bot: 042817". Con `whatsapp_url` (y alguna ficha Pro+), un segundo botón "Abrir WhatsApp" con ese enlace (en el ordenador, un código QR del mismo enlace) y el texto «Pulsa Enviar en WhatsApp sin cambiar el mensaje».

Errores, todos con `detail` en español listo para mostrar:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 403 | Falta la cabecera `X-Requested-With` |
| 404 | Una ficha de `locations` no es de la cuenta, no existe o no tiene la forma `loc_…`: "Esa ficha no está en tu cuenta." |
| 409 | La cuenta tiene una baja programada, como el resto de rutas de la ficha |
| 422 | Una ficha de `locations` está inactiva ("Esa ficha no está activa."), o el cuerpo no vale: lista vacía, más de 50, un campo de más (entonces `detail` es una lista) |
| 429 | Más de 5 códigos en una hora en esa cuenta (`FICHAITOR_LINK_CODE_RATE_LIMIT`) |
| 503 | No se ha podido generar el código. Ofrece reintentar |

#### Cómo saber que ya se ha vinculado

La vinculación ocurre en Telegram o en WhatsApp, no en la web. Mientras la pantalla del código está abierta, consulta `GET /api/v1/me/channels` cada 3 segundos y para cuando aparezca un canal nuevo, cuando caduque el código o a los 10 minutos. El bot da la bienvenida en el chat ("Listo, Marta. Ya llevo la ficha de …").

### `GET /api/v1/me/channels`

Respuesta 200:

```json
{
  "channels": [
    {
      "id": "chid_5f1c9a0e7b2d4c63",
      "channel": "telegram",
      "address": "Telegram de ••••7342",
      "linked_at": "2026-11-12T10:05:02Z",
      "locations": null,
      "locations_label": "Todos tus negocios"
    },
    {
      "id": "chid_8a0e3b7c1d2f4e59",
      "channel": "telegram",
      "address": "Telegram de ••••8877",
      "linked_at": "2026-11-12T11:20:40Z",
      "locations": ["loc_9b1c"],
      "locations_label": "La Parra Getafe"
    }
  ]
}
```

- Solo los canales **activos** de la cuenta de la sesión, el más antiguo primero. Una cuenta puede tener varios (dueño y encargado).
- `locations` es `null` si el chat ve todas las fichas activas, también las futuras. Si no, la lista de fichas de su filtro que la cuenta todavía tiene, en el orden de `GET /api/v1/me` (por `title`).
- `locations_label` va **listo para mostrar** después de «Recibe:»: «Todos tus negocios», un nombre o varios («Bar La Parra y La Parra Getafe»). Una ficha del filtro que está desactivada lleva « (desactivada)». Si Google ya no lista ninguna ficha del filtro, «Ninguno de tus negocios».
- `address` ya va **enmascarada y lista para mostrar**: la web no recibe nunca el identificador completo del chat.
- `channel` vale `telegram` o `whatsapp` (mañana también `email`). Un WhatsApp se enseña como `"WhatsApp de ••••2233"` (los 4 últimos dígitos del teléfono).
- Un WhatsApp solo recibe lo de las fichas Pro+ de su filtro: si el filtro solo tiene fichas Pro o Gratis, no recibe nada. Enséñalo así en el selector de fichas de ese canal.
- Responde también durante una baja programada de la ficha, para poder desvincular. Tras confirmar la baja de la **cuenta**, la lista queda vacía: los canales se desvinculan en el mismo momento.

Errores: 401 sin sesión.

### `PUT /api/v1/me/channels/{id}/locations`

Cambia qué fichas ve un canal ya vinculado. Lleva `X-Requested-With: fichaitor`. El campo es obligatorio: `null` para todas las fichas o la lista.

```json
{ "locations": ["loc_3f2a", "loc_9b1c"] }
```

- Las mismas reglas que al pedir el código: fichas activas de la cuenta, entre 1 y 50, sin repetir.
- Respuesta 200: el canal actualizado, con la forma de cada canal de `GET /api/v1/me/channels`.
- Desde ese momento, el chat solo recibe propuestas, recordatorios y resultados de esas fichas, y un botón de otra ficha responde que ya no está activo. Las propuestas de una ficha nueva en el filtro que esperaban a alguien que la viera le llegan en la siguiente vuelta del proceso de la conversación (un minuto como mucho).
- Para editarlo, parte de `locations` del canal. Una ficha desactivada del filtro hay que quitarla al guardar: con ella, la respuesta es 422.

Errores, todos con `detail` en español:

| Código | Cuándo |
|---|---|
| 401 | Sin sesión |
| 403 | Falta la cabecera `X-Requested-With` |
| 404 | El canal no existe, ya está desvinculado o es de otra cuenta: "Ese canal no está vinculado a tu cuenta.". O una ficha no es de la cuenta: "Esa ficha no está en tu cuenta." |
| 409 | La cuenta tiene una baja programada |
| 422 | Una ficha está inactiva ("Esa ficha no está activa."), o el cuerpo no vale: sin `locations`, lista vacía, más de 50 o un campo de más (`detail` es una lista) |

#### Una ficha del filtro que se desactiva

Desactivar una ficha **no cambia** el filtro de ningún chat. Mientras está inactiva, Fichaitor no propone nada de ella, así que un chat filtrado solo a esa ficha se queda callado; si se vuelve a activar, ese chat la vuelve a recibir sin tocar nada. `GET /api/v1/me/channels` la enseña con « (desactivada)», para que el dueño entienda por qué ese chat no recibe nada.

No se quita sola del filtro porque un filtro vacío no existe: pasaría a `null`, y el chat del encargado empezaría a recibir **todos** los negocios.

### `DELETE /api/v1/me/channels/{id}`

Sin cuerpo. Respuesta **204**, sin cuerpo.

- Desde ese instante el bot no envía nada a ese chat y lo trata como un desconocido.
- 404 si el canal no existe, ya está desvinculado o es de otra cuenta: la misma respuesta en los tres casos.
- 401 sin sesión, 403 sin la cabecera.

Pide confirmación antes ("Dejarás de recibir avisos en este chat").

### Lo que la web NO debe hacer con los canales

- Construir el enlace de Telegram o el de WhatsApp a mano, o meter `code` en él.
- Calcular la bolsa de WhatsApp: usa `whatsapp` de `GET /api/v1/me`.
- Guardar el código o el enlace (en `localStorage`, en la URL o en un registro).
- Enseñar o pedir el identificador del chat de Telegram.
- Componer el texto de las fichas de un chat: usa `locations_label`.
- Mandar fichas inactivas en `locations`: solo se pueden elegir las activas.

### Sin Telegram real

En desarrollo, sin token del bot, `POST /api/v1/channels/link-code` y las dos rutas de canales funcionan igual: el código se genera y se guarda, pero nadie lo canjea. Para probarlo de punta a punta hace falta un bot de pruebas y el proceso de la conversación (`docs/canales.md`, "Probarlo en local con el bot real").

## Fechas clave (2026-11-12)

Con antelación, Fichaitor pregunta al dueño si abre un festivo (o Nochebuena y Nochevieja) y con qué horario. La misma pregunta llega por Telegram y aparece en el panel; se responde en uno de los dos sitios y el otro la ve resuelta. Cuando el dueño confirma, el worker aplica el horario especial de ese día en Google y no toca los demás días. Cómo se eligen las fechas: `docs/calendario.md`.

> **Multinegocio (2026-11-12):** la lista admite `?location=loc_…`; su respuesta y cada fecha llevan `location_id`. Responder y descartar ya identifican la ficha por la propuesta: valen para una fecha de **cualquier ficha activa** de la cuenta, sin necesidad de ficha elegida, y `regular_hours` es el de la ficha de esa fecha. Una fecha de una ficha inactiva da 404. Consulta [La ficha de cada lectura](#la-ficha-de-cada-lectura-location).

Las tres rutas trabajan sobre la ficha elegida de la cuenta de la sesión. Las de escritura llevan la cabecera `X-Requested-With: fichaitor` (403 sin ella). Todas responden 401 sin sesión, 409 durante una baja programada y 409 si no hay ficha elegida. Todo `detail` de error es un texto en español listo para mostrar.

### `GET /api/v1/me/key-dates`

Las fechas pendientes de respuesta, la más próxima primero. Lista vacía si no hay ninguna. Con `?filter=all`, también las ya respondidas, descartadas o caducadas desde hoy en adelante (para el historial de la tarjeta).

```json
{
  "dates": [
    {
      "id": "prop_8f2c1a9b0d3e4f56",
      "date": "2026-12-08",
      "name": "Inmaculada Concepción",
      "regular_hours": [
        { "open": "09:00", "close": "14:00" },
        { "open": "17:00", "close": "20:00" }
      ],
      "date_text": "martes 8 de diciembre",
      "kind": "holiday",
      "status": "pendiente",
      "answerable": true,
      "expires_at": "2026-12-07T23:00:00Z",
      "answer": null,
      "action": null,
      "location_id": "loc_3f2a"
    }
  ],
  "location_id": "loc_3f2a"
}
```

| Campo | Qué es |
|---|---|
| `id`, `date`, `name` | La forma acordada de la tarjeta. `name` va listo para mostrar: el nombre del festivo, "Nochebuena" o "Nochevieja" |
| `regular_hours` | El horario normal de ese día de la semana, tal como lo tenía la ficha en Google en la última sincronización. Para la opción «Horario normal». `null` si la ficha no tiene horario normal (o aún no se ha leído); `[]` si ese día de la semana la ficha cierra. Una franja que acaba al día siguiente sale con el cierre menor que la apertura (`{"open": "20:00", "close": "02:00"}`) |
| `date_text` | La fecha en palabras, como la escribe el agente |
| `kind` | `holiday` (festivo) o `season` (Nochebuena, Nochevieja) |
| `status` | `pendiente`, `en_curso` (confirmada y enviándose), `aplicada`, `fallida`, `descartada` o `caducada` |
| `answerable` | Si todavía se puede responder. En `filter=pending` siempre es `true` |
| `expires_at` | Cuándo deja de recordarse por Telegram (00:00 del día, hora peninsular). El mismo día todavía se puede responder desde el panel |
| `answer` | Lo que se confirmó: `{"kind": "closed" \| "regular" \| "custom", "ranges": [...], "text": "de 10:00 a 14:00"}`, o `null` |
| `action` | El envío a Google: `{"id", "status", "message", "error", "confirmed_at", "finished_at"}`, con los mismos `status` que `GET /api/v1/me/actions/{id}`, o `null`. `message` va listo para mostrar |

Los campos a partir de `date_text` son añadidos a la forma acordada; la tarjeta puede ignorarlos.

### `POST /api/v1/me/key-dates/{id}/answer`

La pulsación de «Confirmar» en el panel. **Es la confirmación del dueño**: registra la confirmación y crea la acción que el worker envía a Google. No hay un segundo paso.

```json
{ "kind": "closed" }
{ "kind": "regular" }
{ "kind": "custom", "ranges": [{ "open": "10:00", "close": "14:00" }, { "open": "17:00", "close": "20:00" }] }
```

- `regular` copia en ese día el horario normal que la ficha tenga en Google **en el momento de enviarlo** (no el de `regular_hours`, que puede ser de hace unas horas).
- `custom` lleva una o dos franjas. El servidor comprueba el formato (`H:MM` o `HH:MM`), que cada franja abra antes de cerrar, que estén en orden y sin solaparse, y que haya como mucho dos. `24:00` vale como cierre.
- **Franjas que cruzan la medianoche** (`20:00` a `02:00`): se rechazan con 422. Un horario especial que acaba al día siguiente ocupa dos fechas en Google y aquí solo se cambia una; el dueño lo pone en Google o elige `regular` (que sí copia una franja normal que acabe de madrugada).

Respuesta 202: el elemento con `status: "en_curso"`, `answer` y `action` (`status: "pendiente"`). El resultado se consulta con `GET /api/v1/me/key-dates?filter=all` o `GET /api/v1/me/actions/{action.id}`, y llega también por Telegram si hay un chat vinculado.

| Código | Cuándo |
|---|---|
| 202 | Confirmada |
| 404 | El id no existe, es de otra cuenta o de otra ficha |
| 409 | Ya no está pendiente: respondida (también por Telegram), descartada o su día ha pasado. También si ya hay un cambio de horario enviándose para ese día, durante una baja programada y sin ficha elegida |
| 422 | Franjas no válidas, o `ranges` con `closed` o `regular` |
| 503 | No se pudo guardar. Se puede repetir |

### `POST /api/v1/me/key-dates/{id}/dismiss`

El dueño no quiere cambiar nada ese día. Respuesta 204, sin cuerpo. La ficha muestra su horario normal. 404 y 409 como en `answer`.

### Qué ve el dueño después

| `action.status` | `action.message` |
|---|---|
| `pendiente` | "Tu cambio de horario está en cola para enviarse a Google." |
| `en_curso` | "Estamos enviando tu cambio de horario a Google." |
| `hecha` | "Enviado a Google. Puede tardar unos minutos en verse en tu ficha." |
| `fallida` | Según `action.error`: `reconnect` (volver a conectar), `rejected` (Google no aceptó el horario), `date_passed` (el día pasó antes de poder enviarlo), `no_regular_hours` (se eligió «Horario normal» y la ficha no tiene), `suspended` (baja programada), `unavailable` (Google no respondió tras los reintentos), `internal` |

"Enviado a Google", nunca "publicado": Google revisa los cambios y puede tardar en mostrarlos.

### Lo que la web NO debe hacer con las fechas clave

- Enviar el horario a Google por su cuenta ni mostrarlo como aplicado antes de `hecha`.
- Ofrecer más de dos franjas o franjas que crucen la medianoche (el servidor las rechaza igualmente).
- Reintentar `answer` tras un 409: la fecha ya está resuelta; vuelve a cargar la lista.

### Sin Google real

Con `FICHAITOR_FAKE_GOOGLE=true`, el worker "aplica" el horario en una ficha en memoria. Sin base de datos (`MemoryStore`) la lista sale vacía: las propuestas las crea el proceso de la conversación.

## Multinegocio (2026-11-12)

Una cuenta puede tener **varias fichas activas** a la vez. Fichaitor vigila, sincroniza y avisa de cada ficha activa; ver una en el panel no pausa las demás. El plan completo está en [multinegocio.md](multinegocio.md); aquí va el contrato exacto con la web.

- **Activa** quiere decir que la ficha tiene plan: `gratis`, `pro` o `pro_whatsapp`. Como mucho una ficha Gratis por cuenta, y una ficha de Google solo puede estar en Gratis en una cuenta de Fichaitor. Durante la beta, cada cuenta tiene 3 fichas Pro.
- `selected_location_id` es la **ficha preferida** del panel: la que sirven las lecturas sin `?location=`. No activa ni pausa nada. Se guarda con `POST /api/v1/me/location`, que solo admite una ficha activa (409 `location_inactive` si no lo está).
- No hay cobro: con `pricing.charging: false`, los precios son de referencia.

### La ficha de cada lectura (`?location=`)

Las cinco lecturas de una ficha admiten el parámetro opcional `location`:

- `GET /api/v1/me/diagnosis`
- `GET /api/v1/me/summary`
- `GET /api/v1/me/sentiment`
- `GET /api/v1/me/reviews`
- `GET /api/v1/me/key-dates`

```
GET /api/v1/me/summary?location=loc_3f2a
```

- Sin `location`, la API usa `selected_location_id`. La web anterior sigue funcionando sin cambios.
- La ficha se busca siempre con la cuenta de la sesión. Una ficha de otra cuenta, una que no existe y un valor que no tiene la forma `loc_…` dan **la misma** respuesta 404.
- Toda respuesta 200 de estas rutas lleva `location_id`, la ficha de la que es. También el 404 de «todavía no hay diagnóstico». **Descarta la respuesta** si su `location_id` no es la ficha que tienes seleccionada: el dueño cambió de negocio mientras cargaba.
- Las rutas con identificador propio (`POST /api/v1/me/reviews/{id}/draft` y `…/reply`, `GET /api/v1/me/actions/{id}`, `POST /api/v1/me/key-dates/{id}/answer` y `…/dismiss`) no llevan `location`: la ficha sale de la reseña o de la propuesta, que debe ser de una ficha activa de la cuenta.

| Caso | Código | `detail` |
|---|---|---|
| Sin `location` y sin ficha preferida | 409 | "Primero elige con qué ficha quieres trabajar." |
| Ficha de otra cuenta, inexistente o con otra forma | 404 | "Esa ficha no está en tu cuenta." |
| Ficha de la cuenta, inactiva | 409 | "Esa ficha no está activa." |
| Baja programada | 409 | El de la baja, como en el resto de rutas de la ficha |

Una ficha preferida que Google dejó de listar por falta de acceso se sigue sirviendo sin `location`, con `status: "needs_reconnect"`, como hasta ahora.

### `GET /api/v1/me` con varias fichas

```json
{
  "email": "dueno@example.com",
  "locations": [
    { "id": "loc_3f2a", "title": "Bar La Parra", "place_id": "ChIJ...", "verified": true,
      "active": true, "plan": "gratis", "active_since": "2026-11-12T10:00:00Z",
      "status": "ok", "pending": { "reviews": 3, "key_dates": 1 } },
    { "id": "loc_9b1c", "title": "La Parra Getafe", "place_id": null, "verified": true,
      "active": false, "plan": null, "active_since": null, "status": null, "pending": null }
  ],
  "selected_location_id": "loc_3f2a",
  "connected": true,
  "pending_deletion": null,
  "plans": { "active": 1, "free_available": false,
             "available": { "pro": 3, "pro_whatsapp": 0 } },
  "pricing": {
    "currency": "EUR", "period": "year", "charging": false,
    "plans": { "pro": { "first": 3500, "additional": 3325 },
               "pro_whatsapp": { "first": 5900, "additional": 5605 } },
    "total": 0,
    "if_activate": { "pro": 3500, "pro_whatsapp": 5900 },
    "if_deactivate": { "loc_3f2a": 0 }
  }
}
```

Campos nuevos de cada ficha de `locations`:

| Campo | Tipo | Qué es |
|---|---|---|
| `active` | booleano | Si Fichaitor la vigila y avisa de ella |
| `plan` | texto o `null` | `gratis`, `pro` o `pro_whatsapp`. `null` si está inactiva. Con `pricing.charging: false`, muestra «Pro (beta)» sin precio |
| `active_since` | fecha y hora UTC, o `null` | Desde cuándo está activa. Un cambio de plan no la cambia |
| `status` | texto o `null` | Los mismos valores que el `status` del resumen: `ok`, `pendiente` (activa y aún sin sincronizar, o leyéndose otra vez tras un fallo, como al volver de conectar con Google), `error` o `needs_reconnect`. `null` si está inactiva. Con `error` o `needs_reconnect`, el selector enseña un aviso |
| `pending` | objeto o `null` | `{"reviews": n, "key_dates": n}`: reseñas sin responder (según la última sincronización) y fechas clave por contestar desde hoy. `null` si está inactiva |

Campos nuevos del cuerpo:

| Campo | Qué es |
|---|---|
| `plans.active` | Fichas activas de la cuenta |
| `plans.free_available` | Si la cuenta puede activar todavía una ficha Gratis |
| `plans.available` | Huecos libres en cada plan de pago: `{"pro": n, "pro_whatsapp": n}` |
| `pricing` | Precios en **céntimos** al año. La web no guarda ni calcula ninguna cifra |
| `pricing.plans` | Precio de la primera ficha de pago (`first`) y de las siguientes (`additional`, con el 5 % de descuento) de cada plan |
| `pricing.total` | Lo que pagaría la cuenta con sus fichas activas. La ficha Gratis no cuenta como primera: el descuento empieza en la segunda de pago |
| `pricing.if_activate` | Total resultante de activar una ficha más en cada plan de pago |
| `pricing.if_deactivate` | Total resultante de desactivar cada ficha activa, por su `id` |
| `pricing.charging` | `false` mientras no haya cobro (beta) |
| `whatsapp` | La bolsa de WhatsApp del mes, o `null` sin fichas Pro+ activas (y durante una baja programada). Ver abajo |

El bloque `whatsapp` (2026-11-13):

```json
"whatsapp": { "month": "2026-11", "used": 37, "limit": 160, "remaining": 123 }
```

| Campo | Qué es |
|---|---|
| `month` | El mes de la bolsa, en hora de Madrid. El día 1 vuelve a cero, sin aviso |
| `used` | Mensajes nuestros enviados por WhatsApp este mes. Lo que escribe el dueño no cuenta |
| `limit` | 80 por cada ficha Pro+ activa, sumados. Cambia en cuanto se activa o desactiva una |
| `remaining` | Los que quedan. Con `0`, los avisos llegan por Telegram o se quedan en el panel: enséñalo en Inicio ("Este mes has usado los mensajes de WhatsApp. Los avisos te llegan por Telegram o aquí.") |

- Las fichas siguen ordenadas por `title`. El selector pone primero las activas.
- Durante una baja programada, `status` y `pending` van a `null` en todas las fichas: no se lee nada de ninguna. `active` y `plan` siguen diciendo cómo están.

### `POST /api/v1/me/locations/{id}/activation`

Activa una ficha de la cuenta o cambia el plan de una ya activa. Lleva `X-Requested-With: fichaitor`.

```json
{ "plan": "pro" }
```

- El cuerpo es opcional: sin cuerpo, `{}` o `{"plan": null}`, una ficha activa se queda con su plan, y una inactiva recibe:
  - **durante la beta** (`pricing.charging: false`, cambio del 2026-11-13): **Pro** si quedan fichas Pro del derecho beta; si no, Gratis si queda libre; si no, Pro WhatsApp si hay hueco;
  - **desde que se cobra:** Gratis si queda libre y, si no, el primer plan de pago con hueco.
- Con `plan`, la ficha pasa a ese plan si cabe. Activar una ficha ya activa con otro plan le cambia el plan.
- Respuesta 200: el cuerpo de `GET /api/v1/me`, ya actualizado. La API pide además la sincronización de la ficha, para que el panel tenga sus datos cuanto antes.
- La API no cambia `selected_location_id` al activar. Para ver la ficha, envía `?location=` o guárdala como preferida.

### `DELETE /api/v1/me/locations/{id}/activation`

Desactiva una ficha. Lleva `X-Requested-With: fichaitor`. Sin cuerpo.

- Al momento, Fichaitor deja de sincronizarla y de avisar de ella, y cancela sus propuestas pendientes (fechas clave y reseñas por chat). Si no se vuelve a activar en 30 días, se borra lo que se tenía de ella. No retira el acceso de Fichaitor a la cuenta de Google: para eso está «Desconectar Google».
- Respuesta 200: el cuerpo de `GET /api/v1/me`. Si era la ficha preferida, `selected_location_id` pasa a la ficha activa más antigua (`active_since`) o a `null` si no queda ninguna.
- Se puede desactivar la última ficha activa: la cuenta vuelve al estado «activa tu primera ficha».
- Desactivar una ficha que ya estaba inactiva no cambia nada y responde 200.
- Antes de llamar, pide confirmación con el texto de la sección 12 de [multinegocio.md](multinegocio.md).

### Errores de la activación

Todos llevan `detail` en español listo para mostrar. Los 409 llevan además `code`, para distinguirlos:

| Código | `code` | Cuándo | `detail` |
|---|---|---|---|
| 401 | — | Sin sesión | "No has iniciado sesión." |
| 402 | `no_seat` | No queda hueco en el plan pedido (o en ninguno, sin `plan`). Solo en `POST`. Trae `offer` y `charging`: el paywall de [Cobro y paywall](#cobro-y-paywall-2026-11-13) | Con `charging: true`: "Esta ficha necesita una plaza Pro: 33,25 € + IVA por un año. Se renueva sola salvo que la canceles. Pásate a Pro o a Pro+ para activarla." |
| 402 | `account_blocked` | La cuenta está bloqueada por un pago pendiente. Solo en `POST`: desactivar sí se puede | "Tu cuenta está en pausa por un pago pendiente. …" |
| 403 | — | Falta la cabecera `X-Requested-With` | "Petición no permitida." |
| 404 | — | La ficha no es de la cuenta, no existe o el `id` no tiene la forma `loc_…`. La misma respuesta en todos los casos | "Esa ficha no está en tu cuenta." |
| 409 | `free_taken` | Se pidió `gratis` y esa ficha de Google ya está en Gratis en otra cuenta. Sin `plan`, la API pasa sola al primer plan de pago con hueco | "Esta ficha ya está activa en el plan Gratis en otra cuenta. Actívala con un plan de pago o pide a esa cuenta que la desactive." |
| 409 | `offboarding_pending` | La cuenta tiene una baja programada. También en `DELETE` | "Tienes una baja programada. Restáurala para seguir usando Fichaitor." |
| 409 | `location_removed` | Google ha dejado de listar la ficha justo antes. Solo en `POST` | "Google ya no muestra esa ficha en tu cuenta. Vuelve a conectar con Google para verla." |
| 422 | — | `plan` desconocido o un campo de más en el cuerpo. `detail` es una lista | — |

### Límites del plan (2026-11-13)

Cada ficha activa tiene su plan, y el plan decide qué puede hacer la ficha. La API lo mira **por ficha y en el momento de cada acción**: cambiar de plan cambia lo que se puede hacer desde la siguiente petición.

| Función | Gratis | Pro y Pro WhatsApp |
|---|---|---|
| Panel, resumen, diagnóstico y análisis de opinión | Sí | Sí |
| Avisos de reseñas por el panel y por Telegram | Sí | Sí |
| Escribir y publicar respuestas propias | Sin límite | Sin límite |
| Respuestas redactadas con IA | **25 por ficha, una sola vez** | Sin límite |
| Festivos y fechas clave | **No** | Sí |
| WhatsApp | No | Solo Pro WhatsApp |

- **Qué cuenta como respuesta con IA:** cada borrador que se pide al modelo, desde el panel (`POST /api/v1/me/reviews/{id}/draft`) o desde el bot. Un borrador ya guardado (`cached: true`) no cuenta: el modelo no se vuelve a llamar.
- **Una sola vez y para siempre** (cambio del 2026-11-13): las 25 no se recargan cada mes ni se acumulan. Van con la ficha de Google, no con la cuenta: borrar la cuenta, retirar el acceso o conectar la misma ficha desde otra cuenta de Google no las devuelve. Gastadas las 25, el borrador responde 402 y la web enseña el paywall.
- En Pro y Pro+ los borradores no gastan nada de esas 25.
- **Gratis no tiene festivos ni fechas clave:** no se crean propuestas para fichas Gratis, y `answer` y `dismiss` responden 402. `GET /api/v1/me/key-dates` sigue respondiendo; en una ficha Gratis la lista sale vacía, salvo propuestas de cuando la ficha tenía otro plan.

#### `usage` y `features` en `GET /api/v1/me`

Cada ficha **activa** trae:

```json
{
  "id": "loc_3f2a",
  "plan": "gratis",
  "usage": { "ai_replies": { "used": 7, "limit": 25, "lifetime": true } },
  "features": { "key_dates": false, "whatsapp": false }
}
```

- `usage.ai_replies.used`: respuestas con IA gastadas de las 25 gratuitas de esa ficha de Google, desde siempre.
- `usage.ai_replies.limit`: `25` en Gratis; `null` sin límite (Pro y Pro+).
- `usage.ai_replies.lifetime`: siempre `true`: el límite no es mensual.
- `features.key_dates` y `features.whatsapp`: si el plan incluye esa función.
- En una ficha inactiva, `usage` y `features` valen `null`.
- La web no guarda cifras de límites: salen de aquí.

#### El 402 `plan_required`

Al gastar las 25 respuestas con IA, o al pedir una función que el plan de la ficha no incluye, la API responde **402**:

```json
{
  "detail": "Has usado tus 25 respuestas con IA gratuitas. Pásate a Pro para tenerlas ilimitadas. Puedes seguir escribiendo tus respuestas.",
  "code": "plan_required",
  "feature": "ai_replies"
}
```

| Ruta | `feature` | `detail` |
|---|---|---|
| `POST /api/v1/me/reviews/{id}/draft` | `ai_replies` | "Has usado tus 25 respuestas con IA gratuitas. Pásate a Pro para tenerlas ilimitadas. Puedes seguir escribiendo tus respuestas." |
| `POST /api/v1/me/key-dates/{id}/answer` y `…/dismiss` | `key_dates` | "Los festivos y las fechas clave están en Pro. Pásate a Pro para que te preguntemos por ellos y cambiemos el horario en Google." |

- `detail` está listo para mostrar tal cual.
- Distingue este 402 de los de la activación por `code`: `plan_required` aquí, `no_seat` o `account_blocked` allí.
- Con `feature: "ai_replies"`, deja escribir la respuesta a mano: `POST …/reply` no tiene límite.
- El botón es el paywall: «Pásate a Pro o a Pro+». Con `pricing.charging: false` (beta), pasa la ficha a Pro con `POST …/activation` y `{"plan": "pro"}` (usa una ficha Pro del derecho beta); desde que se cobra, compra la plaza con `POST /api/v1/me/billing/seats`. Ya no se remite a soporte.

#### En el bot

- Una ficha Gratis que ya ha usado sus 25 respuestas con IA sigue recibiendo el aviso de cada reseña nueva, sin respuesta propuesta. El mensaje lo explica y trae los botones «Escribir respuesta» y «No responder». El dueño escribe la respuesta, el bot se la enseña y la envía solo cuando él la confirma.
- Las fichas Gratis no reciben preguntas de festivos ni de fechas clave.
- Si una ficha pasa a Gratis con una fecha clave ya preguntada, esa propuesta no se puede confirmar por el chat (cambio del 2026-11-13): al pulsar o contestar, el bot la cancela (`outcome_reason: "plan_required"`), no toca Google y responde «Esta ficha ahora está en el plan Gratis, que no incluye las fechas clave. No he cambiado nada en Google. Si quieres que te las siga preguntando, pásate a Pro desde el panel: <enlace>».

### Lo que la web NO debe hacer con el multinegocio

- Calcular precios, descuentos o huecos: salen de `plans` y `pricing`.
- Mezclar respuestas de fichas distintas: descarta la que no traiga el `location_id` seleccionado.
- Llamar a `POST /api/v1/me/location` con una ficha inactiva: responde 409 `location_inactive` y no la activa. Activa antes con `POST …/activation`.
- Reintentar una activación tras un 402 o un 409: no cambiará hasta que cambie la cuenta.
- Calcular los límites del plan o el uso del mes: salen de `usage` y `features`.
- Reintentar un borrador tras un 402 `plan_required`: no cambiará hasta pasar la ficha a Pro o a Pro+.

## Cobro y paywall (2026-11-13)

El paywall sustituye a los «escríbenos a soporte»: cuando una ficha necesita Pro o Pro+, la web ofrece «Pásate a Pro o a Pro+». **Pro+** es el nombre visible del plan `pro_whatsapp`. El diseño está en [admin-facturacion.md](admin-facturacion.md) (sección 4.7); lo de aquí es lo que la API hace de verdad, con los detalles acordados con la web.

Reglas de fondo, que la web cuenta al usuario:

- Solo tarjeta (con Apple Pay y Google Pay).
- Cada plaza es **un año entero desde su alta** y se renueva sola en su fecha. Sin prorrateo y sin devoluciones.
- Desde la segunda plaza de pago, un 5 % menos. La API da el precio exacto: la web no lo calcula.
- Un impago **bloquea toda la cuenta** hasta que se pague, sin límite de tiempo. No se borra nada.
- Una plaza que llega a su renovación sin ficha no se cobra: se cancela sola.
- España (península, Baleares, Canarias, Ceuta y Melilla) y la UE, siempre con NIF o NIF-IVA. **Desde el 2026-11-13, también el resto del mundo, solo empresas**, con la identificación fiscal de la empresa ([Fuera de la UE](#fuera-de-la-ue-2026-11-13)). Siempre en euros.
- **Ventana beta** (decisión del propietario, 2026-11-13): la beta es gratis hasta el lanzamiento (`billing_setting.launch_at`). Las cuentas que ya existían antes del lanzamiento tienen **30 días más** (hasta `launch_at` + 30 días) para elegir plan, con sus fichas Pro de la beta. Si compran una plaza en esos días, Stripe guarda la tarjeta pero **no cobra nada hasta que acaba su ventana**: el primer año empieza ese día, sin días de más. **Nadie más tiene prueba gratis**: una cuenta creada desde el lanzamiento paga la plaza el día que la compra.

### Cómo funciona

La API no habla con Stripe. Cada acción de cobro crea una **petición** y responde `202 {"request_id": "…"}`. El proceso de cobro la atiende en segundos, y la web consulta `GET /api/v1/me/billing/requests/{id}` cada 2 segundos hasta que deja de estar `pendiente`:

| `state` | Trae | Qué hace la web |
|---|---|---|
| `pendiente` | — | Sigue consultando |
| `lista` | `url` (salvo en cancelar y reanudar) | Abre `url`: siempre una dirección `https:` de Stripe (`*.stripe.com`: Checkout, portal o factura alojada). La web no abre ninguna otra |
| `fallida` | `code` y `detail` | Enseña `detail` tal cual |

Durante la **beta** (`pricing.charging: false`) no se cobra: `POST /api/v1/me/billing/seats` responde 409 `beta_no_charge`. La web activa con `POST /api/v1/me/locations/{id}/activation` y `{"plan": "pro"}`, que usa una de las fichas Pro del derecho beta, también para pasar a Pro una ficha que ya está activa en Gratis.

Durante la **ventana beta** de una cuenta de la beta (`pricing.charging: true` y `pricing.trial_days` con un número), la compra es la misma (`POST /api/v1/me/billing/seats`), pero el Checkout de Stripe dice que hoy se paga 0 € y cuándo llega el primer cobro. La ficha se activa en cuanto Stripe confirma el alta, igual que con un pago. Si el primer cobro, al acabar la ventana, falla o se queda esperando la confirmación del banco (3DS), la cuenta se bloquea como en cualquier renovación.

### Rutas

Todas llevan sesión y funcionan **también con la cuenta bloqueada**: pagar es la salida del bloqueo. Las que cambian algo (`PUT` y `POST`) llevan `X-Requested-With: fichaitor` y cuentan para un tope de 30 por cuenta y hora (429 `rate_limited`). Las `GET` no llevan la cabecera, como el resto de lecturas del contrato (cambio frente a la tabla del diseño, que la pedía en todas: la web solo la envía en las escrituras).

| Ruta | Cuerpo | Respuesta |
|---|---|---|
| `GET /api/v1/me/billing/countries` | — | 200 `{"countries": [{"code": "AD", "region": "fuera_ue"}, …]}`: los países a los que se factura, ordenados por código ([Fuera de la UE](#fuera-de-la-ue-2026-11-13)) |
| `GET /api/v1/me/billing/profile` | — | 200 con el perfil fiscal; sin perfil, 200 con los campos a `null` y `complete: false` (nunca 404) |
| `PUT /api/v1/me/billing/profile` | `{legal_name, tax_id, address, invoice_email}` | 200 con el perfil guardado, o 422 con `code` |
| `POST /api/v1/me/billing/seats` | `{"plan": "pro" o "pro_whatsapp", "location_id": "loc_…"}` | 202 `{request_id}`. Al pagarse, la ficha se activa sola en ese plan |
| `POST /api/v1/me/billing/seats/{id}/cancel` | — | 202 `{request_id}`. La plaza deja de renovarse; la ficha sigue hasta el final del año pagado |
| `POST /api/v1/me/billing/seats/{id}/resume` | — | 202 `{request_id}`. Deshace la cancelación antes de la fecha de renovación |
| `POST /api/v1/me/billing/pay` | — | 202 `{request_id}`. El resultado es la factura pendiente alojada en Stripe, para desbloquear |
| `POST /api/v1/me/billing/portal` | — | 202 `{request_id}`. El portal de Stripe, solo para cambiar la tarjeta |
| `GET /api/v1/me/billing/requests/{id}` | — | `{state, url, code, detail}`. 404 si es de otra cuenta o no existe |
| `GET /api/v1/me/billing/invoices` | — | `{"invoices": [{id, kind, number, amount_total, issued_at}]}`. Las facturas y rectificativas legales de Odoo, ya emitidas ([Facturas](#facturas)) |
| `GET /api/v1/me/billing/invoices/{id}/pdf` | — | El PDF (`application/pdf`) con su nombre, o 404 |
| `POST /api/v1/webhooks/stripe` | — | Sin sesión: lo verifica la firma de Stripe. No es para la web |

La ruta de asignar una plaza libre a otra ficha (`…/seats/{id}/assign`) queda para un paso siguiente.

#### Facturas

Cada cobro tiene su factura legal, que emite el Odoo de Bitclick (serie `FTR`); cada reembolso excepcional, su rectificativa (serie `RFTR`). Solo salen cuando ya están emitidas: una pendiente (Odoo caído) o en revisión no aparece. Al emitirse, llegan también por correo con el PDF adjunto.

| Campo | Valor |
|---|---|
| `kind` | `factura` o `rectificativa`, en español |
| `number` | El número de Odoo, por ejemplo `FTR/2027/00001` |
| `amount_total` | Céntimos, **con IVA**. **Negativo en una rectificativa** (`-4235`): devuelve |
| `issued_at` | Cuándo se emitió |

El PDF se descarga con `Content-Disposition: attachment; filename="<numero>.pdf"; filename*=UTF-8''<numero>.pdf`, donde `<numero>` es el número de Odoo con `/` cambiado por `_` y solo los caracteres `[A-Za-z0-9_-]` (por ejemplo, `FTR_2027_00001.pdf`).

#### Perfil fiscal

```json
{
  "legal_name": "Bar La Parra S.L.",
  "tax_id": "B12345674",
  "address": {
    "line1": "Calle Mayor 1",
    "line2": null,
    "postal_code": "18001",
    "city": "Granada",
    "province": "Granada",
    "country": "ES"
  },
  "invoice_email": "facturas@laparra.es"
}
```

- `address`: `{line1, line2?, postal_code?, city, province?, country}`, con `country` en ISO 3166-1 alfa-2 (`ES`, `FR`, `US`…). `postal_code` es obligatorio en España y en la UE; fuera de la UE puede ir vacío, `null` o sin enviar.
- La respuesta trae además `tax_treatment` (`iva_21`; `no_sujeto` en Canarias, Ceuta y Melilla; `isp_ue` en otro país de la UE o en Mónaco; `extracomunitario` en el resto del mundo) y `complete`. `tax_id` vuelve normalizado: en España y la UE, en mayúsculas y sin espacios ni guiones; fuera de la UE, en mayúsculas y con sus separadores (`12-3456789`). `postal_code` vuelve normalizado (en mayúsculas; en España, solo las 5 cifras) o `null`.
- El 422 lleva `code` y un `detail` listo para mostrar, con el texto de la región del país (por ejemplo, «Ese NIF no es válido…» en España y «Esa identificación fiscal no es válida…» fuera de la UE):

| `code` | Cuándo |
|---|---|
| `tax_id_invalid` | España: el NIF, NIE o CIF no es válido (la letra o el dígito de control). UE: el NIF-IVA no tiene el formato de ese país. Fuera de la UE: no tiene de 3 a 32 caracteres, lleva caracteres distintos de letras, números, `-`, `.`, `/` y espacio (se admiten `Ñ` y `&`) o no tiene ninguna cifra |
| `postal_code_invalid` | España: el código postal no existe (5 cifras). UE: falta. Cualquier país de fuera de España: no tiene de 2 a 12 letras, números, espacios o guiones, o empieza o acaba por espacio o guion |
| `country_not_supported` | El código no es un país ISO 3166-1, o es uno al que no se factura (Cuba, Irán, Corea del Norte, Siria, Rusia y Bielorrusia) |

#### Fuera de la UE (2026-11-13)

Decisión del propietario: se cobra con tarjeta **en todo el mundo**. Fuera de la UE, **solo empresas**: la identificación fiscal es obligatoria y no se valida por país. La operación no está sujeta al IVA español (cuota 0) y la factura lo dice. El precio es el mismo y en euros; el banco del cliente convierte. El detalle fiscal está en [admin-facturacion.md](admin-facturacion.md), sección 2.1.

Qué cambia en el formulario de datos de facturación:

- **Lista de países:** la de `GET /api/v1/me/billing/countries`, no una propia. Cada país trae su `region`; el nombre lo pone el navegador (`new Intl.DisplayNames(["es"], {type: "region"}).of(code)`), ordenado por ese nombre. Canarias, Ceuta y Melilla son `ES`, como siempre (elige el país España y el código postal hace el resto).

| `region` | Etiqueta del campo `tax_id` | Código postal |
|---|---|---|
| `es` | «NIF» (NIF, NIE o CIF) | Obligatorio, 5 cifras |
| `ue` | «NIF-IVA» (con el prefijo del país, por ejemplo `FR40303265045`; en Mónaco, el francés) | Obligatorio |
| `fuera_ue` | «Identificación fiscal de tu empresa (Tax ID / EIN / RFC…)» | Opcional: la web no lo marca como obligatorio |

- **Fuera de la UE, solo empresas.** Junto al campo, un texto corto como «Solo facturamos a empresas. Escribe la identificación fiscal de tu empresa tal como aparece en tus documentos». La API no lo puede comprobar: es una declaración del cliente.
- **Precio:** el mismo «€ + IVA» de siempre. Para un cliente de fuera de la UE el IVA es 0; la web puede decir «sin IVA» cuando `tax_treatment` es `extracomunitario` o `isp_ue`, pero no calcula nada: los importes los da la API.
- **Errores:** los mismos tres `code` de arriba; el `detail` ya viene con el texto de la región. La web enseña el `detail` junto al campo: `tax_id_invalid` en la identificación fiscal, `postal_code_invalid` en el código postal y `country_not_supported` en el país.
- **Qué no cambia:** las rutas, los cuerpos (salvo `postal_code` opcional), las peticiones y Checkout. Stripe recibe a ese cliente sin identificación fiscal; la factura de Odoo la lleva.

Un 422 sin `code` (con `detail` en lista) es un cuerpo mal formado: un campo que falta o sobra.

#### Errores de `POST /api/v1/me/billing/seats`

Todos con `detail` listo para mostrar:

| Código | `code` | Cuándo |
|---|---|---|
| 404 | — | La ficha no es de la cuenta o no existe |
| 409 | `beta_no_charge` | Durante la beta: activa con la ruta de activación |
| 409 | `profile_incomplete` | Falta el perfil fiscal: pídelo antes |
| 409 | `seat_pending` | Ya hay un pago en marcha para esa ficha: una petición pendiente, un Checkout abierto sin caducar (31 minutos) o una plaza ya cobrada que aún no ha activado la ficha. Trae `request_id` del existente: la web sigue ese en vez de crear otro |
| 409 | `already_active` | La ficha ya está activa en ese plan |
| 409 | `account_blocked` | La cuenta está bloqueada: primero hay que pagar |
| 409 | `offboarding_pending` | La cuenta tiene una baja programada |
| 422 | — | `plan` que no es `pro` ni `pro_whatsapp`, o un campo de más |
| 429 | `rate_limited` | Demasiadas operaciones de cobro seguidas |

Dos pestañas a la vez reciben la misma respuesta: la base lo resuelve de forma atómica. Si aun así llegasen dos pagos para la misma ficha, solo una plaza activa la ficha; la otra queda libre en la cuenta (`location_id: null`) y se revisa en el backoffice. No se devuelve nada automáticamente.

#### Códigos de una petición `fallida`

| `code` | `detail` |
|---|---|
| `beta_no_charge` | "Durante la beta no se cobra: activa la ficha en Pro desde «Tus negocios»." |
| `profile_incomplete` | "Antes de pagar, completa tus datos de facturación." |
| `location_not_found` | "Esa ficha ya no está en tu cuenta." |
| `seat_not_found` | "Esa plaza ya no se puede cambiar." |
| `nothing_to_pay` | "No tienes ninguna factura pendiente." |
| `no_customer` | "Todavía no tienes ningún pago con nosotros." |
| `stripe_rejected` | "Stripe no ha aceptado la operación. Revisa tus datos de facturación." |
| `unavailable` | "No hemos podido hablar con Stripe. Prueba otra vez en unos minutos." |
| `bad_url` | "No hemos podido preparar el pago. Prueba otra vez en unos minutos." |

#### Vuelta de Stripe Checkout

Las URL de vuelta las monta el backend con `FICHAITOR_PUBLIC_BASE_URL`, solo con estos dos parámetros:

- pago hecho: `<base>/app?pago=ok&ficha=<location_id>`;
- pago cancelado: `<base>/app?pago=cancelado&ficha=<location_id>`.

`pago=ok` no garantiza que ya esté cobrado: la ficha se activa cuando llega el aviso de Stripe, normalmente en segundos. La web vuelve a leer `GET /api/v1/me` hasta ver la ficha activa (o la plaza en `billing.seats`).

### El bloque `billing` de `GET /api/v1/me`

```json
"billing": {
  "profile_complete": true,
  "blocked": { "since": "2027-03-14T09:00:05Z", "reason": "payment_failed" },
  "seats": [
    { "id": "seat_9a1f00c2d3e4f5a6b7c8d9e0", "plan": "pro", "location_id": "loc_3f2a",
      "status": "active", "renews_at": "2028-03-14", "trial_ends_at": null,
      "cancel_at_period_end": false, "next_price": 3500, "payment_method": "card",
      "collection": "auto" }
  ],
  "new_seat_price": { "pro": 3325, "pro_whatsapp": 5900 }
}
```

- `blocked`: `null` si la cuenta no está bloqueada. `reason`: `payment_failed`, `action_required` (3DS sin completar), `dispute` o `manual_unpaid`.
- `seats`: las plazas pagadas (no salen los Checkout sin pagar). `status`: `active`, `payment_failed`, `action_required`, `disputed` o `canceled` (una cancelada sale hasta el final de su año). `location_id: null`: una plaza pagada sin ficha. `renews_at`: el día que se cobra el año siguiente (o en que acaba, si está cancelada). `next_price`: céntimos sin IVA del año siguiente; `null` si no se va a renovar.
- `trial_ends_at` (nuevo, ventana beta): fecha y hora UTC del **primer cobro** de una plaza que una cuenta de la beta compró en su ventana, mientras no se ha cobrado. `renews_at` es ese mismo día y `next_price` es lo que se cobrará (sin IVA). `null` en cualquier otra plaza y en cuanto se cobra. La web dice, por ejemplo: «Gratis hasta el 31/03/2027. Ese día se cobra el primer año: 35,00 € + IVA. Cancélala antes si no quieres seguir». Cancelar en esos días (`POST …/seats/{id}/cancel`) hace que no se cobre nada y la ficha deja su plaza ese día.
- `new_seat_price`: lo que cuesta una plaza nueva de cada plan, en céntimos, sin IVA y por un año entero. Sale de la regla del 5 %. En la ventana beta se cobra al acabar la ventana, no hoy.
- `pricing.charging` sale de `billing_setting.launch_at` (vacío: beta). **`pricing.trial_days`** (nuevo): solo para una cuenta de la beta durante su ventana, los días enteros (redondeados hacia arriba) que le quedan antes del primer cobro de una plaza que compre ahora. `null` en la beta, en cualquier cuenta creada desde el lanzamiento y cuando la ventana ha acabado. No es una prueba gratis general: la web solo lo enseña si viene con un número («Te quedan 12 días de beta: elige tu plan; no se cobra nada hasta el 31/03/2027»).

### El paywall en la activación: 402 `no_seat`

Sin hueco en el plan, `POST /api/v1/me/locations/{id}/activation` responde:

```json
{
  "detail": "Esta ficha necesita una plaza Pro+: 59,00 € + IVA por un año. Se renueva sola salvo que la canceles. Pásate a Pro o a Pro+ para activarla.",
  "code": "no_seat",
  "offer": { "plan": "pro_whatsapp", "price": 5900 },
  "charging": true
}
```

- `offer.plan`: el plan pedido (Pro si no se pidió ninguno). `offer.price`: lo que cuesta hoy esa plaza, en céntimos sin IVA.
- Con `charging: false` (beta) no hay nada que comprar: el `detail` dice que ya se usan todas las fichas Pro de la beta.

### Cuenta bloqueada: 402 `account_blocked`

Con la cuenta bloqueada, las rutas de datos responden:

```json
{ "detail": "Tu cuenta está en pausa por un pago pendiente. Paga la factura pendiente o cambia la tarjeta en Ajustes > Facturación para seguir usando Fichaitor.", "code": "account_blocked" }
```

| Siguen respondiendo | 402 `account_blocked` |
|---|---|
| `GET /api/v1/me` (con `billing.blocked`), todas las de `/api/v1/me/billing/*`, la baja (`/api/v1/me/offboarding/*`), cerrar sesión, `GET /api/v1/me/channels`, desvincular un chat (`DELETE …/channels/{id}`) y desactivar una ficha (`DELETE …/activation`) | Resumen, diagnóstico, sentimiento, reseñas y respuestas, acciones, fechas clave, elegir ficha (`POST /api/v1/me/location`), activar una ficha, crear un código de Telegram y cambiar el filtro de un chat |

La web enseña una pantalla única con el motivo, «Pagar ahora» (`POST …/billing/pay`), «Cambiar tarjeta» (`POST …/billing/portal`), «Darse de baja» y «Cerrar sesión». Al pagar, Stripe avisa y la cuenta se desbloquea sola: la web vuelve a leer `GET /api/v1/me`.

### Lo que la web NO debe hacer con el cobro

- Calcular precios, IVA o el 5 %: salen de `new_seat_price`, `next_price` y `offer`.
- Anunciar «30 días gratis» a una cuenta nueva: no hay prueba gratis. Solo la ventana beta, con `pricing.trial_days` o `trial_ends_at`.
- Abrir una `url` que no sea `https://…stripe.com/…`.
- Crear otra plaza tras un 409 `seat_pending`: sigue el `request_id` que trae.
- Guardar el NIF o la dirección en el navegador.
- Dar por activada una ficha por volver con `pago=ok`: espera a verla activa en `GET /api/v1/me`.
- Remitir a soporte para pasar a Pro: el paywall lo resuelve.

## Pantallas mínimas para tener algo visible

1. **Inicio** (`/`): diagnóstico público, resultado, lista de espera y botón "Conectar con Google".
2. **App** (`/app`): lee `?google=`; sin sesión, invita a conectar; con sesión y sin ficha elegida, lista de fichas; con ficha elegida, panel con el diagnóstico y el resumen de esa ficha (los dos salen de la ficha conectada) y botón de salir. Desde el 2026-11-12, también la lista de reseñas, con el borrador y la publicación de la respuesta, y el bloque de sentimiento del establecimiento.
3. **Privacidad** (`/privacidad/`) y **condiciones** (`/condiciones-de-uso/`).

## Siguiente versión del contrato

El resumen de la ficha, las reseñas con su respuesta ([Reseñas y respuestas](#reseñas-y-respuestas-2026-11-12)) y el sentimiento del establecimiento ([Sentimiento del establecimiento](#sentimiento-del-establecimiento-2026-11-12)) ya existen. Lo siguiente son las fechas clave. Se añadirán aquí antes de implementarse.

## Demostración por captura de reseñas (2026-11-11)

### `POST /api/v1/public/diagnosis/screenshot`

Para quien aún no conecta su ficha: el dueño sube una captura de la pestaña **Reseñas** de su negocio en Google Maps y recibe cuántas de las reseñas visibles están sin contestar y una respuesta propuesta para una de ellas. No devuelve nota. Detalle de funcionamiento, límites y privacidad en `docs/pantallazo.md`.

Petición `multipart/form-data` con `image` (PNG, JPEG o WebP; 6 MB como máximo) y `source` opcional. Usa `fetch` con `FormData`, sin poner `Content-Type` a mano.

Respuesta 200:

```json
{
  "business_name": "Bar La Parra",
  "reviews_seen": 4,
  "unanswered": 3,
  "unanswered_low": 1,
  "headline": "Veo 4 reseñas y 3 están sin contestar. 1 de ellas es de 1 o 2 estrellas.",
  "suggested_reply": {
    "stars": 1,
    "review_excerpt": "Esperamos cuarenta minutos y la comida llegó fría.",
    "reply": "Sentimos mucho que tu visita no fuera como esperabas. ..."
  },
  "locked_checks": [{ "code": "unanswered_reviews", "title": "Cuántas reseñas tienes sin responder" }],
  "attribution": "Según la captura que has enviado"
}
```

- `business_name` y `suggested_reply` pueden ser `null`. Con `suggested_reply: null` el resto es válido.
- `review_excerpt` (200 caracteres como máximo) va vacío si la reseña solo tiene estrellas. `reply`: 600 como máximo.
- La respuesta nunca incluye el nombre de quien escribió una reseña.
- Pinta `business_name`, `review_excerpt` y `reply` como texto, nunca como HTML.
- No guardes la respuesta ni la imagen en el navegador.
- Puede tardar hasta 30 segundos. No reintentes sola.

Errores, todos con `detail` en español listo para mostrar:

| Código | Cuándo |
|---|---|
| 413 | La captura pesa más de 6 MB |
| 415 | No es PNG, JPEG ni WebP (se mira el contenido) |
| 422 | Falta la imagen; o no es una ficha de Google; o es la ficha pero no la pestaña de reseñas; o no se distingue ninguna reseña |
| 429 | Más de 3 capturas por hora desde la misma IP |
| 503 | El modelo no responde o se alcanzó el tope diario global |
| 404 | La función no está configurada en ese entorno |

Con `FICHAITOR_FAKE_SCREENSHOT=true` la respuesta depende del formato: PNG da el 200 de ejemplo, JPEG el 422 de "no es una ficha" y WebP el 422 de "no es la pestaña de reseñas".
