-- Fichaitor: esquema del worker (cola de jobs, resumen de fichas, reseñas, acciones
-- sobre Google, registro de auditoría y sentimiento del establecimiento).
-- Idempotente. Se aplica DESPUÉS de schema.sql, con el rol propietario:
--   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_worker.sql
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

-- Cola de jobs. La API encola; el worker toma, ejecuta y cierra.
-- attempts cuenta los intentos empezados. locked_by y locked_at solo tienen valor en 'running'.
-- idempotency_key: encolar dos veces con la misma clave no crea un segundo job.
CREATE TABLE IF NOT EXISTS job (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            text NOT NULL CHECK (length(kind) BETWEEN 1 AND 60),
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_at          timestamptz NOT NULL DEFAULT now(),
    state           text NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'running', 'done', 'failed')),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts    integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 50),
    locked_by       text CHECK (length(locked_by) <= 120),
    locked_at       timestamptz,
    last_error      text CHECK (length(last_error) <= 2000),
    idempotency_key text UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    created_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);

-- Solo los jobs vivos: la tabla crece con los terminados, el índice no.
CREATE INDEX IF NOT EXISTS job_live_run_at ON job (run_at)
    WHERE state IN ('pending', 'running');

-- Resumen de cada ficha: lo que el panel enseña sin llamar a Google.
-- Una fila por ficha y conexión; cada sincronización la actualiza.
-- Sin datos personales de terceros: ni textos ni autores de reseñas, solo cifras.
-- Las cifras son NULL si nunca hubo una sincronización correcta.
-- facts: hechos de la ficha con los que la API calcula el diagnóstico (descripción
-- medida en caracteres, categorías, teléfono y web como sí o no, fotos, atributos,
-- días con horario especial). El formato lo define app/domain/profile_summary.py
-- y lleva su versión en "v". Es NULL hasta la primera sincronización que los lee.
CREATE TABLE IF NOT EXISTS profile_summary (
    google_sub             text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    location               text NOT NULL CHECK (length(location) BETWEEN 1 AND 200),
    status                 text NOT NULL CHECK (status IN ('ok', 'needs_reconnect', 'error')),
    status_at              timestamptz NOT NULL DEFAULT now(),  -- último intento
    synced_at              timestamptz,                         -- última lectura correcta
    review_count           integer CHECK (review_count >= 0),
    average_rating         numeric(3, 2) CHECK (average_rating BETWEEN 0 AND 5),
    unanswered_count       integer CHECK (unanswered_count >= 0),
    unanswered_low_count   integer CHECK (unanswered_low_count >= 0),  -- de 1 y 2 estrellas
    last_review_at         timestamptz,
    has_regular_hours      boolean,
    upcoming_special_hours integer CHECK (upcoming_special_hours >= 0),
    google_changed         boolean,
    google_changed_fields  text[] NOT NULL DEFAULT '{}',
    facts                  jsonb CHECK (jsonb_typeof(facts) = 'object'),
    PRIMARY KEY (google_sub, location)
);

-- Bases creadas antes del 2026-11-11: añade la columna sin tocar las filas que haya.
-- Su resumen sigue valiendo; los hechos llegan con la siguiente sincronización.
ALTER TABLE profile_summary
    ADD COLUMN IF NOT EXISTS facts jsonb CHECK (jsonb_typeof(facts) = 'object');

-- Privilegios mínimos. Se retira todo y se concede lo justo en cada aplicación.
REVOKE ALL ON job, profile_summary FROM PUBLIC, fichaitor_api, fichaitor_worker;
REVOKE ALL ON google_connection FROM fichaitor_worker;

-- API: encola (sin poder fijar estado, intentos ni bloqueo, y sin leer la cola)
-- y lee los resúmenes. No puede escribirlos.
GRANT INSERT (kind, payload, run_at, idempotency_key, max_attempts) ON job TO fichaitor_api;
GRANT SELECT ON profile_summary TO fichaitor_api;

-- Worker: gestiona la cola y los resúmenes. Solo borra en la baja y en la tarea de
-- retención (privilegios al final de este archivo).
-- De google_connection lee solo lo que necesita para actuar; el email, no.
GRANT SELECT, INSERT, UPDATE ON job, profile_summary TO fichaitor_worker;
GRANT SELECT (id, google_sub, connected_at, refresh_token_enc, key_version, locations)
    ON google_connection TO fichaitor_worker;

-- ---------------------------------------------------------------------------
-- Reseñas y respuestas (2026-11-12): la primera escritura en Google.
-- Necesita las tablas account y location de schema.sql.

-- Bases anteriores (2026-11-12, B5 de docs/seguridad/revision-2026-11-12.md): cuántas
-- sincronizaciones seguidas ha rechazado Google con 403 o 404. A las 2, el usuario ha
-- perdido el acceso: la ficha se retira (location.removed_at) y la API deja de servirla.
-- Una sincronización correcta lo pone a 0.
ALTER TABLE profile_summary ADD COLUMN IF NOT EXISTS denied_count smallint NOT NULL DEFAULT 0
    CHECK (denied_count BETWEEN 0 AND 1000);

-- Bases anteriores: cuándo se guardaron por última vez las reseñas de la ficha.
-- NULL = el worker todavía no las ha guardado (la API responde "pendiente").
ALTER TABLE profile_summary ADD COLUMN IF NOT EXISTS reviews_synced_at timestamptz;

-- Reseñas de cada ficha, como las devuelve Google en la sincronización.
-- Una fila por reseña y conexión (google_sub, location), igual que profile_summary:
-- la misma reseña en dos cuentas son dos filas con identificadores distintos.
-- Minimización de datos: NO se guarda el nombre ni la foto de quien la escribió.
-- comment es solo el texto original, sin la traducción automática de Google.
-- reply_text NULL = sin respuesta del propietario.
-- sent_at: cuándo envió Fichaitor la respuesta. Durante 24 horas, una sincronización
-- en la que Google todavía no devuelve esa respuesta no la borra.
-- Las reseñas que Google deja de devolver no se borran: se marcan con removed_at.
CREATE TABLE IF NOT EXISTS review (
    id            text PRIMARY KEY DEFAULT 'rev_' || encode(gen_random_bytes(8), 'hex')
                  CHECK (id ~ '^rev_[0-9a-f]{8,32}$'),
    google_sub    text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    location      text NOT NULL CHECK (length(location) BETWEEN 1 AND 200),
    gbp_review_id text NOT NULL CHECK (gbp_review_id ~ '^[A-Za-z0-9_-]{1,255}$'),
    rating        smallint CHECK (rating BETWEEN 1 AND 5),
    comment       text NOT NULL DEFAULT '' CHECK (length(comment) <= 20000),
    created_at    timestamptz,
    reply_text    text CHECK (length(reply_text) <= 20000),
    reply_at      timestamptz,
    sent_at       timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    removed_at    timestamptz,
    UNIQUE (google_sub, location, gbp_review_id)
);

-- Último borrador que el modelo redactó para cada reseña. Evita pagar dos veces por
-- la misma reseña: source_hash cambia si cambian las estrellas, el texto o el prompt.
-- ON DELETE CASCADE: al borrar una reseña (retención), su borrador se va con ella sin
-- que el worker necesite acceso a esta tabla.
CREATE TABLE IF NOT EXISTS review_draft (
    review_id   text PRIMARY KEY REFERENCES review (id) ON DELETE CASCADE,
    source_hash text NOT NULL CHECK (length(source_hash) = 64),
    draft       text NOT NULL CHECK (length(draft) BETWEEN 1 AND 4096),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Acciones sobre Google confirmadas por el dueño. La API las crea en 'pending' desde
-- una sesión; el worker las ejecuta. text es EXACTAMENTE lo que el dueño confirmó:
-- nadie puede cambiarlo después (ni la API ni el worker tienen UPDATE sobre text).
-- job_id: el job que tomó la acción. Otro job distinto no puede ejecutarla.
-- error_code: lista cerrada (app/domain/actions.py). Nunca texto de Google.
CREATE TABLE IF NOT EXISTS action (
    id            text PRIMARY KEY DEFAULT 'act_' || encode(gen_random_bytes(8), 'hex')
                  CHECK (id ~ '^act_[0-9a-f]{8,32}$'),
    account_id    uuid NOT NULL REFERENCES account (id),
    type          text NOT NULL CHECK (type IN ('reply_review')),
    google_sub    text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    location      text NOT NULL CHECK (length(location) BETWEEN 1 AND 200),
    -- Sin clave ajena: la acción (el registro de lo que se publicó) dura más que la reseña.
    review_id     text NOT NULL CHECK (length(review_id) <= 64),
    text          text NOT NULL CHECK (octet_length(text) BETWEEN 1 AND 4096),
    state         text NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'running', 'done', 'failed')),
    confirmed_at  timestamptz NOT NULL DEFAULT now(),
    job_id        text CHECK (length(job_id) <= 64),
    attempts      integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    started_at    timestamptz,
    finished_at   timestamptz,
    google_status smallint,
    error_code    text CHECK (error_code ~ '^[a-z_]{1,40}$')
);

CREATE INDEX IF NOT EXISTS action_account ON action (account_id);

-- Como mucho una respuesta en vuelo por reseña: dos pulsaciones seguidas de
-- "Publicar" no crean dos acciones.
CREATE UNIQUE INDEX IF NOT EXISTS action_one_live_per_review ON action (review_id)
    WHERE state IN ('pending', 'running');

-- Registro de auditoría. Solo se añaden filas: nadie las cambia, y solo se borran las
-- de una cuenta que lleva más de 12 meses de baja (política al final de este archivo).
-- Dos filas por acción: 'reply_confirmed' (la escribe la API cuando el dueño confirma)
-- y 'reply_sent' o 'reply_failed' (la escribe el worker con el resultado de Google).
-- at y written_by los pone la base de datos: quien inserta no puede fijarlos.
-- reason: código de una lista cerrada, nunca texto de Google ni datos sensibles.
CREATE TABLE IF NOT EXISTS audit_log (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at            timestamptz NOT NULL DEFAULT now(),
    written_by    name NOT NULL DEFAULT current_user,
    event         text NOT NULL
                  CHECK (event IN ('reply_confirmed', 'reply_sent', 'reply_failed')),
    account_id    uuid NOT NULL,
    action_id     text NOT NULL CHECK (length(action_id) <= 64),
    action_type   text NOT NULL CHECK (length(action_type) <= 60),
    location      text NOT NULL CHECK (length(location) <= 200),
    review_id     text NOT NULL CHECK (length(review_id) <= 64),
    gbp_review_id text NOT NULL CHECK (length(gbp_review_id) <= 255),
    text          text NOT NULL CHECK (octet_length(text) <= 4096),
    confirmed_at  timestamptz NOT NULL,
    google_status smallint,
    reason        text CHECK (reason ~ '^[a-z_]{1,40}$')
);

CREATE INDEX IF NOT EXISTS audit_log_action ON audit_log (action_id);

-- Privilegios mínimos. Se retira todo y se concede lo justo en cada aplicación.
REVOKE ALL ON review, review_draft, action, audit_log
    FROM PUBLIC, fichaitor_api, fichaitor_worker;

-- Reseñas: el worker escribe; la API solo lee.
GRANT SELECT, INSERT, UPDATE ON review TO fichaitor_worker;
GRANT SELECT ON review TO fichaitor_api;

-- Borradores: solo la API (es quien llama al modelo). El worker no los ve.
GRANT SELECT, INSERT, UPDATE ON review_draft TO fichaitor_api;

-- Acciones: la API las crea y las lee, sin poder fijar estado ni resultado y sin
-- poder cambiarlas después. El worker las lee y anota estado y resultado; no puede
-- crearlas, ni cambiar el texto, la reseña o la cuenta.
GRANT SELECT ON action TO fichaitor_api, fichaitor_worker;
GRANT INSERT (account_id, type, google_sub, location, review_id, text, confirmed_at)
    ON action TO fichaitor_api;
GRANT UPDATE (state, job_id, attempts, started_at, finished_at, google_status, error_code)
    ON action TO fichaitor_worker;

-- Auditoría: los dos insertan y nada más. Sin SELECT, UPDATE ni DELETE.
GRANT INSERT (event, account_id, action_id, action_type, location, review_id,
              gbp_review_id, text, confirmed_at, google_status, reason)
    ON audit_log TO fichaitor_api, fichaitor_worker;

-- Segunda barrera, a nivel de fila: cada rol inserta solo sus propios eventos y, al
-- no haber políticas de lectura, cambio ni borrado, esas operaciones quedan negadas
-- aunque alguien concediera el privilegio por error. El propietario no está sujeto.
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS audit_log_api_insert ON audit_log;
CREATE POLICY audit_log_api_insert ON audit_log FOR INSERT TO fichaitor_api
    WITH CHECK (event IN ('reply_confirmed', 'special_hours_confirmed'));
DROP POLICY IF EXISTS audit_log_worker_insert ON audit_log;
CREATE POLICY audit_log_worker_insert ON audit_log FOR INSERT TO fichaitor_worker
    WITH CHECK (event IN ('reply_sent', 'reply_failed', 'special_hours_sent',
                          'special_hours_failed'));

-- ---------------------------------------------------------------------------
-- Fechas clave (2026-11-12): el horario especial de un día que el dueño confirma por el
-- canal o en el panel. Consulta docs/calendario.md y docs/worker.md.
-- Una acción set_special_hours no tiene reseña: special_date es el día que cambia y text
-- es el cambio en su forma canónica (app/domain/special_hours.py), que nadie puede
-- cambiar después, igual que el texto de una respuesta.
ALTER TABLE action ADD COLUMN IF NOT EXISTS special_date date;
ALTER TABLE action ALTER COLUMN review_id DROP NOT NULL;
ALTER TABLE action DROP CONSTRAINT IF EXISTS action_type_check;
ALTER TABLE action ADD CONSTRAINT action_type_check
    CHECK (type IN ('reply_review', 'set_special_hours'));
ALTER TABLE action DROP CONSTRAINT IF EXISTS action_target_check;
ALTER TABLE action ADD CONSTRAINT action_target_check CHECK (
    (type = 'reply_review' AND review_id IS NOT NULL AND special_date IS NULL)
    OR (type = 'set_special_hours' AND review_id IS NULL AND special_date IS NOT NULL));
-- Como mucho un cambio de horario en vuelo por ficha y día.
CREATE UNIQUE INDEX IF NOT EXISTS action_one_live_per_special_date
    ON action (google_sub, location, special_date)
    WHERE type = 'set_special_hours' AND state IN ('pending', 'running');
GRANT INSERT (special_date) ON action TO fichaitor_api;

-- Auditoría: tres eventos más, con las mismas reglas que los de las respuestas (las
-- políticas de fila de más arriba ya los admiten). review_id y gbp_review_id van vacíos.
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_event_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_event_check
    CHECK (event IN ('reply_confirmed', 'reply_sent', 'reply_failed',
                     'special_hours_confirmed', 'special_hours_sent', 'special_hours_failed'));

-- ---------------------------------------------------------------------------
-- Sentimiento del establecimiento (2026-11-12). Consulta docs/sentimiento.md.
-- Necesita la tabla review de más arriba.

-- Clasificación de cada reseña: se hace una sola vez y se guarda.
-- source_hash: de las estrellas y el texto con los que se clasificó. Si cambian, la
-- reseña se clasifica otra vez.
-- sentiment NULL = se envió al modelo y su respuesta no valió; se reintenta en el
-- siguiente análisis (method 'pending') hasta attempts = 3.
-- topics: hasta 3 objetos {"topic", "polarity"} de la lista cerrada del dominio.
-- No se guarda ningún texto de la reseña ni nada sobre quien la escribió.
-- ON DELETE CASCADE: si alguna vez se borra una reseña, su clasificación no la retiene.
CREATE TABLE IF NOT EXISTS review_sentiment (
    review_id     text PRIMARY KEY REFERENCES review (id) ON DELETE CASCADE,
    source_hash   text NOT NULL CHECK (length(source_hash) = 64),
    sentiment     text CHECK (sentiment IN ('positive', 'neutral', 'negative')),
    topics        jsonb NOT NULL DEFAULT '[]'::jsonb
                  CHECK (jsonb_typeof(topics) = 'array' AND jsonb_array_length(topics) <= 3),
    method        text NOT NULL
                  CHECK (method IN ('model', 'stars', 'stars_fallback', 'pending')),
    attempts      smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
    classified_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((sentiment IS NULL) = (method = 'pending'))
);

-- Último análisis de cada ficha y conexión: lo que enseña el panel.
-- status 'insuficiente' = la ficha no tiene reseñas; 'error' = el análisis falló y
-- nunca hubo uno bueno (uno bueno no se sustituye por un error).
-- score, overall, praised y complaints los calcula el código. summary y suggestion los
-- redacta el modelo (summary_source 'model') o salen de reglas fijas ('rules').
-- praised y complaints: listas de {"topic", "mentions"}; solo temas y recuentos.
CREATE TABLE IF NOT EXISTS sentiment_analysis (
    google_sub      text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    location        text NOT NULL CHECK (length(location) BETWEEN 1 AND 200),
    status          text NOT NULL CHECK (status IN ('ok', 'insuficiente', 'error')),
    generated_at    timestamptz NOT NULL,
    based_on        integer NOT NULL DEFAULT 0 CHECK (based_on >= 0),
    with_text       integer NOT NULL DEFAULT 0 CHECK (with_text BETWEEN 0 AND based_on),
    provisional     boolean NOT NULL DEFAULT true,
    period          text CHECK (length(period) <= 60),
    score           smallint CHECK (score BETWEEN 0 AND 100),
    overall         text CHECK (overall IN ('positivo', 'mixto', 'negativo')),
    summary         text CHECK (length(summary) <= 600),
    suggestion      text CHECK (length(suggestion) <= 400),
    summary_source  text NOT NULL DEFAULT 'rules' CHECK (summary_source IN ('model', 'rules')),
    praised         jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(praised) = 'array'),
    complaints      jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(complaints) = 'array'),
    trigger         text NOT NULL DEFAULT 'first' CHECK (trigger ~ '^[a-z_]{1,40}$'),
    formula_version smallint NOT NULL DEFAULT 1,
    PRIMARY KEY (google_sub, location),
    CHECK ((status = 'ok') = (score IS NOT NULL)),
    CHECK ((score IS NULL) = (overall IS NULL))
);

-- Historia: una foto de la nota por ficha y mes. El mes en curso se actualiza con cada
-- análisis; los meses pasados no se tocan. month es siempre el día 1 (UTC).
CREATE TABLE IF NOT EXISTS sentiment_history (
    google_sub text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    location   text NOT NULL CHECK (length(location) BETWEEN 1 AND 200),
    month      date NOT NULL CHECK (extract(day FROM month) = 1),
    score      smallint NOT NULL CHECK (score BETWEEN 0 AND 100),
    based_on   integer NOT NULL CHECK (based_on >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (google_sub, location, month)
);

-- Uso del modelo: una fila por análisis. Solo contadores, nunca textos.
-- input_tokens y output_tokens son NULL si la respuesta del modelo no los traía.
CREATE TABLE IF NOT EXISTS sentiment_usage (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    google_sub     text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    location       text NOT NULL CHECK (length(location) BETWEEN 1 AND 200),
    trigger        text NOT NULL CHECK (trigger ~ '^[a-z_]{1,40}$'),
    classify_calls integer NOT NULL DEFAULT 0 CHECK (classify_calls >= 0),
    summary_calls  integer NOT NULL DEFAULT 0 CHECK (summary_calls >= 0),
    failed_calls   integer NOT NULL DEFAULT 0 CHECK (failed_calls >= 0),
    reviews_sent   integer NOT NULL DEFAULT 0 CHECK (reviews_sent >= 0),
    input_tokens   integer CHECK (input_tokens >= 0),
    output_tokens  integer CHECK (output_tokens >= 0)
);

CREATE INDEX IF NOT EXISTS sentiment_usage_at ON sentiment_usage (at);

-- Privilegios mínimos. Se retira todo y se concede lo justo en cada aplicación.
REVOKE ALL ON review_sentiment, sentiment_analysis, sentiment_history, sentiment_usage
    FROM PUBLIC, fichaitor_api, fichaitor_worker;

-- Worker: escribe clasificaciones, análisis e historia. Del uso solo añade.
-- Solo borra en la baja y en la tarea de retención (al final de este archivo).
GRANT SELECT, INSERT, UPDATE ON review_sentiment, sentiment_analysis, sentiment_history
    TO fichaitor_worker;
GRANT INSERT (google_sub, location, trigger, classify_calls, summary_calls, failed_calls,
              reviews_sent, input_tokens, output_tokens)
    ON sentiment_usage TO fichaitor_worker;

-- API: solo lee el análisis y la historia. La clasificación por reseña y el uso, no.
GRANT SELECT ON sentiment_analysis, sentiment_history TO fichaitor_api;

-- ---------------------------------------------------------------------------
-- Baja y retención (2026-11-12). Consulta docs/retencion.md.
-- Necesita las tablas account, location, session, waitlist_entry y google_connection
-- de schema.sql y todas las de más arriba.

-- Bases anteriores: las claves ajenas que impedían borrar una reseña.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_constraint
               WHERE conname = 'review_draft_review_id_fkey' AND confdeltype <> 'c') THEN
        ALTER TABLE review_draft DROP CONSTRAINT review_draft_review_id_fkey;
        ALTER TABLE review_draft ADD CONSTRAINT review_draft_review_id_fkey
            FOREIGN KEY (review_id) REFERENCES review (id) ON DELETE CASCADE;
    END IF;
END
$$;
ALTER TABLE action DROP CONSTRAINT IF EXISTS action_review_id_fkey;

-- Códigos de un solo uso para confirmar una baja. El código no se guarda: code_hash es
-- un HMAC-SHA256 con un secreto del servidor sobre la cuenta, la sesión, el alcance y
-- el código, así que un código pedido para 'ficha' no vale para 'cuenta' ni desde otra
-- sesión. Pedir otro pone invalidated_at en los anteriores.
-- mail_state: lo anota el worker al enviar el correo ('sent') o al rendirse ('failed').
CREATE TABLE IF NOT EXISTS offboarding_code (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     uuid NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    session_id     uuid NOT NULL REFERENCES session (id) ON DELETE CASCADE,
    scope          text NOT NULL CHECK (scope IN ('ficha', 'cuenta')),
    code_hash      text NOT NULL CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    created_at     timestamptz NOT NULL,
    expires_at     timestamptz NOT NULL,
    attempts       smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 1000),
    used_at        timestamptz,
    invalidated_at timestamptz,
    mail_state     text NOT NULL DEFAULT 'pending'
                   CHECK (mail_state IN ('pending', 'sent', 'failed')),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '1 hour')
);

CREATE INDEX IF NOT EXISTS offboarding_code_account ON offboarding_code (account_id, created_at);

-- Bajas. Una fila por baja confirmada con código.
-- Programada: restored_at y effective_at a NULL. Hasta delete_at se puede restaurar
-- (restored_at); pasada esa fecha el worker la hace efectiva (effective_at): revoca y
-- borra los tokens, y la cuenta queda sin fichas ('ficha') o dada de baja ('cuenta').
-- google_sub: de quién son los datos obtenidos de Google que hay que borrar. Se copia
-- de la cuenta al confirmar y se vacía al borrarlos (purged_at).
-- restore_token_hash: SHA-256 del token del enlace del correo; el token no se guarda.
-- Se vacía al restaurar y al hacerse efectiva la baja.
-- revoke_state: 'revoked' (Google revocó o el token ya no valía), 'failed' (no se pudo
-- revocar; el token se borró igualmente), 'superseded' (el dueño ya había vuelto a
-- conectar: no se revoca para no cortar la conexión nueva) o 'none' (no había tokens).
CREATE TABLE IF NOT EXISTS offboarding (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id         uuid NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    scope              text NOT NULL CHECK (scope IN ('ficha', 'cuenta')),
    google_sub         text CHECK (length(google_sub) BETWEEN 1 AND 255),
    confirmed_at       timestamptz NOT NULL,
    delete_at          timestamptz NOT NULL,
    restore_token_hash text CHECK (restore_token_hash ~ '^[0-9a-f]{64}$'),
    restored_at        timestamptz,
    effective_at       timestamptz,
    revoke_state       text CHECK (revoke_state IN ('revoked', 'failed', 'superseded', 'none')),
    purged_at          timestamptz,
    CHECK (delete_at >= confirmed_at),
    CHECK (restored_at IS NULL OR effective_at IS NULL),
    CHECK (purged_at IS NULL OR effective_at IS NOT NULL)
);

-- Como mucho una baja programada por cuenta.
CREATE UNIQUE INDEX IF NOT EXISTS offboarding_one_pending_per_account ON offboarding (account_id)
    WHERE restored_at IS NULL AND effective_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS offboarding_restore_token ON offboarding (restore_token_hash)
    WHERE restore_token_hash IS NOT NULL;
-- Las que todavía cierran el acceso a los datos de Google de ese usuario.
CREATE INDEX IF NOT EXISTS offboarding_open_sub ON offboarding (google_sub)
    WHERE restored_at IS NULL AND purged_at IS NULL;

-- Pasadas de la tarea de retención: cuántas filas borró de cada tabla. Solo recuentos.
CREATE TABLE IF NOT EXISTS retention_run (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    deleted jsonb NOT NULL CHECK (jsonb_typeof(deleted) = 'object')
);

REVOKE ALL ON offboarding_code, offboarding, retention_run
    FROM PUBLIC, fichaitor_api, fichaitor_worker;

-- Códigos: la API los crea, cuenta los intentos y los gasta o invalida. No puede
-- cambiarles la huella, la cuenta, la sesión ni la caducidad, ni borrarlos.
-- El worker solo anota si el correo salió, y borra los antiguos.
GRANT SELECT ON offboarding_code TO fichaitor_api;
GRANT INSERT (account_id, session_id, scope, code_hash, created_at, expires_at)
    ON offboarding_code TO fichaitor_api;
GRANT UPDATE (attempts, used_at, invalidated_at) ON offboarding_code TO fichaitor_api;
GRANT SELECT (id, expires_at, mail_state) ON offboarding_code TO fichaitor_worker;
GRANT UPDATE (mail_state) ON offboarding_code TO fichaitor_worker;
GRANT DELETE ON offboarding_code TO fichaitor_worker;

-- Bajas: la API las programa y las restaura; no puede hacerlas efectivas ni dar nada
-- por borrado. El worker las hace efectivas y anota el resultado; no puede crearlas,
-- restaurarlas ni cambiarles la fecha.
GRANT SELECT ON offboarding TO fichaitor_api, fichaitor_worker;
GRANT INSERT (account_id, scope, google_sub, confirmed_at, delete_at, restore_token_hash)
    ON offboarding TO fichaitor_api;
GRANT UPDATE (restored_at, restore_token_hash) ON offboarding TO fichaitor_api;
GRANT UPDATE (effective_at, revoke_state, purged_at, google_sub, restore_token_hash)
    ON offboarding TO fichaitor_worker;

GRANT INSERT (deleted) ON retention_run TO fichaitor_worker;

-- El borrado físico lo hace el worker y nadie más: la API sigue sin poder borrar nada.
-- No hay un rol aparte para la retención: lo usaría el mismo proceso, con la misma
-- cadena de conexión al lado, así que no separaría nada y añadiría un secreto más.
-- Cada DELETE lleva lo mínimo que necesita su WHERE.
GRANT DELETE ON google_connection, profile_summary, review, sentiment_analysis,
                sentiment_history, sentiment_usage, action, job
    TO fichaitor_worker;
-- review_sentiment y review_draft se van con su reseña (ON DELETE CASCADE).
GRANT SELECT (google_sub, location) ON sentiment_usage TO fichaitor_worker;

-- De las tablas de schema.sql, lo justo para hacer efectiva una baja y para la retención.
-- No se retira antes nada sobre account ni location: schema_channels.sql concede ahí
-- otras columnas al worker y este archivo puede volver a aplicarse solo.
REVOKE ALL ON session, waitlist_entry FROM fichaitor_worker;
-- Cuenta: fechar la desconexión y la baja. El correo y el google_sub no se cambian
-- (el disparador account_baja_guard los vacía al dar de baja la cuenta).
-- selected_location_id: para dejar de tener elegida una ficha retirada antes de borrarla.
GRANT SELECT (id, google_sub, disconnected_at, deleted_at, selected_location_id)
    ON account TO fichaitor_worker;
GRANT UPDATE (disconnected_at, deleted_at, selected_location_id) ON account TO fichaitor_worker;
-- Fichas: retirarlas (en la baja, y cuando Google niega el acceso a la ficha dos
-- sincronizaciones seguidas) y, vencido el plazo, borrarlas.
GRANT SELECT (id, account_id, gbp_location, removed_at) ON location TO fichaitor_worker;
GRANT UPDATE (removed_at) ON location TO fichaitor_worker;
GRANT DELETE ON location TO fichaitor_worker;
-- Sesiones: cerrarlas al dar de baja la cuenta y borrar las vencidas. La huella del
-- token no se lee.
GRANT SELECT (id, account_id, expires_at, revoked_at) ON session TO fichaitor_worker;
GRANT UPDATE (revoked_at) ON session TO fichaitor_worker;
GRANT DELETE ON session TO fichaitor_worker;
-- Lista de espera: borrar por antigüedad. El contacto no se lee.
GRANT SELECT (id, created_at) ON waitlist_entry TO fichaitor_worker;
GRANT DELETE ON waitlist_entry TO fichaitor_worker;

-- Topes de llamadas de pago (usage_counter, en schema.sql): borrar las ventanas vencidas.
-- Solo lee la fecha de fin; no cuenta ni cambia nada.
REVOKE ALL ON usage_counter FROM fichaitor_worker;
GRANT SELECT (expires_at) ON usage_counter TO fichaitor_worker;
GRANT DELETE ON usage_counter TO fichaitor_worker;

-- Registro de auditoría: el worker puede borrar, y solo, las filas de una cuenta que
-- lleva más de 12 meses sin conexión con Google (baja de la ficha o de la cuenta).
-- Sigue sin poder leerlas ni cambiarlas: no hay política de SELECT ni de UPDATE, y el
-- DELETE del worker no lleva WHERE (no puede: no tiene SELECT sobre ninguna columna);
-- qué filas caen lo decide esta política.
-- Los 12 meses son un suelo fijo en la base. La aplicación puede pedir un plazo más
-- largo, o un "ahora" anterior (las pruebas), con el parámetro de sesión
-- fichaitor.audit_cutoff; nunca uno más corto.
GRANT DELETE ON audit_log TO fichaitor_worker;
DROP POLICY IF EXISTS audit_log_worker_delete_expired ON audit_log;
CREATE POLICY audit_log_worker_delete_expired ON audit_log FOR DELETE TO fichaitor_worker
    USING (EXISTS (
        SELECT 1 FROM account a
        WHERE a.id = audit_log.account_id
          AND a.disconnected_at <= least(
              now() - interval '12 months',
              coalesce(nullif(current_setting('fichaitor.audit_cutoff', true), '')::timestamptz,
                       now() - interval '12 months'))
    ));

-- ---------------------------------------------------------------------------
-- Solo desarrollo local y pruebas: copiar a db/dev-init.sql.
-- En producción el usuario del worker se crea aparte, con su contraseña en un secreto.
--
-- DO $$
-- BEGIN
--     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_worker_dev') THEN
--         CREATE ROLE fichaitor_worker_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_worker;
--     END IF;
-- END
-- $$;
