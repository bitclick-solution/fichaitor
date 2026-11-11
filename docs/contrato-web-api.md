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
- Sin Google real: con `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_PLACES=true` todo el recorrido funciona con datos de ejemplo. El diagnóstico del panel no depende de `FICHAITOR_FAKE_PLACES`: sale de la ficha de demostración que lee el worker.

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

> **Importante (cambio del 2026-11-11): el diagnóstico del panel sale de la ficha conectada, no de Google Maps.**
>
> - La API ya no consulta Google Places en esta ruta. Calcula el diagnóstico con los datos que el worker lee del Perfil de Empresa del dueño: los mismos de `GET /api/v1/me/summary`, más descripción, categorías, teléfono, web, fotos y atributos.
> - `attribution` pasa a ser `"Datos de tu Perfil de Empresa de Google"`. Muéstralo tal cual. Ya no aplica la regla de "Datos de Google Maps" en esta ruta.
> - `locked_checks` va siempre vacío: con la ficha conectada no hay nada bloqueado.
> - La respuesta 200 gana tres campos: `status`, `synced_at` y `stale`.
> - Hay un estado nuevo, "todavía no hay diagnóstico": **404 con `status` y `detail`**. Consulta [Cuando todavía no hay diagnóstico](#cuando-todavía-no-hay-diagnóstico).
> - Desaparecen de esta ruta el 429 (ya no hay límite) y el 503 de Google. El 404 cambia de significado.
> - Aparecen mejoras nuevas (`code`): `unanswered_reviews`, `no_holiday_hours`, `no_description`, `short_description` y `google_changed`.

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
| `pendiente` | Todavía no hay resumen: la sincronización se ha pedido y el worker no ha terminado. Todos los demás campos van a `null` y `stale` a `false` | Muestra las tarjetas cargando y vuelve a preguntar |
| `ok` | Cifras de la última sincronización | Muestra las tarjetas |
| `needs_reconnect` | Google ha retirado el permiso, o no hay ninguna conexión guardada para esa cuenta. Las cifras son las de la última lectura correcta, o `null` si nunca la hubo | Pide volver a conectar con Google. Si hay cifras, muéstralas con su fecha |
| `error` | La última sincronización falló: Google no respondió o rechazó la lectura, o la ficha ya no está en la conexión más reciente. Las cifras son las de la última lectura correcta, o `null` si nunca la hubo | Si hay cifras, muéstralas con su fecha. Si no, "No hemos podido leer tu ficha. Prueba más tarde" |

Con `needs_reconnect` y `error`, `google_changed_fields` y `google_changed_labels` son listas, vacías si nunca hubo una lectura correcta.

`pendiente` no es un estado en el que una ficha se quede: toda sincronización que falla deja `needs_reconnect` o `error`. Si el worker está parado, `pendiente` dura hasta que vuelve a arrancar.

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
- Excepción: al volver de conectar con `google=ok`, el resumen puede seguir unos segundos en `needs_reconnect` o `error`, hasta que el worker termina la sincronización que se acaba de pedir. Vuelve a pedirlo una sola vez pasados 10 segundos.
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
2. **App** (`/app`): lee `?google=`; sin sesión, invita a conectar; con sesión y sin ficha elegida, lista de fichas; con ficha elegida, panel con el diagnóstico y el resumen de esa ficha (los dos salen de la ficha conectada) y botón de salir.
3. **Privacidad** (`/privacidad/`) y **condiciones** (`/condiciones-de-uso/`).

## Siguiente versión del contrato

El resumen de la ficha (`GET /api/v1/me/summary`) ya existe. Lo siguiente es la lista de reseñas con respuesta propuesta y las fechas clave. Se añadirán aquí antes de implementarse.

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
