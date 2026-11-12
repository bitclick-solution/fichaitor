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

-- Worker: gestiona la cola y los resúmenes. No borra.
-- De google_connection lee solo lo que necesita para actuar; el email, no.
GRANT SELECT, INSERT, UPDATE ON job, profile_summary TO fichaitor_worker;
GRANT SELECT (id, google_sub, connected_at, refresh_token_enc, key_version, locations)
    ON google_connection TO fichaitor_worker;

-- ---------------------------------------------------------------------------
-- Reseñas y respuestas (2026-11-12): la primera escritura en Google.
-- Necesita las tablas account y location de schema.sql.

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
CREATE TABLE IF NOT EXISTS review_draft (
    review_id   text PRIMARY KEY REFERENCES review (id),
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
    review_id     text NOT NULL REFERENCES review (id),
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

-- Registro de auditoría. Solo se añaden filas: nadie las cambia ni las borra.
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
    WITH CHECK (event = 'reply_confirmed');
DROP POLICY IF EXISTS audit_log_worker_insert ON audit_log;
CREATE POLICY audit_log_worker_insert ON audit_log FOR INSERT TO fichaitor_worker
    WITH CHECK (event IN ('reply_sent', 'reply_failed'));

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

-- Worker: escribe clasificaciones, análisis e historia. No borra. Del uso solo añade.
GRANT SELECT, INSERT, UPDATE ON review_sentiment, sentiment_analysis, sentiment_history
    TO fichaitor_worker;
GRANT INSERT (google_sub, location, trigger, classify_calls, summary_calls, failed_calls,
              reviews_sent, input_tokens, output_tokens)
    ON sentiment_usage TO fichaitor_worker;

-- API: solo lee el análisis y la historia. La clasificación por reseña y el uso, no.
GRANT SELECT ON sentiment_analysis, sentiment_history TO fichaitor_api;

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
