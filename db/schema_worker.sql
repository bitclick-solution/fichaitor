-- Fichaitor: esquema del worker (cola de jobs y resumen de fichas).
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
