# Contrato del backoffice

Contrato HTTP entre el proceso `fichaitor-admin` (`python -m app.admin`) y su interfaz, `web/admin/`. Lo que puede hacer cada rol, y por qué, está en la sección 6 de `docs/admin-facturacion.md`. Fecha: 2026-11-13. Estado: implementado (paso 12, backend). Revisado el 2026-11-13: métricas comprobadas contra lo que las registra y paginación por cursor en todos los listados.

## 1. Reglas generales

- **Origen.** El backoffice solo se abre por el túnel SSH, en `http://localhost:8090` o `http://127.0.0.1:8090`. Cualquier otro `Host` recibe `400 {"code": "host_not_allowed"}`. La interfaz se sirve en el mismo origen y llama a rutas relativas (`/admin/api/...`). No hay CORS.
- **Formato.** JSON en UTF-8. Las fechas, en ISO 8601 con zona (`2027-03-14T09:00:05.123456+00:00`). Los importes, en céntimos y sin IVA, salvo que se diga otra cosa.
- **Cabeceras de toda petición que cambia algo** (todo lo que no es `GET`):
  - `X-Requested-With: fichaitor-admin`, siempre;
  - `X-CSRF-Token: <csrf_token>`, cuando hay sesión. El valor llega en la respuesta de `POST /login`, `POST /session/totp`, `POST /login/recovery` y `GET /session`. Depende de la cookie: cámbialo cada vez que entres.
  - Si el navegador manda `Origin` o `Sec-Fetch-Site`, tienen que ser del propio backoffice. Si no, `403 {"code": "csrf_failed"}`.
- **Sesión.** Cookie `__Host-fichaitor_admin` (`Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/`). La pone el servidor; la interfaz no la lee. Caduca a las 8 horas o tras 30 minutos sin actividad.
- **Caché.** Todas las respuestas llevan `Cache-Control: no-store`. No guardes respuestas en `localStorage` ni en `sessionStorage`.
- **CSP.** `default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`. Sin scripts ni estilos en línea, sin fuentes ni recursos de terceros. El QR del TOTP se dibuja en el navegador con `qrcode-generator` (como imagen `data:` o en un `<canvas>`).
- **Referencia del ticket** (`ticket_ref`). La piden las lecturas de datos personales y todas las acciones: de 3 a 500 caracteres (80 en los reembolsos), en una línea y **sin direcciones de correo**. Se guarda en la auditoría.

### 1.1 Errores

Todos los errores tienen esta forma:

```json
{ "code": "role_not_allowed", "message": "Tu rol no permite hacer esto." }
```

`message` está en español y se puede enseñar tal cual. Nunca lleva datos personales.

| HTTP | `code` | Qué hacer en la interfaz |
|---|---|---|
| 400 | `host_not_allowed` | Explicar que se entra por `http://localhost:8090` |
| 401 | `session_invalid` | Volver a la pantalla Entrar |
| 401 | `invalid_credentials` | «Usuario o contraseña incorrectos». Es la misma respuesta si el usuario no existe o está bloqueado |
| 401 | `totp_invalid` | El código no vale o ya se usó. Pedir el siguiente |
| 401 | `recovery_invalid` | El código de recuperación no vale o ya se usó |
| 403 | `csrf_failed` | Error de la interfaz: falta una cabecera |
| 403 | `role_not_allowed` | Tu rol no puede. La interfaz no debería ofrecerlo |
| 403 | `totp_required` | La acción es sensible y el último TOTP tiene más de 5 minutos: pedir un código, llamar a `POST /session/totp` y repetir la petición |
| 404 | `not_found`, `enrollment_invalid` | No existe, o el enlace de alta no vale o caducó |
| 409 | `enrollment_expired` | Pasaron más de 15 minutos desde que se mostró el QR: volver a empezar el alta del TOTP |
| 422 | `validation` | Faltan campos o no tienen el formato. `fields` lista los campos, nunca sus valores |
| 422 | `invalid` | La base rechaza la acción. `message` dice por qué |
| 422 | `invalid_cursor` | El `cursor` no lo hizo el backoffice o no vale: volver a la primera página |
| 422 | `password_too_short`, `password_too_long`, `password_common`, `password_has_username` | Contraseña nueva rechazada |
| 422 | `dmarc_failed` | El mensaje pegado no prueba su remitente. `message` lleva el motivo |
| 429 | `too_many_attempts` | 20 fallos en la última hora en todo el backoffice: esperar |
| 503 | `authserv_not_configured` | Falta `FICHAITOR_ADMIN_AUTHSERV_ID` en `admin.env` |

### 1.2 Matriz de roles

Viene de `GET /session` (`role`). La base vuelve a comprobarla en cada petición.

| Pantalla o acción | Rutas | `soporte` | `finanzas` | `propietario` |
|---|---|---|---|---|
| Buscar cuentas | `GET /accounts` | Sí | Sí | Sí |
| Cuenta: resumen, nombres de las fichas (con ticket) y chats | `GET /accounts/{id}`, `GET /accounts/{id}/lists/{locations, entitlements, seats, blocks, activations, offboardings}`, `POST /accounts/{id}/locations`, `GET /accounts/{id}/channels` | Sí | Sí | Sí |
| Baja por correo | `POST /accounts/{id}/offboarding/check`, `POST /accounts/{id}/offboarding` | Sí | No | Sí |
| Salud y sincronización | `GET /health/overview`, `GET /health/failed-jobs`, `GET /health/retention-runs`, `GET /health/sync-errors`, `GET /accounts/{id}/sync` | Sí | No | Sí |
| Cobro de una cuenta, datos fiscales y revisión | `GET /accounts/{id}/billing`, `GET /accounts/{id}/lists/{billing_seats, charges, documents, refunds, billing_blocks}`, `POST /accounts/{id}/billing/contact`, `GET /billing/review`, `GET /billing/review/lists/{nombre}` | No | Sí | Sí |
| Reembolso excepcional | `POST /refunds` | No | Sí | Sí |
| Métricas | `GET /metrics` | No | Sí | Sí |
| Conceder y recortar derechos | `POST /accounts/{id}/entitlements`, `POST /entitlements/{id}/trim` | No | No | Sí |
| Cerrar un bloqueo | `POST /blocks/{id}/close` | No | No | Sí |
| Lista de espera (totales) | `GET /waitlist` | No | No | Sí |
| Usuarios y auditoría | `GET /users`, `GET /audit` | No | No | Sí |
| Comunicaciones por correo (sección 10) | `GET /communications`, `POST /communications`, `PUT /communications/{id}`, `GET /communications/{id}`, `GET /communications/{id}/preview`, `GET /communications/{id}/preview.html`, `GET /communications/{id}/audience`, `POST /communications/{id}/test`, `POST /communications/{id}/confirm` | No | No | Sí |

Todas las rutas empiezan por `/admin/api`.

### 1.3 Paginación

Todos los listados van por páginas **con cursor (keyset), nunca con desplazamiento** (2026-11-13). Así, una fila que se inserta mientras pasas de página ni repite otra ni deja un hueco: si cae antes del cursor no sale, y si cae después sale en su sitio.

- **Petición:** `limit` (cuántas filas) y `cursor` (opcional), como parámetros de la URL. Sin `cursor`, la primera página. También en `POST /accounts/{id}/locations`, en la URL.
- **Tope de `limit`:** 200 en todos los listados y 500 en `GET /audit`. Un valor mayor o menor que 1 da `422 validation`.
- **Respuesta:** el listado y `next_cursor`. `null` si no hay más páginas. Para la siguiente, repite la petición con `cursor = next_cursor`.
- **El cursor es opaco.** No lo construyas ni lo guardes: es la clave de la última fila enseñada (base64url de un JSON pequeño). Uno que no sea del backoffice da `422 invalid_cursor`.
- **Pantallas con varios listados** (el resumen y el cobro de una cuenta, la revisión del cobro y la salud): traen la primera página de cada uno y un objeto `next_cursors` con el cursor de cada listado, o `null`. Las páginas siguientes se piden a su ruta (tabla de abajo).
- **Orden.** Cada listado tiene una clave estable, respaldada por un índice (una clave primaria, un índice único o uno de los índices `*_keyset` de `db/schema_admin.sql`):

| Listado | Ruta de las páginas siguientes | Primera página en | Orden (clave) | `limit` por defecto |
|---|---|---|---|---|
| Búsqueda de cuentas | `GET /accounts?q=…` | — | `created_at`, `account_id`, de la más vieja a la más nueva | 20 |
| Fichas de una cuenta (resumen, sin nombres) | `GET /accounts/{id}/lists/locations` | `GET /accounts/{id}` | `id` | 50 |
| Derechos | `GET /accounts/{id}/lists/entitlements` | `GET /accounts/{id}` | `starts_at`, `id`, del más nuevo al más viejo | 50 |
| Plazas (resumen) | `GET /accounts/{id}/lists/seats` | `GET /accounts/{id}` | `created_at`, `id`, de la más vieja a la más nueva | 50 |
| Bloqueos (resumen) | `GET /accounts/{id}/lists/blocks` | `GET /accounts/{id}` | `blocked_at`, `id`, del más nuevo al más viejo | 50 |
| Activaciones | `GET /accounts/{id}/lists/activations` | `GET /accounts/{id}` | `at`, `id`, de la más nueva a la más vieja | 50 |
| Bajas | `GET /accounts/{id}/lists/offboardings` | `GET /accounts/{id}` | `confirmed_at`, `id`, de la más nueva a la más vieja | 50 |
| Nombres de las fichas (con ticket) | `POST /accounts/{id}/locations` | — | `location_id` | 50 |
| Sincronización de una cuenta | `GET /accounts/{id}/sync` | — | `status_at`, `location_id`, de lo más nuevo a lo más viejo | 50 |
| Chats de una cuenta | `GET /accounts/{id}/channels` | — | `verified_at`, `identity_id`, de lo más viejo a lo más nuevo | 50 |
| Plazas (cobro) | `GET /accounts/{id}/lists/billing_seats` | `GET /accounts/{id}/billing` (`seats`) | `created_at`, `id`, de la más vieja a la más nueva | 50 |
| Cobros | `GET /accounts/{id}/lists/charges` | `GET /accounts/{id}/billing` | `created_at`, `stripe_invoice_id`, del más nuevo al más viejo | 50 |
| Facturas legales | `GET /accounts/{id}/lists/documents` | `GET /accounts/{id}/billing` | `created_at`, `id`, de la más nueva a la más vieja | 50 |
| Reembolsos | `GET /accounts/{id}/lists/refunds` | `GET /accounts/{id}/billing` | `created_at`, `id`, del más nuevo al más viejo | 50 |
| Bloqueos (cobro) | `GET /accounts/{id}/lists/billing_blocks` | `GET /accounts/{id}/billing` (`blocks`) | `blocked_at`, `id`, del más nuevo al más viejo | 50 |
| Revisión: facturas | `GET /billing/review/lists/documents` | `GET /billing/review` | `created_at`, `id`, de la más vieja a la más nueva | 50 |
| Revisión: plazas | `GET /billing/review/lists/seats` | `GET /billing/review` | `updated_at`, `id`, de la más vieja a la más nueva | 50 |
| Revisión: reembolsos | `GET /billing/review/lists/refunds` | `GET /billing/review` | `created_at`, `id`, del más nuevo al más viejo | 50 |
| Revisión: eventos de Stripe | `GET /billing/review/lists/events` | `GET /billing/review` | `received_at`, `id`, del más nuevo al más viejo | 50 |
| Revisión: pagados sin factura | `GET /billing/review/lists/paid_without_document` | `GET /billing/review` | `starts_at`, `id`, del más viejo al más nuevo | 50 |
| Revisión: bloqueos abiertos | `GET /billing/review/lists/blocks` | `GET /billing/review` | `blocked_at`, `id`, del más viejo al más nuevo | 50 |
| Jobs fallidos | `GET /health/failed-jobs` | `GET /health/overview` | `finished_at` (los que no tienen, al final), `job_id`, de lo más nuevo a lo más viejo | 50 |
| Pasadas de la retención | `GET /health/retention-runs` | `GET /health/overview` | `at`, `run_id`, de la más nueva a la más vieja | 10 |
| Sincronizaciones con error | `GET /health/sync-errors` | `GET /health/overview` | `status_at`, `location_id`, de lo más nuevo a lo más viejo | 50 |
| Lista de espera (celdas) | `GET /waitlist` | — | `week`, y canal, tramo y origen | 100 |
| Personas | `GET /users` | — | `username` | 50 |
| Auditoría | `GET /audit` | — | `id`, de lo más nuevo a lo más viejo | 100 |
| Comunicaciones | `GET /communications` | — | `created_at`, `communication_id`, de la más nueva a la más vieja | 50 |

- `GET /accounts/{id}/lists/{nombre}` y `GET /billing/review/lists/{nombre}` responden `{"items": [...], "next_cursor": "…" | null}`, con las filas en la misma forma que en la pantalla que trae la primera página. Las de cobro llevan sus enlaces ya construidos, y las de la revisión, además, `links` y `odoo_link`. Un nombre que no está en la tabla da `404 not_found`.
- **No es un listado** `jobs` de `GET /health/overview`: una fila por tipo y estado de job, acotada por el código.

### 1.4 Acciones sensibles

Piden un TOTP de menos de 5 minutos: reembolsar, conceder o recortar derechos, cerrar un bloqueo, tramitar la baja, confirmar el envío de una comunicación y cambiar la contraseña. `GET /session` devuelve `totp_fresh_until`: antes de esa hora la acción pasa sin pedir código. Si la respuesta es `403 totp_required`, pide un código, llama a `POST /session/totp` y repite la petición.

## 2. Sesión

### `POST /admin/api/login`

Primer paso. Sin sesión: solo `X-Requested-With`.

```json
{ "username": "ana", "password": "…" }
```

Respuesta `200`, con la cookie de la sesión (todavía sin TOTP):

```json
{ "stage": "totp", "csrf_token": "9f…" }
```

Errores: `401 invalid_credentials`, `429 too_many_attempts`.

### `POST /admin/api/session/totp`

Segundo paso del inicio de sesión y, con la sesión ya abierta, la confirmación de una acción sensible.

```json
{ "code": "123456" }
```

Respuesta `200`:

```json
{
  "stage": "ok",
  "username": "ana",
  "role": "soporte",
  "totp_fresh_until": "2027-03-14T09:05:05+00:00",
  "csrf_token": "9f…"
}
```

- Un código vale una sola vez: el mismo código dos veces da `401 totp_invalid`. Si el usuario confirma dos acciones en el mismo medio minuto, tiene que esperar al código siguiente.
- 5 fallos seguidos (de contraseña o de código) bloquean a la persona 15 minutos, el doble en cada bloqueo seguido. La interfaz no lo sabe: la respuesta es la misma.
- La sesión a la espera del TOTP dura 5 minutos.

### `POST /admin/api/login/recovery`

En lugar del TOTP, con la sesión a la espera del código. El código se puede escribir con o sin guiones y en mayúsculas o minúsculas.

```json
{ "code": "k7m2-q9xa-4tpz" }
```

Respuesta `200 {"stage": "reenroll", "csrf_token": "…"}`. El código queda gastado y la persona **tiene que dar de alta un TOTP nuevo en ese momento** (5 minutos) con `POST /totp/start` y `POST /totp/finish`. Hasta entonces, ninguna otra pantalla funciona (`401`). Si no lo termina, solo el propietario puede restablecerla.

### `GET /admin/api/session`

Quién es y en qué estado está la sesión. Úsalo al cargar la página.

- `200` con la forma de `POST /session/totp` (`stage: "ok"`);
- `200 {"stage": "reenroll", "csrf_token": "…"}` tras un código de recuperación;
- `401 session_invalid` si no hay sesión válida.

### `POST /admin/api/logout`

Cierra la sesión y borra la cookie. Respuesta `200 {"ok": true}`.

### `POST /admin/api/totp/start` y `POST /admin/api/totp/finish`

Dar de alta un TOTP nuevo: obligatorio tras un código de recuperación, y voluntario con un TOTP de menos de 5 minutos.

`start`, sin cuerpo. Respuesta `200`:

```json
{
  "otpauth_uri": "otpauth://totp/Fichaitor%20backoffice:ana?secret=…&issuer=Fichaitor%20backoffice",
  "secret": "JBSWY3DPEHPK3PXP…",
  "state": "v1.…"
}
```

- Dibuja el QR con `otpauth_uri`. Enseña `secret` debajo, para teclearlo a mano.
- Guarda `state` en memoria (nunca en el almacenamiento del navegador) y devuélvelo en `finish`. Vale 15 minutos.

`finish`:

```json
{ "state": "v1.…", "code": "123456" }
```

Respuesta `200 {"recovery_codes": ["k7m2-q9xa-4tpz", "…"]}`: **10 códigos nuevos** que sustituyen a los anteriores. Enséñalos una vez, con un aviso para guardarlos fuera del ordenador. Errores: `403 totp_required` (pasaron más de 5 minutos), `409 enrollment_expired`, `422 totp_invalid`.

### `POST /admin/api/password`

Cambiar la propia contraseña. Acción sensible. Cierra las demás sesiones de la persona.

```json
{ "current_password": "…", "new_password": "…" }
```

Respuesta `200 {"ok": true, "changed_at": "…"}`. Errores: `401 invalid_credentials` (la actual no coincide; cuenta como fallo), `422 password_*`, `403 totp_required`.

**Reglas de la contraseña:** de 12 a 128 caracteres; no puede estar en la lista de contraseñas comunes, contener el usuario, ni ser una repetición o una secuencia (`aaaaaaaaaaaa`, `abcdefghijkl`, `123456789012`).

## 3. Alta

El propietario ejecuta `scripts/admin_usuario.py alta` y da a la persona un enlace `http://localhost:8090/#alta=<token>`, válido 24 horas y de un solo uso. El token va en el **fragmento**: la interfaz lo lee de `location.hash`, lo borra de la barra de direcciones (`history.replaceState`) y lo manda en el cuerpo. Nunca en la URL de una petición.

### `POST /admin/api/enrollment/start`

```json
{ "token": "…" }
```

Respuesta `200`:

```json
{
  "username": "ana",
  "role": "soporte",
  "otpauth_uri": "otpauth://totp/…",
  "secret": "…",
  "state": "v1.…"
}
```

`404 enrollment_invalid` si el enlace no vale, caducó o ya se usó.

### `POST /admin/api/enrollment/finish`

```json
{ "token": "…", "password": "…", "state": "v1.…", "code": "123456" }
```

Respuesta `200 {"username": "ana", "recovery_codes": ["…", "… (10)"]}`. Después, la persona entra con `POST /login`. Errores: `422 password_*`, `422 totp_invalid`, `409 enrollment_expired`, `404` o `409 enrollment_invalid`.

## 4. Cuentas

### `GET /admin/api/accounts?q=<búsqueda>`

Busca por id de cuenta (UUID), id de ficha (`loc_…`), correo exacto, NIF o razón social (contiene). Nunca por el nombre de una ficha. De 3 a 254 caracteres. Por páginas (`limit`, por defecto 20, y `cursor`; sección 1.3).

```json
{
  "accounts": [
    {
      "account_id": "6f1c…",
      "email_masked": "d***o@example.com",
      "created_at": "…",
      "deleted_at": null,
      "disconnected_at": null,
      "locations": 2,
      "active_locations": 1
    }
  ],
  "next_cursor": null
}
```

### `GET /admin/api/accounts/{account_id}`

Resumen: plan, plazas, derechos, bloqueos, activaciones y bajas. Sin nombres de fichas, sin direcciones y sin identificadores de Stripe.

```json
{
  "account": { "id": "…", "email_masked": "d***o@example.com", "created_at": "…",
               "deleted_at": null, "disconnected_at": null },
  "blocked": false,
  "locations": [
    { "id": "loc_3f2a…", "plan": "pro", "active_since": "…", "deactivated_at": null,
      "removed_at": null, "verified": true }
  ],
  "entitlements": [
    { "id": "…", "plan": "pro", "quantity": 1, "starts_at": "…", "ends_at": "…",
      "source": "pago", "seat_id": "seat_…", "provisional": false, "granted_by": null,
      "note_code": null, "current": true }
  ],
  "seats": [
    { "id": "seat_…", "plan": "pro", "collection": "auto", "location_id": "loc_…",
      "status": "active", "current_period_start": "…", "current_period_end": "…",
      "cancel_at_period_end": false, "next_price_key": "pro_first_yearly",
      "trial_ends_at": null, "review_reason": null, "created_at": "…" }
  ],
  "blocks": [
    { "id": 12, "blocked_at": "…", "reason": "payment_failed", "seat_id": "seat_…",
      "unblocked_at": null, "unblocked_by": null }
  ],
  "activations": [
    { "at": "…", "location_id": "loc_…", "event": "activated", "plan": "pro", "reason": null }
  ],
  "offboardings": [
    { "id": "…", "scope": "ficha", "confirmed_at": "…", "delete_at": "…",
      "restored_at": null, "effective_at": null, "reason": "email_request" }
  ],
  "next_cursors": { "locations": null, "entitlements": null, "seats": null, "blocks": null,
                    "activations": "eyJ…", "offboardings": null }
}
```

- `source` de un derecho: `beta`, `manual`, `pago`, `cortesia` o `recomendacion`. Solo los que no son `pago` se pueden recortar.
- `next_price_key` → precio: `pro_first_yearly` 3500, `pro_additional_yearly` 3325, `whatsapp_first_yearly` 5900, `whatsapp_additional_yearly` 5605.
- Cada listado trae su primera página (50 filas). Las siguientes, con `GET /accounts/{id}/lists/{nombre}` y el cursor de `next_cursors` (sección 1.3).

### `POST /admin/api/accounts/{account_id}/locations`

Los nombres de las fichas (dato de Google). Solo con la referencia del ticket, que se audita con la lectura.

```json
{ "ticket_ref": "Ticket 4321" }
```

Respuesta: `{"locations": [{"location_id", "title", "active_since", "plan", "deactivated_at", "removed_at", "verified"}], "next_cursor"}`. Por páginas, con `limit` y `cursor` en la URL. Cada página es una lectura con ticket y se audita.

### `GET /admin/api/accounts/{account_id}/sync`

`{"sync": [{"account_id", "location_id", "status", "status_at", "synced_at"}], "next_cursor"}`. `status`: `ok`, `needs_reconnect` o `error`. Por páginas.

### `GET /admin/api/accounts/{account_id}/channels`

Los chats vinculados, sin direcciones ni teléfonos: `{"channels": [{"identity_id", "channel", "verified_at", "last_inbound_at", "unlinked_at"}], "next_cursor"}`. Por páginas.

### Baja por correo

Sustituye a `scripts/baja_manual.py` sin `--inmediata`. Tres pasos en la misma pantalla:

1. **`POST /admin/api/accounts/{account_id}/offboarding/check`.** Pega el original completo del mensaje (en Gmail, «Mostrar original»), con las cabeceras:

   ```json
   { "raw_message": "Authentication-Results: mx.fichaitor.es; dmarc=pass …\r\nFrom: …" }
   ```

   El servidor mira solo la **primera** cabecera `Authentication-Results`, que tiene que ser de nuestro servidor de correo, con `dmarc=pass` y `header.from` igual al dominio del `From:`. Después, la base compara el `From:` con el correo de la cuenta sin enseñarlo. Respuesta `200`:

   ```json
   { "dmarc": "pass", "from_domain": "example.com", "matches_account": true }
   ```

   - `matches_account: false`: el correo no es el de la cuenta. No sigas.
   - `422 dmarc_failed`: el mensaje no prueba su remitente. Contesta al cliente que haga la baja desde el panel. El motivo va en `message` (`no_authentication_results`, `wrong_authserv_id`, `no_dmarc`, `ambiguous_dmarc`, `dmarc_not_pass`, `no_from` o `domain_mismatch`).
   - Hasta 256 KiB.

2. **Elige el alcance** (`ficha` o `cuenta`) y, si la cuenta tiene varias fichas y el alcance es `ficha`, cuál. Escribe la referencia del ticket.
3. **`POST /admin/api/accounts/{account_id}/offboarding`.** Acción sensible. Exige un `check` con `matches_account: true` de la misma persona y la misma cuenta en los últimos 30 minutos.

   ```json
   { "scope": "ficha", "location_id": "loc_3f2a…", "ticket_ref": "Ticket 4321" }
   ```

   Respuesta `200 {"offboarding_id": "…", "delete_at": "…"}`: la baja queda **programada a 48 horas**, con el correo de «baja programada» y su enlace de restauración a la dirección de la cuenta. Errores `422 invalid` con el motivo (por ejemplo, «La cuenta tiene 2 fichas: indica cuál nombra el correo.» o «falta comprobar el remitente del correo»).

La baja inmediata no está en el backoffice: sigue en `scripts/baja_manual.py --inmediata`, con el rol propietario.

## 5. Cobro

### `GET /admin/api/accounts/{account_id}/billing`

El espejo de las tablas de cobro, con los enlaces ya construidos. Sin el PDF, sin la dirección y sin el correo de facturas.

```json
{
  "livemode": false,
  "customer": { "stripe_customer_id": "cus_…", "legal_name": "Bar La Parra SL",
                "tax_id_type": "es_cif", "tax_id": "B12345674", "country": "ES",
                "territory": "peninsula_baleares", "tax_treatment": "iva_21",
                "updated_at": "…", "stripe_url": "https://dashboard.stripe.com/test/customers/cus_…" },
  "seats": [ { "id": "seat_…", "plan": "pro", "collection": "auto",
               "stripe_subscription_id": "sub_…", "location_id": "loc_…", "status": "active",
               "current_period_end": "…", "cancel_at_period_end": false,
               "next_price_key": "pro_first_yearly", "review_reason": null,
               "stripe_url": "https://dashboard.stripe.com/test/subscriptions/sub_…" } ],
  "charges": [ { "stripe_invoice_id": "in_…", "seat_id": "seat_…", "status": "paid",
                 "billing_reason": "subscription_create", "currency": "eur",
                 "subtotal": 3500, "tax": 735, "total": 4235, "amount_paid": 4235,
                 "amount_refunded": 0, "price_key": "pro_first_yearly",
                 "period_start": "…", "period_end": "…", "paid_at": "…",
                 "payment_intent_id": "pi_…", "charge_id": "ch_…", "disputed": false,
                 "stripe_url": "https://dashboard.stripe.com/test/invoices/in_…",
                 "payment_url": "https://dashboard.stripe.com/test/payments/pi_…" } ],
  "documents": [ { "id": "…", "kind": "factura", "stripe_invoice_id": "in_…",
                   "stripe_refund_id": null, "state": "emitida", "odoo_move_id": 812,
                   "number": "FTR/2027/00001", "amount_untaxed": 3500, "amount_tax": 735,
                   "amount_total": 4235, "verifactu_state": "accepted",
                   "review_reason": null, "created_at": "…", "issued_at": "…",
                   "odoo_url": "https://erp.bitclick.solutions/odoo/account.move/812",
                   "stripe_url": "…" } ],
  "refunds": [ { "id": "…", "stripe_invoice_id": "in_…", "amount": null,
                 "reason": "error_cobro", "ticket_ref": "Ticket 77", "requested_by": "eva",
                 "state": "pendiente", "stripe_refund_id": null, "created_at": "…",
                 "finished_at": null } ],
  "blocks": [ { "id": 12, "blocked_at": "…", "reason": "payment_failed",
                "stripe_invoice_id": "in_…", "seat_id": "seat_…", "unblocked_at": null,
                "unblocked_by": null } ],
  "next_cursors": { "seats": null, "charges": null, "documents": null, "refunds": null,
                    "blocks": null }
}
```

- `customer` es `null` si la cuenta no ha rellenado sus datos fiscales.
- Cada listado trae su primera página (50 filas). Las siguientes: `seats` con `GET /accounts/{id}/lists/billing_seats`, `blocks` con `…/lists/billing_blocks`, y `charges`, `documents` y `refunds` con la lista de su mismo nombre.
- `livemode` es el modo de los últimos eventos de Stripe recibidos (`false` si aún no hay ninguno). Decide si los enlaces van a `/test/`.
- Estado de una factura legal: `pendiente`, `emitida` o `revision`. Estado de un reembolso: `pendiente`, `hecho` o `fallido`.

### `POST /admin/api/accounts/{account_id}/billing/contact`

La dirección fiscal y el correo de facturas, con la referencia del ticket (se audita la lectura):

```json
{ "ticket_ref": "Ticket 77" }
```

Respuesta: `{"legal_name", "tax_id", "address_line1", "address_line2", "postal_code", "city", "province", "country", "invoice_email"}`. `404` si la cuenta no tiene datos fiscales.

### `GET /admin/api/billing/review`

Cola de revisión y salud del cobro, para «Cobro > Revisión»:

```json
{
  "livemode": false,
  "counts": { "events_unprocessed": 0, "documents_pending": 1, "documents_overdue": 0,
              "documents_review": 1, "blocks_open": 2, "refunds_pending": 0,
              "paid_without_document": 0 },
  "documents": [ { "id", "account_id", "kind", "stripe_invoice_id", "stripe_refund_id", "state",
                   "odoo_move_id", "number", "amount_total", "verifactu_state",
                   "review_reason", "created_at" } ],
  "seats": [ { "id", "account_id", "plan", "status", "stripe_subscription_id", "review_reason" } ],
  "refunds": [ { "id", "account_id", "stripe_invoice_id", "amount", "reason", "state",
                 "requested_by", "created_at" } ],
  "events": [ { "id", "type", "object_id", "received_at", "processed_at", "outcome" } ],
  "paid_without_document": [ { "entitlement_id", "account_id", "stripe_invoice_id", "seat_id",
                               "starts_at" } ],
  "blocks": [ { "id", "account_id", "blocked_at", "reason", "stripe_invoice_id", "seat_id" } ],
  "next_cursors": { "documents": null, "seats": null, "refunds": null, "events": null,
                    "paid_without_document": null, "blocks": null },
  "links": { "customer": "https://dashboard.stripe.com/test/customers/{id}",
             "invoice": "…/invoices/{id}", "subscription": "…/subscriptions/{id}",
             "payment": "…/payments/{id}" },
  "odoo_link": "https://erp.bitclick.solutions/odoo/account.move/{id}"
}
```

- `documents`: en revisión, o pendientes desde hace más de una hora.
- `events`: huérfanos (`outcome = "orphan"`) o sin procesar desde hace más de una hora.
- Cada lista trae su primera página (50 filas); las siguientes, con `GET /billing/review/lists/{nombre}` (sección 1.3). Los recuentos de `counts` son totales, no de la página. Sustituye `{id}` en las plantillas de `links` y `odoo_link`.

### `POST /admin/api/refunds`

Reembolso excepcional. Acción sensible.

```json
{
  "stripe_invoice_id": "in_…",
  "amount": 1000,
  "reason": "error_cobro",
  "ticket_ref": "Ticket 77"
}
```

- `amount`, en céntimos con IVA (lo que se devuelve del cobro). Sin `amount`: todo lo que quede por devolver.
- `reason`: `error_cobro`, `decision_comercial`, `cambio_tercero` o `disputa`.
- `ticket_ref`: de 3 a 80 caracteres.

Respuesta `202 {"refund_request_id": "…"}`. El proceso de cobro lo ejecuta en segundo plano: el estado se ve en `refunds` de `GET /accounts/{id}/billing`. Si el reembolso es total, la plaza deja de renovarse y se emite la rectificativa. Errores: `404` (no hay cobro con esa factura), `422 invalid` (nada que devolver, importe mayor que lo pendiente o ya hay un reembolso pendiente de esa factura).

## 6. Derechos y bloqueos

### `POST /admin/api/accounts/{account_id}/entitlements`

Conceder un derecho. Acción sensible.

```json
{
  "plan": "pro",
  "quantity": 1,
  "source": "cortesia",
  "ends_at": "2027-06-30T00:00:00Z",
  "note_code": "cliente_fundador",
  "ticket_ref": "Ticket 55"
}
```

- `source`: `cortesia`, `recomendacion`, `manual` o `beta`. Ampliar la beta es conceder un derecho `beta` con un fin posterior.
- `quantity`: de 1 a 10.
- `ends_at`: entre una hora y dos años desde ahora. El derecho empieza en el momento de concederlo.
- `note_code` (opcional): minúsculas y guiones bajos, hasta 40.

Respuesta `200 {"entitlement_id": "…"}`. `granted_by` queda con tu usuario.

### `POST /admin/api/entitlements/{entitlement_id}/trim`

Recortar un derecho no pagado: adelanta su fin, nunca lo alarga. Acción sensible.

```json
{ "ends_at": null, "ticket_ref": "Ticket 55" }
```

Sin `ends_at` (o `null`), termina ahora. Respuesta `200 {"ok": true}`. `422` si el derecho es `pago` (se cierra con un reembolso) o si el fin nuevo no es anterior.

### `POST /admin/api/blocks/{block_id}/close`

Cerrar a mano un bloqueo por impago (`unblocked_by = "admin"`). Acción sensible. Úsalo solo en casos excepcionales: lo normal es que el cliente pague.

```json
{ "ticket_ref": "Ticket 90" }
```

Respuesta `200 {"account_id": "…"}`. `404` si el bloqueo no existe o ya estaba cerrado.

## 7. Salud, métricas y lista de espera

### `GET /admin/api/health/overview`

```json
{
  "summaries": { "ok": 120, "needs_reconnect": 3, "error": 1 },
  "summaries_stale": 4,
  "channels": { "telegram": { "linked": 80, "unlinked": 5 },
                "whatsapp": { "linked": 12, "unlinked": 0 } },
  "jobs_stuck": 0,
  "jobs_failed_24h": 2,
  "jobs": [ { "kind": "sync_profile", "state": "done", "jobs": 900,
              "oldest_run_at": "…", "stuck": 0 } ],
  "failed_jobs": [ { "job_id": "…", "kind": "send_mail", "attempts": 3,
                     "finished_at": "…", "last_error": "… (200 caracteres como mucho)" } ],
  "retention_runs": [ { "run_id": 31, "at": "…", "deleted": { "review": 10 } } ],
  "sync_errors": [ { "account_id": "…", "location_id": "loc_…", "status": "error",
                     "status_at": "…", "synced_at": "…" } ],
  "next_cursors": { "failed_jobs": "eyJ…", "retention_runs": null, "sync_errors": null }
}
```

- `summaries_stale`: fichas sin una lectura correcta en las últimas 24 horas.
- `failed_jobs` (50), `retention_runs` (10) y `sync_errors` (fichas con estado distinto de `ok`, 50): la primera página. Las siguientes, con `GET /health/failed-jobs`, `GET /health/retention-runs` y `GET /health/sync-errors`, que responden `{"failed_jobs" | "retention_runs" | "sync_errors": [...], "next_cursor"}`. Cada pasada de la retención lleva ahora su `run_id`.

### `GET /admin/api/metrics?from=AAAA-MM-DD&to=AAAA-MM-DD`

Totales del periodo `[from, to)`, en días UTC. Por defecto, los 30 últimos días hasta hoy incluido. Como mucho, 400 días. No es un listado: no lleva páginas.

```json
{
  "period": { "from": "2027-02-12", "to": "2027-03-15" },
  "revenue": { "arr_cents": 125600, "mrr_cents": 10467, "estimated_seats": 0 },
  "seats_in_force": [ { "plan": "pro", "next_price_key": "pro_first_yearly", "seats": 20 } ],
  "signups": { "accounts": 40, "first_seats": 6, "new_seats": 9 },
  "churn": { "seats_canceled": 1, "seats_to_cancel": 2, "account_leaves": 1 },
  "blocks": { "opened": 2, "closed": 1, "open_now": 1 },
  "conversion": { "beta_accounts": 30, "beta_paid": 12, "free_accounts": 25, "free_paid": 3 },
  "funnel": { "visit": 900, "diagnosis": 40, "screenshot_diagnosis": 260, "signup": 40,
              "connected": 35, "checkout_started": 10, "paid": 6 }
}
```

**Revisión del 2026-11-13.** Cada cifra se ha comprobado contra el código que la registra. Cambios:

- **Fuera `churn.seats_lost_unpaid`.** Contaba las plazas canceladas que alguna vez tuvieron un bloqueo. Con las reglas del propietario (`docs/admin-facturacion.md`, 1.1 P3 y 9.0, preguntas 2 y 3) el bloqueo por impago **no tiene límite** y **no se pierde la plaza a los 30 días**: no existe la «plaza perdida por impago». Además sumaba plazas que se bloquearon, se pagaron y luego se cancelaron por decisión del cliente. Lo que hay sin pagar se ve en `blocks.open_now`.
- **Una plaza «contratada» es la que tiene un derecho `pago`.** `signups.first_seats`, `signups.new_seats` y `churn.seats_canceled` contaban también las plazas cuyo primer pago nunca llegó a completarse (acaban `canceled` sin derecho): ni eran altas ni eran bajas.
- **«Pagan», en la conversión, es un cobro con dinero** (`billing_charge` pagado con `amount_paid > 0`). Antes bastaba un derecho `pago`, y una cuenta de la beta que contrata en su ventana de 30 días recibe ese derecho con una factura de 0 € (decisión 9.0, 11): aún no ha pagado nada.
- **Cuentas de la beta: solo las del derecho beta automático** (el que recibe cada cuenta creada antes del lanzamiento, sin `granted_by`). Una ampliación de la beta desde el backoffice no convierte en «de la beta» a una cuenta creada después.
- **Embudo:** se añade `screenshot_diagnosis` (la prueba por captura, la entrada principal de la portada). `visit` se registra desde la portada (`POST /api/v1/public/visit`, antes nada lo registraba) y `connected`, solo la primera conexión de cada identidad de Google (antes contaba cada inicio de sesión).

#### Métricas

| Campo | Qué es | De dónde sale |
|---|---|---|
| `revenue.arr_cents` | La suma del precio siguiente de las plazas activas que no están marcadas para cancelarse. Sin IVA. Una plaza sin precio siguiente fijado cuenta con el de su plan para la primera plaza (`estimated_seats`). Incluye las plazas de la ventana de la beta que aún no han tenido su primer cobro | `billing_seat` (`status = 'active'` y `cancel_at_period_end = false`), que copia `fichaitor-billing` de Stripe; precios de `app/domain/billing.py` |
| `revenue.mrr_cents` | `arr_cents / 12`, redondeado | El ARR |
| `seats_in_force` | Esas mismas plazas, por plan y precio siguiente | `billing_seat` |
| `signups.accounts` | Cuentas creadas en el periodo (primera conexión con Google) | `account.created_at`. La API crea la cuenta al volver de Google |
| `signups.first_seats` | Plazas contratadas en el periodo que son la primera plaza contratada de su cuenta | `billing_seat` con un derecho `pago` (lo crea `fichaitor-billing` cuando Stripe da por pagada la primera factura de la plaza, también la de 0 € de la ventana de la beta) |
| `signups.new_seats` | Plazas contratadas en el periodo | Ídem |
| `churn.seats_canceled` | Plazas contratadas que están canceladas y cuya última actualización cae en el periodo. Normalmente es el día de la cancelación; si Stripe vuelve a mandar un evento de una plaza cancelada, la fecha se mueve | `billing_seat.status = 'canceled'` y `updated_at` |
| `churn.seats_to_cancel` | Plazas activas marcadas para cancelarse en su renovación (hoy, sin periodo) | `billing_seat.cancel_at_period_end` |
| `churn.account_leaves` | Bajas de cuenta que se hicieron efectivas en el periodo (no las restauradas en las 48 horas) | `offboarding.effective_at`, que fija el worker |
| `blocks.opened`, `blocks.closed` | Bloqueos por impago abiertos y cerrados en el periodo | `account_billing_block` (`blocked_at`, `unblocked_at`). Los abre `fichaitor-billing` con el impago; los cierra un pago o el propietario a mano |
| `blocks.open_now` | Bloqueos abiertos ahora, sin periodo. Un bloqueo no caduca: sigue abierto hasta que se paga | `account_billing_block.unblocked_at IS NULL` |
| `conversion.beta_accounts` | Cuentas de la beta, de siempre (sin periodo) | `plan_entitlement` con `source = 'beta'` y sin `granted_by` (el disparador `account_beta_entitlement`, antes de `launch_at`) |
| `conversion.beta_paid` | De ellas, las que han pagado algún cobro con dinero, hasta hoy | `billing_charge` con `status = 'paid'` y `amount_paid > 0` |
| `conversion.free_accounts` | Cuentas creadas en el periodo que no son de la beta (empiezan en el plan Gratis) | `account.created_at` sin derecho beta automático |
| `conversion.free_paid` | De ellas, las que han pagado algún cobro con dinero, hasta hoy (aunque pagaran después del periodo) | `billing_charge`, como arriba |

Mientras no haya `launch_at`, todas las cuentas nuevas reciben el derecho beta: `free_accounts` sale a 0 hasta el lanzamiento, y nada se cobra.

#### Embudo

`funnel`: eventos de `funnel_event` de cada tipo en el periodo. Sin datos personales. Los pasos no son una secuencia estricta: los dos diagnósticos son entradas distintas de la portada, y la lista de espera es anterior al lanzamiento. El porcentaje entre pasos tiene sentido de `connected` a `checkout_started` y de `checkout_started` a `paid`.

| Paso | Qué lo registra | Desde | Cuándo no se registra |
|---|---|---|---|
| `visit` | La portada, con `navigator.sendBeacon` a `POST /api/v1/public/visit` (`app/api/visit.py`). Con `source` (`utm_source` o `ref`) | 2026-11-13 en la API. Cuenta cuando la portada haga la llamada (la pone la sesión de interfaz) | Modo «solo web» (no hay API). Robots, vistas previas y clientes que no son navegadores. Más de 10 por dirección y hora. Navegadores que bloquean `sendBeacon`. Al reiniciar la API, el límite vuelve a cero |
| `diagnosis` | `POST /api/v1/public/diagnosis` cuando encuentra el negocio (`app/api/main.py`). Con `source` | 2026-11-11 | Modo «solo web». En la portada, el buscador por nombre solo sale con `?v=buscador`. Un negocio no encontrado registra `not_found`, que no está en el embudo. Límites por IP y diarios (429 y 503) |
| `screenshot_diagnosis` | `POST /api/v1/public/diagnosis/screenshot` cuando lee la captura (`app/api/screenshot.py`). Con `source` | 2026-11-11 | Modo «solo web». Sin `FICHAITOR_GEMINI_API_KEY` ni `FICHAITOR_FAKE_SCREENSHOT` la ruta no existe. Una captura rechazada registra `screenshot_rejected`, que no está en el embudo |
| `signup` | `POST /api/v1/public/waitlist`: alta en la lista de espera (`app/api/waitlist.py`). Con `source` | 2026-11-11 | Modo «solo web» (el formulario no sale). Cuenta envíos: el mismo contacto apuntado dos veces cuenta dos. Si no se pudo guardar el alta, no cuenta |
| `connected` | La vuelta de Google (`app/api/google_oauth.py`) cuando la identidad de Google **no tenía cuenta**: la primera conexión | 2026-11-11 (cada conexión); solo la primera desde 2026-11-13 | Modo «solo web». Volver a entrar con la misma cuenta de Google. Una conexión que falla (permiso denegado, error de Google) |
| `checkout_started` | `fichaitor-billing`, al abrir un Checkout de Stripe para una plaza (`app/billing/requests.py`). `source` = el plan | 2026-11-13 | Antes del lanzamiento (`launch_at` vacío: no se vende). Cuenta cada Checkout abierto: si alguien lo abre dos veces, cuenta dos |
| `paid` | `fichaitor-billing`, cuando Stripe da por pagada la primera factura de una plaza y se crea su derecho (`app/adapters/billing_store/postgres_billing.py`). `source` = el plan | 2026-11-13 | Las renovaciones. Cuenta plazas, no cuentas: una cuenta que compra tres plazas cuenta tres. Incluye la factura de 0 € de una plaza contratada en la ventana de la beta |

### `GET /admin/api/waitlist`

Solo totales anónimos, por semana (lunes, hora de Madrid), canal, tramo de nota y origen. Las celdas de menos de 5 personas no salen, y no hay totales por fila ni por columna:

```json
{
  "min_cell": 5,
  "cells": [ { "week": "2027-03-01", "channel": "email", "score_band": "70-89",
               "source": "instagram", "people": 7 } ],
  "next_cursor": null
}
```

Por páginas (`limit`, por defecto 100, y `cursor`).

`score_band`: `0-49`, `50-69`, `70-89`, `90-100` o `sin_nota`.

## 8. Usuarios y auditoría

### `GET /admin/api/users`

```json
{
  "users": [ { "admin_user_id": "…", "username": "ana", "role": "soporte", "active": true,
               "must_enroll": false, "locked_until": null, "created_at": "…",
               "password_changed_at": "…" } ],
  "next_cursor": null
}
```

Solo lectura: las altas, los restablecimientos y las bajas se hacen con `scripts/admin_usuario.py`, en el servidor.

### `GET /admin/api/audit?limit=100&cursor=<next_cursor>`

El registro, de lo más nuevo a lo más viejo. `limit`, de 1 a 500. Para la página siguiente, pasa `cursor` = `next_cursor` de la respuesta. **Cambio del 2026-11-13:** sustituye a `before` y `next_before`.

```json
{
  "rows": [ { "id": 812, "at": "…", "written_by": "fichaitor_admin_prod",
              "admin_user": "…", "username": "ana", "role": "soporte",
              "action": "account_detail_read", "account_id": "…", "target": null,
              "ticket_ref": "Ticket 4321", "detail": {} } ],
  "next_cursor": "WyI4MTIiLCI4MTIiXQ"
}
```

Acciones que verás:

| Grupo | `action` |
|---|---|
| Sesión | `login_password_ok`, `login_ok`, `login_failed` (`detail.stage`), `locked`, `totp_stepup`, `recovery_code_used`, `logout` |
| Personas | `enrolled`, `totp_changed`, `password_changed`, `password_rehashed`, `user_created`, `user_reset`, `user_activated`, `user_deactivated`, `user_email_set` (`detail.set`, sin la dirección) |
| Lecturas | `find_accounts` (solo el tipo de búsqueda), `account_overview_read`, `account_detail_read`, `account_billing_read`, `billing_contact_read`, `billing_review_read`, `health_read`, `metrics_read`, `list_read` (`detail.list`: el listado de la página pedida), `waitlist_totals_read`, `audit_read`, `app_sync_read`, `app_channels_read`, `app_jobs_read`, `app_users_read`, `communication_read`, `communication_audience_read` (`detail`: el recuento), `communication_list_read` |
| Acciones | `offboarding_email_checked`, `offboarding_email`, `entitlement_granted`, `entitlement_trimmed`, `block_closed`, `refund_requested`, `communication_saved`, `communication_test_sent`, `communication_sent` (`target` = la comunicación; `detail`: tipo, público, plan, `recipients`, `excluded` y `per_minute`; `account_id`, si el público es una cuenta) |
| Rechazos | `app_denied` (`target` = la ruta, `detail.code` = `role_not_allowed` o `totp_required`), `app_offboarding_dmarc_failed` (`detail.reason`) |

## 9. Otras rutas

- `GET /healthz`: `{"ok": true}`, sin sesión. La usa la comprobación de salud del contenedor.
- `GET /`: la interfaz (`web/admin/dist/index.html`) y sus recursos en `/assets/`. Mientras la imagen no la incluya, una página que lo dice.

## 10. Comunicaciones por correo (2026-11-13)

Correos a los clientes con la plantilla de Fichaitor (`brand/correo/LEEME.md`, 12). Diseño: anexo «Comunicaciones por correo desde el backoffice» de `docs/admin-facturacion.md`. **Solo el rol `propietario`.** Flujo: redactar → vista previa → prueba a tu dirección → recuento → confirmar con TOTP → el worker envía en cola, a ritmo limitado.

- **Tipos.** `servicio` (cambios en el producto o en las condiciones, incidencias, fin de la beta): llega a todo el público, sin enlace de baja. `comercial` (ofertas, novedades con intención de venta, boletín): no llega a las cuentas que se dieron de baja de las comerciales, y cada correo lleva su enlace de baja y las cabeceras `List-Unsubscribe` y `List-Unsubscribe-Post`. Si dudas, es comercial.
- **Público** (`audience`): `todas` (las cuentas activas), `beta` (con un derecho beta en vigor), `plan` (con al menos una ficha activa en `audience_plan`: `gratis`, `pro` o `pro_whatsapp`) o `cuenta` (`audience_account`, el id de una cuenta). Nunca entra la lista de espera ni una dirección escrita a mano, ni una cuenta dada de baja o con la baja de la cuenta programada.
- **Sin direcciones.** Ninguna ruta devuelve la dirección de un cliente: solo recuentos. La comunicación guarda el texto, el tipo, el público, el estado, los recuentos y las fechas.
- **Estados** (`state`): `borrador` (se cambia, se prueba y se confirma), `enviando` (los correos están en cola) y `enviada` (todos han terminado).

### `POST /admin/api/communications`

Crea un borrador. Respuesta `201 {"communication_id": "…"}`.

```json
{
  "kind": "comercial",
  "subject": "Novedad en Fichaitor: contesta tus reseñas desde WhatsApp",
  "preheader": "Te avisamos de cada reseña nueva y la respondes desde el mismo chat.",
  "headline": "Reseñas por WhatsApp",
  "paragraphs": ["Con el plan Pro+, Fichaitor…", "Puedes pasar a Pro+…"],
  "button_label": "Ver el plan Pro+",
  "button_url": "https://fichaitor.es/app#facturacion",
  "audience": "plan",
  "audience_plan": "pro",
  "audience_account": null
}
```

| Campo | Regla |
|---|---|
| `subject` | De 1 a 90 caracteres, en una línea |
| `preheader` | De 1 a 120, en una línea |
| `headline` | De 1 a 40, en una línea y **sin punto final** (la plantilla pone el punto rojo) |
| `paragraphs` | De 1 a 8, de hasta 1000 caracteres cada uno. Texto, no HTML: se escapa todo. Un salto de línea dentro de un párrafo no se convierte en `<br>`: cada párrafo es un elemento de la lista |
| `button_label` y `button_url` | Opcionales, los dos o ninguno. Etiqueta de hasta 30. El enlace **solo** puede ser `https://fichaitor.es/…` (sin usuario, puerto, espacios ni comillas): se comprueba al guardar |
| `audience_plan` | Obligatorio con `audience = "plan"`; se ignora con los demás |
| `audience_account` | Obligatorio con `audience = "cuenta"`; se ignora con los demás. `404 not_found` si la cuenta no existe |

Errores: `422 invalid` con `message` sobre el campo (nunca repite el valor), `422 validation` si falta un campo.

### `PUT /admin/api/communications/{id}`

Cambia un borrador, con el mismo cuerpo. Respuesta `200 {"communication_id": "…"}`. `422 invalid` si ya se envió. Un cambio invalida la prueba anterior: hay que enviar otra antes de confirmar.

### `GET /admin/api/communications/{id}`

```json
{
  "communication_id": "…", "kind": "comercial", "subject": "…", "preheader": "…",
  "headline": "…", "paragraphs": ["…"], "button_label": "…", "button_url": "https://fichaitor.es/…",
  "audience": "plan", "audience_plan": "pro", "audience_account": null,
  "state": "borrador", "created_at": "…", "updated_at": "…", "created_by": "jesus",
  "test_sent_at": null, "test_is_current": false,
  "confirmed_at": null, "confirmed_by": null, "per_minute": null,
  "recipients": null, "excluded": null, "sent": 0, "failed": 0, "skipped": 0, "finished_at": null
}
```

`test_is_current`: hay una prueba de esta versión (sin ella, confirmar da `422`). Tras confirmar: `recipients` (correos encolados), `excluded` (cuentas del público que no la reciben porque se dieron de baja de las comerciales), y `sent`, `failed` y `skipped` (dados de baja entre la confirmación y su envío) según avanza el worker.

### `GET /admin/api/communications/{id}/preview` y `/preview.html`

La vista previa, con el mismo código que usa el worker:

```json
{
  "subject": "…", "preheader": "…", "html": "<!doctype html>…", "text": "RESEÑAS POR WHATSAPP\n\n…",
  "headers": [
    { "name": "List-Unsubscribe", "value": "<https://fichaitor.es/api/v1/public/unsubscribe/one-click?token=prueba>" },
    { "name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click" }
  ]
}
```

- `headers` va vacío en las de servicio. En la vista previa (y en la prueba) el token de baja es `prueba`: no da de baja a nadie.
- `preview.html` devuelve solo el HTML (`text/html`), para enseñarlo en un `<iframe sandbox src="/admin/api/communications/{id}/preview.html">`. Es la única respuesta con otra CSP: `default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; sandbox …` y `X-Frame-Options: SAMEORIGIN`. Sin scripts. No pongas el HTML del JSON en `srcdoc`: la CSP del backoffice bloquearía sus estilos en línea.

### `GET /admin/api/communications/{id}/audience`

Cuántos la recibirían si se confirmara ahora. Enséñalo antes de confirmar.

```json
{ "recipients": 182, "excluded": 4, "per_minute": 60 }
```

`excluded` solo cuenta en las comerciales. `per_minute`: el ritmo de envío (`FICHAITOR_ADMIN_COMMUNICATION_PER_MINUTE`, 60 por defecto): 182 correos tardan unos 3 minutos.

### `POST /admin/api/communications/{id}/test`

Envía la prueba a **tu** dirección (la que el propietario guardó con `scripts/admin_usuario.py correo`), con «[Prueba]» delante del asunto. Respuesta `202 {"sent_to": "j***s@bitclick.es"}`. Solo de un borrador y 10 por hora como mucho. `422 invalid` si no tienes dirección (el `message` lo dice).

### `POST /admin/api/communications/{id}/confirm`

Confirma el envío. **Acción sensible** (TOTP de menos de 5 minutos).

```json
{ "expected_recipients": 182 }
```

- `expected_recipients`: el `recipients` que enseñaste. Si el público ha cambiado, `422 invalid` con el número nuevo en `message`: enséñalo y pide confirmar otra vez.
- `422 invalid` también si no es un borrador, si falta la prueba de esta versión o si no hay ningún destinatario.
- Respuesta `202`:

```json
{ "communication_id": "…", "recipients": 182, "excluded": 4, "per_minute": 60, "last_run_at": "…" }
```

`last_run_at`: cuándo sale el último correo. Cada correo es un job `send_mail`; su carga lleva la dirección y la cuenta hasta que se envía, y se vacía al terminar. Una comunicación confirmada no se puede cambiar ni volver a enviar.

### `GET /admin/api/communications?limit=50&cursor=<next_cursor>`

El historial, de la más nueva a la más vieja (sin los párrafos). Clave: `created_at`, `communication_id`.

```json
{
  "communications": [ { "communication_id": "…", "kind": "servicio", "subject": "…", "audience": "todas",
                        "audience_plan": null, "audience_account": null, "state": "enviada",
                        "created_at": "…", "created_by": "jesus", "test_sent_at": "…",
                        "confirmed_at": "…", "confirmed_by": "jesus", "recipients": 182,
                        "excluded": 0, "sent": 180, "failed": 1, "skipped": 1, "finished_at": "…" } ],
  "next_cursor": null,
  "per_minute": 60
}
```

Las comunicaciones se borran a los 12 meses de escribirse (`docs/retencion.md`).
