# Multinegocio: varias fichas activas por cuenta

Plan de implementación del paso de «una ficha elegida por cuenta» a «varias fichas activas por cuenta». Fecha: 2026-11-12. Estado: aprobado; decisiones del propietario en la [sección 15](#15-decisiones-del-propietario-2026-11-12).

> **Importante:** Este plan parte de que ya están aplicadas las correcciones de `docs/seguridad/revision-2026-11-12-b.md`: políticas de fila en `job` (B9 y B10), disparadores en `offboarding` (M5 e I8) y en `usage_counter` (B11) y la cancelación de propuestas al confirmar una baja (B12). Ningún paso de este plan reescribe esas piezas. Solo añade columnas, tablas, disparadores y políticas nuevos, o amplía alguna lista.

## 1. Decisiones de este documento

| Tema | Decisión | Motivo, en una línea |
|---|---|---|
| Qué hace que una ficha esté activa | Dos columnas en `location`: `active_since` y `plan`. Además, una tabla de derechos de plan (`plan_entitlement`) y un registro de activaciones de solo inserción | Todas las consultas ya hacen *join* con `location`. Los derechos y el registro son lo que el cobro usará después |
| Dónde se impone el límite | En la base, con un disparador sobre `location` | Igual que M5 y B11: un SQL de la API no puede saltárselo |
| `selected_location_id` | Se queda como **preferencia de la vista del panel**. No decide nada fuera del panel | No obliga a cambiar la web de golpe |
| Cómo indican la ficha las rutas `/me/*` | Parámetro `?location=loc_…`, opcional. Si falta, se usa la preferencia | La web actual sigue funcionando sin cambios |
| Hilo de la conversación | **Un hilo por cuenta y un carril por ficha**: como mucho una propuesta activa por ficha | Una ficha no pausa a otra, y el filtro por chat funciona |
| Texto libre con varias propuestas abiertas | Se resuelve con estos criterios, por orden: respuesta citando el mensaje, foco reciente, candidata única y tipo de respuesta. Si sigue habiendo duda, se pregunta «¿De qué negocio?». Nunca se adivina | El principio «nunca se supone» de `docs/01-flujos.md` |
| Encabezado | Cada mensaje ligado a una propuesta empieza con el nombre del negocio. Si dos fichas activas se llaman igual, se añade la localidad | Decisión 2 del propietario |
| Filtro por chat | `channel_identity.location_filter text[]`. `NULL` quiere decir todas las fichas, también las futuras | Una columna. La API no tiene que borrar filas |
| Baja de una ficha | Desactivar la ficha: el servicio se para al momento y los datos se borran a los 30 días. «Desconectar Google» y «Borrar cuenta» siguen como hoy y afectan a todas las fichas | Google concede el acceso por usuario, no por ficha: no se puede revocar una sola ficha |
| Descuento por volumen | **20 % desde la segunda ficha de pago**: Pro a 28 €, Pro WhatsApp a 47 € | Consulta la [sección 11](#11-precio-descuento-por-volumen) |

## 2. Dónde decide hoy `selected_location_id`

Inventario completo. Cada línea se cambia en el paso indicado (consulta la [sección 13](#13-orden-de-implementación)).

### Base de datos

| Dónde | Qué hace | Paso |
|---|---|---|
| `db/schema.sql:81` | Columna `account.selected_location_id` | Se queda (preferencia) |
| `db/schema.sql:101-131` (`account_baja_guard`) | La vacía al dar de baja la cuenta | Sin cambios |
| `db/schema.sql:151-160` | Clave ajena compuesta `(id, selected_location_id)` contra `location (account_id, id)` | Se queda. Es el patrón que se copia en las tablas nuevas |
| `db/schema.sql:183` | La API la actualiza | Sin cambios |
| `db/schema_worker.sql:558-561` | El worker la lee y la vacía | Sin cambios |
| `db/schema_channels.sql:159-170` | `conversation.active_proposal_id`: una propuesta activa por cuenta | 1 y 6: carriles |
| `db/schema_channels.sql:233-234`, `:250`, `:417-419` | Quién puede liberar el hilo | 1: lo mismo sobre `conversation_lane` |
| `db/schema_channels.sql:267-279` | La conversación lee `selected_location_id` y `location` | 1: lee además `active_since`, `plan` y `deactivated_at` |

### Código

| Dónde | Qué decide | Paso |
|---|---|---|
| `app/adapters/store/postgres.py:102-119` (`_SELECTED`, `_OWN_REVIEWS`) | Todo lo que lee o escribe el panel: resumen (`selected_profile`, `:392`), reseñas (`:462`, `:482`), borrador (`:499`), respuesta (`:521-540`), cambio de horario (`:605-635`) y sentimiento (`:793-816`) | 3 |
| `app/adapters/store/postgres.py:297-302` | Al volver a conectar, deja de estar elegida una ficha retirada | 3 (la desactivación la hace el disparador) |
| `app/adapters/store/postgres.py:319-343`, `:363-376`, `:436-460` | Sesión, elegir ficha y ficha sin acceso | 3 |
| `app/adapters/store/postgres.py:676-690` (`_key_dates_sql`) | Fechas clave de la ficha elegida | 3 |
| `app/adapters/store/memory.py:81`, `:206`, `:223`, `:240-261`, `:295`, `:312`, `:394` | Lo mismo, en memoria | 3 |
| `app/application/profile_sync.py:86-128` | `request_for_account` y `view`: sincroniza y lee solo la ficha elegida | 3 |
| `app/api/me.py:60`, `:96`, `:112-174` | `GET /me`, `POST /me/location`, diagnóstico, resumen y sentimiento | 4 |
| `app/api/reviews.py:153`, `app/api/key_dates.py:180` | Reseñas y horario semanal de la ficha elegida | 4 |
| `app/api/google_oauth.py:147`, `app/api/offboarding.py:157` | Sincronizan la ficha elegida al reconectar o al restaurar | 4 (todas las activas) |
| `app/adapters/conversation_store/postgres.py:457-469` (`get_account_info`) | El nombre del negocio de los mensajes | 6 |
| `app/adapters/conversation_store/postgres.py:695-733` (`review_targets`) | A quién se propone una reseña | 6 |
| `app/adapters/conversation_store/postgres.py:735-755` (`linked_profiles`) | Qué sincroniza el planificador | 6 |
| `app/adapters/conversation_store/postgres.py:757-791` (`key_date_profiles`) | De qué fichas se preguntan las fechas clave | 6 |
| `app/adapters/worker_store/postgres.py:786-803`, `:984-992` | Baja efectiva y retención: la vacían | Sin cambios |
| `app/adapters/worker_store/postgres.py:350-393` (`claim_action`, M1) | No mira si la ficha está activa | 5 |
| `app/domain/conversation.py:111-120`, `app/application/conversation_service.py:344-421`, `:908-983` | Una propuesta abierta por cuenta | 2b y 6 |
| `app/domain/quota.py:24-26` (`draft_scope`) | Tope de borradores por cuenta | 3 (por ficha) |
| `app/worker/channel_planner.py`, `propose_review.py`, `plan_key_dates.py` | Docstrings y llamadas a lo anterior | 6 |

### Documentación y web

- `README.md:67-71`, `:106`, `:112`.
- `docs/contrato-web-api.md:136`, `:148`, `:160-170`, `:184`, `:287`, `:365-375`, `:395`, `:528`, `:578`, `:689`, `:733`, `:812-818`, `:913`, `:1024`, `:1087`, `:1119`.
- `docs/canales.md:352`, `:415`, `:429`, `:462` y la sección «Fechas clave» («si esa ficha ya no es la elegida»).
- `docs/worker.md:67`, `:226`, `:613`, `:617`.
- `web/app/src/App.tsx:26`, `:66-68`, `web/app/src/api.ts:45`, `web/app/src/mock.ts:107-110` y `web/app/src/Fechas.tsx:49`.

## 3. Modelo de datos

### 3.1 La ficha activa

El estado actual de cada ficha va en `location`. Las consultas ya pasan por esa tabla:

```sql
ALTER TABLE location
    ADD COLUMN IF NOT EXISTS active_since   timestamptz,
    ADD COLUMN IF NOT EXISTS plan           text CHECK (plan IN ('gratis', 'pro', 'pro_whatsapp')),
    ADD COLUMN IF NOT EXISTS deactivated_at timestamptz;
-- Restricciones con nombre (con DO $$ ... IF NOT EXISTS, como account_selected_location_fk):
--   location_active_has_plan:     (active_since IS NULL) = (plan IS NULL)
--   location_removed_not_active:  removed_at IS NULL OR active_since IS NULL
CREATE INDEX IF NOT EXISTS location_active ON location (account_id) WHERE active_since IS NOT NULL;
```

- **Activa** quiere decir `active_since IS NOT NULL`. Se sincroniza, se propone por los chats, se planifican sus fechas clave y el panel la sirve.
- `deactivated_at` marca desde cuándo está inactiva una ficha que estuvo activa. Desde esa fecha cuentan los 30 días de retención (consulta la [sección 9](#9-retención)). Al activarla otra vez vuelve a `NULL`.
- Una ficha retirada (`removed_at`) nunca está activa. Lo impone un disparador (consulta la [sección 3.4](#34-reglas-en-la-base)), no el código.

### 3.2 Derechos de plan

Preparan el cobro sin construirlo. Un derecho dice «esta cuenta puede tener N fichas activas en este plan hasta esta fecha». Al llegar los pagos, cada suscripción será un derecho con `source = 'pago'`.

```sql
CREATE TABLE IF NOT EXISTS plan_entitlement (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   uuid NOT NULL REFERENCES account (id),
    plan         text NOT NULL CHECK (plan IN ('pro', 'pro_whatsapp')),
    quantity     smallint NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    starts_at    timestamptz NOT NULL,
    ends_at      timestamptz CHECK (ends_at IS NULL OR ends_at > starts_at),
    source       text NOT NULL CHECK (source IN ('beta', 'manual', 'pago')),
    external_ref text CHECK (length(external_ref) BETWEEN 1 AND 200),
    created_at   timestamptz NOT NULL DEFAULT now()
);
```

- El plan `gratis` no necesita derecho: como mucho una ficha gratis activa por cuenta.
- Solo escriben aquí el propietario de la base (con un script, `scripts/conceder_plan.sh`) y, en el futuro, un rol de cobro. La API, el worker y la conversación solo leen.
- El precio no se guarda en la ficha. Es una regla que se calcula al cobrar (consulta la [sección 11](#11-precio-descuento-por-volumen)).

### 3.3 Registro de activaciones

Sirve para auditar y para prorratear cuando haya cobro. Es de solo inserción y lo escribe un disparador, así que no se puede olvidar:

```sql
CREATE TABLE IF NOT EXISTS location_activation_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    written_by  name NOT NULL DEFAULT current_user,
    account_id  uuid NOT NULL,
    location_id text NOT NULL CHECK (length(location_id) <= 64),
    event       text NOT NULL CHECK (event IN ('activated', 'deactivated', 'plan_changed')),
    plan        text CHECK (plan IN ('gratis', 'pro', 'pro_whatsapp')),
    reason      text CHECK (reason ~ '^[a-z_]{1,40}$')   -- owner, removed, offboarding, migration…
);
```

No tiene clave ajena: el registro dura más que la fila de `location`, que la retención borra.

### 3.4 Reglas en la base

Un disparador `BEFORE UPDATE` sobre `location`, `location_active_guard`, con función `SECURITY DEFINER` y `SET search_path = pg_catalog, public`:

1. **Al retirar.** Si `NEW.removed_at IS NOT NULL`, pone `active_since` y `plan` a `NULL` y `deactivated_at` a `now()`, como hace `account_baja_guard` con `selected_location_id`. Así funcionan sin cambios las tres vías que retiran fichas: `sync_locations` (API), `record_denied` (worker, B5) y `begin_offboarding` (worker).
2. **Al activar o cambiar de plan.** Fuera del propietario (`fichaitor_is_owner`):
   - bloquea la fila de la cuenta (`SELECT … FOR UPDATE`), para que dos activaciones a la vez se cuenten una detrás de otra;
   - exige que la cuenta no esté dada de baja, que no tenga una baja programada y que la ficha no esté retirada;
   - exige que `active_since` esté dentro de un minuto de `now()`, para que nadie pueda fechar una activación en el pasado;
   - cuenta las fichas activas de la cuenta por plan: `gratis` ≤ 1, y `pro` y `pro_whatsapp` ≤ la suma de `quantity` de sus derechos vigentes en `now()`;
   - si algo no se cumple, lanza `check_violation` con el mensaje `limite_fichas`, y la API lo traduce a 402.
3. **Al desactivar.** Pone `deactivated_at = now()`.
4. Un disparador `AFTER UPDATE` escribe la fila de `location_activation_log`. Lo hace también para el propietario, porque el relleno inicial debe quedar registrado.

> **Nota:** Los privilegios de columna se comprueban sobre las columnas del `SET`, no sobre las que cambia el disparador. Por eso el worker sigue retirando fichas con solo `UPDATE (removed_at)`.

### 3.5 `selected_location_id` pasa a ser preferencia

- Indica qué ficha enseña el panel cuando la petición no trae `?location=`. Nada más.
- No pausa ni activa nada, salvo durante la compatibilidad (consulta la [sección 5.5](#55-post-apimev1melocation-durante-la-transición)).
- Se conserva su clave ajena. Puede apuntar a una ficha inactiva: entonces las rutas responden 409 «Esa ficha no está activa».

### 3.6 Propuestas por ficha y carriles

```sql
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS location_id text;
-- Clave ajena con nombre: (account_id, location_id) REFERENCES location (account_id, id)
--   ON DELETE SET NULL (location_id)            -- PostgreSQL 15 o posterior
CREATE INDEX IF NOT EXISTS proposal_unfinished_location ON proposal (account_id, location_id)
    WHERE state IN ('programada','enviada','recordada','recopilando','por_confirmar','ejecutando');

CREATE TABLE IF NOT EXISTS conversation_lane (
    account_id         uuid NOT NULL,
    location_id        text NOT NULL,
    active_proposal_id text REFERENCES proposal (id),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, location_id),
    FOREIGN KEY (account_id, location_id) REFERENCES location (account_id, id) ON DELETE CASCADE
);
```

- La clave compuesta es la segunda barrera entre cuentas: no se puede crear una propuesta ni un carril con la ficha de otra cuenta.
- `conversation` se queda: es el bloqueo por cuenta (`FOR UPDATE`) y guarda `unknown_streak`. Su columna `active_proposal_id` queda obsoleta y se borra en el paso 9.
- En el paso 6, no antes, un disparador `BEFORE INSERT` sobre `proposal` exige `location_id` no nulo y la ficha activa. En el paso 1 rompería el código antiguo.
- Una restricción con nombre exige que, en `type = 'key_date'`, `payload->>'location_id'` coincida con `location_id`.

> **Nota:** La retención borra las propuestas terminadas «si ningún mensaje ni hilo las nombra». Ahora tampoco puede borrar las que nombre un carril (`conversation_lane.active_proposal_id`).

### 3.7 Filtro por chat y foco

```sql
ALTER TABLE channel_identity
    ADD COLUMN IF NOT EXISTS location_filter   text[] CHECK (location_filter IS NULL
                                                   OR cardinality(location_filter) BETWEEN 1 AND 50),
    ADD COLUMN IF NOT EXISTS focus_proposal_id text CHECK (focus_proposal_id ~ '^prop_[0-9a-f]{16}$'),
    ADD COLUMN IF NOT EXISTS focus_at          timestamptz;
ALTER TABLE link_code ADD COLUMN IF NOT EXISTS location_filter text[] CHECK (…lo mismo…);
```

- `location_filter IS NULL`: el chat recibe todas las fichas activas, también las que se activen después.
- Un disparador comprueba, al insertar y al actualizar, que cada elemento es una ficha de `NEW.account_id`. Con un SQL de la API no se puede apuntar a la ficha de otra cuenta.
- El filtro se fija al pedir el código de vinculación y se copia a la dirección al vincularla. Así el chat del encargado nunca recibe nada de las otras fichas, ni durante el primer minuto.
- `focus_*`: la última propuesta en la que esa dirección pulsó un botón. Sirve para resolver el texto libre (consulta la [sección 6.3](#63-texto-libre-con-varias-propuestas-abiertas)).

### 3.8 Migración desde los datos actuales

Todo va en los `db/schema*.sql`, que son idempotentes y se vuelven a aplicar en cada despliegue. Cada relleno se puede repetir sin efecto:

1. **La ficha elegida pasa a activa**, con `plan = 'gratis'` y `reason = 'migration'` en el registro. Condición: está elegida, no está retirada, la cuenta no está dada de baja y **la cuenta no tiene ninguna fila en `location_activation_log`**.
   - Esa última condición es la marca de «ya migrada». Al aplicar otra vez el esquema, no se reactiva una ficha que el dueño desactivó.
   - Se vuelve a aplicar en el despliegue del paso 3, para las cuentas que eligieron ficha con el código antiguo entre los pasos 1 y 3.
2. **Datos de fichas que ya no están elegidas.** Las fichas no activas que tienen `profile_summary` reciben `deactivated_at = now()`. Sus datos se borrarán a los 30 días, como los de una ficha que se desactiva.
3. **`proposal.location_id`**, solo donde es `NULL`:
   - en `key_date`, de `payload.location_id`;
   - en `review_reply`, con `review` (por `source_ref`, `google_sub` y `location`) contra la `location` de la cuenta;
   - en las que siguen sin terminar, con la ficha elegida de la cuenta;
   - las terminadas que no se puedan resolver se quedan a `NULL`.
4. **Carriles** (en el despliegue del paso 6). Se copia `conversation.active_proposal_id` al carril de su ficha, solo si la propuesta sigue abierta. Después se pone a `NULL`.

> **Precaución:** Antes de aplicar el relleno de los carriles, detén el proceso de la conversación y despliega el código nuevo en la misma ventana. Si el código antiguo sigue escribiendo `conversation.active_proposal_id`, el carril y el hilo dejan de coincidir.

## 4. Privilegios

| Rol | Qué se añade | Qué sigue sin poder hacer |
|---|---|---|
| `fichaitor_api` | `UPDATE (active_since, plan)` en `location`. `SELECT` en `plan_entitlement`. `UPDATE (location_filter)` en `channel_identity`. `INSERT (location_filter)` en `link_code`. `SELECT` y `UPDATE (active_proposal_id, updated_at)` en `conversation_lane`, con política de fila `WITH CHECK (active_proposal_id IS NULL)`, como `conversation_api_release`. En `proposal_api_update` se amplía el `WITH CHECK` con `OR (state = 'cancelada' AND outcome_reason = 'location_inactive')` | Escribir derechos de plan. Fijar `deactivated_at` (lo hace el disparador). Borrar nada. Saltarse el límite, porque lo impone el disparador. Escribir en el registro de activaciones (lo hace la función `SECURITY DEFINER`) |
| `fichaitor_conversation` | `SELECT (active_since, plan, deactivated_at)` en `location`. `INSERT (location_id)` en `proposal`. `SELECT`, `INSERT` y `UPDATE (active_proposal_id, updated_at)` en `conversation_lane`. `INSERT (location_filter)` y `UPDATE (focus_proposal_id, focus_at)` en `channel_identity`. `SELECT (location_filter)` en `link_code`. `SELECT (account_id, location_id) … ` no hace falta en `plan_entitlement` | Activar o desactivar fichas. Cambiar el filtro de un chat: solo lo copia del código al vincular |
| `fichaitor_worker` | `SELECT (active_since, plan, deactivated_at)` en `location`. `SELECT` y `DELETE` en `conversation_lane`. `SELECT (account_id)` y `DELETE` en `location_activation_log` y `plan_entitlement`, solo para la retención | Activar fichas. Leer el filtro o el foco de un chat |
| Propietario | Escribe `plan_entitlement` con el script de concesión | — |

**Coherencia M1 por ficha.** `claim_action` (`app/adapters/worker_store/postgres.py:350-393`) añade `AND l.active_since IS NOT NULL` a la subconsulta `owned`. Una acción confirmada sobre una ficha que se desactiva antes de ejecutarse falla sin llamar a Google, con el código nuevo `location_inactive`. El riesgo residual de `docs/02-hld.md` (I11) no cambia: una acción coherente con otra ficha **activa** de la misma cuenta sigue siendo posible para quien ejecute SQL con la API o con la conversación.

## 5. API

### 5.1 Cómo se indica la ficha: `?location=`

Elijo el parámetro opcional `?location=loc_…` en todas las rutas de lectura de una ficha: `diagnosis`, `summary`, `reviews`, `sentiment` y `key-dates`.

- **Compatibilidad.** Si falta, se usa `selected_location_id`. La web actual sigue funcionando sin tocarla. Con rutas `/me/locations/{id}/…` habría que duplicar unas 12 rutas y mantener las antiguas como alias.
- **Las rutas con identificador propio no lo necesitan.** `reviews/{id}/draft`, `reviews/{id}/reply`, `actions/{id}` y `key-dates/{id}/answer|dismiss` ya identifican la ficha por la reseña o la propuesta. Solo exigen que esa ficha sea de la cuenta y esté activa, en la misma sentencia.
- **Aislamiento.** El identificador se busca siempre en la misma sentencia que la cuenta de la sesión: un fragmento `_ACTIVE` sustituye a `_SELECTED`, con `l.id = $2 AND l.account_id = a.id AND l.active_since IS NOT NULL`. Una ficha de otra cuenta y una que no existe dan el mismo 404.
- **Respuestas.** Todas llevan `location_id`. Así la web descarta la respuesta de una ficha anterior si el dueño cambia el selector mientras carga.

| Caso | Respuesta |
|---|---|
| Sin `?location=` y sin preferencia | 409 «Primero elige con qué ficha quieres trabajar.», como hoy |
| Ficha de otra cuenta o inexistente | 404 «Esa ficha no está en tu cuenta.» |
| Ficha de la cuenta, inactiva | 409 «Esa ficha no está activa.» |
| Baja programada | 409, como hoy |

### 5.2 `GET /api/v1/me`

Cambios, todos aditivos:

- Cada ficha de `locations` trae `active`, `plan`, `active_since`, `status` y `pending`:
  - `status` vale `ok`, `pendiente`, `error` o `reconectar`, y `null` si la ficha está inactiva;
  - `pending` es `{ "reviews": n, "key_dates": n }`, solo en las activas.
- Un objeto `plans` con el uso de la cuenta: fichas activas, si queda la ficha gratis y huecos libres por plan.
- `selected_location_id` se queda, ahora como preferencia.

Los indicadores salen de una sola consulta con `profile_summary.unanswered_count` y el recuento de propuestas `key_date` pendientes, con la condición `_NO_OPEN_LEAVE`.

### 5.3 Activar y desactivar

| Ruta | Cuerpo | Respuesta |
|---|---|---|
| `POST /api/v1/me/locations/{id}/activation` | `{"plan": "gratis" \| "pro" \| "pro_whatsapp"}`. Si falta `plan`, `gratis` si está libre y, si no, el primer plan con hueco | 200 con el cuerpo de `GET /me`. Pide la sincronización de la ficha |
| `DELETE /api/v1/me/locations/{id}/activation` | — | 200 con el cuerpo de `GET /me` |

- Las dos llevan `X-Requested-With: fichaitor` (sin ella, 403) y pasan por `active_account_guard`.
- **402**: no queda hueco en el plan pedido. Es distinto de cualquier otro 409, y la web enseña la oferta de planes.
- **404**: la ficha no es de la cuenta. **409**: baja programada. **422**: plan desconocido.
- Activar una ficha ya activa con otro plan la cambia de plan, si hay hueco.
- **Desactivar**, en una transacción:
  1. bloquea el hilo (`conversation … FOR UPDATE`, como `_locked_key_date`);
  2. pone `active_since` y `plan` a `NULL`;
  3. cancela las propuestas abiertas de esa ficha (`outcome_reason = 'location_inactive'`) y cambia su testigo;
  4. libera su carril.

### 5.4 Rutas existentes

| Ruta | Cambio |
|---|---|
| `GET /me/diagnosis`, `/summary`, `/sentiment` | `?location=` y `location_id` en la respuesta. `ProfileSync.view(account_id, location_id)` |
| `GET /me/reviews` | Lo mismo |
| `POST /me/reviews/{id}/draft` y `…/reply`, `GET /me/actions/{id}` | `_OWN_REVIEWS` pasa a «reseñas de cualquier ficha activa de la cuenta». El tope de borradores pasa a ser por ficha: `review_draft:<id de la ficha>` |
| `GET /me/key-dates` y `POST …/{id}/answer|dismiss` | `?location=` en la lista. La propuesta debe ser de una ficha activa |
| `POST /channels/link-code` | Cuerpo opcional `{"locations": [...]}` (paso 7) |
| `PATCH /me/channels/{id}` (nueva) | `{"locations": null \| [...]}` (paso 7) |
| `GET /me/channels` | Cada canal trae `locations: null \| [...]` (paso 7) |
| Reconexión (`google_oauth.py:147`) y restauración (`offboarding.py:157`) | `request_for_account` sincroniza todas las fichas activas |

> **Nota:** `usage_counter.scope` hoy solo admite `[0-9a-f-]` tras los dos puntos (`db/schema.sql:196`), y el identificador de una ficha es `loc_…`. Amplía la expresión a `(:[0-9a-z_-]{1,64})?`. Esa línea la toca también B11: aplícalo cuando esté integrado.

### 5.5 `POST /api/v1/me/location` durante la transición

- Cambia la preferencia y pide la sincronización, como hoy.
- **Compatibilidad hasta el paso 9.** Si la ficha está inactiva y la cuenta no tiene ninguna activa, la activa en `gratis`. Si la cuenta tiene exactamente una activa en `gratis` y ningún derecho, **la sustituye**: desactiva la anterior y activa la nueva. Es lo que la web antigua entiende por «cambiar de ficha».
- La web nueva no llama a esta ruta con fichas inactivas: primero activa.
- En el paso 9 se quita la compatibilidad, y la ruta solo cambia la preferencia.

## 6. Conversación

### 6.1 Un hilo por cuenta, un carril por ficha

- **Un hilo por cuenta** (`conversation`). Hay un solo chat por persona, y el bloqueo de cuenta ya serializa el panel y el chat. No cambia.
- **Una propuesta activa por ficha** (`conversation_lane`). Las reglas de hoy (cola, interrupción por una reseña de 1 o 2 estrellas y retomar la interrumpida) se aplican **dentro de cada carril**.
- He descartado una sola propuesta activa por cuenta: con N fichas, una reseña sin contestar de A bloquearía hasta 7 días los festivos de B. Además, el chat del encargado de B esperaría a que el dueño contestara a A, y eso contradice las decisiones 1 y 4.
- `_pump` recorre los carriles con trabajo. `accounts_with_open_work` pasa a devolver pares (cuenta, ficha), y `_deliver_next(account_id, location_id)` entrega a las direcciones que **ven** esa ficha.
- **Límite semanal por ficha.** `count_capped_sent_since(account_id, location_id, since)`: cada negocio de pago recibe su servicio.
- **Horario de silencio**, por cuenta, como hoy: es el horario de la persona que lee.
- Los plazos de las fechas clave podrían usar la zona de cada ficha (Canarias). El modelo lo permite, pero queda fuera de este plan.

### 6.2 Encabezado de los mensajes

- Toda propuesta, recordatorio, propuesta retomada, resultado de una acción y respuesta dentro de una propuesta empieza con una línea: `fmt.business`, con el nombre del negocio.
- Si otra ficha activa de la cuenta tiene el mismo `title`, se añade la localidad de `facts.address`: «Bar La Parra · Getafe».
- El nombre se pasa por `cm.one_line(…, 60)`.
- Reserva su sitio aparte de `PREFIX_RESERVE` (`app/domain/channel_messages.py:189`), con un `HEADER_RESERVE` de 70 caracteres. Con WhatsApp el cuerpo máximo es de 1.024 caracteres, y el extracto de la reseña es lo que cede.
- Los botones no cambian: el identificador opaco ya lleva la propuesta, y con ella la ficha.
- `get_account_info` se divide en dos: `account_info(account_id)` para la zona y `location_label(account_id, location_id)` para el encabezado.
- La bienvenida (`welcome.linked`) nombra las fichas que verá ese chat: una, «tus N negocios» o la lista del filtro.

### 6.3 Texto libre con varias propuestas abiertas

Una función pura en `app/domain/conversation.py`, `resolve_text_target(text, candidates, reply_to, focus, now)`. Los candidatos son las propuestas activas de los carriles que ve esa dirección. Criterios, por orden:

1. **Respuesta citando un mensaje.** El adaptador de Telegram lee `reply_to_message.message_id` y lo pone en `InboundEvent.reply_to`. Si coincide con `message_refs[identity.id]` de una candidata, es esa.
2. **Una sola candidata.** Es esa, igual que hoy.
3. **Foco.** La última propuesta en la que esa dirección pulsó un botón, hace menos de 30 minutos, si sigue siendo candidata. Cubre `Editar` → texto y `Otro horario` → «de 10 a 14».
4. **Tipo de respuesta.** Si el texto solo vale para una candidata con reglas deterministas, es esa. Por ejemplo, `parse_hours` reconoce un horario y solo una candidata está en un paso que lo admite. Un paso que acepta cualquier texto (`REVIEW_EDIT_WAIT`) cuenta siempre como candidato, así que con él no se decide.
5. **Si sigue habiendo duda**, se responde `text.which` («¿De qué negocio me hablas?») con un botón por negocio candidato, hasta 3 por el límite de WhatsApp. Si hay más de 3, se pide que responda citando el mensaje.
   - El botón (opción `focus`) fija el foco y vuelve a enseñar el paso de esa propuesta con «Escríbelo otra vez».
   - El texto no se guarda en ningún sitio más que en `channel_message`, como hoy.

Un saludo o un agradecimiento nunca se contesta. Un «sí» o un «no» ambiguos nunca confirman nada.

### 6.4 Filtro por chat

- `sees(identity, location_id)`: `location_filter IS NULL OR location_id = ANY(location_filter)`.
- `may_act(identity, proposal)` añade `sees`: el encargado de B no mueve una propuesta de A aunque conozca su identificador. Responde lo mismo que a un identificador inexistente (`stale.inactive`).
- La entrega y los recordatorios solo van a quien ve la ficha. Si nadie la ve, la propuesta espera, como hoy sin chat vinculado, y el panel la tiene.
- `_clear_buttons` y los resultados de las acciones también se filtran.

### 6.5 Reseñas y fechas clave

- `review_targets` devuelve pares (cuenta, ficha) **activos**. Como `google_sub` es único por cuenta, como mucho hay una cuenta por reseña.
- No se propone una reseña si el `profile_summary` de esa ficha no está en `ok`. Así se evita avisar a un gestor que acaba de perder el acceso (consulta la [sección 10](#10-seguridad)).
- `ReviewInput` y `KeyDateInput` llevan `location_id` obligatorio, y `submit_*` lo guarda en `proposal.location_id`.
- **Limpieza perezosa.** `_pump` cancela con `location_inactive`, sin enviar nada, las propuestas de fichas que ya no están activas: las retiradas por el worker o por una reconexión, que no tiene privilegios sobre `proposal`.

## 7. Planificador, sincronización, fechas clave y cuotas

- **`linked_profiles`.** Devuelve las fichas **activas** de cuentas sin baja que tengan al menos un chat vinculado que las vea. Sustituye a la ficha elegida (`conversation_store/postgres.py:735`).
- **`key_date_profiles`.** Devuelve todas las fichas activas con hechos y `status <> 'needs_reconnect'`. Desde el cobro, solo las de planes con fechas clave (`plans.features`).
- **Reparto en la franja.** El planificador encola el `sync_profile` de cada ficha con `run_at = inicio de la franja + hash(google_sub, location) mod (duración de la franja)`. Hoy salen todos en el mismo minuto. La clave de idempotencia no cambia y la política `job_conversation_insert` ya admite `run_at`.
- **Sincronizar al conectar o restaurar.** `ProfileSync.request_for_account` encola todas las fichas activas de la cuenta, cada una con su clave `reconnect`.
- **Cuotas de Google.** `docs/fase0/matriz-api.md` no tiene cifras de cuota: añade una sección con las de la consola de Google Cloud del proyecto de producción.
  - Una sincronización hace unas 6 llamadas: token, reseñas, detalles, `getGoogleUpdated`, atributos y fotos. Con 25 franjas de día y 3 de noche, son unas 170 llamadas al día por ficha.
  - La cuota habitual de las API de Business Profile es de 300 peticiones por minuto y proyecto (**por verificar**). Con el reparto en la franja de 30 minutos, eso cubre unas 1.500 fichas activas. Sin reparto, unas 50 por minuto de ráfaga.
  - Las escrituras de horario tienen además un tope por ficha (**por verificar**). Una ficha recibe pocas, pero `set_special_hours` debe tratar un 429 como temporal.
  - Al llegar a unas 500 fichas activas, revisa la cuota o alarga la franja.
- **Token de acceso compartido (opcional, paso 5b).** Hoy cada sincronización refresca el token. Con N fichas del mismo usuario son N refrescos por franja. Una caché en memoria del worker por conexión, con caducidad, los reduce a uno. Toca el manejo de secretos: pide revisión de seguridad.
- **Telegram.** Como mucho un mensaje por segundo y chat (`docs/canales.md`, «Límites verificados»). Con varias fichas, varias propuestas salen a la vez al mismo chat. El adaptador debe espaciarlas un segundo y tratar el 429 con `retry_after`.

## 8. Baja

| Alcance | Qué hace | Cambio |
|---|---|---|
| **Desactivar una ficha** (nuevo) | Al momento: no se sincroniza, no se propone nada, se cancelan sus propuestas abiertas y el panel no la sirve. A los 30 días se borra lo obtenido de Google de esa ficha. Reactivarla antes lo conserva | Sin código ni plazo de 48 horas: no revoca ni borra nada al momento y se deshace activándola. No revoca en Google: el permiso es del usuario, no de la ficha |
| **Desconectar Google** (`scope: "ficha"`, sin cambiar el valor) | Como hoy, para **todas** las fichas: la baja efectiva las retira, y el disparador las desactiva | El texto de la web cambia a «Desconectar Google (todas tus fichas)» |
| **Borrar la cuenta** (`scope: "cuenta"`) | Como hoy | B12 ya cancela las propuestas de todas las fichas; además, libera todos los carriles |

- Con una baja programada no se puede activar nada: lo impone el disparador y responde 409.
- Al restaurar una baja, las fichas siguen activas: la baja programada no las desactiva.
- Al volver a conectar tras una baja efectiva de `ficha`, todas están inactivas: el dueño las activa otra vez. Es lo mismo que hoy pasa con la elección.

## 9. Retención

Filas nuevas para `docs/retencion.md`:

| Dato | Tabla | Plazo | Quién lo borra |
|---|---|---|---|
| Lo obtenido de Google de una ficha **desactivada**: resumen, hechos, reseñas, sentimiento e historia | `profile_summary`, `review`, `sentiment_*` | 30 días desde `deactivated_at`, si no se reactiva | Worker, job `retention`, con las mismas sentencias que las fichas retiradas. La fila de `location` se queda mientras Google la liste |
| Registro de activaciones | `location_activation_log` | 12 meses desde la baja efectiva de la cuenta, como `audit_log`. El cobro lo revisará (obligaciones fiscales) | Worker, job `retention` |
| Derechos de plan | `plan_entitlement` | Lo mismo | Worker, job `retention` |
| Carriles | `conversation_lane` | Con la ficha (`ON DELETE CASCADE`) o con la baja efectiva | Base y `apply_offboarding` (`_purge_channels`) |
| Filtro y foco de un chat | `channel_identity` | Con la dirección | Como hoy |

- Ninguna fila nueva lleva datos personales: solo identificadores, fechas y planes.
- `FICHAITOR_RETENTION_INACTIVE_DAYS` (por defecto 30, máximo 30) va en `RetentionPolicy`.
- La política de privacidad (`docs/legal/privacidad.md`) debe decir que desactivar una ficha borra sus datos a los 30 días.

## 10. Seguridad

| Riesgo nuevo | Medida |
|---|---|
| **Ficha de otra cuenta** por `?location=`, en el filtro de un chat o en una propuesta | Búsqueda en la misma sentencia con la cuenta de la sesión y el mismo 404 en los dos casos. Claves ajenas compuestas `(account_id, location_id)` en `proposal` y `conversation_lane`. Disparador que valida `location_filter`. Pruebas de los tres caminos con dos cuentas |
| Saltarse el límite con dos activaciones a la vez o con SQL de la API | Disparador con bloqueo de la cuenta. Los derechos solo los escribe el propietario |
| **Gestor retirado en Google** (pierde el acceso a la ficha y sigue teniendo su cuenta de Fichaitor) | Google rechaza sus escrituras. Hasta dos sincronizaciones denegadas (B5) podría recibir reseñas nuevas: no se propone si el resumen no está en `ok`. La ficha retirada se desactiva en la base |
| **Encargado retirado del negocio con un chat vinculado** a la cuenta del dueño | Es el riesgo más real del filtro por chat. El panel enseña cada chat con su filtro y su última actividad (`last_inbound_at`). Desactivar una ficha deja mudos los chats filtrados solo a ella. Propuesta: avisar en el panel de los chats sin actividad en 60 días |
| Un chat filtrado ve nombres de otros negocios | El encabezado, la bienvenida, `text.which` y los resultados solo nombran fichas que ve ese chat |
| Fechar activaciones hacia atrás (relevante para el cobro) | El disparador exige `active_since` dentro de un minuto de `now()` |
| Abuso del plan gratis: la misma ficha de Google gratis en varias cuentas | Consulta la pregunta abierta 5 |
| Más propuestas abiertas, y más textos de reseñas en el chat | Sin cambio de modelo: el texto de terceros sigue siendo dato y la confirmación es del texto exacto (I14) |

Actualiza `docs/02-hld.md`, «Riesgo residual», con el alcance por ficha de M1.

## 11. Precio: descuento por volumen

**Propuesta: 20 % desde la segunda ficha de pago de la cuenta.**

| Plan | Primera ficha | Segunda y siguientes |
|---|---|---|
| Pro | 35 € | 28 € |
| Pro WhatsApp | 59 € | 47 € (47,20 redondeado) |

Por qué el 20 %:

- **El coste marginal es casi nulo en Pro.** La API de Google no cuesta y el modelo cuesta céntimos. El 20 % deja unos 27 € por ficha extra.
- **WhatsApp sí cuesta por ficha:** hasta 16 € al año con 80 mensajes al mes (`docs/fase0/precios.md`). Con el 20 % quedan al menos 31 €. Con el 30 % (41,30 €) quedarían 25 €, demasiado poco antes de la pasarela.
- **Se entiende de un vistazo:** «la segunda, a 28 €». Un 10 % no mueve a nadie a activar otra ficha. Un 25 % deja precios sin redondear.
- **Frente a la referencia de mercado**, Opinas, a 49 € por ficha y sin descuento conocido, dos fichas Pro cuestan 63 € frente a 98 €.
- A partir de unas 10 fichas, el cliente es una agencia: plan «Agencias», por definir.

Reglas para cuando exista el cobro (no se construyen ahora):

- El descuento se calcula al cobrar, no se guarda en la ficha.
- La ficha a precio completo es la de plan más caro y, a igualdad, la más antigua (`active_since`).
- Los 80 mensajes de WhatsApp son por ficha `pro_whatsapp`, y se suman en la bolsa de la cuenta, porque el chat es uno.

## 12. Contrato para la web

### Selector de negocio

- Va arriba del panel, con las fichas activas primero y, en cada una, el nombre, el plan y sus indicadores:
  - `pending.reviews` y `pending.key_dates` como contadores;
  - `status` `error` o `reconectar` como aviso.
- Debajo, «Otras fichas de tu cuenta de Google», las inactivas, con el botón «Activar».
- Al elegir, llama a `POST /me/location` para guardar la preferencia y, después, pide los datos con `?location=`.
- Envía `?location=` en todas las lecturas y descarta las respuestas cuyo `location_id` no sea la ficha seleccionada.
- Con una sola ficha activa y ninguna inactiva, el selector es solo el título.

### Activación

- «Activar» llama a `POST …/activation`. Con 402, enseña la oferta de planes con el precio con descuento. No hay cobro: hasta el lanzamiento, el texto es «Escríbenos para activar más fichas».
- «Desactivar», en Ajustes de la ficha, pide confirmación con este texto: «Dejaremos de leer esta ficha y de avisarte de ella. Las propuestas pendientes se cancelan. Si no la vuelves a activar en 30 días, borraremos lo que tenemos de ella. Para retirar el acceso de Fichaitor a tu cuenta de Google, usa Desconectar Google.»

### Precios en la respuesta (añadido 2026-11-12)

La web no guarda ninguna cifra: todo precio sale de la API. `GET /me` (y la respuesta de activar y desactivar) añade:

```json
"pricing": {
  "currency": "EUR", "period": "year", "charging": false,
  "plans": { "pro": { "first": 3500, "additional": 3325 },
             "pro_whatsapp": { "first": 5900, "additional": 5605 } },
  "total": 0,
  "if_activate": { "pro": 3325, "pro_whatsapp": 5605 },
  "if_deactivate": { "loc_3f2a": 0 }
}
```

- Importes en céntimos. `total` es lo que pagaría la cuenta con sus fichas activas; `if_activate` y `if_deactivate`, el total resultante de cada cambio. Los calcula `app/domain/plans.py` con las reglas de la sección 11 y las decisiones de la sección 15.
- `charging: false` mientras no haya cobro (beta): la web enseña los precios como referencia y, ante un 402, «Escríbenos para activar más fichas».

### Canales

- Cada chat enseña «Recibe: todos tus negocios» o la lista de fichas, y se puede editar.
- Al pedir un código, se puede elegir de antemano qué fichas verá el chat (el caso del encargado).

### Estados de `App.tsx`

- `elegir` pasa a ser «activar tu primera ficha».
- `panel` recibe la ficha seleccionada entre las activas.
- `sin-acceso` se queda.

### Ejemplo de `GET /me`

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
  "plans": { "active": 1, "free_available": false,
             "available": { "pro": 0, "pro_whatsapp": 0 } },
  "connected": true,
  "pending_deletion": null
}
```

## 13. Orden de implementación

Cada paso se despliega con los `db/schema*.sql` aplicados otra vez y deja el sistema funcionando. Un archivo pertenece a un solo paso en cada ola, para que dos agentes no lo toquen a la vez.

| Ola | Pasos en paralelo |
|---|---|
| 1 | 1, 2a, 2b y 8 (contra `mock.ts`) |
| 2 | 3, 5 y 6a |
| 3 | 4 y 6b |
| 4 | 7 y el cierre de 8 |
| 5 | 9 |

### Paso 1. Esquema y relleno

- **Depende de:** la revisión b integrada en `db/`.
- **Archivos:** `db/schema.sql`, `db/schema_worker.sql`, `db/schema_channels.sql`, `docs/03-lld.md` y el nuevo `tests/test_multinegocio_schema_postgres.py`.
- **Qué hace:** todo el apartado 3, salvo el disparador de inserción de `proposal` y el relleno de carriles (paso 6a). Privilegios de la sección 4. La expresión de `usage_counter.scope`.
- **Deja funcionando:** el código antiguo, sin cambios. La ficha elegida ya está activa.
- **Pruebas clave:**
  - el relleno activa la ficha elegida, y aplicar otra vez el esquema no reactiva una desactivada;
  - una segunda ficha `gratis` se rechaza; `pro` sin derecho se rechaza y con derecho se acepta; dos activaciones a la vez en dos conexiones, solo una gana;
  - retirar una ficha la desactiva, también con el rol del worker;
  - la API no escribe en `plan_entitlement` ni fija `deactivated_at`;
  - una propuesta o un carril con la ficha de otra cuenta, y un `location_filter` ajeno, se rechazan;
  - el `DELETE` de una ficha por el worker pone `proposal.location_id` a `NULL` y borra su carril, pese a las políticas de fila;
  - el registro guarda `written_by`.
  - Todo contra `fichaitor_test`, con `WHERE false` en las sentencias que deben denegarse.

### Paso 2a. Dominio de planes

- **Depende de:** nada.
- **Archivos:** el nuevo `app/domain/plans.py`, el nuevo `tests/test_plans.py` y `docs/fase0/precios.md` (cuando el propietario confirme el descuento).
- **Qué hace:** planes, sus características, `activation_allowed(activas_por_plan, derechos, plan)`, el plan por defecto y `yearly_price(plans)` con la constante del descuento. Es una función pura: no cobra.
- **Pruebas clave:** límites, plan por defecto y precio de una mezcla (gratis + pro + pro WhatsApp).

### Paso 2b. Dominio de la conversación

- **Depende de:** nada.
- **Archivos:** `app/domain/conversation.py`, `app/domain/channel_messages.py` (encabezado, `HEADER_RESERVE`, `text.which`, `welcome.linked` para varias fichas, `InboundEvent.reply_to`), `app/domain/proposals.py` (`OUTCOME_LOCATION_INACTIVE`) y sus pruebas.
- **Qué hace:** `sees`, `may_act` con filtro, `resolve_text_target` y la etiqueta con la localidad. Todo puro.
- **Pruebas clave:**
  - los cinco criterios del 6.3, en ese orden;
  - una cortesía nunca se contesta;
  - un «sí» ambiguo no confirma;
  - un chat filtrado no actúa sobre otra ficha;
  - el mensaje más largo con encabezado y prefijo cabe en 1.024 caracteres.

### Paso 3. Panel por ficha: almacén y aplicación

- **Depende de:** 1 y 2a.
- **Archivos:** `app/ports/store.py`, `app/adapters/store/postgres.py`, `app/adapters/store/memory.py`, `app/application/profile_sync.py`, `app/application/review_replies.py`, `app/application/key_dates.py`, `app/domain/quota.py` y sus pruebas.
- **Qué hace:**
  - métodos con `location_id`: `profile`, `list_reviews`, `sentiment`, `list_key_dates`, `activate`, `deactivate` y `locations_overview` (indicadores), con el fragmento `_ACTIVE`;
  - los métodos antiguos delegan en la preferencia, así que es un cambio aditivo;
  - `request_for_account` sincroniza todas las fichas activas;
  - tope de borradores por ficha;
  - aplica otra vez el relleno del 3.8.1.
- **Deja funcionando:** la API sin cambios.
- **Pruebas clave:**
  - con dos cuentas, cualquier `location_id` ajeno da `None` o 404;
  - una ficha inactiva no se sirve;
  - desactivar cancela sus propuestas y libera su carril en la misma transacción;
  - el disparador traduce su error a `LocationLimit`.

### Paso 4. Rutas de la API y contrato

- **Depende de:** 3.
- **Archivos:** `app/api/me.py`, `app/api/reviews.py`, `app/api/key_dates.py`, el nuevo `app/api/locations.py`, `app/api/session.py` (dependencia `location_param`), `app/api/main.py`, `app/api/{summary,diagnosis,sentiment}_out.py`, `docs/contrato-web-api.md`, `README.md` y sus pruebas.
- **Qué hace:** la sección 5.
- **Deja funcionando:** la web actual sin `?location=`. La web nueva ya puede activar.
- **Pruebas clave:**
  - `?location=` ajeno, inexistente o inactivo;
  - 402 al pasar el límite;
  - compatibilidad de `POST /me/location`, incluida la sustitución;
  - `location_id` en cada respuesta;
  - 409 con una baja programada.

### Paso 5. Worker: coherencia y retención

- **Depende de:** 1.
- **Archivos:** `app/adapters/worker_store/postgres.py` (`claim_action`, retención), `app/ports/worker_store.py`, `app/domain/offboarding.py` (`RetentionPolicy.inactive_days`), `app/retention_config.py`, `app/worker/retention.py`, `docs/retencion.md` y la sección «Baja, correo y retención» de `docs/worker.md` (no otras secciones).
- **Paso 5b, opcional:** caché del token de acceso en `app/worker/sync_profile.py`, con revisión de seguridad.
- **Pruebas clave:**
  - una acción sobre una ficha desactivada no llega a Google;
  - la retención borra los datos de una ficha inactiva 30 días y no los de una reactivada;
  - no borra propuestas nombradas por un carril.

### Paso 6a. Almacén de la conversación con carriles

- **Depende de:** 1 y 2b.
- **Archivos:** `app/ports/conversation_store.py`, `app/adapters/conversation_store/postgres.py`, `app/adapters/conversation_store/memory.py` y el bloque de carriles de `db/schema_channels.sql` (relleno del 3.8.4 y disparador de inserción de `proposal`; ese archivo ya no lo toca nadie en esta ola).
- **Qué hace:**
  - métodos nuevos: `get_lane`, `save_lane`, `lanes_with_open_work`, `identities_for(account, location)`, `location_label`, `proposal_by_message_ref`, `set_focus` y `count_capped_sent_since` con ficha;
  - `linked_profiles`, `key_date_profiles` y `review_targets` sobre fichas activas.
  - Es aditivo: los métodos antiguos siguen hasta el 6b.
- **Pruebas clave:**
  - con dos cuentas, `identities_for` respeta el filtro;
  - las consultas ignoran las fichas inactivas y las de cuentas con baja programada;
  - el relleno de carriles es idempotente y no copia propuestas terminadas.

### Paso 6b. Servicio de la conversación y jobs

- **Depende de:** 6a.
- **Archivos:** `app/application/conversation_service.py`, `app/worker/{propose_review,plan_key_dates,channel_planner,conversation}.py`, `app/adapters/proposal_actions/{review_replies,special_hours}.py`, `app/adapters/messaging/telegram.py` (`reply_to`, espaciado de 1 segundo por chat), `docs/canales.md` y las secciones del planificador y del proceso de la conversación de `docs/worker.md`.
- **Deja funcionando:** varias fichas por Telegram en un solo chat.
- **Pruebas clave**, con el almacén en memoria y con PostgreSQL:
  - dos fichas, dos propuestas abiertas a la vez, cada una con su encabezado;
  - una reseña de 1 estrella en A no interrumpe a B;
  - un texto con una sola candidata, con foco, citando el mensaje y ambiguo (`text.which`);
  - un botón de una ficha desactivada da `stale.inactive`;
  - el límite semanal por ficha;
  - el planificador reparte el `run_at` y no duplica claves;
  - `plan_key_dates` crea una propuesta por ficha y día.

### Paso 7. Filtro por chat

- **Depende de:** 4 y 6b.
- **Archivos:** `app/api/channels.py`, `app/adapters/store/postgres.py` y `memory.py` (solo los métodos de canales), `app/domain/link_codes.py`, el método de vinculación de `app/adapters/conversation_store/postgres.py` (copia el filtro), `docs/contrato-web-api.md` (sección de canales) y `docs/canales.md`.
- **Pruebas clave:**
  - un código con filtro vincula un chat que nunca recibe otra ficha, desde el primer mensaje;
  - `PATCH` con una ficha ajena da 404;
  - quitar el filtro devuelve todas las fichas.

### Paso 8. Web

- **Depende de:** el contrato de las secciones 5 y 12 para empezar con `mock.ts`; los pasos 4 y 7 para cerrar.
- **Archivos:** `web/app/src/{api.ts,App.tsx,mock.ts,Ajustes.tsx,Canales.tsx,Resumen.tsx,Resenas.tsx,Sentimiento.tsx,Fechas.tsx}` y el nuevo `Selector.tsx`.
- **Pruebas clave:** cambiar de ficha mientras carga no mezcla datos; flujo de activación y 402; desactivar con su confirmación.

### Paso 9. Retirada de la compatibilidad

- **Depende de:** 8 desplegado.
- **Qué hace:**
  - `POST /me/location` solo cambia la preferencia;
  - se borran `conversation.active_proposal_id` y sus políticas, `_SELECTED`, `selected_profile` y los métodos antiguos de los puertos;
  - se marca hecho el punto 8 de `docs/05-roadmap.md`.

## 14. Riesgos

- **Ruido en un solo chat.** Cinco fichas pueden mandar cinco mensajes a las 9:00. El límite semanal por ficha y el horario de silencio lo acotan. Si la beta lo confirma, el siguiente paso es `review.digest`.
- **Doble significado transitorio de `selected_location_id`**, entre los pasos 1 y 9. Lo cubren el relleno idempotente y la compatibilidad de la sección 5.5. No despliegues el 6b sin el 6a ni el relleno de carriles con la conversación en marcha.
- **Cuotas de Google sin verificar.** Hay que medirlas antes de pasar de unos cientos de fichas activas.
- **Borrado a los 30 días de una ficha desactivada.** Puede sorprender al dueño. La web debe decirlo al desactivar, y la política de privacidad, recogerlo.
- **Fichas con el mismo nombre.** El encabezado con localidad lo resuelve si la ficha tiene dirección. Si no la tiene, se usa el final del identificador público.
- **El modelo de derechos puede quedarse corto** con el cobro real (prorrateos y cambios de plan a mitad de año). El registro de activaciones da la historia necesaria.

## 15. Decisiones del propietario (2026-11-12)

1. **Descuento: 5 %** desde la segunda ficha **de pago** (no el 20 % propuesto). Pro: 35 € la primera y 33,25 € las siguientes. Pro WhatsApp: 59 € y 56,05 €. Se aplica al cobrar; la sección 11 queda como razonamiento, con este porcentaje.
2. **Gratis más Pro:** la ficha Gratis no cuenta como primera. El descuento empieza en la segunda ficha de pago.
3. **Mezcla de planes:** sí. WhatsApp solo avisa de las fichas `pro_whatsapp`; las demás, por Telegram, correo o panel.
4. **Beta:** derecho `beta` de **3 fichas Pro** por cuenta hasta el lanzamiento, ampliable a mano.
5. **Abuso del plan gratis:** una ficha de Google solo puede estar activa en Gratis en **una** cuenta de Fichaitor.
6. **Chat limitado (encargado):** **puede actuar** (publicar respuestas y confirmar horarios), solo sobre sus fichas.
