-- Fichaitor: esquema de los canales de mensajería (direcciones vinculadas, códigos de
-- vinculación, conversaciones, mensajes y propuestas). Consulta docs/canales.md.
-- Idempotente. Se aplica DESPUÉS de schema.sql y schema_worker.sql, con el rol propietario:
--   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_channels.sql
-- Necesita la extensión pgcrypto y las tablas account y location de schema.sql.
-- Pendiente de fusionar en schema.sql por el responsable.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_api') THEN
        CREATE ROLE fichaitor_api NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_worker') THEN
        CREATE ROLE fichaitor_worker NOLOGIN;
    END IF;
END
$$;

-- Direcciones vinculadas con código a una cuenta: un chat de Telegram, un teléfono de
-- WhatsApp (E.164) o un correo. Solo una dirección vinculada se atiende y puede actuar.
-- Una cuenta puede tener varias (dueño y encargado), cada una con su propio código.
-- Datos personales: la dirección. Desvincular no borra la fila: pone unlinked_at, y
-- desde ese instante no se le envía nada ni se le atiende.
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

-- Códigos de vinculación: 6 dígitos de un solo uso que caducan a los 10 minutos.
-- El código no se guarda: code_hash es su SHA-256 (con el secreto del servidor
-- FICHAITOR_LINK_CODE_PEPPER si está definido). used_at lo marca quien lo consume o
-- quien lo retira al pedir otro.
CREATE TABLE IF NOT EXISTS link_code (
    code_hash  text PRIMARY KEY CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    account_id uuid NOT NULL REFERENCES account (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz,
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '15 minutes')
);

CREATE INDEX IF NOT EXISTS link_code_account ON link_code (account_id) WHERE used_at IS NULL;

-- Lo que se recuerda de una dirección que NO está vinculada: códigos fallidos seguidos,
-- bloqueo de una hora al tercero y cuándo se le dio la respuesta genérica (una cada 24
-- horas). Sin datos personales: address_hash es el SHA-256 de canal y dirección.
CREATE TABLE IF NOT EXISTS channel_sender (
    address_hash           text PRIMARY KEY CHECK (address_hash ~ '^[0-9a-f]{64}$'),
    failed_attempts        smallint NOT NULL DEFAULT 0 CHECK (failed_attempts BETWEEN 0 AND 1000),
    blocked_until          timestamptz,
    last_unlinked_reply_at timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now()
);

-- Propuestas: todo aquello en lo que el agente toma la iniciativa. Estados y
-- transiciones en app/domain/proposals.py (docs/01-flujos.md, sección 4).
-- payload: con qué se creó (estrellas, texto de la reseña y respuesta propuesta, o la
-- fecha clave). No cambia nunca: el worker no tiene UPDATE sobre él.
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

-- El hilo entre el agente y una cuenta, participen las direcciones que participen:
-- una sola propuesta abierta a la vez. La fila se bloquea (SELECT ... FOR UPDATE)
-- mientras se atiende un mensaje, para que dos pulsaciones no se traten a la vez.
CREATE TABLE IF NOT EXISTS conversation (
    id                 text PRIMARY KEY DEFAULT 'conv_' || encode(gen_random_bytes(8), 'hex')
                       CHECK (id ~ '^conv_[0-9a-f]{8,32}$'),
    account_id         uuid NOT NULL UNIQUE REFERENCES account (id),
    active_proposal_id text REFERENCES proposal (id),
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
-- prueba de qué se mostró al dueño y qué contestó.
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

-- Privilegios mínimos. Se retira todo y se concede lo justo en cada aplicación.
-- Nadie borra nada en estas tablas.
REVOKE ALL ON channel_identity, link_code, channel_sender, proposal, conversation,
              channel_inbound_event, channel_message
    FROM PUBLIC, fichaitor_api, fichaitor_worker;

-- Direcciones vinculadas. El worker (que lleva la conversación) vincula y anota la
-- actividad. La API las lista en Ajustes y las desvincula; no puede crearlas ni
-- cambiarles la cuenta o la dirección: vincular exige enviar el código desde el chat.
GRANT SELECT ON channel_identity TO fichaitor_api, fichaitor_worker;
GRANT UPDATE (unlinked_at) ON channel_identity TO fichaitor_api;
GRANT INSERT (account_id, channel, address, verified_at, last_inbound_at)
    ON channel_identity TO fichaitor_worker;
GRANT UPDATE (last_inbound_at, unlinked_at) ON channel_identity TO fichaitor_worker;

-- Códigos. La API (la sesión web) los crea y retira los anteriores de la cuenta; no
-- lee las huellas. El worker los busca por huella y los consume; no puede crearlos.
GRANT INSERT (code_hash, account_id, created_at, expires_at) ON link_code TO fichaitor_api;
GRANT SELECT (account_id, used_at) ON link_code TO fichaitor_api;
GRANT UPDATE (used_at) ON link_code TO fichaitor_api, fichaitor_worker;
GRANT SELECT ON link_code TO fichaitor_worker;

-- Remitentes sin vincular, hilos y eventos vistos: solo el worker.
GRANT SELECT, INSERT, UPDATE ON channel_sender, channel_inbound_event TO fichaitor_worker;
GRANT SELECT, INSERT ON conversation TO fichaitor_worker;
GRANT UPDATE (active_proposal_id, unknown_streak, updated_at) ON conversation TO fichaitor_worker;

-- Propuestas. El worker las crea y las mueve, sin poder cambiar de quién son ni con
-- qué se crearon. La API solo las lee (bandeja del panel).
GRANT SELECT ON proposal TO fichaitor_api, fichaitor_worker;
GRANT INSERT ON proposal TO fichaitor_worker;
GRANT UPDATE (state, priority, slots, step, step_token, message_refs, scheduled_for, sent_at,
              remind_at, reminder_sent_at, expires_at, parked_at, confirmed_at,
              confirmed_event_id, confirmed_summary, action_id, outcome_reason)
    ON proposal TO fichaitor_worker;

-- Mensajes: el worker añade y nada más. Sin SELECT, UPDATE ni DELETE.
GRANT INSERT (conversation_id, identity_id, direction, channel, channel_message_id, kind, key,
              body, proposal_id)
    ON channel_message TO fichaitor_worker;

-- Para el saludo de bienvenida el worker necesita el nombre de la ficha elegida.
-- De account y location lee solo eso; el correo, no.
GRANT SELECT (id, selected_location_id) ON account TO fichaitor_worker;
GRANT SELECT (id, account_id, title) ON location TO fichaitor_worker;
