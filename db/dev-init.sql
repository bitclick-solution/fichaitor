-- Solo para desarrollo local y pruebas. No se usa en producción:
-- allí el usuario de la API se crea aparte, con su contraseña en un secreto.
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
