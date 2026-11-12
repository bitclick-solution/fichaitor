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
- Se envía un correo que confirma la restauración («Has restaurado Fichaitor»).

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

## Reseñas y respuestas (2026-11-12)

Primera función que **escribe en Google**: el dueño ve las reseñas de su ficha, pide una respuesta redactada si quiere y **publica la que confirma**. Una respuesta es pública y Google avisa a quien escribió la reseña: no se puede deshacer, solo editar o borrar después.

Las cuatro rutas trabajan sobre la **ficha elegida de la cuenta de la sesión** y sobre ninguna otra. Los identificadores (`rev_…`, `act_…`) son propios de cada cuenta: no sirven en otra.

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

Estado de una acción de esa cuenta. Respuesta 200:

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

Con sesión. Devuelve el análisis de la ficha elegida de esa cuenta. No lleva parámetros: nada de la petición elige la ficha.

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
