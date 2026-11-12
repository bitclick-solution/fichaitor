-- Solo para desarrollo local y pruebas. No se usa en producción:
-- allí los usuarios se crean aparte (deploy/init-roles.sh), con su contraseña en un secreto.
-- Crea los roles base si faltan, para poder aplicarse antes o después de los esquemas.
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

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_api_dev') THEN
        CREATE ROLE fichaitor_api_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_api;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_worker_dev') THEN
        CREATE ROLE fichaitor_worker_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_worker;
    END IF;
END
$$;

-- El proceso de la conversación (docs/canales.md): sin la clave privada y sin acceso a
-- google_connection.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_conversation_dev') THEN
        CREATE ROLE fichaitor_conversation_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_conversation;
    END IF;
END
$$;
