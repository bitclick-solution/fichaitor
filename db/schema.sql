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
    kind       text NOT NULL CHECK (kind IN ('visit', 'diagnosis', 'not_found', 'signup')),
    source     text CHECK (length(source) <= 60),
    place_id   text CHECK (length(place_id) <= 200),
    score      smallint CHECK (score BETWEEN 0 AND 100)
);

CREATE INDEX IF NOT EXISTS funnel_event_kind_at ON funnel_event (kind, at);

-- La API solo puede insertar. No puede leer, cambiar ni borrar contactos:
-- si la API se ve comprometida, la lista de espera no se puede extraer por ella.
REVOKE ALL ON waitlist_entry, funnel_event FROM fichaitor_api;
GRANT INSERT ON waitlist_entry, funnel_event TO fichaitor_api;
