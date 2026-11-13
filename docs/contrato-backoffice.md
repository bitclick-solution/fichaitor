# Contrato del backoffice

Contrato HTTP entre el proceso `fichaitor-admin` (`python -m app.admin`) y su interfaz, `web/admin/`. Lo que puede hacer cada rol, y por qué, está en la sección 6 de `docs/admin-facturacion.md`. Fecha: 2026-11-13. Estado: implementado (paso 12, backend).

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
| 422 | `password_too_short`, `password_too_long`, `password_common`, `password_has_username` | Contraseña nueva rechazada |
| 422 | `dmarc_failed` | El mensaje pegado no prueba su remitente. `message` lleva el motivo |
| 429 | `too_many_attempts` | 20 fallos en la última hora en todo el backoffice: esperar |
| 503 | `authserv_not_configured` | Falta `FICHAITOR_ADMIN_AUTHSERV_ID` en `admin.env` |

### 1.2 Matriz de roles

Viene de `GET /session` (`role`). La base vuelve a comprobarla en cada petición.

| Pantalla o acción | Rutas | `soporte` | `finanzas` | `propietario` |
|---|---|---|---|---|
| Buscar cuentas | `GET /accounts` | Sí | Sí | Sí |
| Cuenta: resumen, nombres de las fichas (con ticket) y chats | `GET /accounts/{id}`, `POST /accounts/{id}/locations`, `GET /accounts/{id}/channels` | Sí | Sí | Sí |
| Baja por correo | `POST /accounts/{id}/offboarding/check`, `POST /accounts/{id}/offboarding` | Sí | No | Sí |
| Salud y sincronización | `GET /health/overview`, `GET /accounts/{id}/sync` | Sí | No | Sí |
| Cobro de una cuenta, datos fiscales y revisión | `GET /accounts/{id}/billing`, `POST /accounts/{id}/billing/contact`, `GET /billing/review` | No | Sí | Sí |
| Reembolso excepcional | `POST /refunds` | No | Sí | Sí |
| Métricas | `GET /metrics` | No | Sí | Sí |
| Conceder y recortar derechos | `POST /accounts/{id}/entitlements`, `POST /entitlements/{id}/trim` | No | No | Sí |
| Cerrar un bloqueo | `POST /blocks/{id}/close` | No | No | Sí |
| Lista de espera (totales) | `GET /waitlist` | No | No | Sí |
| Usuarios y auditoría | `GET /users`, `GET /audit` | No | No | Sí |

Todas las rutas empiezan por `/admin/api`.

### 1.3 Acciones sensibles

Piden un TOTP de menos de 5 minutos: reembolsar, conceder o recortar derechos, cerrar un bloqueo, tramitar la baja y cambiar la contraseña. `GET /session` devuelve `totp_fresh_until`: antes de esa hora la acción pasa sin pedir código. Si la respuesta es `403 totp_required`, pide un código, llama a `POST /session/totp` y repite la petición.

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

Busca por id de cuenta (UUID), id de ficha (`loc_…`), correo exacto, NIF o razón social (contiene). Nunca por el nombre de una ficha. De 3 a 254 caracteres. Como mucho, 20 resultados.

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
  ]
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
  ]
}
```

- `source` de un derecho: `beta`, `manual`, `pago`, `cortesia` o `recomendacion`. Solo los que no son `pago` se pueden recortar.
- `next_price_key` → precio: `pro_first_yearly` 3500, `pro_additional_yearly` 3325, `whatsapp_first_yearly` 5900, `whatsapp_additional_yearly` 5605.
- Las activaciones, las 100 últimas.

### `POST /admin/api/accounts/{account_id}/locations`

Los nombres de las fichas (dato de Google). Solo con la referencia del ticket, que se audita con la lectura.

```json
{ "ticket_ref": "Ticket 4321" }
```

Respuesta: `{"locations": [{"location_id", "title", "active_since", "plan", "deactivated_at", "removed_at", "verified"}]}`.

### `GET /admin/api/accounts/{account_id}/sync`

`{"sync": [{"account_id", "location_id", "status", "status_at", "synced_at"}]}`. `status`: `ok`, `needs_reconnect` o `error`.

### `GET /admin/api/accounts/{account_id}/channels`

Los chats vinculados, sin direcciones ni teléfonos: `{"channels": [{"identity_id", "channel", "verified_at", "last_inbound_at", "unlinked_at"}]}`.

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
                "unblocked_by": null } ]
}
```

- `customer` es `null` si la cuenta no ha rellenado sus datos fiscales.
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
  "links": { "customer": "https://dashboard.stripe.com/test/customers/{id}",
             "invoice": "…/invoices/{id}", "subscription": "…/subscriptions/{id}",
             "payment": "…/payments/{id}" },
  "odoo_link": "https://erp.bitclick.solutions/odoo/account.move/{id}"
}
```

- `documents`: en revisión, o pendientes desde hace más de una hora.
- `events`: huérfanos (`outcome = "orphan"`) o sin procesar desde hace más de una hora.
- Cada lista, como mucho 200 filas. Sustituye `{id}` en las plantillas de `links` y `odoo_link`.

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
  "retention_runs": [ { "at": "…", "deleted": { "review": 10 } } ],
  "sync_errors": [ { "account_id": "…", "location_id": "loc_…", "status": "error",
                     "status_at": "…", "synced_at": "…" } ]
}
```

- `summaries_stale`: fichas sin una lectura correcta en las últimas 24 horas.
- `failed_jobs`: los 50 últimos. `retention_runs`: las 10 últimas pasadas. `sync_errors`: fichas con estado distinto de `ok`, 200 como mucho.

### `GET /admin/api/metrics?from=AAAA-MM-DD&to=AAAA-MM-DD`

Totales del periodo `[from, to)`, en días UTC. Por defecto, los 30 últimos días hasta hoy incluido. Como mucho, 400 días.

```json
{
  "period": { "from": "2027-02-12", "to": "2027-03-15" },
  "revenue": { "arr_cents": 125600, "mrr_cents": 10467, "estimated_seats": 0 },
  "seats_in_force": [ { "plan": "pro", "next_price_key": "pro_first_yearly", "seats": 20 } ],
  "signups": { "accounts": 40, "first_seats": 6, "new_seats": 9 },
  "churn": { "seats_canceled": 1, "seats_lost_unpaid": 0, "seats_to_cancel": 2,
             "account_leaves": 1 },
  "blocks": { "opened": 2, "closed": 1, "open_now": 1 },
  "conversion": { "beta_accounts": 30, "beta_paid": 12, "free_accounts": 25, "free_paid": 3 },
  "funnel": { "visit": 900, "diagnosis": 300, "signup": 40, "connected": 35,
              "checkout_started": 10, "paid": 6 }
}
```

Definiciones:

| Campo | Definición |
|---|---|
| `revenue.arr_cents` | La suma del precio siguiente de las plazas activas que no están marcadas para cancelarse. Sin IVA. Una plaza sin precio siguiente fijado cuenta con el de su plan para la primera plaza (`estimated_seats`) |
| `revenue.mrr_cents` | `arr_cents / 12`, redondeado |
| `signups.accounts` | Cuentas creadas en el periodo |
| `signups.first_seats` | Plazas creadas en el periodo que son la primera plaza de su cuenta (sin contar las que no llegaron a pagarse) |
| `signups.new_seats` | Plazas creadas en el periodo (sin contar las pendientes) |
| `churn.seats_canceled` | Plazas que pasaron a canceladas en el periodo |
| `churn.seats_lost_unpaid` | De ellas, las que tuvieron un bloqueo por impago |
| `churn.seats_to_cancel` | Plazas activas marcadas para cancelarse en su renovación (hoy) |
| `churn.account_leaves` | Bajas de cuenta que se hicieron efectivas en el periodo |
| `blocks` | Bloqueos abiertos y cerrados en el periodo, y abiertos ahora |
| `conversion.beta_*` | Cuentas con un derecho `beta` (de siempre) y, de ellas, las que tienen algún derecho pagado |
| `conversion.free_*` | Cuentas creadas en el periodo sin derecho `beta` y, de ellas, las que tienen algún derecho pagado |
| `funnel` | Eventos del embudo de cada tipo en el periodo |

### `GET /admin/api/waitlist`

Solo totales anónimos, por semana (lunes, hora de Madrid), canal, tramo de nota y origen. Las celdas de menos de 5 personas no salen, y no hay totales por fila ni por columna:

```json
{
  "min_cell": 5,
  "cells": [ { "week": "2027-03-01", "channel": "email", "score_band": "70-89",
               "source": "instagram", "people": 7 } ]
}
```

`score_band`: `0-49`, `50-69`, `70-89`, `90-100` o `sin_nota`.

## 8. Usuarios y auditoría

### `GET /admin/api/users`

```json
{
  "users": [ { "admin_user_id": "…", "username": "ana", "role": "soporte", "active": true,
               "must_enroll": false, "locked_until": null, "created_at": "…",
               "password_changed_at": "…" } ]
}
```

Solo lectura: las altas, los restablecimientos y las bajas se hacen con `scripts/admin_usuario.py`, en el servidor.

### `GET /admin/api/audit?limit=100&before=<id>`

El registro, de lo más nuevo a lo más viejo. `limit`, de 1 a 500. Para la página siguiente, pasa `before` = `next_before` de la respuesta.

```json
{
  "rows": [ { "id": 812, "at": "…", "written_by": "fichaitor_admin_prod",
              "admin_user": "…", "username": "ana", "role": "soporte",
              "action": "account_detail_read", "account_id": "…", "target": null,
              "ticket_ref": "Ticket 4321", "detail": {} } ],
  "next_before": 812
}
```

Acciones que verás:

| Grupo | `action` |
|---|---|
| Sesión | `login_password_ok`, `login_ok`, `login_failed` (`detail.stage`), `locked`, `totp_stepup`, `recovery_code_used`, `logout` |
| Personas | `enrolled`, `totp_changed`, `password_changed`, `password_rehashed`, `user_created`, `user_reset`, `user_activated`, `user_deactivated` |
| Lecturas | `find_accounts` (solo el tipo de búsqueda), `account_overview_read`, `account_detail_read`, `account_billing_read`, `billing_contact_read`, `billing_review_read`, `health_read`, `metrics_read`, `waitlist_totals_read`, `audit_read`, `app_sync_read`, `app_channels_read`, `app_jobs_read`, `app_users_read` |
| Acciones | `offboarding_email_checked`, `offboarding_email`, `entitlement_granted`, `entitlement_trimmed`, `block_closed`, `refund_requested` |
| Rechazos | `app_denied` (`target` = la ruta, `detail.code` = `role_not_allowed` o `totp_required`), `app_offboarding_dmarc_failed` (`detail.reason`) |

## 9. Otras rutas

- `GET /healthz`: `{"ok": true}`, sin sesión. La usa la comprobación de salud del contenedor.
- `GET /`: la interfaz (`web/admin/dist/index.html`) y sus recursos en `/assets/`. Mientras la imagen no la incluya, una página que lo dice.
