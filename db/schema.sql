-- Fichaitor: esquema de base de datos. Idempotente: se puede aplicar las veces que haga falta.
-- Lo aplica el rol propietario. La API se conecta con un usuario que hereda de fichaitor_api.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_api') THEN
        CREATE ROLE fichaitor_api NOLOGIN;
    END IF;
END
$$;

-- Lista de espera del experimento de captación.
-- Datos personales: el contacto. Base legal: consentimiento, con versión del texto y fecha.
CREATE TABLE IF NOT EXISTS waitlist_entry (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      timestamptz NOT NULL DEFAULT now(),
    channel         text NOT NULL CHECK (channel IN ('whatsapp', 'email')),
    contact         text NOT NULL CHECK (length(contact) BETWEEN 5 AND 254),
    place_id        text CHECK (length(place_id) <= 200),
    business_name   text CHECK (length(business_name) <= 200),
    score           smallint CHECK (score BETWEEN 0 AND 100),
    source          text CHECK (length(source) <= 60),
    consent_version text NOT NULL CHECK (length(consent_version) BETWEEN 1 AND 40),
    consent_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, contact)
);

-- Embudo del experimento. Sin datos personales: ni IP ni contacto.
CREATE TABLE IF NOT EXISTS funnel_event (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    kind       text NOT NULL,
    source     text CHECK (length(source) <= 60),
    place_id   text CHECK (length(place_id) <= 200),
    score      smallint CHECK (score BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS funnel_event_kind_at ON funnel_event (kind, at);

-- La lista de tipos de evento crece: se recrea la restricción en cada aplicación.
ALTER TABLE funnel_event DROP CONSTRAINT IF EXISTS funnel_event_kind_check;
ALTER TABLE funnel_event ADD CONSTRAINT funnel_event_kind_check
    CHECK (kind IN ('visit', 'diagnosis', 'not_found', 'signup', 'connected'));

-- Conexiones con Google. Solo se añaden filas: una reconexión es una fila nueva
-- y vale la más reciente de cada google_sub.
-- refresh_token_enc va cifrado con clave pública; solo el worker tiene la privada.
CREATE TABLE IF NOT EXISTS google_connection (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connected_at      timestamptz NOT NULL DEFAULT now(),
    google_sub        text NOT NULL CHECK (length(google_sub) BETWEEN 1 AND 255),
    email             text NOT NULL CHECK (length(email) <= 254),
    refresh_token_enc text NOT NULL,
    key_version       smallint NOT NULL,
    scopes            text[] NOT NULL,
    locations         jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS google_connection_sub_at
    ON google_connection (google_sub, connected_at DESC);

-- La API solo puede insertar. No puede leer, cambiar ni borrar contactos ni tokens:
-- si la API se ve comprometida, no se pueden extraer por ella.
REVOKE ALL ON waitlist_entry, funnel_event, google_connection FROM fichaitor_api;
GRANT INSERT ON waitlist_entry, funnel_event, google_connection TO fichaitor_api;

-- Cuentas. Una cuenta es una identidad de Google: no hay contraseñas.
-- Datos personales: el correo. Se toma de Google al conectar.
CREATE TABLE IF NOT EXISTS account (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    google_sub           text NOT NULL UNIQUE CHECK (length(google_sub) BETWEEN 1 AND 255),
    email                text NOT NULL CHECK (length(email) <= 254),
    created_at           timestamptz NOT NULL DEFAULT now(),
    selected_location_id text
);

-- Fichas de cada cuenta. El identificador público es 'loc_' más 12 caracteres aleatorios.
-- La misma ficha de Google en dos cuentas son dos filas con identificadores distintos.
-- Las fichas que Google deja de listar no se borran: se marcan con removed_at.
CREATE TABLE IF NOT EXISTS location (
    id           text PRIMARY KEY DEFAULT 'loc_' || encode(gen_random_bytes(6), 'hex')
                 CHECK (id ~ '^loc_[0-9a-z]{4,32}$'),
    account_id   uuid NOT NULL REFERENCES account (id),
    gbp_account  text NOT NULL CHECK (length(gbp_account) <= 255),
    gbp_location text NOT NULL CHECK (length(gbp_location) BETWEEN 1 AND 255),
    title        text NOT NULL CHECK (length(title) <= 500),
    place_id     text CHECK (length(place_id) BETWEEN 1 AND 255),
    verified     boolean NOT NULL DEFAULT false,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    removed_at   timestamptz,
    UNIQUE (account_id, gbp_location),
    UNIQUE (account_id, id)
);

-- Segunda barrera del aislamiento entre cuentas: la ficha elegida tiene que ser
-- de esa misma cuenta, lo compruebe o no la consulta de la API.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'account_selected_location_fk') THEN
        ALTER TABLE account ADD CONSTRAINT account_selected_location_fk
            FOREIGN KEY (id, selected_location_id) REFERENCES location (account_id, id);
    END IF;
END
$$;

-- Sesiones. token_hash es el SHA-256 del token de la cookie; el token no se guarda.
-- Cerrar sesión marca revoked_at: las filas no se borran.
CREATE TABLE IF NOT EXISTS session (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES account (id),
    token_hash text NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS session_account ON session (account_id);

-- En cuentas, fichas y sesiones la API lee, inserta y cambia solo las columnas que
-- usan sus rutas. No borra nada, no cambia google_sub ni el dueño de una ficha,
-- y no puede alargar una sesión ni cambiarle el token o la cuenta.
REVOKE ALL ON account, location, session FROM fichaitor_api;
GRANT SELECT, INSERT ON account, location, session TO fichaitor_api;
GRANT UPDATE (email, selected_location_id) ON account TO fichaitor_api;
GRANT UPDATE (gbp_account, title, place_id, verified, updated_at, removed_at)
    ON location TO fichaitor_api;
GRANT UPDATE (revoked_at) ON session TO fichaitor_api;
