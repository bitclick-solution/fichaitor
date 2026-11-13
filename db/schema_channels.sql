-- Fichaitor: esquema de los canales de mensajería (direcciones vinculadas, códigos de
-- vinculación, conversaciones, mensajes y propuestas). Consulta docs/canales.md.
-- Idempotente. Se aplica DESPUÉS de schema.sql y schema_worker.sql, con el rol propietario:
--   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_channels.sql
-- Necesita la extensión pgcrypto, las tablas account, location y usage_counter de
-- schema.sql y las tablas job, review, review_draft, action, audit_log y offboarding de
-- schema_worker.sql.
--
-- Tres roles (docs/canales.md, "Paso 0", opción A):
--   fichaitor_api           la sesión web: pide códigos, lista y desvincula direcciones,
--                           y encola lo que llega por el webhook.
--   fichaitor_conversation  el proceso de la conversación (python -m app.worker.conversation):
--                           habla con el dueño, crea la acción cuando confirma (como la
--                           API desde el panel) y no puede ejecutarla: no lee
--                           google_connection ni tiene la clave privada.
--   fichaitor_worker        el ejecutor: no habla con nadie y no crea acciones. En estas
--                           tablas solo borra (baja y retención).

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_api') THEN
        CREATE ROLE fichaitor_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_worker') THEN
        CREATE ROLE fichaitor_worker NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_conversation') THEN
        CREATE ROLE fichaitor_conversation NOLOGIN;
    END IF;
END
$$;

-- Direcciones vinculadas con código a una cuenta: un chat de Telegram, un teléfono de
-- WhatsApp (E.164) o un correo. Solo una dirección vinculada se atiende y puede actuar.
-- Una cuenta puede tener varias (dueño y encargado), cada una con su propio código.
-- Datos personales: la dirección. Desvincular no borra la fila: pone unlinked_at, y
-- desde ese instante no se le envía nada ni se le atiende. La baja efectiva de la cuenta
-- borra sus filas; la retención, las desvinculadas hace más de 90 días.
-- La clave ajena a account no tiene ON DELETE: una cuenta dada de baja no se borra (queda
-- como lápida, sin correo), y sus filas aquí se borran antes, en la misma transacción.
CREATE TABLE IF NOT EXISTS channel_identity (
    id              text PRIMARY KEY DEFAULT 'chid_' || encode(gen_random_bytes(8), 'hex')
                    CHECK (id ~ '^chid_[0-9a-f]{8,32}$'),
    account_id      uuid NOT NULL REFERENCES account (id),
    channel         text NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'email')),
    address         text NOT NULL CHECK (length(address) BETWEEN 1 AND 254),
    verified_at     timestamptz NOT NULL DEFAULT now(),
    last_inbound_at timestamptz,
    unlinked_at     timestamptz,
    CHECK (channel <> 'telegram' OR address ~ '^-?[0-9]{1,20}$'),
    CHECK (channel <> 'whatsapp' OR address ~ '^\+[1-9][0-9]{7,14}$')
);

-- Una dirección pertenece a una sola cuenta a la vez: vincularla a otra cierra antes
-- el vínculo anterior, en la misma transacción.
CREATE UNIQUE INDEX IF NOT EXISTS channel_identity_one_active_per_address
    ON channel_identity (channel, address) WHERE unlinked_at IS NULL;
CREATE INDEX IF NOT EXISTS channel_identity_account
    ON channel_identity (account_id) WHERE unlinked_at IS NULL;

-- Códigos de vinculación: 6 dígitos para teclearlos y un testigo largo y aleatorio para
-- el enlace t.me/<bot>?start=<testigo> (B3 de docs/seguridad/revision-2026-11-12.md).
-- Son el mismo código: de un solo uso (el primero que llega lo gasta) y caduca a los 10
-- minutos. Ni los dígitos ni el testigo se guardan: code_hash y token_hash son su
-- SHA-256 (HMAC con el secreto FICHAITOR_LINK_CODE_PEPPER si está definido). used_at lo
-- marca quien lo consume o quien lo retira al pedir otro.
CREATE TABLE IF NOT EXISTS link_code (
    code_hash  text PRIMARY KEY CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    account_id uuid NOT NULL REFERENCES account (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    token_hash text CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '15 minutes')
);

-- Bases anteriores al 2026-11-12 (B3): el testigo del enlace.
ALTER TABLE link_code ADD COLUMN IF NOT EXISTS token_hash text
    CHECK (token_hash ~ '^[0-9a-f]{64}$');
CREATE UNIQUE INDEX IF NOT EXISTS link_code_token ON link_code (token_hash)
    WHERE token_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS link_code_account ON link_code (account_id) WHERE used_at IS NULL;

-- Lo que se recuerda de una dirección que NO está vinculada: códigos fallidos seguidos,
-- bloqueo de una hora al tercero y cuándo se le dio la respuesta genérica (una cada 24
-- horas). Sin datos personales: address_hash es el SHA-256 de canal y dirección.
-- El intento se cuenta ANTES de mirar el código, con una sola sentencia (B3).
CREATE TABLE IF NOT EXISTS channel_sender (
    address_hash           text PRIMARY KEY CHECK (address_hash ~ '^[0-9a-f]{64}$'),
    failed_attempts        smallint NOT NULL DEFAULT 0 CHECK (failed_attempts BETWEEN 0 AND 1000),
    blocked_until          timestamptz,
    last_unlinked_reply_at timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now()
);

-- Códigos incorrectos de todas las direcciones juntas, por ventana de una hora (B3).
-- Pasado el límite, los códigos tecleados no se miran hasta la ventana siguiente; el
-- enlace con el testigo sigue funcionando. Sin datos personales.
CREATE TABLE IF NOT EXISTS link_failure_window (
    window_start timestamptz PRIMARY KEY,
    failures     integer NOT NULL DEFAULT 0 CHECK (failures >= 0)
);

-- Propuestas: todo aquello en lo que el agente toma la iniciativa. Estados y
-- transiciones en app/domain/proposals.py (docs/01-flujos.md, sección 4).
-- payload: con qué se creó (estrellas, texto de la reseña y respuesta propuesta, o la
-- fecha clave). No cambia nunca: nadie tiene UPDATE sobre él.
-- slots: lo que recoge la conversación (el texto del dueño, el horario).
-- step y step_token: paso de la conversación y testigo del botón vigente.
-- message_refs: {identidad: id del último mensaje con botones en su canal}.
-- confirmed_*: la confirmación registrada del dueño. Sin ella no hay acción.
-- Una propuesta por cuenta, tipo y origen: la misma reseña no se propone dos veces.
CREATE TABLE IF NOT EXISTS proposal (
    id                 text PRIMARY KEY CHECK (id ~ '^prop_[0-9a-f]{16}$'),
    account_id         uuid NOT NULL REFERENCES account (id),
    type               text NOT NULL CHECK (type IN ('review_reply', 'key_date', 'owner_order')),
    source_ref         text NOT NULL CHECK (length(source_ref) BETWEEN 1 AND 200),
    state              text NOT NULL DEFAULT 'programada'
                       CHECK (state IN ('programada', 'enviada', 'recordada', 'recopilando',
                                        'por_confirmar', 'ejecutando', 'aplicada', 'caducada',
                                        'descartada', 'fallida', 'cancelada')),
    priority           smallint NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 1000),
    payload            jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    slots              jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(slots) = 'object'),
    step               text NOT NULL DEFAULT '' CHECK (step ~ '^[a-z_.]{0,40}$'),
    step_token         text NOT NULL DEFAULT '' CHECK (step_token ~ '^[0-9a-f]{0,16}$'),
    message_refs       jsonb NOT NULL DEFAULT '{}'::jsonb
                       CHECK (jsonb_typeof(message_refs) = 'object'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    scheduled_for      timestamptz NOT NULL DEFAULT now(),
    sent_at            timestamptz,
    remind_at          timestamptz,
    reminder_sent_at   timestamptz,
    expires_at         timestamptz,
    parked_at          timestamptz,
    confirmed_at       timestamptz,
    confirmed_event_id text CHECK (length(confirmed_event_id) BETWEEN 1 AND 120),
    confirmed_summary  text CHECK (octet_length(confirmed_summary) <= 4096),
    action_id          text CHECK (length(action_id) <= 64),
    outcome_reason     text CHECK (outcome_reason ~ '^[a-z_]{1,40}$'),
    UNIQUE (account_id, type, source_ref),
    -- Segunda barrera del invariante: nada llega a ejecutarse sin la confirmación
    -- registrada del dueño (cuándo y con qué mensaje entrante).
    CHECK (state NOT IN ('ejecutando', 'aplicada', 'fallida')
           OR (confirmed_at IS NOT NULL AND confirmed_event_id IS NOT NULL))
);

-- Solo las propuestas sin terminar: la tabla crece con las terminadas, el índice no.
CREATE INDEX IF NOT EXISTS proposal_unfinished ON proposal (account_id)
    WHERE state IN ('programada', 'enviada', 'recordada', 'recopilando', 'por_confirmar',
                    'ejecutando');
-- Para el límite semanal de mensajes proactivos.
CREATE INDEX IF NOT EXISTS proposal_sent ON proposal (account_id, sent_at)
    WHERE sent_at IS NOT NULL;

-- Para avisar al dueño del resultado de la acción que salió de su confirmación.
CREATE INDEX IF NOT EXISTS proposal_action ON proposal (action_id) WHERE action_id IS NOT NULL;

-- El hilo entre el agente y una cuenta, participen las direcciones que participen.
-- La fila se bloquea (SELECT ... FOR UPDATE) mientras se atiende un mensaje, para que
-- dos pulsaciones no se traten a la vez. Las propuestas abiertas van en los carriles
-- (conversation_lane), una por ficha. La columna active_proposal_id de antes del
-- multinegocio se retira al final de este archivo (paso 9 de docs/multinegocio.md).
CREATE TABLE IF NOT EXISTS conversation (
    id                 text PRIMARY KEY DEFAULT 'conv_' || encode(gen_random_bytes(8), 'hex')
                       CHECK (id ~ '^conv_[0-9a-f]{8,32}$'),
    account_id         uuid NOT NULL UNIQUE REFERENCES account (id),
    unknown_streak     smallint NOT NULL DEFAULT 0 CHECK (unknown_streak BETWEEN 0 AND 1000),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Mensajes entrantes ya vistos, de direcciones vinculadas o no: el mismo evento de un
-- canal se atiende una sola vez (Telegram repite la entrega si el webhook falla).
CREATE TABLE IF NOT EXISTS channel_inbound_event (
    channel     text NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'email')),
    event_id    text NOT NULL CHECK (length(event_id) BETWEEN 1 AND 120),
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, event_id)
);

CREATE INDEX IF NOT EXISTS channel_inbound_event_at ON channel_inbound_event (received_at);

-- Mensajes de la conversación, en los dos sentidos. Solo se añaden filas: son la
-- prueba de qué se mostró al dueño y qué contestó. Se conservan 90 días.
-- Datos personales y de terceros: body lleva lo que escribió el dueño y, en los
-- salientes, el extracto de la reseña. Un código de vinculación nunca se guarda aquí
-- (el mensaje que lo trae se anota como '[código]').
CREATE TABLE IF NOT EXISTS channel_message (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at                 timestamptz NOT NULL DEFAULT now(),
    conversation_id    text NOT NULL REFERENCES conversation (id),
    identity_id        text REFERENCES channel_identity (id),
    direction          text NOT NULL CHECK (direction IN ('in', 'out')),
    channel            text NOT NULL CHECK (channel IN ('telegram', 'whatsapp', 'email')),
    channel_message_id text NOT NULL CHECK (length(channel_message_id) <= 120),
    kind               text NOT NULL CHECK (kind ~ '^[a-z_]{1,20}$'),
    key                text NOT NULL DEFAULT '' CHECK (key ~ '^[a-z_.0-9]{0,60}$'),
    body               text NOT NULL CHECK (length(body) <= 4096),
    proposal_id        text REFERENCES proposal (id)
);

CREATE INDEX IF NOT EXISTS channel_message_conversation ON channel_message (conversation_id, id);
CREATE INDEX IF NOT EXISTS channel_message_at ON channel_message (at);
CREATE INDEX IF NOT EXISTS channel_message_identity ON channel_message (identity_id);
CREATE INDEX IF NOT EXISTS channel_message_proposal ON channel_message (proposal_id);

-- ---------------------------------------------------------------------------
-- Privilegios mínimos. Se retira todo y se concede lo justo en cada aplicación.
REVOKE ALL ON channel_identity, link_code, channel_sender, link_failure_window, proposal,
              conversation, channel_inbound_event, channel_message
    FROM PUBLIC, fichaitor_api, fichaitor_worker, fichaitor_conversation;

-- === API =====================================================================
-- Direcciones: las lista en Ajustes y las desvincula (también al confirmar la baja de
-- la cuenta). No puede crearlas ni cambiarles la cuenta o la dirección: vincular exige
-- enviar el código desde el chat.
GRANT SELECT ON channel_identity TO fichaitor_api;
GRANT UPDATE (unlinked_at) ON channel_identity TO fichaitor_api;
-- Códigos: los crea y retira los anteriores de la cuenta. No lee las huellas.
GRANT INSERT (code_hash, token_hash, account_id, created_at, expires_at)
    ON link_code TO fichaitor_api;
GRANT SELECT (account_id, used_at) ON link_code TO fichaitor_api;
GRANT UPDATE (used_at) ON link_code TO fichaitor_api;
-- Propuestas: las lee (bandeja del panel) y, desde las fechas clave (2026-11-12),
-- responde o descarta las de su cuenta como lo haría el chat: estado, testigo y
-- confirmación registrada. No puede crearlas ni cambiar de quién son, su tipo, su
-- origen ni su carga. Para no pisarse con el chat, bloquea antes el hilo
-- (SELECT ... FOR UPDATE sobre conversation) y libera el carril de su ficha si era la
-- activa. Bloquear exige UPDATE en alguna columna: updated_at, y la política
-- conversation_api_lock no deja cambiar ninguna fila.
GRANT SELECT ON proposal TO fichaitor_api;
GRANT UPDATE (state, slots, step_token, message_refs, confirmed_at, confirmed_event_id,
              confirmed_summary, action_id, outcome_reason)
    ON proposal TO fichaitor_api;
GRANT SELECT (id, account_id) ON conversation TO fichaitor_api;
GRANT UPDATE (updated_at) ON conversation TO fichaitor_api;

-- === Conversación ============================================================
GRANT SELECT ON channel_identity TO fichaitor_conversation;
GRANT INSERT (account_id, channel, address, verified_at, last_inbound_at)
    ON channel_identity TO fichaitor_conversation;
GRANT UPDATE (last_inbound_at, unlinked_at) ON channel_identity TO fichaitor_conversation;

-- Códigos: los busca por huella y los consume; no puede crearlos.
GRANT SELECT ON link_code TO fichaitor_conversation;
GRANT UPDATE (used_at) ON link_code TO fichaitor_conversation;

-- Remitentes sin vincular, contador global, hilos y eventos vistos.
GRANT SELECT, INSERT, UPDATE ON channel_sender, link_failure_window, channel_inbound_event
    TO fichaitor_conversation;
GRANT SELECT, INSERT ON conversation TO fichaitor_conversation;
GRANT UPDATE (unknown_streak, updated_at) ON conversation TO fichaitor_conversation;

-- Propuestas: las crea y las mueve, sin poder cambiar de quién son ni con qué se crearon.
GRANT SELECT, INSERT ON proposal TO fichaitor_conversation;
GRANT UPDATE (state, priority, slots, step, step_token, message_refs, scheduled_for, sent_at,
              remind_at, reminder_sent_at, expires_at, parked_at, confirmed_at,
              confirmed_event_id, confirmed_summary, action_id, outcome_reason)
    ON proposal TO fichaitor_conversation;

-- Mensajes: añade y nada más. Sin SELECT, UPDATE ni DELETE.
GRANT INSERT (conversation_id, identity_id, direction, channel, channel_message_id, kind, key,
              body, proposal_id, at)
    ON channel_message TO fichaitor_conversation;

-- Fuera de las tablas de canales, lo que necesita para hacer lo mismo que la API hace
-- desde el panel (app/application/review_replies.py y Store.create_reply_action), con
-- las mismas consultas: leer las reseñas de las fichas activas de una cuenta, guardar el
-- borrador, contar el tope de borradores y crear la acción confirmada con su fila de
-- auditoría y su job. No lee el correo de la cuenta ni google_connection.
REVOKE ALL ON account, location, review, review_draft, usage_counter, action, audit_log,
              job, offboarding, profile_summary
    FROM fichaitor_conversation;
-- Sin selected_location_id desde el paso 9: la preferencia del panel no decide nada en
-- la conversación.
GRANT SELECT (id, google_sub, deleted_at, disconnected_at) ON account
    TO fichaitor_conversation;
-- Fechas clave: el planificador lee de cada ficha elegida su estado y sus hechos
-- (comunidad, dirección y días con horario especial). Nada más del resumen.
GRANT SELECT (google_sub, location, status, facts) ON profile_summary
    TO fichaitor_conversation;
GRANT SELECT (id, account_id, gbp_location, title, removed_at) ON location
    TO fichaitor_conversation;
-- Multinegocio (2026-11-12): qué fichas están activas y con qué plan. No las activa ni
-- las desactiva.
GRANT SELECT (active_since, plan, deactivated_at) ON location TO fichaitor_conversation;
GRANT SELECT (account_id, google_sub, restored_at, effective_at, purged_at) ON offboarding
    TO fichaitor_conversation;
GRANT SELECT ON review TO fichaitor_conversation;
GRANT SELECT, INSERT, UPDATE ON review_draft TO fichaitor_conversation;
GRANT SELECT (scope, window_start, hits) ON usage_counter TO fichaitor_conversation;
GRANT INSERT (scope, window_start, expires_at, hits) ON usage_counter TO fichaitor_conversation;
GRANT UPDATE (hits) ON usage_counter TO fichaitor_conversation;
-- Acciones: las crea como la API (sin estado ni resultado) y las lee. No puede
-- cambiarlas. Solo el ejecutor del worker escribe en Google, y antes comprueba que la
-- cuenta, su usuario de Google, la ficha y la reseña casan (claim_action, M1).
GRANT SELECT ON action TO fichaitor_conversation;
GRANT INSERT (account_id, type, google_sub, location, review_id, special_date, text,
                confirmed_at)
    ON action TO fichaitor_conversation;
GRANT INSERT (event, account_id, action_id, action_type, location, review_id,
              gbp_review_id, text, confirmed_at, google_status, reason)
    ON audit_log TO fichaitor_conversation;
DROP POLICY IF EXISTS audit_log_conversation_insert ON audit_log;
CREATE POLICY audit_log_conversation_insert ON audit_log FOR INSERT TO fichaitor_conversation
    WITH CHECK (event IN ('reply_confirmed', 'special_hours_confirmed'));
-- Cola: toma y cierra sus propios jobs (los de canal), encola el job del ejecutor de
-- cada acción que crea y las sincronizaciones periódicas, y vacía la carga de los jobs
-- del webhook al terminarlos. No puede borrar jobs. Qué filas ve, toma y crea lo dicen
-- las políticas de la cola, más abajo (B9).
GRANT SELECT ON job TO fichaitor_conversation;
GRANT INSERT (kind, payload, run_at, idempotency_key, max_attempts) ON job
    TO fichaitor_conversation;
GRANT UPDATE (state, attempts, locked_by, locked_at, run_at, finished_at, last_error, payload)
    ON job TO fichaitor_conversation;

-- === Worker ==================================================================
-- Ya no lleva la conversación: en estas tablas solo borra, al hacer efectiva una baja y
-- en la tarea de retención. Lee solo las columnas que usan sus WHERE.
GRANT SELECT (id, account_id, unlinked_at) ON channel_identity TO fichaitor_worker;
GRANT SELECT (account_id, expires_at, used_at) ON link_code TO fichaitor_worker;
GRANT SELECT (updated_at, blocked_until) ON channel_sender TO fichaitor_worker;
GRANT SELECT (window_start) ON link_failure_window TO fichaitor_worker;
GRANT SELECT (id, account_id, state, created_at) ON proposal TO fichaitor_worker;
GRANT SELECT (id, account_id) ON conversation TO fichaitor_worker;
GRANT SELECT (received_at) ON channel_inbound_event TO fichaitor_worker;
GRANT SELECT (id, at, conversation_id, identity_id, proposal_id) ON channel_message
    TO fichaitor_worker;
GRANT DELETE ON channel_identity, link_code, channel_sender, link_failure_window, proposal,
                conversation, channel_inbound_event, channel_message
    TO fichaitor_worker;
-- Antes la conversación iba en el worker y leía el nombre de la ficha: ya no lo necesita.
REVOKE SELECT (title) ON location FROM fichaitor_worker;

-- ---------------------------------------------------------------------------
-- Cola de jobs: quién ve, toma y encola qué (B9 y B10 de
-- docs/seguridad/revision-2026-11-12-b.md). Aquí y no en schema_worker.sql porque nombra
-- a los tres roles.
-- - El worker: todo (es quien ejecuta, planifica y borra).
-- - La API: solo encola, y solo los tipos que encola su código: la respuesta y el horario
--   confirmados, el correo y la baja, la sincronización de la ficha y lo que llega por el
--   webhook. No lee la cola.
-- - La conversación: ve, toma y cierra solo sus tipos (app/domain/channel_jobs.py:KINDS),
--   así que nunca lee la carga de un send_mail (el correo del dueño, el código de baja y el
--   token de restauración). Encola solo lo que encola su código.
-- - Clave de idempotencia: vacía, o "<tipo>:..." del tipo que se encola (más la forma
--   antigua "<canal>:<update_id>" de channel_inbound). Así nadie más que el worker ocupa
--   "retention:<fecha>" ni otra clave diaria del sistema.
-- - I16 de docs/seguridad/revision-2026-11-12-c.md: "sync:<google_sub>:<ficha>:<franja>",
--   la clave de la sincronización periódica, es del planificador (la conversación) y del
--   worker. La API no la puede ocupar: si lo hacía, su job (con su carga) ocupaba la
--   franja y el planificador no encolaba el suyo. Las sincronizaciones que pide la API
--   llevan "sync_profile:..." (app/application/profile_sync.py), otro espacio de claves.
CREATE OR REPLACE FUNCTION job_key_fits(kind text, key text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT key IS NULL
        OR starts_with(key, kind || ':')
        OR (kind = 'channel_inbound' AND key ~ '^(telegram|whatsapp|email):[0-9]{1,20}$')
$$;

ALTER TABLE job ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS job_worker ON job;
CREATE POLICY job_worker ON job FOR ALL TO fichaitor_worker USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS job_api_insert ON job;
CREATE POLICY job_api_insert ON job FOR INSERT TO fichaitor_api
    WITH CHECK (kind IN ('reply_review', 'set_special_hours', 'send_mail', 'apply_offboarding',
                         'sync_profile', 'channel_inbound')
                AND job_key_fits(kind, idempotency_key));
DROP POLICY IF EXISTS job_conversation_read ON job;
CREATE POLICY job_conversation_read ON job FOR SELECT TO fichaitor_conversation
    USING (kind IN ('channel_inbound', 'conversation_tick', 'propose_review',
                    'channel_action_result', 'plan_key_dates'));
DROP POLICY IF EXISTS job_conversation_update ON job;
CREATE POLICY job_conversation_update ON job FOR UPDATE TO fichaitor_conversation
    USING (kind IN ('channel_inbound', 'conversation_tick', 'propose_review',
                    'channel_action_result', 'plan_key_dates'))
    WITH CHECK (kind IN ('channel_inbound', 'conversation_tick', 'propose_review',
                         'channel_action_result', 'plan_key_dates'));
DROP POLICY IF EXISTS job_conversation_insert ON job;
CREATE POLICY job_conversation_insert ON job FOR INSERT TO fichaitor_conversation
    WITH CHECK (kind IN ('conversation_tick', 'sync_profile', 'plan_key_dates', 'reply_review',
                         'set_special_hours')
                AND (job_key_fits(kind, idempotency_key)
                     OR (kind = 'sync_profile' AND starts_with(idempotency_key, 'sync:'))));

-- Solo en una base de pruebas (su nombre termina en _test): las pruebas encolan y toman
-- jobs de tipos propios ('pytest-...') para que ningún proceso real los ejecute. Con las
-- mismas reglas de clave. En cualquier otra base esta política no existe.
DROP POLICY IF EXISTS job_pytest_api ON job;
DROP POLICY IF EXISTS job_pytest_conversation ON job;
DO $$
BEGIN
    IF current_database() LIKE '%\_test' THEN
        CREATE POLICY job_pytest_api ON job FOR INSERT TO fichaitor_api
            WITH CHECK (kind LIKE 'pytest-%' AND job_key_fits(kind, idempotency_key));
        CREATE POLICY job_pytest_conversation ON job FOR ALL TO fichaitor_conversation
            USING (kind LIKE 'pytest-%')
            WITH CHECK (kind LIKE 'pytest-%' AND job_key_fits(kind, idempotency_key));
    END IF;
END
$$;

-- Propuestas e hilos: lo que la API puede cambiar (fechas clave, 2026-11-12). La base no
-- sabe para qué cuenta trabaja la API en cada petición, así que no puede limitarla a "su"
-- cuenta: eso lo hacen sus sentencias (siempre con account_id = la de la sesión). Lo que sí
-- limita aquí, para el caso de SQL arbitrario con su usuario:
-- - Propuestas: solo cambia las de fechas clave (las que se contestan en el panel). Una
--   propuesta de respuesta a una reseña solo puede cancelarla por una baja (B12) o
--   porque su ficha se desactiva (multinegocio, 'location_inactive').
-- - Hilos: solo los bloquea (SELECT ... FOR UPDATE); no cambia ninguna fila. Los carriles
--   (más abajo) solo los libera.
-- La conversación hace lo de siempre; el worker lee lo que usan sus WHERE y borra.
ALTER TABLE proposal ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS proposal_api_read ON proposal;
CREATE POLICY proposal_api_read ON proposal FOR SELECT TO fichaitor_api USING (true);
DROP POLICY IF EXISTS proposal_api_update ON proposal;
CREATE POLICY proposal_api_update ON proposal FOR UPDATE TO fichaitor_api
    USING (true)
    WITH CHECK (type = 'key_date'
                OR (state = 'cancelada' AND outcome_reason IN ('offboarding', 'location_inactive')));
DROP POLICY IF EXISTS proposal_conversation ON proposal;
CREATE POLICY proposal_conversation ON proposal FOR ALL TO fichaitor_conversation
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS proposal_worker ON proposal;
CREATE POLICY proposal_worker ON proposal FOR ALL TO fichaitor_worker
    USING (true) WITH CHECK (true);

ALTER TABLE conversation ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conversation_api_read ON conversation;
CREATE POLICY conversation_api_read ON conversation FOR SELECT TO fichaitor_api USING (true);
-- Paso 9: el hilo ya no guarda la propuesta activa. La API solo lo bloquea: un
-- SELECT ... FOR UPDATE mira el USING de la política de UPDATE, no su WITH CHECK, así
-- que cualquier UPDATE de verdad se rechaza.
DROP POLICY IF EXISTS conversation_api_release ON conversation;
DROP POLICY IF EXISTS conversation_api_lock ON conversation;
CREATE POLICY conversation_api_lock ON conversation FOR UPDATE TO fichaitor_api
    USING (true) WITH CHECK (false);
DROP POLICY IF EXISTS conversation_conversation ON conversation;
CREATE POLICY conversation_conversation ON conversation FOR ALL TO fichaitor_conversation
    USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS conversation_worker ON conversation;
CREATE POLICY conversation_worker ON conversation FOR ALL TO fichaitor_worker
    USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Multinegocio (2026-11-12): propuestas por ficha, un carril por ficha y filtro por
-- chat. Consulta docs/multinegocio.md, secciones 3.6, 3.7, 3.8 y 4. Necesita las
-- columnas nuevas de location (schema.sql).

-- Propuestas por ficha. La clave ajena compuesta es la segunda barrera entre cuentas:
-- una propuesta no puede nombrar la ficha de otra cuenta. Si la retención borra la
-- ficha, la propuesta se queda sin ella (ON DELETE SET NULL solo de location_id).
-- Mientras el código antiguo siga creando propuestas sin ficha, location_id admite NULL;
-- el disparador que lo exige llega con el paso 6a.
ALTER TABLE proposal ADD COLUMN IF NOT EXISTS location_id text;
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_constraint
                   WHERE conname = 'proposal_location_fk' AND conrelid = 'proposal'::regclass) THEN
        ALTER TABLE proposal ADD CONSTRAINT proposal_location_fk
            FOREIGN KEY (account_id, location_id) REFERENCES location (account_id, id)
            ON DELETE SET NULL (location_id);
    END IF;
END
$$;
-- Una fecha clave es de la ficha que dice su carga.
ALTER TABLE proposal DROP CONSTRAINT IF EXISTS proposal_key_date_location;
ALTER TABLE proposal ADD CONSTRAINT proposal_key_date_location
    CHECK (type <> 'key_date' OR location_id IS NULL OR location_id = payload->>'location_id');
CREATE INDEX IF NOT EXISTS proposal_unfinished_location ON proposal (account_id, location_id)
    WHERE state IN ('programada', 'enviada', 'recordada', 'recopilando', 'por_confirmar',
                    'ejecutando');
-- Para la clave ajena del carril: la propuesta activa es de esa cuenta y esa ficha.
CREATE UNIQUE INDEX IF NOT EXISTS proposal_account_location_id
    ON proposal (account_id, location_id, id);

-- Carriles: como mucho una propuesta activa por ficha. El hilo (conversation) se queda:
-- es el bloqueo por cuenta y guarda unknown_streak. Su active_proposal_id se retira al
-- final de este archivo (paso 9).
-- La clave ajena a proposal incluye la cuenta y la ficha: un carril no puede apuntar a
-- la propuesta de otra ficha ni de otra cuenta. Al borrar la ficha se borra su carril.
CREATE TABLE IF NOT EXISTS conversation_lane (
    account_id         uuid NOT NULL,
    location_id        text NOT NULL,
    active_proposal_id text,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, location_id),
    CONSTRAINT conversation_lane_location_fk FOREIGN KEY (account_id, location_id)
        REFERENCES location (account_id, id) ON DELETE CASCADE,
    CONSTRAINT conversation_lane_proposal_fk
        FOREIGN KEY (account_id, location_id, active_proposal_id)
        REFERENCES proposal (account_id, location_id, id)
);

-- Filtro por chat y foco. location_filter NULL = el chat ve todas las fichas activas de
-- la cuenta, también las que se activen después. Se fija al pedir el código de
-- vinculación (link_code) y se copia a la dirección al vincularla.
-- focus_*: la última propuesta en la que esa dirección pulsó un botón (para resolver el
-- texto libre con varias propuestas abiertas).
ALTER TABLE channel_identity
    ADD COLUMN IF NOT EXISTS location_filter   text[],
    ADD COLUMN IF NOT EXISTS focus_proposal_id text,
    ADD COLUMN IF NOT EXISTS focus_at          timestamptz;
ALTER TABLE link_code ADD COLUMN IF NOT EXISTS location_filter text[];
ALTER TABLE channel_identity DROP CONSTRAINT IF EXISTS channel_identity_location_filter_check;
ALTER TABLE channel_identity ADD CONSTRAINT channel_identity_location_filter_check
    CHECK (location_filter IS NULL
           OR (array_ndims(location_filter) = 1 AND cardinality(location_filter) BETWEEN 1 AND 50));
ALTER TABLE channel_identity DROP CONSTRAINT IF EXISTS channel_identity_focus_check;
ALTER TABLE channel_identity ADD CONSTRAINT channel_identity_focus_check
    CHECK (focus_proposal_id ~ '^prop_[0-9a-f]{16}$');
ALTER TABLE link_code DROP CONSTRAINT IF EXISTS link_code_location_filter_check;
ALTER TABLE link_code ADD CONSTRAINT link_code_location_filter_check
    CHECK (location_filter IS NULL
           OR (array_ndims(location_filter) = 1 AND cardinality(location_filter) BETWEEN 1 AND 50));

-- Cada ficha del filtro es de la cuenta de la fila: con SQL de la API o de la
-- conversación no se puede apuntar un chat a la ficha de otra cuenta. Solo se comprueba
-- cuando el filtro o la cuenta cambian: una ficha borrada después por la retención no
-- impide anotar la última actividad del chat.
CREATE OR REPLACE FUNCTION location_filter_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.location_filter IS NULL THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.location_filter IS NOT DISTINCT FROM OLD.location_filter
       AND NEW.account_id = OLD.account_id THEN
        RETURN NEW;
    END IF;
    IF array_position(NEW.location_filter, NULL) IS NOT NULL
       OR EXISTS (SELECT 1 FROM unnest(NEW.location_filter) AS f (id)
                  WHERE NOT EXISTS (SELECT 1 FROM public.location l
                                    WHERE l.id = f.id AND l.account_id = NEW.account_id)) THEN
        RAISE EXCEPTION 'filtro_ajeno' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS location_filter_guard ON channel_identity;
CREATE TRIGGER location_filter_guard BEFORE INSERT OR UPDATE ON channel_identity
    FOR EACH ROW EXECUTE FUNCTION location_filter_guard();
DROP TRIGGER IF EXISTS location_filter_guard ON link_code;
CREATE TRIGGER location_filter_guard BEFORE INSERT OR UPDATE ON link_code
    FOR EACH ROW EXECUTE FUNCTION location_filter_guard();

-- Privilegios (sección 4). Se retira todo del carril y se concede lo justo.
REVOKE ALL ON conversation_lane
    FROM PUBLIC, fichaitor_api, fichaitor_worker, fichaitor_conversation;
-- API: fija el filtro de un chat y el del código que pide; libera el carril de una ficha
-- que desactiva (como libera hoy el hilo). location_id de una propuesta no lo cambia
-- nadie: no está en ningún UPDATE.
GRANT UPDATE (location_filter) ON channel_identity TO fichaitor_api;
GRANT INSERT (location_filter) ON link_code TO fichaitor_api;
GRANT SELECT ON conversation_lane TO fichaitor_api;
GRANT UPDATE (active_proposal_id, updated_at) ON conversation_lane TO fichaitor_api;
-- Conversación: crea la propuesta con su ficha (ya tiene INSERT en proposal), lleva los
-- carriles, copia el filtro del código al vincular y anota el foco. No cambia el filtro.
GRANT INSERT (location_filter) ON channel_identity TO fichaitor_conversation;
GRANT UPDATE (focus_proposal_id, focus_at) ON channel_identity TO fichaitor_conversation;
GRANT SELECT, INSERT ON conversation_lane TO fichaitor_conversation;
GRANT UPDATE (active_proposal_id, updated_at) ON conversation_lane TO fichaitor_conversation;
-- Worker: lee y borra (baja y retención).
GRANT SELECT, DELETE ON conversation_lane TO fichaitor_worker;

-- Segunda barrera, como en conversation: la API solo libera un carril.
ALTER TABLE conversation_lane ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS conversation_lane_api_read ON conversation_lane;
CREATE POLICY conversation_lane_api_read ON conversation_lane FOR SELECT TO fichaitor_api
    USING (true);
DROP POLICY IF EXISTS conversation_lane_api_release ON conversation_lane;
CREATE POLICY conversation_lane_api_release ON conversation_lane FOR UPDATE TO fichaitor_api
    USING (true) WITH CHECK (active_proposal_id IS NULL);
DROP POLICY IF EXISTS conversation_lane_conversation ON conversation_lane;
CREATE POLICY conversation_lane_conversation ON conversation_lane FOR ALL
    TO fichaitor_conversation USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS conversation_lane_worker ON conversation_lane;
CREATE POLICY conversation_lane_worker ON conversation_lane FOR ALL TO fichaitor_worker
    USING (true) WITH CHECK (true);

-- Relleno 3.8.3: la ficha de cada propuesta, solo donde falta. Se puede repetir.
-- - Fechas clave: la de su carga, si es una ficha de la cuenta.
UPDATE proposal p SET location_id = p.payload->>'location_id'
WHERE p.location_id IS NULL AND p.type = 'key_date'
  AND EXISTS (SELECT 1 FROM location l
              WHERE l.account_id = p.account_id AND l.id = p.payload->>'location_id');
-- - Respuestas a reseñas: la ficha de la cuenta que tiene esa reseña (mismo usuario de
--   Google y misma ficha de Google).
UPDATE proposal p SET location_id = l.id
FROM account a, review r, location l
WHERE p.location_id IS NULL AND p.type = 'review_reply'
  AND a.id = p.account_id AND r.id = p.source_ref AND r.google_sub = a.google_sub
  AND l.account_id = a.id AND l.gbp_location = r.location;
-- - Las demás que siguen sin terminar: la ficha elegida de la cuenta. Las terminadas
--   que no se pueden resolver se quedan sin ficha. Es migración desde el modelo de una
--   ficha elegida (docs/03-lld.md, «selected_location_id»): solo toca propuestas sin
--   ficha, y desde el paso 6a ninguna propuesta nueva se crea sin ella.
UPDATE proposal p SET location_id = a.selected_location_id
FROM account a
WHERE p.location_id IS NULL AND p.type <> 'key_date' AND a.id = p.account_id
  AND a.selected_location_id IS NOT NULL
  AND p.state IN ('programada', 'enviada', 'recordada', 'recopilando', 'por_confirmar',
                  'ejecutando');

-- Paso 6a (docs/multinegocio.md, 3.6): toda propuesta nueva es de una ficha activa de
-- su cuenta. Fuera del propietario (que aplica los esquemas y prepara las pruebas):
-- - Sin location_id: check_violation 'propuesta_sin_ficha'. Desde el paso 9 no se
--   rellena nada: ni con la carga de una fecha clave ni con la ficha elegida.
-- - Si la ficha no es de la cuenta o no está activa: check_violation 'ficha_inactiva'.
--   La clave ajena compuesta ya impide la ficha de otra cuenta; aquí se mira además que
--   esté activa, con el mismo mensaje en los dos casos.
-- Con los privilegios de quien escribe (no SECURITY DEFINER), para que fichaitor_is_owner
-- vea al propietario de verdad. La conversación lee location.active_since.
CREATE OR REPLACE FUNCTION proposal_location_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF fichaitor_is_owner(TG_RELID) THEN
        RETURN NEW;
    END IF;
    IF NEW.location_id IS NULL THEN
        RAISE EXCEPTION 'propuesta_sin_ficha' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.location l
                   WHERE l.account_id = NEW.account_id AND l.id = NEW.location_id
                     AND l.active_since IS NOT NULL) THEN
        RAISE EXCEPTION 'ficha_inactiva' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS proposal_location_guard ON proposal;
CREATE TRIGGER proposal_location_guard BEFORE INSERT ON proposal
    FOR EACH ROW EXECUTE FUNCTION proposal_location_guard();

-- Relleno 3.8.4 de los carriles y retirada de conversation.active_proposal_id (paso 9).
-- SÍ se ejecuta en cada aplicación de este esquema: la última línea del archivo lo llama
-- (I18 de docs/seguridad/revision-2026-11-12-c.md). Por eso deploy/deploy.sh para la
-- conversación antes de aplicar los esquemas y la arranca después con el código nuevo.
-- Consulta la precaución de la sección 3.8 de docs/multinegocio.md.
-- Mientras el hilo tenga la columna (una base de antes del paso 9):
-- 1. copia la propuesta activa de cada hilo al carril de su ficha, solo si sigue abierta
--    y tiene ficha, sin pisar un carril que ya tenga una. Una propuesta abierta cuyo
--    carril ya tenía otra queda fuera de los dos sin perderse: el carril la vuelve a
--    entregar;
-- 2. borra la columna. No se pierde nada: lo que apuntaba a una propuesta abierta ya
--    está en su carril, y lo demás nadie lo lee desde el paso 6b. Su clave ajena y sus
--    privilegios de columna se van con ella; las políticas que la nombraban ya se han
--    sustituido más arriba.
-- Devuelve cuántos carriles ha escrito. Se puede repetir: sin la columna, no hace nada y
-- devuelve 0. SQL dinámico: el cuerpo no nombra una columna que puede no existir.
-- Solo el propietario la ejecuta (es quien puede cambiar la tabla).
CREATE OR REPLACE FUNCTION fichaitor_fill_conversation_lanes() RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    written integer := 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_attribute
                   WHERE attrelid = 'public.conversation'::regclass
                     AND attname = 'active_proposal_id' AND NOT attisdropped) THEN
        RETURN 0;
    END IF;
    EXECUTE $sql$
        WITH moved AS (
            INSERT INTO public.conversation_lane AS cl
                (account_id, location_id, active_proposal_id)
            SELECT p.account_id, p.location_id, p.id
            FROM public.conversation c
            JOIN public.proposal p ON p.id = c.active_proposal_id AND p.account_id = c.account_id
            WHERE p.location_id IS NOT NULL
              AND p.state IN ('enviada', 'recordada', 'recopilando', 'por_confirmar')
            ON CONFLICT (account_id, location_id) DO UPDATE
                SET active_proposal_id = EXCLUDED.active_proposal_id, updated_at = now()
                WHERE cl.active_proposal_id IS NULL
            RETURNING 1
        )
        SELECT count(*) FROM moved
    $sql$ INTO written;
    ALTER TABLE public.conversation DROP COLUMN active_proposal_id;
    RETURN written;
END
$$;
REVOKE ALL ON FUNCTION fichaitor_fill_conversation_lanes() FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Solo desarrollo local y pruebas: está en db/dev-init.sql.
-- En producción el usuario fichaitor_conversation_prod lo crea deploy/init-roles.sh.

-- El relleno de los carriles y la retirada del hilo, en cada aplicación (ver el
-- comentario de la función).
SELECT fichaitor_fill_conversation_lanes();
