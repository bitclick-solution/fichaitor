-- Fichaitor: esquema del backoffice (fichaitor-admin). Paso 5 de docs/admin-facturacion.md;
-- el diseño está en su sección 6 (acceso solo por túnel, contraseña con TOTP, roles y
-- auditoría) y las decisiones del propietario en sus secciones 1.1 (P5) y 9.0.
-- Idempotente. Se aplica con el rol propietario DESPUÉS de schema.sql, schema_worker.sql,
-- schema_channels.sql y, cuando exista, schema_billing.sql (concede la lectura de las
-- tablas de cobro que ya existan; si se aplica antes, basta con volver a aplicarlo):
--   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_admin.sql
--
-- Qué puede hacer el rol fichaitor_admin (sección 6.6):
-- - Leer, sin función, solo columnas sin datos personales: account (id y fechas),
--   location (sin title), plan_entitlement, location_activation_log, offboarding (sin
--   google_sub ni restore_token_hash) y, si existen, las tablas de cobro (sin el PDF, la
--   dirección ni invoice_email).
-- - Todo lo demás, por funciones SECURITY DEFINER del propietario. Cada una comprueba que
--   quien llama es miembro de fichaitor_admin, que la sesión del backoffice es válida,
--   el rol de la persona (soporte, finanzas o propietario: sección 6.3) y, en las
--   acciones sensibles, un TOTP de menos de 5 minutos. Las que cambian algo escriben su
--   fila de auditoría en la misma transacción.
-- - Nunca: google_connection, profile_summary, review, review_draft, sentiment_*,
--   proposal, channel_identity, channel_message, waitlist_entry, job, session, action,
--   audit_log, offboarding_code, link_code, funnel_event ni usage_counter. El correo de
--   una cuenta, solo enmascarado o comparado dentro de una función. La lista de espera,
--   solo en totales anónimos sin celdas de menos de 5 personas.
--
-- Lo que la base NO puede impedir: la contraseña (Argon2id con pimienta) y el código TOTP
-- (con la clave FICHAITOR_ADMIN_TOTP_KEY) se comprueban en el proceso, que es quien tiene
-- la pimienta y la clave. Un proceso fichaitor-admin comprometido puede hacerse pasar por
-- cualquier persona del backoffice. Lo que sí impone la base: qué datos ve (nunca tokens,
-- correos ni la lista de espera), que no cambia roles ni da de alta usuarios (solo el
-- propietario, con scripts/admin_usuario.py), que no repite un paso TOTP, el bloqueo por
-- intentos y que cada acción queda en un registro que no puede leer ni borrar.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_admin') THEN
        CREATE ROLE fichaitor_admin NOLOGIN;
    END IF;
END
$$;

-- M6 de docs/seguridad/revision-2026-11-12-c.md: sin TEMPORARY. Quitado a PUBLIC (como en
-- schema.sql) y, por si alguien se lo concedió, a fichaitor_admin y a sus usuarios
-- (fichaitor_admin_prod, fichaitor_admin_dev). Un usuario creado después de aplicar este
-- archivo no lo tiene salvo que se le conceda a mano: PUBLIC ya no lo da.
DO $$
DECLARE
    who name;
BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    FOR who IN
        SELECT 'fichaitor_admin'::name
        UNION
        SELECT m.rolname FROM pg_catalog.pg_auth_members am
        JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
        JOIN pg_catalog.pg_roles m ON m.oid = am.member
        WHERE g.rolname = 'fichaitor_admin' AND NOT m.rolsuper
    LOOP
        EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM %I', current_database(), who);
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- Personas del backoffice (sección 6.2). Las da de alta, restablece y desactiva el
-- propietario con scripts/admin_usuario.py (funciones admin_owner_*, más abajo).
-- password_hash: Argon2id (argon2-cffi, 64 MiB, 3 iteraciones, paralelismo 4) sobre la
--   contraseña con la «pimienta» de admin.env. NULL hasta el alta.
-- totp_secret_enc: el secreto TOTP cifrado con AES-256-GCM y FICHAITOR_ADMIN_TOTP_KEY
--   (admin.env), en base64 (nonce y texto cifrado). Una copia de la base no basta para
--   generar códigos. NULL hasta el alta.
-- totp_last_step: el último paso de 30 segundos aceptado. Un código no vale dos veces.
-- failed_count: fallos seguidos de contraseña o de TOTP. Al quinto, bloqueo.
-- lock_streak: bloqueos seguidos. Cada uno dura el doble que el anterior (15 minutos el
--   primero, 24 horas como mucho). Vuelve a 0 al entrar bien.
-- must_enroll: tiene que dar de alta la contraseña y el TOTP (alta, restablecimiento o
--   tras entrar con un código de recuperación).
CREATE TABLE IF NOT EXISTS admin_user (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username            text NOT NULL UNIQUE CHECK (username ~ '^[a-z][a-z0-9_.-]{1,39}$'),
    role                text NOT NULL CHECK (role IN ('soporte', 'finanzas', 'propietario')),
    password_hash       text CHECK (password_hash ~ '^\$argon2id\$' AND length(password_hash) <= 512),
    totp_secret_enc     text CHECK (length(totp_secret_enc) BETWEEN 1 AND 512),
    totp_last_step      bigint,
    failed_count        smallint NOT NULL DEFAULT 0 CHECK (failed_count BETWEEN 0 AND 1000),
    lock_streak         smallint NOT NULL DEFAULT 0 CHECK (lock_streak BETWEEN 0 AND 1000),
    locked_until        timestamptz,
    active              boolean NOT NULL DEFAULT true,
    must_enroll         boolean NOT NULL DEFAULT true,
    created_at          timestamptz NOT NULL DEFAULT now(),
    password_changed_at timestamptz,
    CHECK (must_enroll OR (password_hash IS NOT NULL AND totp_secret_enc IS NOT NULL))
);

-- Códigos de recuperación: 10 por alta, con Argon2id. Se enseñan una vez y no se guardan.
CREATE TABLE IF NOT EXISTS admin_recovery_code (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_user_id uuid NOT NULL REFERENCES admin_user (id) ON DELETE CASCADE,
    code_hash     text NOT NULL CHECK (code_hash ~ '^\$argon2id\$' AND length(code_hash) <= 512),
    created_at    timestamptz NOT NULL DEFAULT now(),
    used_at       timestamptz
);

CREATE INDEX IF NOT EXISTS admin_recovery_code_user ON admin_recovery_code (admin_user_id)
    WHERE used_at IS NULL;

-- Enlaces de alta (http://localhost:8090/#alta=<token>): 24 horas y un solo uso. Solo se
-- guarda el SHA-256 del token.
CREATE TABLE IF NOT EXISTS admin_enrollment (
    token_hash    text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    admin_user_id uuid NOT NULL REFERENCES admin_user (id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    used_at       timestamptz,
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '24 hours')
);

-- Sesiones del backoffice (cookie __Host-fichaitor_admin). token_hash es el SHA-256 del
-- token de la cookie. Caduca a las 8 horas o tras 30 minutos sin actividad.
-- totp_verified_at: NULL entre la contraseña y el TOTP (5 minutos como mucho). Las acciones
-- sensibles piden que tenga menos de 5 minutos.
CREATE TABLE IF NOT EXISTS admin_session (
    token_hash       text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    admin_user_id    uuid NOT NULL REFERENCES admin_user (id) ON DELETE CASCADE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    totp_verified_at timestamptz,
    revoked_at       timestamptz,
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '8 hours')
);

CREATE INDEX IF NOT EXISTS admin_session_user ON admin_session (admin_user_id)
    WHERE revoked_at IS NULL;

-- Registro de auditoría del backoffice (sección 6.2). Solo se inserta: ningún rol de la
-- aplicación lo lee, lo cambia ni lo borra; el propietario del backoffice lo ve con
-- admin_audit_read. Sin datos personales: ni correos, ni nombres de fichas, ni lo que se
-- buscó.
-- at, written_by (session_user, el usuario con el que se conectó quien escribe) y role
-- (el de admin_user) los pone la base, también si se intenta fijarlos.
-- action: las funciones escriben las suyas (login_ok, offboarding_email…). El proceso
--   solo puede insertar acciones que empiezan por app_ (lecturas de pantallas, por
--   ejemplo): no puede fabricar un login_ok ni una baja.
-- ticket_ref: la referencia del caso, sin direcciones de correo.
-- Sin clave ajena a admin_user: el registro dura más que las personas.
-- Retención: 2 años (docs/admin-facturacion.md, 4.8). Ningún rol de la aplicación borra;
-- el disparador de abajo solo deja borrar las filas de más de 2 años.
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    written_by name NOT NULL DEFAULT session_user,
    admin_user uuid,
    role       text CHECK (role IN ('soporte', 'finanzas', 'propietario')),
    action     text NOT NULL CHECK (action ~ '^[a-z_]{1,60}$'),
    account_id uuid,
    target     text CHECK (target ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    ticket_ref text CHECK (length(ticket_ref) BETWEEN 3 AND 500
                           AND ticket_ref !~ '[[:cntrl:]]'
                           AND ticket_ref !~ '[^[:space:]]+@[^[:space:]]+'),
    detail     jsonb NOT NULL DEFAULT '{}'::jsonb
               CHECK (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 2048)
);

CREATE INDEX IF NOT EXISTS admin_audit_log_at ON admin_audit_log (at);
CREATE INDEX IF NOT EXISTS admin_audit_log_actor ON admin_audit_log (admin_user, action, at);

-- Quien llama es el propietario de la base (o un superusuario): el camino de los scripts
-- de un solo uso (admin_usuario.py y baja_manual.py). Se mira session_user, porque dentro
-- de una función SECURITY DEFINER current_user es siempre el propietario.
CREATE OR REPLACE FUNCTION admin_caller_is_owner() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT coalesce(bool_or(r.rolsuper OR c.relowner = r.oid), false)
    FROM pg_catalog.pg_roles r, pg_catalog.pg_class c
    WHERE r.rolname = session_user AND c.oid = 'public.admin_audit_log'::pg_catalog.regclass
$$;

-- El registro no cambia nunca, ni lo vacía nadie; borrar, solo lo de más de 2 años.
-- Vale también para el propietario: para saltárselo tiene que quitar el disparador, y eso
-- es un cambio de esquema que se ve.
-- Al insertar: at, written_by y role los pone la base.
CREATE OR REPLACE FUNCTION admin_audit_log_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.at := now();
        NEW.written_by := session_user;
        IF NEW.admin_user IS NOT NULL THEN
            NEW.role := (SELECT u.role FROM public.admin_user u WHERE u.id = NEW.admin_user);
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.at < now() - interval '2 years' THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'el registro de auditoría del backoffice solo admite inserciones';
END
$$;

DROP TRIGGER IF EXISTS admin_audit_log_guard ON admin_audit_log;
CREATE TRIGGER admin_audit_log_guard BEFORE INSERT OR UPDATE OR DELETE ON admin_audit_log
    FOR EACH ROW EXECUTE FUNCTION admin_audit_log_guard();
DROP TRIGGER IF EXISTS admin_audit_log_no_truncate ON admin_audit_log;
CREATE TRIGGER admin_audit_log_no_truncate BEFORE TRUNCATE ON admin_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION admin_audit_log_guard();

-- Fila de auditoría escrita por una función (interna: no la ejecuta la aplicación).
CREATE OR REPLACE FUNCTION admin_audit_write(
    p_actor uuid, p_action text, p_account uuid, p_target text, p_ticket text, p_detail jsonb)
RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    INSERT INTO public.admin_audit_log (admin_user, action, account_id, target, ticket_ref, detail)
    VALUES (p_actor, p_action, p_account, p_target, p_ticket, coalesce(p_detail, '{}'::jsonb));
$$;

-- Quién llama desde el backoffice (interna). Comprueba, en este orden: que quien llama es
-- miembro de fichaitor_admin; que la sesión existe, no está cerrada ni caducada, lleva
-- menos de 30 minutos sin actividad y tiene el TOTP confirmado; que la persona está activa,
-- sin bloqueo y sin alta pendiente; su rol; y, si se pide, un TOTP de menos de 5 minutos.
-- Anota la actividad. Errores: insufficient_privilege con sesion_no_valida,
-- rol_no_permitido o totp_requerido.
CREATE OR REPLACE FUNCTION admin_actor(
    p_session text, p_roles text[], p_fresh boolean, OUT actor_id uuid, OUT actor_role text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    verified timestamptz;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER') THEN
        RAISE EXCEPTION 'solo el backoffice' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT u.id, u.role, s.totp_verified_at INTO actor_id, actor_role, verified
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_session AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.last_seen_at > now() - interval '30 minutes' AND s.totp_verified_at IS NOT NULL
      AND u.active AND NOT u.must_enroll AND (u.locked_until IS NULL OR u.locked_until <= now())
    FOR UPDATE OF s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'sesion_no_valida' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT actor_role = ANY (p_roles) THEN
        RAISE EXCEPTION 'rol_no_permitido' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_fresh AND verified <= now() - interval '5 minutes' THEN
        RAISE EXCEPTION 'totp_requerido' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE public.admin_session s SET last_seen_at = now() WHERE s.token_hash = p_session;
END
$$;

-- Un fallo de contraseña, de TOTP o de código de recuperación (interna). Al quinto
-- seguido, bloqueo: 15 minutos, el doble en cada bloqueo seguido, 24 horas como mucho.
CREATE OR REPLACE FUNCTION admin_fail(p_user uuid, p_stage text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    failures integer;
    streak integer;
    minutes integer;
BEGIN
    UPDATE public.admin_user u SET failed_count = u.failed_count + 1
    WHERE u.id = p_user RETURNING u.failed_count, u.lock_streak INTO failures, streak;
    PERFORM public.admin_audit_write(p_user, 'login_failed', NULL, NULL, NULL,
                                     jsonb_build_object('stage', p_stage));
    IF failures >= 5 THEN
        minutes := least(15 * power(2, least(streak, 10))::integer, 1440);
        UPDATE public.admin_user u
        SET failed_count = 0, lock_streak = u.lock_streak + 1,
            locked_until = now() + make_interval(mins => minutes)
        WHERE u.id = p_user;
        UPDATE public.admin_session s SET revoked_at = now()
        WHERE s.admin_user_id = p_user AND s.revoked_at IS NULL;
        PERFORM public.admin_audit_write(p_user, 'locked', NULL, NULL, NULL,
                                         jsonb_build_object('minutes', minutes));
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Inicio de sesión (sección 6.2). El proceso comprueba la contraseña y el código; la base
-- guarda el estado, impide repetir un paso TOTP, bloquea y audita.

-- Tope global: 20 intentos fallidos por hora en todo el backoffice. Antes de mirar una
-- contraseña.
CREATE OR REPLACE FUNCTION admin_login_allowed() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT count(*) < 20 FROM public.admin_audit_log a
    WHERE a.action = 'login_failed' AND a.at > now() - interval '1 hour'
$$;

-- Lo que hace falta para comprobar la contraseña y el TOTP de una persona activa con el
-- alta hecha. Ninguna fila si no existe, está desactivada o tiene el alta pendiente: el
-- proceso responde igual y tarda lo mismo (calcula un hash de relleno).
CREATE OR REPLACE FUNCTION admin_login_lookup(p_username text)
RETURNS TABLE (admin_user_id uuid, password_hash text, totp_secret_enc text,
               locked_until timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.id, u.password_hash, u.totp_secret_enc, u.locked_until
    FROM public.admin_user u
    WHERE u.username = lower(p_username) AND u.active AND NOT u.must_enroll
$$;

-- Un fallo de contraseña ('password'), de TOTP ('totp') o de código de recuperación
-- ('recovery'). Con un usuario que no existe se audita igual, sin guardar lo tecleado.
CREATE OR REPLACE FUNCTION admin_login_failed(p_username text, p_stage text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    IF p_stage NOT IN ('password', 'totp', 'recovery') THEN
        RAISE EXCEPTION 'fase desconocida' USING ERRCODE = 'check_violation';
    END IF;
    SELECT u.id INTO who FROM public.admin_user u
    WHERE u.username = lower(p_username) AND u.active
    FOR UPDATE;
    IF who IS NULL THEN
        PERFORM public.admin_audit_write(NULL, 'login_failed', NULL, NULL, NULL,
                                         jsonb_build_object('stage', p_stage, 'known', false));
        RETURN;
    END IF;
    PERFORM public.admin_fail(who, p_stage);
END
$$;

-- Contraseña correcta: sesión a la espera del TOTP (5 minutos para darlo). false si la
-- persona no puede entrar (desactivada, bloqueada o con el alta pendiente).
CREATE OR REPLACE FUNCTION admin_session_open(p_username text, p_token_hash text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    SELECT u.id INTO who FROM public.admin_user u
    WHERE u.username = lower(p_username) AND u.active AND NOT u.must_enroll
      AND (u.locked_until IS NULL OR u.locked_until <= now());
    IF who IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO public.admin_session (token_hash, admin_user_id, expires_at)
    VALUES (p_token_hash, who, now() + interval '8 hours');
    PERFORM public.admin_audit_write(who, 'login_password_ok', NULL, NULL, NULL, NULL);
    RETURN true;
END
$$;

-- El proceso ha comprobado el código TOTP del paso p_step (ventana de ±1 paso). La base
-- solo lo acepta si es de la ventana y posterior al último aceptado: un código no vale
-- dos veces. Si vale, la sesión queda con el TOTP confirmado ahora (primer paso o
-- confirmación de una acción sensible). Un paso repetido o fuera de ventana cuenta como
-- fallo.
CREATE OR REPLACE FUNCTION admin_session_totp(p_token_hash text, p_step bigint)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    last_step bigint;
    verified timestamptz;
    opened timestamptz;
    current_step bigint := floor(extract(epoch FROM now()) / 30)::bigint;
BEGIN
    SELECT u.id, u.totp_last_step, s.totp_verified_at, s.created_at
    INTO who, last_step, verified, opened
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.last_seen_at > now() - interval '30 minutes'
      AND u.active AND NOT u.must_enroll AND (u.locked_until IS NULL OR u.locked_until <= now())
    FOR UPDATE OF s, u;
    IF who IS NULL THEN
        RETURN false;
    END IF;
    IF verified IS NULL AND opened <= now() - interval '5 minutes' THEN
        UPDATE public.admin_session s SET revoked_at = now() WHERE s.token_hash = p_token_hash;
        RETURN false;
    END IF;
    IF p_step IS NULL OR p_step NOT BETWEEN current_step - 1 AND current_step + 1
       OR p_step <= coalesce(last_step, -1) THEN
        PERFORM public.admin_fail(who, 'totp_replay');
        RETURN false;
    END IF;
    UPDATE public.admin_user u
    SET totp_last_step = p_step, failed_count = 0, lock_streak = 0, locked_until = NULL
    WHERE u.id = who;
    UPDATE public.admin_session s SET totp_verified_at = now(), last_seen_at = now()
    WHERE s.token_hash = p_token_hash;
    PERFORM public.admin_audit_write(
        who, CASE WHEN verified IS NULL THEN 'login_ok' ELSE 'totp_stepup' END,
        NULL, NULL, NULL, NULL);
    RETURN true;
END
$$;

-- Los códigos de recuperación sin usar de la persona de una sesión a la espera del TOTP,
-- para que el proceso compare el tecleado con sus hashes Argon2id.
CREATE OR REPLACE FUNCTION admin_recovery_candidates(p_token_hash text)
RETURNS TABLE (code_id bigint, code_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT c.id, c.code_hash
    FROM public.admin_session s
    JOIN public.admin_user u ON u.id = s.admin_user_id
    JOIN public.admin_recovery_code c ON c.admin_user_id = u.id AND c.used_at IS NULL
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.created_at > now() - interval '5 minutes' AND s.totp_verified_at IS NULL
      AND u.active AND NOT u.must_enroll AND (u.locked_until IS NULL OR u.locked_until <= now())
      AND pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER')
$$;

-- Entra con un código de recuperación: se gasta, la sesión queda confirmada y la persona
-- tiene que volver a dar de alta el TOTP en ese momento (must_enroll; admin_set_totp).
CREATE OR REPLACE FUNCTION admin_session_recovery(p_token_hash text, p_code_id bigint)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    SELECT u.id INTO who
    FROM public.admin_session s
    JOIN public.admin_user u ON u.id = s.admin_user_id
    JOIN public.admin_recovery_code c ON c.admin_user_id = u.id
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.created_at > now() - interval '5 minutes' AND s.totp_verified_at IS NULL
      AND u.active AND NOT u.must_enroll AND (u.locked_until IS NULL OR u.locked_until <= now())
      AND c.id = p_code_id AND c.used_at IS NULL
    FOR UPDATE OF s, u, c;
    IF who IS NULL THEN
        RETURN false;
    END IF;
    UPDATE public.admin_recovery_code c SET used_at = now() WHERE c.id = p_code_id;
    UPDATE public.admin_user u
    SET must_enroll = true, failed_count = 0, lock_streak = 0, locked_until = NULL
    WHERE u.id = who;
    UPDATE public.admin_session s SET totp_verified_at = now(), last_seen_at = now()
    WHERE s.token_hash = p_token_hash;
    PERFORM public.admin_audit_write(who, 'recovery_code_used', NULL, NULL, NULL, NULL);
    RETURN true;
END
$$;

-- Nuevo TOTP para la persona de la sesión: tras un código de recuperación (must_enroll) o
-- por cambio, con un TOTP o un código de recuperación de menos de 5 minutos. p_step es el
-- paso del código con el que confirmó el secreto nuevo. Con p_recovery_hashes (10), los
-- códigos de recuperación se sustituyen.
CREATE OR REPLACE FUNCTION admin_set_totp(
    p_token_hash text, p_totp_secret_enc text, p_step bigint, p_recovery_hashes text[])
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    current_step bigint := floor(extract(epoch FROM now()) / 30)::bigint;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER') THEN
        RAISE EXCEPTION 'solo el backoffice' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT u.id INTO who
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.totp_verified_at > now() - interval '5 minutes' AND u.active
      AND (u.locked_until IS NULL OR u.locked_until <= now())
      AND (u.must_enroll IS FALSE OR u.password_hash IS NOT NULL)
    FOR UPDATE OF s, u;
    IF who IS NULL OR p_step IS NULL OR p_step NOT BETWEEN current_step - 1 AND current_step + 1 THEN
        RETURN false;
    END IF;
    IF p_recovery_hashes IS NOT NULL AND cardinality(p_recovery_hashes) <> 10 THEN
        RAISE EXCEPTION 'hacen falta 10 códigos de recuperación' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE public.admin_user u
    SET totp_secret_enc = p_totp_secret_enc, totp_last_step = greatest(p_step, u.totp_last_step),
        must_enroll = false
    WHERE u.id = who;
    IF p_recovery_hashes IS NOT NULL THEN
        DELETE FROM public.admin_recovery_code c WHERE c.admin_user_id = who;
        INSERT INTO public.admin_recovery_code (admin_user_id, code_hash)
        SELECT who, h FROM unnest(p_recovery_hashes) AS h;
    END IF;
    PERFORM public.admin_audit_write(who, 'totp_changed', NULL, NULL, NULL,
        jsonb_build_object('recovery_codes_renewed', p_recovery_hashes IS NOT NULL));
    RETURN true;
END
$$;

-- Cambio de contraseña desde una sesión con TOTP de menos de 5 minutos: cierra las demás
-- sesiones de la persona.
CREATE OR REPLACE FUNCTION admin_change_password(p_token_hash text, p_password_hash text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    role_name text;
BEGIN
    SELECT a.actor_id, a.actor_role INTO who, role_name
    FROM public.admin_actor(p_token_hash, ARRAY['soporte', 'finanzas', 'propietario'], true) a;
    UPDATE public.admin_user u SET password_hash = p_password_hash, password_changed_at = now()
    WHERE u.id = who;
    UPDATE public.admin_session s SET revoked_at = now()
    WHERE s.admin_user_id = who AND s.revoked_at IS NULL AND s.token_hash <> p_token_hash;
    PERFORM public.admin_audit_write(who, 'password_changed', NULL, NULL, NULL, NULL);
END
$$;

-- Nuevo hash de la misma contraseña, cuando cambian los parámetros de Argon2id. Solo
-- desde la sesión recién abierta con esa contraseña (antes del TOTP, como mucho 5 minutos).
CREATE OR REPLACE FUNCTION admin_rehash_password(p_token_hash text, p_password_hash text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    SELECT s.admin_user_id INTO who FROM public.admin_session s
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL
      AND s.created_at > now() - interval '5 minutes' AND s.totp_verified_at IS NULL
      AND pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER');
    IF who IS NULL THEN
        RETURN false;
    END IF;
    UPDATE public.admin_user u SET password_hash = p_password_hash WHERE u.id = who;
    PERFORM public.admin_audit_write(who, 'password_rehashed', NULL, NULL, NULL, NULL);
    RETURN true;
END
$$;

-- La sesión de la cookie: quién es y con qué rol. Ninguna fila si no vale. Anota la
-- actividad (los 30 minutos sin actividad cuentan desde aquí).
CREATE OR REPLACE FUNCTION admin_session_check(p_token_hash text)
RETURNS TABLE (admin_user_id uuid, username text, role text, totp_verified_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    who_role text;
BEGIN
    BEGIN
        SELECT a.actor_id, a.actor_role INTO who, who_role
        FROM public.admin_actor(p_token_hash, ARRAY['soporte', 'finanzas', 'propietario'], false) a;
    EXCEPTION WHEN insufficient_privilege THEN
        RETURN;
    END;
    RETURN QUERY
    SELECT u.id, u.username, u.role, s.totp_verified_at
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_token_hash;
END
$$;

-- Tras un código de recuperación la sesión vale solo para dar de alta el TOTP nuevo:
-- admin_session_check no la devuelve (must_enroll). Esta dice si está en ese caso.
CREATE OR REPLACE FUNCTION admin_session_must_enroll(p_token_hash text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT coalesce(bool_or(u.must_enroll), false)
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.totp_verified_at > now() - interval '5 minutes' AND u.active
      AND pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER')
$$;

CREATE OR REPLACE FUNCTION admin_session_close(p_token_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER') THEN
        RAISE EXCEPTION 'solo el backoffice' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE public.admin_session s SET revoked_at = now()
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL
    RETURNING s.admin_user_id INTO who;
    IF who IS NOT NULL THEN
        PERFORM public.admin_audit_write(who, 'logout', NULL, NULL, NULL, NULL);
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Alta (sección 6.2, «Alta y recuperación»).

-- El enlace de alta: para quién es. Ninguna fila si no vale (caducado, usado o
-- desconocido).
CREATE OR REPLACE FUNCTION admin_enrollment_check(p_token_hash text)
RETURNS TABLE (username text, role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.username, u.role
    FROM public.admin_enrollment e JOIN public.admin_user u ON u.id = e.admin_user_id
    WHERE e.token_hash = p_token_hash AND e.used_at IS NULL AND e.expires_at > now()
      AND u.active AND pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER')
$$;

-- La persona elige contraseña, escanea el QR y confirma un código (paso p_step): el
-- proceso lo ha comprobado y manda los hashes. Gasta el enlace, cierra sus sesiones y
-- sustituye los 10 códigos de recuperación.
CREATE OR REPLACE FUNCTION admin_enroll(
    p_token_hash text, p_password_hash text, p_totp_secret_enc text, p_step bigint,
    p_recovery_hashes text[])
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    current_step bigint := floor(extract(epoch FROM now()) / 30)::bigint;
BEGIN
    IF NOT pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER') THEN
        RAISE EXCEPTION 'solo el backoffice' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_recovery_hashes IS NULL OR cardinality(p_recovery_hashes) <> 10 THEN
        RAISE EXCEPTION 'hacen falta 10 códigos de recuperación' USING ERRCODE = 'check_violation';
    END IF;
    SELECT e.admin_user_id INTO who
    FROM public.admin_enrollment e JOIN public.admin_user u ON u.id = e.admin_user_id
    WHERE e.token_hash = p_token_hash AND e.used_at IS NULL AND e.expires_at > now() AND u.active
    FOR UPDATE OF e, u;
    IF who IS NULL OR p_step IS NULL OR p_step NOT BETWEEN current_step - 1 AND current_step + 1 THEN
        RETURN false;
    END IF;
    UPDATE public.admin_enrollment e SET used_at = now() WHERE e.token_hash = p_token_hash;
    UPDATE public.admin_user u
    SET password_hash = p_password_hash, totp_secret_enc = p_totp_secret_enc,
        totp_last_step = greatest(p_step, u.totp_last_step), must_enroll = false,
        failed_count = 0, lock_streak = 0, locked_until = NULL, password_changed_at = now()
    WHERE u.id = who;
    UPDATE public.admin_session s SET revoked_at = now()
    WHERE s.admin_user_id = who AND s.revoked_at IS NULL;
    DELETE FROM public.admin_recovery_code c WHERE c.admin_user_id = who;
    INSERT INTO public.admin_recovery_code (admin_user_id, code_hash)
    SELECT who, h FROM unnest(p_recovery_hashes) AS h;
    PERFORM public.admin_audit_write(who, 'enrolled', NULL, NULL, NULL, NULL);
    RETURN true;
END
$$;

-- Solo el propietario de la base (scripts/admin_usuario.py, en un contenedor de un solo
-- uso): alta, restablecimiento y desactivación. fichaitor_admin no las ejecuta, así que
-- el backoffice no crea personas ni cambia roles.
CREATE OR REPLACE FUNCTION admin_owner_create_user(
    p_username text, p_role text, p_enroll_token_hash text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    IF NOT public.admin_caller_is_owner() THEN
        RAISE EXCEPTION 'solo el propietario de la base' USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO public.admin_user (username, role) VALUES (lower(p_username), p_role)
    RETURNING id INTO who;
    INSERT INTO public.admin_enrollment (token_hash, admin_user_id, expires_at)
    VALUES (p_enroll_token_hash, who, now() + interval '24 hours');
    PERFORM public.admin_audit_write(NULL, 'user_created', NULL, who::text, NULL,
                                     jsonb_build_object('role', p_role));
    RETURN who;
END
$$;

-- Olvidó la contraseña (comprobada su identidad en persona o por teléfono): alta nueva.
-- Se cierran sus sesiones y se retiran sus códigos de recuperación. totp_last_step se
-- conserva: los códigos ya usados siguen sin valer.
CREATE OR REPLACE FUNCTION admin_owner_reset_user(p_username text, p_enroll_token_hash text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    IF NOT public.admin_caller_is_owner() THEN
        RAISE EXCEPTION 'solo el propietario de la base' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE public.admin_user u
    SET password_hash = NULL, totp_secret_enc = NULL, must_enroll = true, failed_count = 0,
        lock_streak = 0, locked_until = NULL, active = true
    WHERE u.username = lower(p_username) RETURNING u.id INTO who;
    IF who IS NULL THEN
        RAISE EXCEPTION 'no existe esa persona' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE public.admin_session s SET revoked_at = now()
    WHERE s.admin_user_id = who AND s.revoked_at IS NULL;
    UPDATE public.admin_enrollment e SET used_at = now()
    WHERE e.admin_user_id = who AND e.used_at IS NULL;
    DELETE FROM public.admin_recovery_code c WHERE c.admin_user_id = who;
    INSERT INTO public.admin_enrollment (token_hash, admin_user_id, expires_at)
    VALUES (p_enroll_token_hash, who, now() + interval '24 hours');
    PERFORM public.admin_audit_write(NULL, 'user_reset', NULL, who::text, NULL, NULL);
END
$$;

CREATE OR REPLACE FUNCTION admin_owner_set_active(p_username text, p_active boolean)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    IF NOT public.admin_caller_is_owner() THEN
        RAISE EXCEPTION 'solo el propietario de la base' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE public.admin_user u SET active = p_active
    WHERE u.username = lower(p_username) RETURNING u.id INTO who;
    IF who IS NULL THEN
        RAISE EXCEPTION 'no existe esa persona' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT p_active THEN
        UPDATE public.admin_session s SET revoked_at = now()
        WHERE s.admin_user_id = who AND s.revoked_at IS NULL;
    END IF;
    PERFORM public.admin_audit_write(NULL,
        CASE WHEN p_active THEN 'user_activated' ELSE 'user_deactivated' END,
        NULL, who::text, NULL, NULL);
END
$$;

-- ---------------------------------------------------------------------------
-- Cuentas (sección 6.4).

-- El correo como lo enseña el panel: "d***o@example.com" (mask_email de
-- app/domain/offboarding.py). Interna de las funciones de búsqueda.
CREATE OR REPLACE FUNCTION admin_mask_email(p_email text) RETURNS text
LANGUAGE sql IMMUTABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT CASE
        WHEN position('@' IN coalesce(p_email, '')) <= 1 THEN '***'
        ELSE left(p_email, 1) || '***'
             || CASE WHEN length(split_part(p_email, '@', 1)) > 2
                     THEN right(split_part(p_email, '@', 1), 1) ELSE '' END
             || '@' || substr(p_email, position('@' IN p_email) + 1)
    END
$$;

-- Buscar cuentas: por id de cuenta, id de ficha (loc_…), correo exacto o, cuando exista
-- billing_customer, NIF o razón social. Nunca por el nombre de una ficha (dato de
-- Google). El correo sale enmascarado. Se audita el tipo de búsqueda, no lo buscado.
-- Por páginas (keyset, nunca offset): created_at e id, de lo más viejo a lo más nuevo.
-- p_after_key y p_after_id, los de la última fila de la página anterior (el proceso los
-- lleva en el cursor). Mismo patrón en todos los listados de este archivo: hasta 201
-- filas, porque el proceso pide una más de las que enseña para saber si hay otra página.
DROP FUNCTION IF EXISTS admin_find_accounts(text, text);
CREATE OR REPLACE FUNCTION admin_find_accounts(
    p_session text, p_q text, p_limit integer DEFAULT 20, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (account_id uuid, email_masked text, created_at timestamptz,
               deleted_at timestamptz, disconnected_at timestamptz,
               locations integer, active_locations integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    q text := btrim(coalesce(p_q, ''));
    kind text;
    ids uuid[] := '{}';
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['soporte', 'finanzas', 'propietario'], false) a;
    IF length(q) NOT BETWEEN 3 AND 254 THEN
        RAISE EXCEPTION 'búsqueda demasiado corta o larga' USING ERRCODE = 'check_violation';
    END IF;
    IF q ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
        kind := 'account_id';
        ids := ARRAY(SELECT x.id FROM public.account x WHERE x.id = q::uuid);
    ELSIF q ~ '^loc_[0-9a-z]{4,32}$' THEN
        kind := 'location_id';
        ids := ARRAY(SELECT l.account_id FROM public.location l WHERE l.id = q);
    ELSIF q ~ '^[^[:space:]@]+@[^[:space:]@]+$' THEN
        kind := 'email';
        ids := ARRAY(SELECT x.id FROM public.account x
                     WHERE x.email <> '' AND lower(x.email) = lower(q));
    ELSIF pg_catalog.to_regclass('public.billing_customer') IS NOT NULL THEN
        kind := 'billing';
        EXECUTE 'SELECT coalesce(array_agg(DISTINCT c.account_id), ''{}'') FROM public.billing_customer c'
                ' WHERE upper(c.tax_id) = upper($1) OR c.legal_name ILIKE ''%'' || $1 || ''%'''
            INTO ids USING q;
    ELSE
        kind := 'none';
    END IF;
    PERFORM public.admin_audit_write(who, 'find_accounts', NULL, NULL, NULL,
        jsonb_build_object('kind', kind, 'found', cardinality(ids)));
    RETURN QUERY
    SELECT x.id, public.admin_mask_email(x.email), x.created_at, x.deleted_at,
           x.disconnected_at,
           (SELECT count(*)::integer FROM public.location l
            WHERE l.account_id = x.id AND l.removed_at IS NULL),
           (SELECT count(*)::integer FROM public.location l
            WHERE l.account_id = x.id AND l.active_since IS NOT NULL)
    FROM public.account x
    WHERE x.id = ANY (ids)
      AND (p_after_key IS NULL
           OR (x.created_at, x.id) > (p_after_key::timestamptz, p_after_id::uuid))
    ORDER BY x.created_at, x.id
    LIMIT least(greatest(coalesce(p_limit, 20), 1), 201);
END
$$;

-- Las fichas de una cuenta, con su nombre (dato de Google): solo aquí y con la referencia
-- del ticket, que se audita con la lectura (política de datos de usuario de Google:
-- lecturas humanas solo para soporte, seguridad u obligación legal).
DROP FUNCTION IF EXISTS admin_account_detail(text, uuid, text);
CREATE OR REPLACE FUNCTION admin_account_detail(
    p_session text, p_account uuid, p_ticket_ref text, p_limit integer DEFAULT 50,
    p_after_key text DEFAULT NULL, p_after_id text DEFAULT NULL)
RETURNS TABLE (location_id text, title text, active_since timestamptz, plan text,
               deactivated_at timestamptz, removed_at timestamptz, verified boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    ticket text := btrim(coalesce(p_ticket_ref, ''));
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['soporte', 'finanzas', 'propietario'], false) a;
    IF length(ticket) NOT BETWEEN 3 AND 500 OR ticket ~ '[[:cntrl:]]'
       OR ticket ~ '[^[:space:]]+@[^[:space:]]+' THEN
        RAISE EXCEPTION 'hace falta la referencia del ticket, sin direcciones de correo'
            USING ERRCODE = 'check_violation';
    END IF;
    PERFORM public.admin_audit_write(who, 'account_detail_read', p_account, NULL, ticket, NULL);
    RETURN QUERY
    SELECT l.id, l.title, l.active_since, l.plan, l.deactivated_at, l.removed_at, l.verified
    FROM public.location l
    WHERE l.account_id = p_account AND (p_after_key IS NULL OR l.id > p_after_key)
    ORDER BY l.id
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201);
END
$$;

-- Baja por correo, paso 2 (sección 6.4): compara el From: del mensaje pegado con el correo
-- de la cuenta sin enseñarlo. Se guardan el resultado, el dominio del From: y la huella
-- del Message-ID (SHA-256 en hexadecimal, la calcula el proceso), sin direcciones. Es la
-- prueba que exige fichaitor_email_offboarding (30 minutos).
CREATE OR REPLACE FUNCTION admin_email_matches(
    p_session text, p_account uuid, p_from text, p_message_id_hash text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    sender text := lower(btrim(coalesce(p_from, '')));
    domain text;
    matched boolean;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false) a;
    IF coalesce(p_message_id_hash, '') !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'falta la huella del Message-ID' USING ERRCODE = 'check_violation';
    END IF;
    domain := split_part(sender, '@', 2);
    IF domain !~ '^[a-z0-9.-]{1,253}$' THEN
        domain := '';
    END IF;
    matched := EXISTS (SELECT 1 FROM public.account x
                       WHERE x.id = p_account AND x.deleted_at IS NULL AND x.email <> ''
                         AND lower(x.email) = sender);
    PERFORM public.admin_audit_write(who, 'offboarding_email_checked', p_account, NULL, NULL,
        jsonb_build_object('match', matched, 'domain', domain,
                           'message_id_sha256', p_message_id_hash));
    RETURN matched;
END
$$;

-- Baja por correo (sección 6.4, paso 4). La misma transacción que scripts/baja_manual.py:
-- - sin p_immediate: baja programada 48 horas (MIN_GRACE_HOURS), reason = 'email_request',
--   con el correo de «baja programada» y su enlace de restauración a la dirección de la
--   cuenta (B15 de docs/seguridad/revision-2026-11-12-c.md);
-- - con p_immediate (solo el propietario de la base, el camino de baja_manual.py
--   --inmediata): efectiva al momento, sin correo ni enlace.
-- En los dos casos: sustituye una baja del panel programada, cancela las propuestas sin
-- confirmar y libera los carriles; con 'cuenta', cierra las sesiones, desvincula los chats
-- y gasta los códigos de vinculación; una línea en audit_log (offboarding_email_request,
-- con la referencia en text); el job apply_offboarding a la fecha. Y además una fila en
-- admin_audit_log.
-- Desde el backoffice (p_session): roles soporte o propietario, TOTP de menos de 5 minutos
-- y una comparación del From: con resultado positivo de esa persona y esa cuenta en los
-- últimos 30 minutos (admin_email_matches). Sin p_session: solo el propietario de la base.
-- La propietaria de la función es la dueña de las tablas: los disparadores de guardia
-- (offboarding_guard) la eximen, igual que al script.
-- Los errores de datos son check_violation con el mismo texto que el script.
CREATE OR REPLACE FUNCTION fichaitor_email_offboarding(
    p_session text, p_account uuid, p_scope text, p_location text, p_ticket_ref text,
    p_immediate boolean DEFAULT false, OUT offboarding_id uuid, OUT delete_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    ticket text := btrim(coalesce(p_ticket_ref, ''));
    sub text;
    mail text;
    disconnected timestamptz;
    locs text[];
    at_now timestamptz := now();
    due timestamptz;
    token text;
    leave_id uuid;
BEGIN
    IF p_session IS NULL THEN
        IF NOT public.admin_caller_is_owner() THEN
            RAISE EXCEPTION 'solo el propietario de la base o el backoffice'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    ELSE
        SELECT a.actor_id INTO who
        FROM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], true) a;
        IF p_immediate THEN
            RAISE EXCEPTION 'la baja inmediata solo la tramita el propietario de la base'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.admin_audit_log g
            WHERE g.admin_user = who AND g.account_id = p_account
              AND g.action = 'offboarding_email_checked' AND g.detail->>'match' = 'true'
              AND g.at > now() - interval '30 minutes') THEN
            RAISE EXCEPTION 'falta comprobar el remitente del correo (DMARC y From:)'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF p_scope IS NULL OR p_scope NOT IN ('ficha', 'cuenta') THEN
        RAISE EXCEPTION 'alcance desconocido' USING ERRCODE = 'check_violation';
    END IF;
    IF length(ticket) NOT BETWEEN 3 AND 500 OR ticket ~ '[[:cntrl:]]' THEN
        RAISE EXCEPTION 'El motivo debe tener entre 3 y 500 caracteres, en una línea.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF ticket ~ '[^[:space:]]+@[^[:space:]]+' THEN
        RAISE EXCEPTION 'El motivo no puede llevar una dirección de correo: usa la referencia.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_scope = 'cuenta' AND p_location IS NOT NULL THEN
        RAISE EXCEPTION '--location solo vale con --scope ficha.' USING ERRCODE = 'check_violation';
    END IF;
    IF p_location IS NOT NULL AND p_location !~ '^loc_[0-9a-z]{4,32}$' THEN
        RAISE EXCEPTION '--location no es un identificador de ficha (loc_...).'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT x.google_sub, x.email, x.disconnected_at INTO sub, mail, disconnected
    FROM public.account x WHERE x.id = p_account AND x.deleted_at IS NULL
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No hay ninguna cuenta activa con ese correo.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_scope = 'ficha' AND disconnected IS NOT NULL THEN
        RAISE EXCEPTION 'La cuenta ya está desconectada de Google: no hay ficha que dar de baja.'
            USING ERRCODE = 'check_violation';
    END IF;
    locs := ARRAY(SELECT l.id FROM public.location l
                  WHERE l.account_id = p_account AND l.removed_at IS NULL ORDER BY l.id);
    IF p_scope = 'ficha' THEN
        IF p_location IS NOT NULL AND NOT p_location = ANY (locs) THEN
            RAISE EXCEPTION 'Esa ficha no es de esta cuenta.' USING ERRCODE = 'check_violation';
        END IF;
        IF cardinality(locs) > 1 AND p_location IS NULL THEN
            RAISE EXCEPTION 'La cuenta tiene % fichas: indica cuál nombra el correo.',
                cardinality(locs) USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    due := CASE WHEN p_immediate THEN at_now ELSE at_now + interval '48 hours' END;
    IF NOT p_immediate THEN
        -- secrets.token_urlsafe(32): 32 bytes aleatorios en base64 para URL, sin relleno.
        token := rtrim(translate(encode(public.gen_random_bytes(32), 'base64'), '+/', '-_'), '=');
    END IF;
    -- Una baja del panel todavía programada queda sustituida, como hace la API.
    UPDATE public.offboarding o SET restored_at = at_now, restore_token_hash = NULL
    WHERE o.account_id = p_account AND o.restored_at IS NULL AND o.effective_at IS NULL;
    INSERT INTO public.offboarding
        (account_id, scope, google_sub, confirmed_at, delete_at, reason, restore_token_hash)
    VALUES (p_account, p_scope, sub, at_now, due, 'email_request',
            CASE WHEN token IS NULL THEN NULL
                 ELSE encode(public.digest(token, 'sha256'), 'hex') END)
    RETURNING id INTO leave_id;
    -- Lo mismo que al confirmar en el panel (B12): lo que esperaba en el chat no sale.
    UPDATE public.proposal p
    SET state = 'cancelada', outcome_reason = 'offboarding',
        step_token = encode(public.gen_random_bytes(3), 'hex'), message_refs = '{}'::jsonb
    WHERE p.account_id = p_account
      AND p.state IN ('programada', 'enviada', 'recordada', 'recopilando', 'por_confirmar');
    UPDATE public.conversation_lane c SET active_proposal_id = NULL, updated_at = now()
    WHERE c.account_id = p_account AND c.active_proposal_id IS NOT NULL;
    IF p_scope = 'cuenta' THEN
        UPDATE public.session s SET revoked_at = at_now
        WHERE s.account_id = p_account AND s.revoked_at IS NULL;
        UPDATE public.channel_identity c SET unlinked_at = at_now
        WHERE c.account_id = p_account AND c.unlinked_at IS NULL;
        UPDATE public.link_code k SET used_at = at_now
        WHERE k.account_id = p_account AND k.used_at IS NULL;
    END IF;
    INSERT INTO public.audit_log
        (written_by, event, account_id, action_id, action_type, location, review_id,
         gbp_review_id, text, confirmed_at, reason)
    VALUES (session_user, 'offboarding_email_request', p_account, leave_id::text,
            'offboarding_' || p_scope, coalesce(p_location, ''), '', '', ticket, at_now,
            'email_request');
    IF token IS NOT NULL THEN
        -- El mismo correo que manda el panel al confirmar, a la dirección de la cuenta.
        INSERT INTO public.job (kind, payload, max_attempts)
        VALUES ('send_mail',
                jsonb_build_object(
                    'template', 'offboarding_scheduled', 'to', mail, 'scope', p_scope,
                    'delete_at', to_char(due AT TIME ZONE 'UTC',
                                         'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
                    'token', token),
                3);
    END IF;
    INSERT INTO public.job (kind, payload, run_at, idempotency_key, max_attempts)
    VALUES ('apply_offboarding', jsonb_build_object('offboarding_id', leave_id::text), due,
            'apply_offboarding:' || leave_id::text, 5);
    PERFORM public.admin_audit_write(who, 'offboarding_email', p_account, leave_id::text, ticket,
        jsonb_build_object('scope', p_scope, 'location', p_location,
                           'immediate', p_immediate, 'delete_at', due));
    offboarding_id := leave_id;
    delete_at := due;
END
$$;

-- ---------------------------------------------------------------------------
-- Salud (sección 6.4): sin cargas ni textos. Roles soporte y propietario.

CREATE OR REPLACE FUNCTION admin_job_stats(p_session text)
RETURNS TABLE (kind text, state text, jobs bigint, oldest_run_at timestamptz, stuck bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false);
    RETURN QUERY
    SELECT j.kind, j.state, count(*), min(j.run_at),
           count(*) FILTER (WHERE j.state = 'running' AND j.locked_at < now() - interval '15 minutes')
    FROM public.job j GROUP BY j.kind, j.state ORDER BY j.kind, j.state;
END
$$;

-- Los jobs fallidos, de lo más nuevo a lo más viejo, con el error recortado y sin la
-- carga. Clave: finished_at (sin fecha, al final) e id; índice job_failed_keyset.
DROP FUNCTION IF EXISTS admin_failed_jobs(text);
CREATE OR REPLACE FUNCTION admin_failed_jobs(
    p_session text, p_limit integer DEFAULT 50, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (job_id uuid, kind text, attempts integer, finished_at timestamptz,
               last_error text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false);
    RETURN QUERY
    SELECT j.id, j.kind, j.attempts, j.finished_at, left(j.last_error, 200)
    FROM public.job j
    WHERE j.state = 'failed'
      AND (p_after_key IS NULL
           OR (coalesce(j.finished_at, '-infinity'::timestamptz), j.id)
              < (p_after_key::timestamptz, p_after_id::uuid))
    ORDER BY coalesce(j.finished_at, '-infinity'::timestamptz) DESC, j.id DESC
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201);
END
$$;

-- Las pasadas de la retención, de la más nueva a la más vieja. Clave: at e id; índice
-- retention_run_keyset.
DROP FUNCTION IF EXISTS admin_retention_runs(text);
CREATE OR REPLACE FUNCTION admin_retention_runs(
    p_session text, p_limit integer DEFAULT 10, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (run_id bigint, at timestamptz, deleted jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false);
    RETURN QUERY
    SELECT r.id, r.at, r.deleted FROM public.retention_run r
    WHERE p_after_key IS NULL OR (r.at, r.id) < (p_after_key::timestamptz, p_after_id::bigint)
    ORDER BY r.at DESC, r.id DESC
    LIMIT least(greatest(coalesce(p_limit, 10), 1), 201);
END
$$;

-- Estado de la sincronización de las fichas de una cuenta (o de todas con error), sin los
-- hechos de la ficha.
-- De lo más nuevo a lo más viejo. Clave: status_at y el id de la ficha; índice
-- profile_summary_problem_keyset para las de todas las cuentas.
DROP FUNCTION IF EXISTS admin_sync_status(text, uuid);
CREATE OR REPLACE FUNCTION admin_sync_status(
    p_session text, p_account uuid, p_limit integer DEFAULT 50, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (account_id uuid, location_id text, status text, status_at timestamptz,
               synced_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false);
    RETURN QUERY
    SELECT a.id, l.id, ps.status, ps.status_at, ps.synced_at
    FROM public.account a
    JOIN public.location l ON l.account_id = a.id
    JOIN public.profile_summary ps ON ps.google_sub = a.google_sub AND ps.location = l.gbp_location
    WHERE ((p_account IS NULL AND ps.status <> 'ok') OR a.id = p_account)
      AND (p_after_key IS NULL OR (ps.status_at, l.id) < (p_after_key::timestamptz, p_after_id))
    ORDER BY ps.status_at DESC, l.id DESC
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201);
END
$$;

-- Los chats de una cuenta, sin direcciones.
-- Clave: verified_at e id, de lo más viejo a lo más nuevo.
DROP FUNCTION IF EXISTS admin_channel_links(text, uuid);
CREATE OR REPLACE FUNCTION admin_channel_links(
    p_session text, p_account uuid, p_limit integer DEFAULT 50, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (identity_id text, channel text, verified_at timestamptz,
               last_inbound_at timestamptz, unlinked_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'finanzas', 'propietario'], false);
    RETURN QUERY
    SELECT c.id, c.channel, c.verified_at, c.last_inbound_at, c.unlinked_at
    FROM public.channel_identity c
    WHERE c.account_id = p_account
      AND (p_after_key IS NULL OR (c.verified_at, c.id) > (p_after_key::timestamptz, p_after_id))
    ORDER BY c.verified_at, c.id
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201);
END
$$;

-- ---------------------------------------------------------------------------
-- Lista de espera (sección 6.4), solo el rol propietario y solo en totales anónimos: por
-- semana (lunes, hora de Madrid), canal, tramo de nota y origen. Las celdas de menos de 5
-- personas no salen. No hay totales por fila ni por columna: con ellos, restando, se
-- deducirían las celdas suprimidas.
-- Clave: la semana y el resto de la celda (canal|tramo|origen; el origen no lleva «|»).
DROP FUNCTION IF EXISTS admin_waitlist_totals(text);
CREATE OR REPLACE FUNCTION admin_waitlist_totals(
    p_session text, p_limit integer DEFAULT 100, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (week date, channel text, score_band text, source text, people bigint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    PERFORM public.admin_audit_write(who, 'waitlist_totals_read', NULL, NULL, NULL, NULL);
    RETURN QUERY
    SELECT t.week, t.channel, t.score_band, t.source, count(*)
    FROM (
        SELECT date_trunc('week', w.created_at AT TIME ZONE 'Europe/Madrid')::date AS week,
               w.channel,
               CASE WHEN w.score IS NULL THEN 'sin_nota'
                    WHEN w.score < 50 THEN '0-49'
                    WHEN w.score < 70 THEN '50-69'
                    WHEN w.score < 90 THEN '70-89'
                    ELSE '90-100' END AS score_band,
               coalesce(w.source, '') AS source
        FROM public.waitlist_entry w
    ) t
    GROUP BY t.week, t.channel, t.score_band, t.source
    HAVING count(*) >= 5
       AND (p_after_key IS NULL
            OR (t.week, t.channel || '|' || t.score_band || '|' || t.source)
               > (p_after_key::date, p_after_id))
    ORDER BY t.week, t.channel || '|' || t.score_band || '|' || t.source
    LIMIT least(greatest(coalesce(p_limit, 100), 1), 201);
END
$$;

-- ---------------------------------------------------------------------------
-- Personas y registro (sección 6.3: solo el rol propietario).

-- Por nombre de usuario (único, con su índice).
DROP FUNCTION IF EXISTS admin_users(text);
CREATE OR REPLACE FUNCTION admin_users(
    p_session text, p_limit integer DEFAULT 50, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (admin_user_id uuid, username text, role text, active boolean,
               must_enroll boolean, locked_until timestamptz, created_at timestamptz,
               password_changed_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['propietario'], false);
    RETURN QUERY
    SELECT u.id, u.username, u.role, u.active, u.must_enroll, u.locked_until, u.created_at,
           u.password_changed_at
    FROM public.admin_user u
    WHERE p_after_key IS NULL OR u.username > p_after_key
    ORDER BY u.username
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201);
END
$$;

-- El registro, de lo más nuevo a lo más viejo, por páginas (keyset por id, la clave
-- primaria; p_before: el id de la última fila de la página anterior). Hasta 501 filas: el
-- proceso pide una más de las que enseña. La propia lectura queda registrada.
CREATE OR REPLACE FUNCTION admin_audit_read(p_session text, p_limit integer, p_before bigint)
RETURNS TABLE (id bigint, at timestamptz, written_by name, admin_user uuid, username text,
               role text, action text, account_id uuid, target text, ticket_ref text,
               detail jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    PERFORM public.admin_audit_write(who, 'audit_read', NULL, NULL, NULL, NULL);
    RETURN QUERY
    SELECT g.id, g.at, g.written_by, g.admin_user, u.username, g.role, g.action, g.account_id,
           g.target, g.ticket_ref, g.detail
    FROM public.admin_audit_log g LEFT JOIN public.admin_user u ON u.id = g.admin_user
    WHERE p_before IS NULL OR g.id < p_before
    ORDER BY g.id DESC
    LIMIT least(greatest(coalesce(p_limit, 100), 1), 501);
END
$$;

-- ---------------------------------------------------------------------------
-- Paso 12 (backend del backoffice, app/admin). Mismo patrón que las anteriores: quien
-- llama es miembro de fichaitor_admin, admin_actor comprueba la sesión, el rol y, en las
-- acciones sensibles, un TOTP de menos de 5 minutos; cada lectura y cada acción deja su
-- fila en admin_audit_log en la misma transacción. Las que tocan tablas de cobro son
-- plpgsql: se pueden crear antes que schema_billing.sql (fallan al llamarlas, no al
-- crearlas). search_path = pg_catalog, public, pg_temp y nombres cualificados (M6).

-- La persona de una sesión, para comprobar su TOTP: la sesión a la espera del código
-- (abierta hace menos de 5 minutos) o una sesión confirmada, con actividad en los últimos
-- 30 minutos y sin alta pendiente (confirmación de una acción sensible). Ninguna fila si
-- no vale. Sin esta función el proceso tendría que fiarse del usuario que manda el
-- navegador junto al código, y con la contraseña de una persona y el TOTP de otra se
-- podría confirmar la sesión de la primera.
CREATE OR REPLACE FUNCTION admin_session_pending_user(p_token_hash text)
RETURNS TABLE (username text, totp_secret_enc text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.username, u.totp_secret_enc
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.last_seen_at > now() - interval '30 minutes'
      AND u.active AND NOT u.must_enroll AND u.totp_secret_enc IS NOT NULL
      AND (u.locked_until IS NULL OR u.locked_until <= now())
      AND (s.totp_verified_at IS NOT NULL OR s.created_at > now() - interval '5 minutes')
      AND pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER')
$$;

-- La persona de una sesión con el TOTP (o un código de recuperación) de hace menos de 5
-- minutos, también con el alta del TOTP pendiente: para la etiqueta del QR y el cifrado del
-- secreto nuevo (admin_set_totp). Ninguna fila si no vale.
CREATE OR REPLACE FUNCTION admin_session_username(p_token_hash text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT u.username
    FROM public.admin_session s JOIN public.admin_user u ON u.id = s.admin_user_id
    WHERE s.token_hash = p_token_hash AND s.revoked_at IS NULL AND s.expires_at > now()
      AND s.totp_verified_at > now() - interval '5 minutes' AND u.active
      AND (u.locked_until IS NULL OR u.locked_until <= now())
      AND pg_catalog.pg_has_role(session_user, 'fichaitor_admin', 'MEMBER')
$$;

-- La referencia del caso (interna): de 3 a p_max caracteres, en una línea y sin
-- direcciones de correo. Devuelve el texto sin espacios a los lados.
CREATE OR REPLACE FUNCTION admin_ticket(p_ticket text, p_max integer) RETURNS text
LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    ticket text := btrim(coalesce(p_ticket, ''));
BEGIN
    IF length(ticket) NOT BETWEEN 3 AND p_max OR ticket ~ '[[:cntrl:]]'
       OR ticket ~ '[^[:space:]]+@[^[:space:]]+' THEN
        RAISE EXCEPTION 'hace falta la referencia del ticket (de 3 a % caracteres, en una línea y sin direcciones de correo)',
            p_max USING ERRCODE = 'check_violation';
    END IF;
    RETURN ticket;
END
$$;

-- Resumen de una cuenta (sección 6.3, fila 2; todos los roles): plan, plazas, derechos,
-- bloqueo, activaciones y bajas. Sin el nombre de las fichas (admin_account_detail, con
-- ticket), sin direcciones y sin identificadores de Stripe (eso es de finanzas). El correo,
-- enmascarado.
CREATE OR REPLACE FUNCTION admin_account_overview(p_session text, p_account uuid)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
    list_key text;
    list_name text;
    page jsonb;
    nexts jsonb := '{}'::jsonb;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['soporte', 'finanzas', 'propietario'], false) a;
    IF NOT EXISTS (SELECT 1 FROM public.account x WHERE x.id = p_account) THEN
        RAISE EXCEPTION 'no existe esa cuenta' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM public.admin_audit_write(who, 'account_overview_read', p_account, NULL, NULL, NULL);
    SELECT jsonb_build_object(
        'account', (SELECT jsonb_build_object(
                        'id', x.id, 'email_masked', public.admin_mask_email(x.email),
                        'created_at', x.created_at, 'deleted_at', x.deleted_at,
                        'disconnected_at', x.disconnected_at)
                    FROM public.account x WHERE x.id = p_account),
        'blocked', EXISTS (SELECT 1 FROM public.account_billing_block b
                           WHERE b.account_id = p_account AND b.unblocked_at IS NULL)
    ) INTO result;
    -- Los listados, en su primera página (keyset; la siguiente, con admin_list).
    FOR list_key, list_name IN SELECT t.key, t.list FROM (VALUES
        ('locations', 'locations'),
        ('entitlements', 'entitlements'),
        ('seats', 'seats'),
        ('blocks', 'blocks'),
        ('activations', 'activations'),
        ('offboardings', 'offboardings')
    ) AS t (key, list) LOOP
        page := public.admin_list_page(list_name, p_account, 50, NULL, NULL);
        result := result || jsonb_build_object(list_key, page -> 'items');
        nexts := nexts || jsonb_build_object(list_key, page -> 'next');
    END LOOP;
    RETURN result || jsonb_build_object('next', nexts);
END
$$;

-- El último modo de Stripe visto (interna): el de los eventos que la API acepta
-- (FICHAITOR_STRIPE_LIVEMODE). Sirve para construir los enlaces al panel de Stripe sin
-- que admin.env lleve nada de Stripe. false si aún no hay eventos.
CREATE OR REPLACE FUNCTION admin_stripe_livemode() RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    RETURN coalesce((SELECT e.livemode FROM public.stripe_event e
                     ORDER BY e.received_at DESC LIMIT 1), false);
END
$$;

-- Cobro de una cuenta (sección 6.3, «Pagos, facturas, enlaces»: finanzas y propietario).
-- El espejo de billing_*, sin el PDF, sin la dirección y sin invoice_email (esos, con
-- admin_billing_contact y la referencia del ticket).
CREATE OR REPLACE FUNCTION admin_account_billing(p_session text, p_account uuid)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
    list_key text;
    list_name text;
    page jsonb;
    nexts jsonb := '{}'::jsonb;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['finanzas', 'propietario'], false) a;
    IF NOT EXISTS (SELECT 1 FROM public.account x WHERE x.id = p_account) THEN
        RAISE EXCEPTION 'no existe esa cuenta' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM public.admin_audit_write(who, 'account_billing_read', p_account, NULL, NULL, NULL);
    SELECT jsonb_build_object(
        'livemode', public.admin_stripe_livemode(),
        'customer', (SELECT jsonb_build_object(
                        'stripe_customer_id', c.stripe_customer_id, 'legal_name', c.legal_name,
                        'tax_id_type', c.tax_id_type, 'tax_id', c.tax_id,
                        'country', c.country, 'territory', c.territory,
                        'tax_treatment', c.tax_treatment, 'updated_at', c.updated_at)
                     FROM public.billing_customer c WHERE c.account_id = p_account)
    ) INTO result;
    -- Los listados, en su primera página (keyset; la siguiente, con admin_list).
    FOR list_key, list_name IN SELECT t.key, t.list FROM (VALUES
        ('seats', 'billing_seats'),
        ('charges', 'charges'),
        ('documents', 'documents'),
        ('refunds', 'refunds'),
        ('blocks', 'billing_blocks')
    ) AS t (key, list) LOOP
        page := public.admin_list_page(list_name, p_account, 50, NULL, NULL);
        result := result || jsonb_build_object(list_key, page -> 'items');
        nexts := nexts || jsonb_build_object(list_key, page -> 'next');
    END LOOP;
    RETURN result || jsonb_build_object('next', nexts);
END
$$;

-- La dirección fiscal y el correo de facturas de una cuenta (sección 6.6: «Los ve
-- finanzas, por función»): solo con la referencia del ticket, que se audita con la lectura.
CREATE OR REPLACE FUNCTION admin_billing_contact(p_session text, p_account uuid, p_ticket_ref text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    ticket text;
    result jsonb;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['finanzas', 'propietario'], false) a;
    ticket := public.admin_ticket(p_ticket_ref, 500);
    PERFORM public.admin_audit_write(who, 'billing_contact_read', p_account, NULL, ticket, NULL);
    SELECT jsonb_build_object(
        'legal_name', c.legal_name, 'tax_id', c.tax_id, 'address_line1', c.address_line1,
        'address_line2', c.address_line2, 'postal_code', c.postal_code, 'city', c.city,
        'province', c.province, 'country', c.country, 'invoice_email', c.invoice_email)
    INTO result
    FROM public.billing_customer c WHERE c.account_id = p_account;
    IF result IS NULL THEN
        RAISE EXCEPTION 'esa cuenta no tiene datos fiscales' USING ERRCODE = 'no_data_found';
    END IF;
    RETURN result;
END
$$;

-- Cola de revisión del cobro y salud del cobro (secciones 4.3, 4.4 y 6.4; finanzas y
-- propietario): facturas en revisión o pendientes desde hace más de una hora, plazas con
-- algo que revisar, reembolsos pendientes o fallidos, eventos huérfanos o sin procesar
-- desde hace más de una hora, derechos pagados sin factura y bloqueos abiertos. Solo
-- identificadores, códigos e importes.
CREATE OR REPLACE FUNCTION admin_billing_review(p_session text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
    list_key text;
    list_name text;
    page jsonb;
    nexts jsonb := '{}'::jsonb;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['finanzas', 'propietario'], false) a;
    PERFORM public.admin_audit_write(who, 'billing_review_read', NULL, NULL, NULL, NULL);
    SELECT jsonb_build_object(
        'livemode', public.admin_stripe_livemode(),
        'counts', jsonb_build_object(
            'events_unprocessed', (SELECT count(*) FROM public.stripe_event e
                                   WHERE e.processed_at IS NULL),
            'documents_pending', (SELECT count(*) FROM public.billing_document d
                                  WHERE d.state = 'pendiente'),
            'documents_overdue', (SELECT count(*) FROM public.billing_document d
                                  WHERE d.state = 'pendiente'
                                    AND d.created_at < now() - interval '1 hour'),
            'documents_review', (SELECT count(*) FROM public.billing_document d
                                 WHERE d.state = 'revision'),
            'blocks_open', (SELECT count(*) FROM public.account_billing_block b
                            WHERE b.unblocked_at IS NULL),
            'refunds_pending', (SELECT count(*) FROM public.billing_refund_request r
                                WHERE r.state = 'pendiente'),
            'paid_without_document', (SELECT count(*) FROM public.plan_entitlement p
                                      WHERE p.source = 'pago' AND NOT p.provisional
                                        AND NOT EXISTS (
                                            SELECT 1 FROM public.billing_document d
                                            WHERE d.kind = 'factura'
                                              AND d.stripe_invoice_id = p.external_ref)))
    ) INTO result;
    -- Los listados, en su primera página (keyset; la siguiente, con admin_list).
    FOR list_key, list_name IN SELECT t.key, t.list FROM (VALUES
        ('documents', 'review_documents'),
        ('seats', 'review_seats'),
        ('refunds', 'review_refunds'),
        ('events', 'review_events'),
        ('paid_without_document', 'review_paid_without_document'),
        ('blocks', 'review_blocks')
    ) AS t (key, list) LOOP
        page := public.admin_list_page(list_name, NULL, 50, NULL, NULL);
        result := result || jsonb_build_object(list_key, page -> 'items');
        nexts := nexts || jsonb_build_object(list_key, page -> 'next');
    END LOOP;
    RETURN result || jsonb_build_object('next', nexts);
END
$$;

-- Salud general (sección 6.4; soporte y propietario): estado de los resúmenes de las fichas
-- y chats vinculados por canal. Solo recuentos: sin hechos de la ficha ni direcciones.
CREATE OR REPLACE FUNCTION admin_health_summary(p_session text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false) a;
    PERFORM public.admin_audit_write(who, 'health_read', NULL, NULL, NULL, NULL);
    SELECT jsonb_build_object(
        'summaries', coalesce((SELECT jsonb_object_agg(t.status, t.n)
                     FROM (SELECT ps.status, count(*) AS n FROM public.profile_summary ps
                           GROUP BY ps.status) t), '{}'::jsonb),
        'summaries_stale', (SELECT count(*) FROM public.profile_summary ps
                            WHERE ps.synced_at IS NULL OR ps.synced_at < now() - interval '24 hours'),
        'channels', coalesce((SELECT jsonb_object_agg(t.channel, jsonb_build_object(
                                 'linked', t.linked, 'unlinked', t.unlinked))
                     FROM (SELECT c.channel,
                                  count(*) FILTER (WHERE c.unlinked_at IS NULL) AS linked,
                                  count(*) FILTER (WHERE c.unlinked_at IS NOT NULL) AS unlinked
                           FROM public.channel_identity c GROUP BY c.channel) t), '{}'::jsonb),
        'jobs_stuck', (SELECT count(*) FROM public.job j
                       WHERE j.state = 'running' AND j.locked_at < now() - interval '15 minutes'),
        'jobs_failed_24h', (SELECT count(*) FROM public.job j
                            WHERE j.state = 'failed' AND j.finished_at > now() - interval '24 hours')
    ) INTO result;
    RETURN result;
END
$$;

-- Conceder un derecho a mano (sección 6.4; solo propietario y con TOTP de menos de 5
-- minutos): 'cortesia', 'recomendacion', 'manual' o 'beta' (ampliar la beta es un derecho
-- 'beta' nuevo con un fin posterior). Los límites de plan_entitlement_guard (10 como mucho,
-- un fin de 2 años como mucho, empieza ahora, granted_by), comprobados aquí también: la
-- función es del propietario de las tablas y el disparador la eximiría.
CREATE OR REPLACE FUNCTION admin_grant_entitlement(
    p_session text, p_account uuid, p_plan text, p_quantity integer, p_source text,
    p_ends_at timestamptz, p_note_code text, p_ticket_ref text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    who_name text;
    ticket text;
    granted uuid;
    at_now timestamptz := now();
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['propietario'], true) a;
    ticket := public.admin_ticket(p_ticket_ref, 500);
    IF p_source IS NULL OR p_source NOT IN ('cortesia', 'recomendacion', 'manual', 'beta') THEN
        RAISE EXCEPTION 'origen no permitido: cortesia, recomendacion, manual o beta'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_plan IS NULL OR p_plan NOT IN ('pro', 'pro_whatsapp') THEN
        RAISE EXCEPTION 'plan desconocido' USING ERRCODE = 'check_violation';
    END IF;
    IF p_quantity IS NULL OR p_quantity NOT BETWEEN 1 AND 10 THEN
        RAISE EXCEPTION 'la cantidad va de 1 a 10' USING ERRCODE = 'check_violation';
    END IF;
    IF p_ends_at IS NULL OR p_ends_at <= at_now + interval '1 hour'
       OR p_ends_at > at_now + interval '2 years' THEN
        RAISE EXCEPTION 'el fin tiene que estar entre una hora y dos años desde ahora'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_note_code IS NOT NULL AND p_note_code !~ '^[a-z_]{1,40}$' THEN
        RAISE EXCEPTION 'nota no válida (minúsculas y guiones bajos)' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.account x WHERE x.id = p_account AND x.deleted_at IS NULL) THEN
        RAISE EXCEPTION 'no hay ninguna cuenta activa con ese id' USING ERRCODE = 'no_data_found';
    END IF;
    SELECT u.username INTO who_name FROM public.admin_user u WHERE u.id = who;
    INSERT INTO public.plan_entitlement
        (account_id, plan, quantity, starts_at, ends_at, source, granted_by, note_code)
    VALUES (p_account, p_plan, p_quantity, at_now, p_ends_at, p_source, who_name, p_note_code)
    RETURNING id INTO granted;
    PERFORM public.admin_audit_write(who, 'entitlement_granted', p_account, granted::text, ticket,
        jsonb_build_object('plan', p_plan, 'quantity', p_quantity, 'source', p_source,
                           'ends_at', p_ends_at, 'note_code', p_note_code));
    RETURN granted;
END
$$;

-- Recortar un derecho no pagado (sección 6.3; solo propietario y con TOTP): adelanta su
-- fin, nunca lo alarga. Un derecho 'pago' no se toca aquí (lo cierra el reembolso).
CREATE OR REPLACE FUNCTION admin_trim_entitlement(
    p_session text, p_entitlement uuid, p_ends_at timestamptz, p_ticket_ref text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    ticket text;
    row_account uuid;
    row_source text;
    row_start timestamptz;
    row_end timestamptz;
    new_end timestamptz;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['propietario'], true) a;
    ticket := public.admin_ticket(p_ticket_ref, 500);
    SELECT e.account_id, e.source, e.starts_at, e.ends_at
    INTO row_account, row_source, row_start, row_end
    FROM public.plan_entitlement e WHERE e.id = p_entitlement FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no existe ese derecho' USING ERRCODE = 'no_data_found';
    END IF;
    IF row_source = 'pago' THEN
        RAISE EXCEPTION 'un derecho pagado no se recorta aquí: se cierra con un reembolso'
            USING ERRCODE = 'check_violation';
    END IF;
    new_end := greatest(coalesce(p_ends_at, now()), now());
    IF new_end <= row_start THEN
        new_end := row_start + interval '1 second';
    END IF;
    IF new_end >= coalesce(row_end, 'infinity'::timestamptz) THEN
        RAISE EXCEPTION 'un derecho solo se recorta: el fin nuevo tiene que ser anterior'
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE public.plan_entitlement e SET ends_at = new_end WHERE e.id = p_entitlement;
    PERFORM public.admin_audit_write(who, 'entitlement_trimmed', row_account, p_entitlement::text,
        ticket, jsonb_build_object('source', row_source, 'ends_at', new_end, 'was', row_end));
END
$$;

-- Cerrar un bloqueo a mano (sección 3.6; solo propietario y con TOTP): unblocked_by =
-- 'admin'. Devuelve la cuenta.
CREATE OR REPLACE FUNCTION admin_close_block(p_session text, p_block bigint, p_ticket_ref text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    ticket text;
    blocked_account uuid;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['propietario'], true) a;
    ticket := public.admin_ticket(p_ticket_ref, 500);
    UPDATE public.account_billing_block b SET unblocked_at = now(), unblocked_by = 'admin'
    WHERE b.id = p_block AND b.unblocked_at IS NULL
    RETURNING b.account_id INTO blocked_account;
    IF blocked_account IS NULL THEN
        RAISE EXCEPTION 'no hay ningún bloqueo abierto con ese id' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM public.admin_audit_write(who, 'block_closed', blocked_account, p_block::text, ticket,
        NULL);
    RETURN blocked_account;
END
$$;

-- Reembolso excepcional (P2 y sección 3.4; finanzas o propietario, con TOTP): una fila en
-- billing_refund_request y el job billing_refund, que hace el proceso de cobro
-- (app/billing/refunds.py). p_amount NULL: lo que queda por devolver de esa factura. Una
-- sola petición pendiente por factura. La referencia del caso, de 3 a 80 caracteres (la
-- columna ticket_ref de la tabla).
CREATE OR REPLACE FUNCTION admin_request_refund(
    p_session text, p_stripe_invoice text, p_amount integer, p_reason text, p_ticket_ref text)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    who_name text;
    ticket text;
    charge_account uuid;
    paid integer;
    refunded integer;
    request uuid;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['finanzas', 'propietario'], true) a;
    ticket := public.admin_ticket(p_ticket_ref, 80);
    IF p_reason IS NULL
       OR p_reason NOT IN ('error_cobro', 'decision_comercial', 'cambio_tercero', 'disputa') THEN
        RAISE EXCEPTION 'motivo no permitido: error_cobro, decision_comercial, cambio_tercero o disputa'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT h.account_id, h.amount_paid, h.amount_refunded INTO charge_account, paid, refunded
    FROM public.billing_charge h WHERE h.stripe_invoice_id = p_stripe_invoice FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no hay ningún cobro con esa factura de Stripe' USING ERRCODE = 'no_data_found';
    END IF;
    IF paid - refunded <= 0 THEN
        RAISE EXCEPTION 'esa factura no tiene nada que devolver' USING ERRCODE = 'check_violation';
    END IF;
    IF p_amount IS NOT NULL AND (p_amount <= 0 OR p_amount > paid - refunded) THEN
        RAISE EXCEPTION 'el importe va de 1 a % céntimos', paid - refunded
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM public.billing_refund_request r
               WHERE r.stripe_invoice_id = p_stripe_invoice AND r.state = 'pendiente') THEN
        RAISE EXCEPTION 'ya hay un reembolso pendiente de esa factura' USING ERRCODE = 'check_violation';
    END IF;
    SELECT u.username INTO who_name FROM public.admin_user u WHERE u.id = who;
    INSERT INTO public.billing_refund_request
        (account_id, stripe_invoice_id, amount, reason, ticket_ref, requested_by)
    VALUES (charge_account, p_stripe_invoice, p_amount, p_reason, ticket, who_name)
    RETURNING id INTO request;
    INSERT INTO public.job (kind, payload, idempotency_key, max_attempts)
    VALUES ('billing_refund', jsonb_build_object('refund_request_id', request::text),
            'billing_refund:' || request::text, 5);
    PERFORM public.admin_audit_write(who, 'refund_requested', charge_account, p_stripe_invoice,
        ticket, jsonb_build_object('amount', p_amount, 'reason', p_reason,
                                   'refund_request_id', request));
    RETURN request;
END
$$;

-- Listados de una cuenta y de la cola de revisión, por páginas (keyset, nunca offset).
-- admin_list_page (interna, sin sesión ni registro): una página de un listado con nombre.
-- Cada listado tiene una clave de orden estable: una fecha (k) y su id, en el sentido del
-- listado; la página siguiente empieza justo después de (p_after_key, p_after_id). Una
-- fila que se inserta entre dos páginas no repite ninguna otra ni deja un hueco: o cae
-- antes del cursor (no sale) o después (sale en su sitio). Devuelve
-- {"items": [...], "next": {"key", "id"} o null}. Las consultas son texto fijo de este
-- archivo; p_list solo elige cuál.
CREATE OR REPLACE FUNCTION admin_list_page(
    p_list text, p_account uuid, p_limit integer, p_after_key text, p_after_id text)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    lim integer := least(greatest(coalesce(p_limit, 50), 1), 200);
    dir text;        -- ASC o DESC
    k text;          -- la fecha de la clave
    id_expr text;    -- el id de la clave, en su tipo
    id_type text;
    item text;
    src text;
    cond text;
    q text;
    page jsonb;
    n integer;
BEGIN
    CASE p_list
    WHEN 'locations' THEN
        dir := 'ASC'; k := '''epoch''::timestamptz'; id_expr := 'l.id'; id_type := 'text';
        src := 'public.location l'; cond := 'l.account_id = $1';
        item := 'jsonb_build_object(''id'', l.id, ''plan'', l.plan, ''active_since'', l.active_since,
                 ''deactivated_at'', l.deactivated_at, ''removed_at'', l.removed_at,
                 ''verified'', l.verified)';
    WHEN 'entitlements' THEN
        dir := 'DESC'; k := 'e.starts_at'; id_expr := 'e.id'; id_type := 'uuid';
        src := 'public.plan_entitlement e'; cond := 'e.account_id = $1';
        item := 'jsonb_build_object(''id'', e.id, ''plan'', e.plan, ''quantity'', e.quantity,
                 ''starts_at'', e.starts_at, ''ends_at'', e.ends_at, ''source'', e.source,
                 ''seat_id'', e.seat_id, ''provisional'', e.provisional,
                 ''granted_by'', e.granted_by, ''note_code'', e.note_code,
                 ''current'', e.starts_at <= now() AND (e.ends_at IS NULL OR e.ends_at > now()))';
    WHEN 'seats' THEN
        dir := 'ASC'; k := 's.created_at'; id_expr := 's.id'; id_type := 'text';
        src := 'public.billing_seat s'; cond := 's.account_id = $1';
        item := 'jsonb_build_object(''id'', s.id, ''plan'', s.plan, ''collection'', s.collection,
                 ''location_id'', s.location_id, ''status'', s.status,
                 ''current_period_start'', s.current_period_start,
                 ''current_period_end'', s.current_period_end,
                 ''cancel_at_period_end'', s.cancel_at_period_end,
                 ''next_price_key'', s.next_price_key, ''trial_ends_at'', s.trial_ends_at,
                 ''review_reason'', s.review_reason, ''created_at'', s.created_at)';
    WHEN 'blocks' THEN
        dir := 'DESC'; k := 'b.blocked_at'; id_expr := 'b.id'; id_type := 'bigint';
        src := 'public.account_billing_block b'; cond := 'b.account_id = $1';
        item := 'jsonb_build_object(''id'', b.id, ''blocked_at'', b.blocked_at, ''reason'', b.reason,
                 ''seat_id'', b.seat_id, ''unblocked_at'', b.unblocked_at,
                 ''unblocked_by'', b.unblocked_by)';
    WHEN 'activations' THEN
        dir := 'DESC'; k := 'g.at'; id_expr := 'g.id'; id_type := 'bigint';
        src := 'public.location_activation_log g'; cond := 'g.account_id = $1';
        item := 'jsonb_build_object(''at'', g.at, ''location_id'', g.location_id,
                 ''event'', g.event, ''plan'', g.plan, ''reason'', g.reason)';
    WHEN 'offboardings' THEN
        dir := 'DESC'; k := 'o.confirmed_at'; id_expr := 'o.id'; id_type := 'uuid';
        src := 'public.offboarding o'; cond := 'o.account_id = $1';
        item := 'jsonb_build_object(''id'', o.id, ''scope'', o.scope, ''confirmed_at'', o.confirmed_at,
                 ''delete_at'', o.delete_at, ''restored_at'', o.restored_at,
                 ''effective_at'', o.effective_at, ''reason'', o.reason)';
    WHEN 'billing_seats' THEN
        dir := 'ASC'; k := 's.created_at'; id_expr := 's.id'; id_type := 'text';
        src := 'public.billing_seat s'; cond := 's.account_id = $1';
        item := 'jsonb_build_object(''id'', s.id, ''plan'', s.plan, ''collection'', s.collection,
                 ''stripe_subscription_id'', s.stripe_subscription_id,
                 ''location_id'', s.location_id, ''status'', s.status,
                 ''current_period_end'', s.current_period_end,
                 ''cancel_at_period_end'', s.cancel_at_period_end,
                 ''next_price_key'', s.next_price_key, ''review_reason'', s.review_reason)';
    WHEN 'charges' THEN
        dir := 'DESC'; k := 'h.created_at'; id_expr := 'h.stripe_invoice_id'; id_type := 'text';
        src := 'public.billing_charge h'; cond := 'h.account_id = $1';
        item := 'jsonb_build_object(''stripe_invoice_id'', h.stripe_invoice_id, ''seat_id'', h.seat_id,
                 ''status'', h.status, ''billing_reason'', h.billing_reason,
                 ''currency'', h.currency, ''subtotal'', h.subtotal, ''tax'', h.tax,
                 ''total'', h.total, ''amount_paid'', h.amount_paid,
                 ''amount_refunded'', h.amount_refunded, ''price_key'', h.price_key,
                 ''period_start'', h.period_start, ''period_end'', h.period_end,
                 ''paid_at'', h.paid_at, ''payment_intent_id'', h.payment_intent_id,
                 ''charge_id'', h.charge_id, ''disputed'', h.disputed)';
    WHEN 'documents' THEN
        dir := 'DESC'; k := 'd.created_at'; id_expr := 'd.id'; id_type := 'uuid';
        src := 'public.billing_document d'; cond := 'd.account_id = $1';
        item := 'jsonb_build_object(''id'', d.id, ''kind'', d.kind, ''stripe_invoice_id'', d.stripe_invoice_id,
                 ''stripe_refund_id'', d.stripe_refund_id, ''state'', d.state,
                 ''odoo_move_id'', d.odoo_move_id, ''number'', d.number,
                 ''amount_untaxed'', d.amount_untaxed, ''amount_tax'', d.amount_tax,
                 ''amount_total'', d.amount_total, ''verifactu_state'', d.verifactu_state,
                 ''review_reason'', d.review_reason, ''created_at'', d.created_at,
                 ''issued_at'', d.issued_at)';
    WHEN 'refunds' THEN
        dir := 'DESC'; k := 'r.created_at'; id_expr := 'r.id'; id_type := 'uuid';
        src := 'public.billing_refund_request r'; cond := 'r.account_id = $1';
        item := 'jsonb_build_object(''id'', r.id, ''stripe_invoice_id'', r.stripe_invoice_id,
                 ''amount'', r.amount, ''reason'', r.reason, ''ticket_ref'', r.ticket_ref,
                 ''requested_by'', r.requested_by, ''state'', r.state,
                 ''stripe_refund_id'', r.stripe_refund_id, ''created_at'', r.created_at,
                 ''finished_at'', r.finished_at)';
    WHEN 'billing_blocks' THEN
        dir := 'DESC'; k := 'b.blocked_at'; id_expr := 'b.id'; id_type := 'bigint';
        src := 'public.account_billing_block b'; cond := 'b.account_id = $1';
        item := 'jsonb_build_object(''id'', b.id, ''blocked_at'', b.blocked_at, ''reason'', b.reason,
                 ''stripe_invoice_id'', b.stripe_invoice_id, ''seat_id'', b.seat_id,
                 ''unblocked_at'', b.unblocked_at, ''unblocked_by'', b.unblocked_by)';
    WHEN 'review_documents' THEN
        dir := 'ASC'; k := 'd.created_at'; id_expr := 'd.id'; id_type := 'uuid';
        src := 'public.billing_document d';
        cond := 'd.state IN (''revision'', ''pendiente'')
                 AND (d.state = ''revision'' OR d.created_at < now() - interval ''1 hour'')';
        item := 'jsonb_build_object(''id'', d.id, ''account_id'', d.account_id, ''kind'', d.kind,
                 ''stripe_invoice_id'', d.stripe_invoice_id,
                 ''stripe_refund_id'', d.stripe_refund_id, ''state'', d.state,
                 ''odoo_move_id'', d.odoo_move_id, ''number'', d.number,
                 ''amount_total'', d.amount_total, ''verifactu_state'', d.verifactu_state,
                 ''review_reason'', d.review_reason, ''created_at'', d.created_at)';
    WHEN 'review_seats' THEN
        dir := 'ASC'; k := 's.updated_at'; id_expr := 's.id'; id_type := 'text';
        src := 'public.billing_seat s'; cond := 's.review_reason IS NOT NULL';
        item := 'jsonb_build_object(''id'', s.id, ''account_id'', s.account_id, ''plan'', s.plan,
                 ''status'', s.status, ''stripe_subscription_id'', s.stripe_subscription_id,
                 ''review_reason'', s.review_reason)';
    WHEN 'review_refunds' THEN
        dir := 'DESC'; k := 'r.created_at'; id_expr := 'r.id'; id_type := 'uuid';
        src := 'public.billing_refund_request r'; cond := 'r.state IN (''pendiente'', ''fallido'')';
        item := 'jsonb_build_object(''id'', r.id, ''account_id'', r.account_id,
                 ''stripe_invoice_id'', r.stripe_invoice_id, ''amount'', r.amount,
                 ''reason'', r.reason, ''state'', r.state, ''requested_by'', r.requested_by,
                 ''created_at'', r.created_at)';
    WHEN 'review_events' THEN
        dir := 'DESC'; k := 'e.received_at'; id_expr := 'e.id'; id_type := 'text';
        src := 'public.stripe_event e';
        cond := '(e.outcome = ''orphan'' OR e.processed_at IS NULL)
                 AND (e.outcome = ''orphan'' OR e.received_at < now() - interval ''1 hour'')';
        item := 'jsonb_build_object(''id'', e.id, ''type'', e.type, ''object_id'', e.object_id,
                 ''received_at'', e.received_at, ''processed_at'', e.processed_at,
                 ''outcome'', e.outcome)';
    WHEN 'review_paid_without_document' THEN
        dir := 'ASC'; k := 'p.starts_at'; id_expr := 'p.id'; id_type := 'uuid';
        src := 'public.plan_entitlement p';
        cond := 'p.source = ''pago'' AND NOT p.provisional
                 AND NOT EXISTS (SELECT 1 FROM public.billing_document d
                                 WHERE d.kind = ''factura'' AND d.stripe_invoice_id = p.external_ref)';
        item := 'jsonb_build_object(''entitlement_id'', p.id, ''account_id'', p.account_id,
                 ''stripe_invoice_id'', p.external_ref, ''seat_id'', p.seat_id,
                 ''starts_at'', p.starts_at)';
    WHEN 'review_blocks' THEN
        dir := 'ASC'; k := 'b.blocked_at'; id_expr := 'b.id'; id_type := 'bigint';
        src := 'public.account_billing_block b'; cond := 'b.unblocked_at IS NULL';
        item := 'jsonb_build_object(''id'', b.id, ''account_id'', b.account_id,
                 ''blocked_at'', b.blocked_at, ''reason'', b.reason,
                 ''stripe_invoice_id'', b.stripe_invoice_id, ''seat_id'', b.seat_id)';
    ELSE
        RAISE EXCEPTION 'ese listado no existe' USING ERRCODE = 'check_violation';
    END CASE;
    q := format(
        'SELECT coalesce(jsonb_agg(jsonb_build_object(''k'', s.k, ''id'', s.id, ''item'', s.item)'
        '                 ORDER BY s.k %1$s, s.kid %1$s), ''[]''::jsonb)'
        ' FROM (SELECT %2$s AS k, %3$s AS kid, (%3$s)::text AS id, %4$s AS item'
        '       FROM %5$s'
        '       WHERE (%6$s)'
        '         AND ($2::text IS NULL OR (%2$s, %3$s) %7$s ($2::timestamptz, $3::%8$s))'
        '       ORDER BY %2$s %1$s, %3$s %1$s LIMIT $4) s',
        dir, k, id_expr, item, src, cond, CASE dir WHEN 'ASC' THEN '>' ELSE '<' END, id_type);
    EXECUTE q INTO page USING p_account, p_after_key, p_after_id, lim + 1;
    n := jsonb_array_length(page);
    RETURN jsonb_build_object(
        'items', coalesce((SELECT jsonb_agg(x.e -> 'item' ORDER BY x.i)
                           FROM jsonb_array_elements(page) WITH ORDINALITY AS x (e, i)
                           WHERE x.i <= lim), '[]'::jsonb),
        'next', CASE WHEN n > lim THEN jsonb_build_object(
                    'key', page -> (lim - 1) ->> 'k', 'id', page -> (lim - 1) ->> 'id') END);
END
$$;

-- La página siguiente de un listado de la cuenta (Resumen: soporte, finanzas y
-- propietario; Cobro y revisión: finanzas y propietario). Cada lectura queda registrada
-- (list_read, con el nombre del listado).
CREATE OR REPLACE FUNCTION admin_list(
    p_session text, p_list text, p_account uuid, p_limit integer, p_after_key text,
    p_after_id text)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    roles text[];
BEGIN
    IF p_list IN ('locations', 'entitlements', 'seats', 'blocks', 'activations', 'offboardings') THEN
        roles := ARRAY['soporte', 'finanzas', 'propietario'];
    ELSIF p_list IN ('billing_seats', 'charges', 'documents', 'refunds', 'billing_blocks',
                     'review_documents', 'review_seats', 'review_refunds', 'review_events',
                     'review_paid_without_document', 'review_blocks') THEN
        roles := ARRAY['finanzas', 'propietario'];
    ELSE
        RAISE EXCEPTION 'ese listado no existe' USING ERRCODE = 'check_violation';
    END IF;
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, roles, false) a;
    IF p_list NOT LIKE 'review\_%' THEN
        IF p_account IS NULL OR NOT EXISTS (SELECT 1 FROM public.account x WHERE x.id = p_account) THEN
            RAISE EXCEPTION 'no existe esa cuenta' USING ERRCODE = 'no_data_found';
        END IF;
    END IF;
    PERFORM public.admin_audit_write(who, 'list_read', p_account, NULL, NULL,
        jsonb_build_object('list', p_list));
    -- Los de cobro llevan el modo de Stripe, para construir los enlaces a su panel.
    RETURN public.admin_list_page(p_list, p_account, p_limit, p_after_key, p_after_id)
        || CASE WHEN 'soporte' = ANY (roles) THEN '{}'::jsonb
                ELSE jsonb_build_object('livemode', public.admin_stripe_livemode()) END;
END
$$;

-- Índices que respaldan el orden de los listados globales (los de una cuenta filtran antes
-- por account_id, que ya tiene índice). Las tablas de cobro, solo si ya existen.
CREATE INDEX IF NOT EXISTS job_failed_keyset
    ON job ((coalesce(finished_at, '-infinity'::timestamptz)) DESC, id DESC)
    WHERE state = 'failed';
CREATE INDEX IF NOT EXISTS retention_run_keyset ON retention_run (at DESC, id DESC);
CREATE INDEX IF NOT EXISTS profile_summary_problem_keyset ON profile_summary (status_at DESC)
    WHERE status <> 'ok';
CREATE INDEX IF NOT EXISTS location_activation_log_keyset
    ON location_activation_log (account_id, at DESC, id DESC);
DO $$
BEGIN
    IF pg_catalog.to_regclass('public.billing_document') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS billing_document_review_keyset
            ON public.billing_document (created_at, id) WHERE state IN ('revision', 'pendiente');
        CREATE INDEX IF NOT EXISTS billing_seat_review_keyset
            ON public.billing_seat (updated_at, id) WHERE review_reason IS NOT NULL;
        CREATE INDEX IF NOT EXISTS billing_refund_request_review_keyset
            ON public.billing_refund_request (created_at DESC, id DESC)
            WHERE state IN ('pendiente', 'fallido');
        CREATE INDEX IF NOT EXISTS stripe_event_review_keyset
            ON public.stripe_event (received_at DESC, id DESC)
            WHERE outcome = 'orphan' OR processed_at IS NULL;
        CREATE INDEX IF NOT EXISTS plan_entitlement_paid_keyset
            ON public.plan_entitlement (starts_at, id) WHERE source = 'pago' AND NOT provisional;
        CREATE INDEX IF NOT EXISTS account_billing_block_open_keyset
            ON public.account_billing_block (blocked_at, id) WHERE unblocked_at IS NULL;
    END IF;
END
$$;

-- Métricas (sección 6.4; finanzas y propietario), siempre en totales, del periodo
-- [p_from, p_to). Las plazas en vigor van por precio: el proceso calcula el ARR y el MRR
-- con los precios del dominio (app/domain/billing.py, price_cents), sin copiarlos aquí.
-- Solo lo que algo registra de verdad. Definiciones, quién registra cada cosa y cuándo
-- no, en docs/contrato-backoffice.md, «Métricas».
-- - Una plaza «contratada» es la que tiene un derecho 'pago': fichaitor-billing lo crea
--   cuando Stripe da por pagada su primera factura (también la de 0 € de la ventana de la
--   beta). Una plaza cuyo primer pago no llegó a completarse acaba 'canceled' sin derecho:
--   ni es plaza nueva ni es baja.
-- - Sin «plazas perdidas por impago»: el bloqueo no tiene límite y no se pierde la plaza
--   (decisión 9.0, 2 y 3, de docs/admin-facturacion.md). Lo que hay sin pagar son los
--   bloqueos abiertos.
-- - «Pagan» en la conversión: un cobro con dinero (amount_paid > 0). La plaza de la ventana
--   de la beta no paga hasta que acaba la ventana (decisión 9.0, 11).
-- - Cuentas de la beta: las que recibieron el derecho beta automático al crearse antes del
--   lanzamiento (granted_by vacío). Una ampliación de la beta desde el backoffice lleva
--   granted_by y no cambia la cohorte de la cuenta.
-- - Embudo: los eventos de funnel_event, cada uno con quien lo registra en el contrato.
--   'visit' lo registra la portada (POST /api/v1/public/visit) y 'connected', solo la
--   primera conexión de una identidad de Google (no cada inicio de sesión).
CREATE OR REPLACE FUNCTION admin_metrics(p_session text, p_from timestamptz, p_to timestamptz)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
BEGIN
    SELECT a.actor_id INTO who
    FROM public.admin_actor(p_session, ARRAY['finanzas', 'propietario'], false) a;
    IF p_from IS NULL OR p_to IS NULL OR p_to <= p_from OR p_to - p_from > interval '400 days' THEN
        RAISE EXCEPTION 'periodo no válido (como mucho 400 días)' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM public.admin_audit_write(who, 'metrics_read', NULL, NULL, NULL,
        jsonb_build_object('from', p_from, 'to', p_to));
    WITH contracted AS (
        SELECT s.id, s.account_id, s.status, s.created_at, s.updated_at
        FROM public.billing_seat s
        WHERE EXISTS (SELECT 1 FROM public.plan_entitlement p
                      WHERE p.seat_id = s.id AND p.source = 'pago')
    ),
    paying AS (
        SELECT DISTINCT c.account_id FROM public.billing_charge c
        WHERE c.status = 'paid' AND c.amount_paid > 0
    ),
    beta AS (
        SELECT DISTINCT e.account_id FROM public.plan_entitlement e
        WHERE e.source = 'beta' AND e.granted_by IS NULL
    ),
    free AS (
        SELECT x.id FROM public.account x
        WHERE x.created_at >= p_from AND x.created_at < p_to
          AND NOT EXISTS (SELECT 1 FROM beta b WHERE b.account_id = x.id)
    )
    SELECT jsonb_build_object(
        'seats_in_force', coalesce((SELECT jsonb_agg(jsonb_build_object(
                'plan', t.plan, 'next_price_key', t.next_price_key, 'seats', t.n)
                ORDER BY t.plan, t.next_price_key)
            FROM (SELECT s.plan, s.next_price_key, count(*) AS n FROM public.billing_seat s
                  WHERE s.status = 'active' AND NOT s.cancel_at_period_end
                  GROUP BY s.plan, s.next_price_key) t), '[]'::jsonb),
        'signups', jsonb_build_object(
            'accounts', (SELECT count(*) FROM public.account x
                         WHERE x.created_at >= p_from AND x.created_at < p_to),
            'first_seats', (SELECT count(*) FROM contracted s
                            WHERE s.created_at >= p_from AND s.created_at < p_to
                              AND NOT EXISTS (SELECT 1 FROM contracted o
                                              WHERE o.account_id = s.account_id
                                                AND o.created_at < s.created_at)),
            'new_seats', (SELECT count(*) FROM contracted s
                          WHERE s.created_at >= p_from AND s.created_at < p_to)),
        'churn', jsonb_build_object(
            'seats_canceled', (SELECT count(*) FROM contracted s
                               WHERE s.status = 'canceled' AND s.updated_at >= p_from
                                 AND s.updated_at < p_to),
            'seats_to_cancel', (SELECT count(*) FROM public.billing_seat s
                                WHERE s.status = 'active' AND s.cancel_at_period_end),
            'account_leaves', (SELECT count(DISTINCT o.account_id) FROM public.offboarding o
                               WHERE o.scope = 'cuenta' AND o.effective_at >= p_from
                                 AND o.effective_at < p_to)),
        'blocks', jsonb_build_object(
            'opened', (SELECT count(*) FROM public.account_billing_block b
                       WHERE b.blocked_at >= p_from AND b.blocked_at < p_to),
            'closed', (SELECT count(*) FROM public.account_billing_block b
                       WHERE b.unblocked_at >= p_from AND b.unblocked_at < p_to),
            'open_now', (SELECT count(*) FROM public.account_billing_block b
                         WHERE b.unblocked_at IS NULL)),
        'conversion', jsonb_build_object(
            'beta_accounts', (SELECT count(*) FROM beta),
            'beta_paid', (SELECT count(*) FROM beta b
                          WHERE EXISTS (SELECT 1 FROM paying p WHERE p.account_id = b.account_id)),
            'free_accounts', (SELECT count(*) FROM free),
            'free_paid', (SELECT count(*) FROM free f
                          WHERE EXISTS (SELECT 1 FROM paying p WHERE p.account_id = f.id))),
        'funnel', (SELECT jsonb_object_agg(k.kind, coalesce(f.n, 0))
                   FROM unnest(ARRAY['visit', 'diagnosis', 'screenshot_diagnosis', 'signup',
                                     'connected', 'checkout_started', 'paid']) AS k (kind)
                   LEFT JOIN (SELECT e.kind, count(*) AS n FROM public.funnel_event e
                              WHERE e.at >= p_from AND e.at < p_to GROUP BY e.kind) f
                     ON f.kind = k.kind)
    ) INTO result;
    RETURN result;
END
$$;

-- ---------------------------------------------------------------------------
-- Privilegios. Primero se retira todo al rol (también lo que conceden los demás archivos
-- de esquema, que no le conceden nada) y después se concede lo justo (sección 6.6).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM fichaitor_admin;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM fichaitor_admin;
REVOKE ALL ON admin_user, admin_recovery_code, admin_enrollment, admin_session, admin_audit_log
    FROM PUBLIC, fichaitor_api, fichaitor_worker;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_conversation') THEN
        REVOKE ALL ON admin_user, admin_recovery_code, admin_enrollment, admin_session,
            admin_audit_log FROM fichaitor_conversation;
    END IF;
END
$$;

GRANT SELECT (id, created_at, deleted_at, disconnected_at) ON account TO fichaitor_admin;
GRANT SELECT (id, account_id, active_since, plan, deactivated_at, removed_at, verified)
    ON location TO fichaitor_admin;
GRANT SELECT ON plan_entitlement, location_activation_log TO fichaitor_admin;
GRANT SELECT (id, account_id, scope, confirmed_at, delete_at, restored_at, effective_at,
              revoke_state, purged_at, reason)
    ON offboarding TO fichaitor_admin;

-- Las dos tablas tienen seguridad a nivel de fila (schema_worker.sql): el backoffice las
-- lee enteras.
DROP POLICY IF EXISTS plan_entitlement_admin_read ON plan_entitlement;
CREATE POLICY plan_entitlement_admin_read ON plan_entitlement FOR SELECT TO fichaitor_admin
    USING (true);
DROP POLICY IF EXISTS location_activation_log_admin_read ON location_activation_log;
CREATE POLICY location_activation_log_admin_read ON location_activation_log
    FOR SELECT TO fichaitor_admin USING (true);

-- Registro del backoffice: el proceso solo inserta sus acciones app_*, sin fijar at,
-- written_by ni role. Sin políticas de lectura, cambio ni borrado: negadas aunque alguien
-- concediera el privilegio por error.
GRANT INSERT (admin_user, action, account_id, target, ticket_ref, detail)
    ON admin_audit_log TO fichaitor_admin;
ALTER TABLE admin_audit_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS admin_audit_log_app_insert ON admin_audit_log;
CREATE POLICY admin_audit_log_app_insert ON admin_audit_log FOR INSERT TO fichaitor_admin
    WITH CHECK (action ~ '^app_[a-z_]{1,56}$');

-- Tablas de cobro (schema_billing.sql, paso 2), si ya existen: lectura, sin el PDF de
-- billing_document ni la dirección ni invoice_email de billing_customer (los ve finanzas
-- por función, en el paso 12). Lista blanca de columnas: una columna nueva no se
-- concede sola. Si la tabla tiene seguridad a nivel de fila, una política de lectura.
DO $$
DECLARE
    tab text;
    cols text;
BEGIN
    FOR tab, cols IN
        SELECT t.name, string_agg(quote_ident(c.attname), ', ' ORDER BY c.attnum)
        FROM (VALUES ('billing_seat'), ('billing_charge'), ('account_billing_block'),
                     ('stripe_event'), ('billing_document'), ('billing_customer')) AS t (name)
        JOIN pg_catalog.pg_attribute c
          ON c.attrelid = pg_catalog.to_regclass('public.' || t.name)
         AND c.attnum > 0 AND NOT c.attisdropped
        WHERE (t.name <> 'billing_document' OR c.attname <> 'pdf')
          AND (t.name <> 'billing_customer'
               OR c.attname IN ('account_id', 'stripe_customer_id', 'legal_name', 'tax_id_type',
                                'tax_id', 'tax_treatment', 'updated_at'))
        GROUP BY t.name
    LOOP
        EXECUTE format('GRANT SELECT (%s) ON public.%I TO fichaitor_admin', cols, tab);
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_class k
                   WHERE k.oid = pg_catalog.to_regclass('public.' || tab) AND k.relrowsecurity) THEN
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tab || '_admin_read', tab);
            EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT TO fichaitor_admin USING (true)',
                           tab || '_admin_read', tab);
        END IF;
    END LOOP;
END
$$;

-- Funciones: nadie las ejecuta salvo a quien se le conceden (por defecto, PUBLIC puede).
-- El backoffice, las de su sesión y las de sus pantallas. Las internas (admin_actor,
-- admin_audit_write, admin_fail, admin_caller_is_owner, admin_mask_email y el disparador) y
-- las admin_owner_* (scripts del propietario), nadie.
DO $$
DECLARE
    fn regprocedure;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND (p.proname LIKE 'admin\_%' OR p.proname = 'fichaitor_email_offboarding')
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM fichaitor_admin', fn);
    END LOOP;
END
$$;

GRANT EXECUTE ON FUNCTION
    admin_login_allowed(),
    admin_login_lookup(text),
    admin_login_failed(text, text),
    admin_session_open(text, text),
    admin_session_totp(text, bigint),
    admin_recovery_candidates(text),
    admin_session_recovery(text, bigint),
    admin_set_totp(text, text, bigint, text[]),
    admin_change_password(text, text),
    admin_rehash_password(text, text),
    admin_session_check(text),
    admin_session_must_enroll(text),
    admin_session_close(text),
    admin_enrollment_check(text),
    admin_enroll(text, text, text, bigint, text[]),
    admin_find_accounts(text, text, integer, text, text),
    admin_account_detail(text, uuid, text, integer, text, text),
    admin_email_matches(text, uuid, text, text),
    fichaitor_email_offboarding(text, uuid, text, text, text, boolean),
    admin_job_stats(text),
    admin_failed_jobs(text, integer, text, text),
    admin_retention_runs(text, integer, text, text),
    admin_sync_status(text, uuid, integer, text, text),
    admin_channel_links(text, uuid, integer, text, text),
    admin_waitlist_totals(text, integer, text, text),
    admin_users(text, integer, text, text),
    admin_audit_read(text, integer, bigint),
    -- Paso 12.
    admin_session_pending_user(text),
    admin_session_username(text),
    admin_account_overview(text, uuid),
    admin_account_billing(text, uuid),
    admin_billing_contact(text, uuid, text),
    admin_billing_review(text),
    admin_health_summary(text),
    admin_grant_entitlement(text, uuid, text, integer, text, timestamptz, text, text),
    admin_trim_entitlement(text, uuid, timestamptz, text),
    admin_close_block(text, bigint, text),
    admin_request_refund(text, text, integer, text, text),
    admin_metrics(text, timestamptz, timestamptz),
    admin_list(text, text, uuid, integer, text, text)
TO fichaitor_admin;

-- ---------------------------------------------------------------------------
-- Solo desarrollo local y pruebas: copiar a db/dev-init.sql (lo hace el paso 2 u 11).
-- En producción el usuario se crea aparte (deploy/init-roles.sh: fichaitor_admin_prod),
-- con su contraseña en un secreto.
--
-- DO $$
-- BEGIN
--     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_admin_dev') THEN
--         CREATE ROLE fichaitor_admin_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_admin;
--     END IF;
-- END
-- $$;

-- ---------------------------------------------------------------------------
-- Comunicaciones por correo desde el backoffice (2026-11-13). Diseño: anexo «Comunicaciones
-- por correo desde el backoffice» de docs/admin-facturacion.md; plantilla «comunicacion»:
-- brand/correo/LEEME.md, 12. Contrato: docs/contrato-backoffice.md, sección 10.
--
-- - Dos tipos: 'servicio' (a todo el público elegido, sin enlace de baja) y 'comercial'
--   (solo a quien no se ha dado de baja de las comerciales, con su enlace y las cabeceras
--   List-Unsubscribe de un clic; LSSI art. 21.2).
-- - Público: todas las cuentas activas, solo la beta, un plan o una cuenta. Siempre de
--   `account`: nunca la lista de espera ni direcciones escritas a mano. Nunca una cuenta
--   dada de baja ni con la baja de la cuenta programada.
-- - Se guarda la comunicación (texto, tipo, público, estado, recuentos y fechas), nunca la
--   lista de direcciones: las direcciones solo van en las cargas de los jobs send_mail, que
--   el worker vacía al enviar (o al fallar).
-- - Solo el rol propietario, con auditoría; confirmar el envío pide un TOTP de menos de 5
--   minutos y el número de destinatarios que se le enseñó.
-- - Retención: la comunicación, 12 meses (fichaitor_communication_purge, en la pasada
--   diaria del worker); la baja de comunicaciones comerciales, mientras exista la cuenta
--   (se borra con ella: al darse de baja la cuenta y por la clave ajena).
-- Funciones con search_path = pg_catalog, public, pg_temp y nombres cualificados (M6).

-- La dirección de la persona del backoffice, solo para recibir el envío de prueba de una
-- comunicación. La pone el propietario con `scripts/admin_usuario.py correo`. El backoffice
-- nunca la enseña entera.
ALTER TABLE admin_user ADD COLUMN IF NOT EXISTS email text;
ALTER TABLE admin_user DROP CONSTRAINT IF EXISTS admin_user_email_check;
ALTER TABLE admin_user ADD CONSTRAINT admin_user_email_check
    CHECK (length(email) <= 254 AND email ~ '^[^[:space:][:cntrl:]@]+@[^[:space:][:cntrl:]@]+\.[^[:space:][:cntrl:]@]+$');

-- Solo el propietario de la base (scripts/admin_usuario.py correo): la dirección de las
-- pruebas de una persona. NULL la quita. Se audita sin la dirección.
CREATE OR REPLACE FUNCTION admin_owner_set_email(p_username text, p_email text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    IF NOT public.admin_caller_is_owner() THEN
        RAISE EXCEPTION 'solo el propietario de la base' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE public.admin_user u SET email = nullif(lower(btrim(coalesce(p_email, ''))), '')
    WHERE u.username = lower(p_username) RETURNING u.id INTO who;
    IF who IS NULL THEN
        RAISE EXCEPTION 'no existe esa persona' USING ERRCODE = 'check_violation';
    END IF;
    PERFORM public.admin_audit_write(NULL, 'user_email_set', NULL, who::text, NULL,
        jsonb_build_object('set', p_email IS NOT NULL AND btrim(p_email) <> ''));
END
$$;

REVOKE ALL ON FUNCTION admin_owner_set_email(text, text) FROM PUBLIC;

-- Reglas del texto (LEEME.md, 12), también en la base: de 1 a 8 párrafos de hasta 1000
-- caracteres, sin caracteres de control salvo el salto de línea; y el enlace del botón,
-- solo https://fichaitor.es/…, sin espacios, comillas ni ángulos. Inmutables y sin datos.
CREATE OR REPLACE FUNCTION communication_paragraphs_ok(p text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT p IS NOT NULL AND pg_catalog.cardinality(p) BETWEEN 1 AND 8
       AND pg_catalog.array_ndims(p) = 1
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.unnest(p) AS t (x)
                       WHERE x IS NULL OR pg_catalog.length(pg_catalog.btrim(x)) = 0
                          OR pg_catalog.length(x) > 1000
                          OR pg_catalog.translate(x, E'\n', '') ~ '[[:cntrl:]]')
$$;

CREATE OR REPLACE FUNCTION communication_url_ok(p text) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT p IS NULL
        OR (pg_catalog.length(p) <= 500 AND p ~ '^https://fichaitor\.es/'
            AND p !~ '[[:space:][:cntrl:]"''<>`]' AND pg_catalog.strpos(p, pg_catalog.chr(92)) = 0)
$$;

-- Una comunicación. created_by y confirmed_by: la persona del backoffice (sin clave
-- ajena, como en la auditoría: la comunicación dura más que la persona). Estados:
-- borrador (se puede cambiar, probar y confirmar), enviando (jobs en cola) y enviada
-- (todos los correos han terminado: enviados, fallidos u omitidos).
-- recipients: los correos que se encolaron al confirmar; excluded: las cuentas del público
-- que no lo reciben porque se dieron de baja de las comerciales; skipped_count: las que se
-- dieron de baja (o de la cuenta) entre la confirmación y su envío.
CREATE TABLE IF NOT EXISTS admin_communication (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL CHECK (kind IN ('servicio', 'comercial')),
    subject          text NOT NULL CHECK (length(btrim(subject)) BETWEEN 1 AND 90
                                          AND subject !~ '[[:cntrl:]]'),
    preheader        text NOT NULL CHECK (length(btrim(preheader)) BETWEEN 1 AND 120
                                          AND preheader !~ '[[:cntrl:]]'),
    headline         text NOT NULL CHECK (length(btrim(headline)) BETWEEN 1 AND 40
                                          AND headline !~ '[[:cntrl:]]'
                                          AND right(headline, 1) <> '.'),
    paragraphs       text[] NOT NULL CHECK (communication_paragraphs_ok(paragraphs)),
    button_label     text CHECK (length(btrim(button_label)) BETWEEN 1 AND 30
                                 AND button_label !~ '[[:cntrl:]]'),
    button_url       text CHECK (communication_url_ok(button_url)),
    audience         text NOT NULL CHECK (audience IN ('todas', 'beta', 'plan', 'cuenta')),
    audience_plan    text CHECK (audience_plan IN ('gratis', 'pro', 'pro_whatsapp')),
    audience_account uuid,
    state            text NOT NULL DEFAULT 'borrador'
                     CHECK (state IN ('borrador', 'enviando', 'enviada')),
    created_by       uuid NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    test_sent_at     timestamptz,
    confirmed_by     uuid,
    confirmed_at     timestamptz,
    per_minute       integer CHECK (per_minute BETWEEN 1 AND 600),
    recipients       integer CHECK (recipients >= 0),
    excluded         integer CHECK (excluded >= 0),
    sent_count       integer NOT NULL DEFAULT 0 CHECK (sent_count >= 0),
    failed_count     integer NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count    integer NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    finished_at      timestamptz,
    CHECK ((button_label IS NULL) = (button_url IS NULL)),
    CHECK ((audience = 'plan') = (audience_plan IS NOT NULL)),
    CHECK ((audience = 'cuenta') = (audience_account IS NOT NULL)),
    CHECK ((state = 'borrador') = (confirmed_at IS NULL)),
    CHECK (state = 'borrador' OR (confirmed_by IS NOT NULL AND per_minute IS NOT NULL
                                  AND recipients IS NOT NULL AND excluded IS NOT NULL)),
    CHECK ((state = 'enviada') = (finished_at IS NOT NULL)),
    CHECK (sent_count + failed_count + skipped_count <= coalesce(recipients, 0))
);

-- El historial (de la más nueva a la más vieja) y la retención, por la misma clave.
CREATE INDEX IF NOT EXISTS admin_communication_keyset ON admin_communication (created_at, id);

-- Baja de las comunicaciones comerciales, por cuenta: una fila por cuenta que no quiere
-- recibirlas. La escribe la API (enlace del correo o un clic de List-Unsubscribe) con
-- fichaitor_communication_unsubscribe. source: 'enlace' (página de la web) o 'un_clic'
-- (el cliente de correo, RFC 8058); desde 2026-11-13 también 'signup' (la casilla del
-- alta) y 'settings' (Ajustes): consulta la migración del final. Se borra con la cuenta:
-- al darse de baja (disparador de abajo) y por la clave ajena si alguna vez se borra la
-- fila de la cuenta.
CREATE TABLE IF NOT EXISTS communication_optout (
    account_id uuid PRIMARY KEY REFERENCES account (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    source     text NOT NULL CHECK (source IN ('enlace', 'un_clic', 'signup', 'settings'))
);

-- Ningún rol de la aplicación lee ni escribe estas tablas directamente: solo por las
-- funciones de abajo. Con seguridad a nivel de fila y sin políticas, por si alguien
-- concediera algo por error.
ALTER TABLE admin_communication ENABLE ROW LEVEL SECURITY;
ALTER TABLE communication_optout ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON admin_communication, communication_optout
    FROM PUBLIC, fichaitor_api, fichaitor_worker, fichaitor_admin;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_conversation') THEN
        REVOKE ALL ON admin_communication, communication_optout FROM fichaitor_conversation;
    END IF;
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_billing') THEN
        REVOKE ALL ON admin_communication, communication_optout FROM fichaitor_billing;
    END IF;
END
$$;

-- La baja de comunicaciones se va con la cuenta: cuando se da de baja (deleted_at), en la
-- misma transacción.
CREATE OR REPLACE FUNCTION communication_optout_on_leave() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
        DELETE FROM public.communication_optout o WHERE o.account_id = NEW.id;
        -- La retirada de la oposición (2026-11-13, al final del archivo), también.
        DELETE FROM public.communication_optout_withdrawal w WHERE w.account_id = NEW.id;
    END IF;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION communication_optout_on_leave() FROM PUBLIC;
DROP TRIGGER IF EXISTS communication_optout_on_leave ON account;
CREATE TRIGGER communication_optout_on_leave AFTER UPDATE OF deleted_at ON account
    FOR EACH ROW EXECUTE FUNCTION communication_optout_on_leave();

-- ¿Recibe hoy esta cuenta comunicaciones? (interna). Activa: no dada de baja, con correo
-- y sin la baja de la cuenta programada. Una baja de ficha no cuenta: sigue siendo cliente.
CREATE OR REPLACE FUNCTION communication_account_ok(p_account uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.account x
        WHERE x.id = p_account AND x.deleted_at IS NULL AND x.email <> ''
          AND NOT EXISTS (SELECT 1 FROM public.offboarding o
                          WHERE o.account_id = x.id AND o.scope = 'cuenta'
                            AND o.restored_at IS NULL AND o.effective_at IS NULL))
$$;

-- El público de una comunicación (interna): cuentas activas del público, con su correo y
-- si se dieron de baja de las comerciales. Solo de `account`: ni la lista de espera ni
-- direcciones escritas a mano. Beta: con un derecho 'beta' en vigor. Plan: con al menos
-- una ficha activa en ese plan.
CREATE OR REPLACE FUNCTION communication_recipients(p_id uuid)
RETURNS TABLE (account_id uuid, email text, opted_out boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT x.id, x.email,
           EXISTS (SELECT 1 FROM public.communication_optout o WHERE o.account_id = x.id)
    FROM public.admin_communication c, public.account x
    WHERE c.id = p_id
      AND x.deleted_at IS NULL AND x.email <> ''
      AND NOT EXISTS (SELECT 1 FROM public.offboarding o
                      WHERE o.account_id = x.id AND o.scope = 'cuenta'
                        AND o.restored_at IS NULL AND o.effective_at IS NULL)
      AND CASE c.audience
            WHEN 'todas' THEN true
            WHEN 'beta' THEN EXISTS (
                SELECT 1 FROM public.plan_entitlement e
                WHERE e.account_id = x.id AND e.source = 'beta' AND e.starts_at <= now()
                  AND (e.ends_at IS NULL OR e.ends_at > now()))
            WHEN 'plan' THEN EXISTS (
                SELECT 1 FROM public.location l
                WHERE l.account_id = x.id AND l.active_since IS NOT NULL
                  AND l.plan = c.audience_plan)
            WHEN 'cuenta' THEN x.id = c.audience_account
            ELSE false
          END
$$;

-- Cuántos la recibirían ahora (interna): {"recipients", "excluded"}. En una comercial,
-- excluded son las cuentas del público dadas de baja de las comerciales.
CREATE OR REPLACE FUNCTION communication_count(p_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT jsonb_build_object(
        'recipients', count(*) FILTER (WHERE NOT (c.kind = 'comercial' AND r.opted_out)),
        'excluded', count(*) FILTER (WHERE c.kind = 'comercial' AND r.opted_out))
    FROM public.admin_communication c
    LEFT JOIN LATERAL public.communication_recipients(c.id) r ON true
    WHERE c.id = p_id AND r.account_id IS NOT NULL
$$;

-- Una comunicación entera, como la enseña el backoffice (interna).
CREATE OR REPLACE FUNCTION communication_json(p_id uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT jsonb_build_object(
        'communication_id', c.id, 'kind', c.kind, 'subject', c.subject,
        'preheader', c.preheader, 'headline', c.headline, 'paragraphs', to_jsonb(c.paragraphs),
        'button_label', c.button_label, 'button_url', c.button_url,
        'audience', c.audience, 'audience_plan', c.audience_plan,
        'audience_account', c.audience_account, 'state', c.state,
        'created_at', c.created_at, 'updated_at', c.updated_at,
        'created_by', (SELECT u.username FROM public.admin_user u WHERE u.id = c.created_by),
        'test_sent_at', c.test_sent_at,
        'test_is_current', c.test_sent_at IS NOT NULL AND c.test_sent_at >= c.updated_at,
        'confirmed_at', c.confirmed_at,
        'confirmed_by', (SELECT u.username FROM public.admin_user u WHERE u.id = c.confirmed_by),
        'per_minute', c.per_minute, 'recipients', c.recipients, 'excluded', c.excluded,
        'sent', c.sent_count, 'failed', c.failed_count, 'skipped', c.skipped_count,
        'finished_at', c.finished_at)
    FROM public.admin_communication c WHERE c.id = p_id
$$;

-- Crear (p_id NULL) o cambiar un borrador. Solo propietario. El texto llega ya comprobado
-- por el proceso (app/domain/communication.py); la base lo vuelve a comprobar con sus
-- CHECK. Una comunicación confirmada no cambia.
CREATE OR REPLACE FUNCTION admin_communication_save(
    p_session text, p_id uuid, p_kind text, p_subject text, p_preheader text,
    p_headline text, p_paragraphs text[], p_button_label text, p_button_url text,
    p_audience text, p_plan text, p_account uuid)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    saved uuid;
    plan text := CASE WHEN p_audience = 'plan' THEN p_plan END;
    acct uuid := CASE WHEN p_audience = 'cuenta' THEN p_account END;
    label text := nullif(btrim(coalesce(p_button_label, '')), '');
    link text := nullif(btrim(coalesce(p_button_url, '')), '');
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    IF p_kind IS NULL OR p_kind NOT IN ('servicio', 'comercial') THEN
        RAISE EXCEPTION 'El tipo es servicio o comercial.' USING ERRCODE = 'check_violation';
    END IF;
    IF p_audience IS NULL OR p_audience NOT IN ('todas', 'beta', 'plan', 'cuenta') THEN
        RAISE EXCEPTION 'El público es todas, beta, plan o cuenta.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_audience = 'plan' AND (plan IS NULL OR plan NOT IN ('gratis', 'pro', 'pro_whatsapp')) THEN
        RAISE EXCEPTION 'Elige el plan: gratis, pro o pro_whatsapp.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_audience = 'cuenta' AND NOT EXISTS (SELECT 1 FROM public.account x WHERE x.id = acct) THEN
        RAISE EXCEPTION 'No hay ninguna cuenta con ese id.' USING ERRCODE = 'no_data_found';
    END IF;
    IF (label IS NULL) <> (link IS NULL) THEN
        RAISE EXCEPTION 'El botón necesita la etiqueta y el enlace.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT public.communication_url_ok(link) THEN
        RAISE EXCEPTION 'El enlace del botón solo puede ser https://fichaitor.es/…'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT public.communication_paragraphs_ok(p_paragraphs) THEN
        RAISE EXCEPTION 'De 1 a 8 párrafos de hasta 1000 caracteres.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_id IS NULL THEN
        INSERT INTO public.admin_communication
            (kind, subject, preheader, headline, paragraphs, button_label, button_url,
             audience, audience_plan, audience_account, created_by)
        VALUES (p_kind, btrim(p_subject), btrim(p_preheader), btrim(p_headline), p_paragraphs,
                label, link, p_audience, plan, acct, who)
        RETURNING id INTO saved;
    ELSE
        UPDATE public.admin_communication c
        SET kind = p_kind, subject = btrim(p_subject), preheader = btrim(p_preheader),
            headline = btrim(p_headline), paragraphs = p_paragraphs, button_label = label,
            button_url = link, audience = p_audience, audience_plan = plan,
            audience_account = acct, updated_at = pg_catalog.clock_timestamp()
        WHERE c.id = p_id AND c.state = 'borrador'
        RETURNING c.id INTO saved;
        IF saved IS NULL THEN
            IF EXISTS (SELECT 1 FROM public.admin_communication c WHERE c.id = p_id) THEN
                RAISE EXCEPTION 'Esta comunicación ya se ha enviado: no se puede cambiar.'
                    USING ERRCODE = 'check_violation';
            END IF;
            RAISE EXCEPTION 'No existe esa comunicación.' USING ERRCODE = 'no_data_found';
        END IF;
    END IF;
    PERFORM public.admin_audit_write(who, 'communication_saved', acct, saved::text, NULL,
        jsonb_build_object('kind', p_kind, 'audience', p_audience, 'plan', plan,
                           'created', p_id IS NULL));
    RETURN saved;
END
$$;

-- Una comunicación, para la vista previa y la pantalla de confirmar. Solo propietario.
CREATE OR REPLACE FUNCTION admin_communication_get(p_session text, p_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    result := public.communication_json(p_id);
    IF result IS NULL THEN
        RAISE EXCEPTION 'No existe esa comunicación.' USING ERRCODE = 'no_data_found';
    END IF;
    PERFORM public.admin_audit_write(who, 'communication_read', NULL, p_id::text, NULL, NULL);
    RETURN result;
END
$$;

-- Cuántos la recibirían si se confirmara ahora: lo que se enseña antes de confirmar.
-- {"recipients", "excluded"}. Solo propietario. Nunca las direcciones.
CREATE OR REPLACE FUNCTION admin_communication_audience(p_session text, p_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    result jsonb;
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    IF NOT EXISTS (SELECT 1 FROM public.admin_communication c WHERE c.id = p_id) THEN
        RAISE EXCEPTION 'No existe esa comunicación.' USING ERRCODE = 'no_data_found';
    END IF;
    result := public.communication_count(p_id);
    PERFORM public.admin_audit_write(who, 'communication_audience_read', NULL, p_id::text,
                                     NULL, result);
    RETURN result;
END
$$;

-- Envío de prueba a la dirección de la persona conectada (admin_user.email), con
-- «[Prueba]» en el asunto. Solo de un borrador, 10 por persona y hora como mucho. Devuelve
-- la dirección enmascarada. Confirmar exige una prueba de la versión que se confirma.
CREATE OR REPLACE FUNCTION admin_communication_test(p_session text, p_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    mail text;
    current_state text;
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    SELECT c.state INTO current_state FROM public.admin_communication c WHERE c.id = p_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No existe esa comunicación.' USING ERRCODE = 'no_data_found';
    END IF;
    IF current_state <> 'borrador' THEN
        RAISE EXCEPTION 'Esta comunicación ya se ha enviado.' USING ERRCODE = 'check_violation';
    END IF;
    SELECT u.email INTO mail FROM public.admin_user u WHERE u.id = who;
    IF mail IS NULL THEN
        RAISE EXCEPTION 'No tienes dirección de correo para las pruebas: pídesela al propietario (scripts/admin_usuario.py correo).'
            USING ERRCODE = 'check_violation';
    END IF;
    IF (SELECT count(*) FROM public.admin_audit_log g
        WHERE g.admin_user = who AND g.action = 'communication_test_sent'
          AND g.at > now() - interval '1 hour') >= 10 THEN
        RAISE EXCEPTION 'Ya has enviado 10 pruebas en la última hora: espera un poco.'
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO public.job (kind, payload, max_attempts)
    VALUES ('send_mail',
            jsonb_build_object('template', 'comunicacion', 'to', mail,
                               'communication_id', p_id::text, 'test', true),
            3);
    UPDATE public.admin_communication c SET test_sent_at = pg_catalog.clock_timestamp()
    WHERE c.id = p_id;
    PERFORM public.admin_audit_write(who, 'communication_test_sent', NULL, p_id::text, NULL, NULL);
    RETURN jsonb_build_object('sent_to', public.admin_mask_email(mail));
END
$$;

-- Confirmar el envío (acción sensible: propietario y TOTP de menos de 5 minutos). Pide:
-- un borrador, una prueba de esta versión, y que p_expected sea el número de destinatarios
-- de ahora (el que se enseñó; si ha cambiado, se vuelve a enseñar). Encola un job
-- send_mail por destinatario, separados 60 / p_per_minute segundos (el ritmo, de 1 a 600
-- por minuto, lo pone el proceso con FICHAITOR_ADMIN_COMMUNICATION_PER_MINUTE). Cada carga
-- lleva la dirección y la cuenta, nada del texto: el worker lo lee al enviar y vacía la
-- carga. La clave de idempotencia (comunicación y cuenta) impide un segundo envío.
CREATE OR REPLACE FUNCTION admin_communication_confirm(
    p_session text, p_id uuid, p_expected integer, p_per_minute integer)
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
    c public.admin_communication%ROWTYPE;
    counts jsonb;
    total integer;
    left_out integer;
    at_now timestamptz := now();
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], true) a;
    IF p_per_minute IS NULL OR p_per_minute NOT BETWEEN 1 AND 600 THEN
        RAISE EXCEPTION 'El ritmo va de 1 a 600 correos por minuto.'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO c FROM public.admin_communication x WHERE x.id = p_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No existe esa comunicación.' USING ERRCODE = 'no_data_found';
    END IF;
    IF c.state <> 'borrador' THEN
        RAISE EXCEPTION 'Esta comunicación ya se ha enviado.' USING ERRCODE = 'check_violation';
    END IF;
    IF c.test_sent_at IS NULL OR c.test_sent_at < c.updated_at THEN
        RAISE EXCEPTION 'Envía antes la prueba de esta versión y revísala.'
            USING ERRCODE = 'check_violation';
    END IF;
    counts := public.communication_count(p_id);
    total := (counts->>'recipients')::integer;
    left_out := (counts->>'excluded')::integer;
    IF total = 0 THEN
        RAISE EXCEPTION 'Este público no tiene ningún destinatario.'
            USING ERRCODE = 'check_violation';
    END IF;
    IF p_expected IS DISTINCT FROM total THEN
        RAISE EXCEPTION 'El número de destinatarios ha cambiado (ahora %): revísalo y confirma de nuevo.',
            total USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO public.job (kind, payload, run_at, idempotency_key, max_attempts)
    SELECT 'send_mail',
           jsonb_build_object('template', 'comunicacion', 'to', r.email,
                              'communication_id', p_id::text, 'account_id', r.account_id::text),
           at_now + make_interval(secs => (r.n - 1) * 60.0 / p_per_minute),
           'comunicacion:' || p_id::text || ':' || r.account_id::text,
           3
    FROM (SELECT s.account_id, s.email, row_number() OVER (ORDER BY s.account_id) AS n
          FROM public.communication_recipients(p_id) s
          WHERE NOT (c.kind = 'comercial' AND s.opted_out)) r;
    UPDATE public.admin_communication x
    SET state = 'enviando', confirmed_by = who, confirmed_at = at_now,
        per_minute = p_per_minute, recipients = total, excluded = left_out
    WHERE x.id = p_id;
    PERFORM public.admin_audit_write(who, 'communication_sent', c.audience_account, p_id::text,
        NULL, jsonb_build_object('kind', c.kind, 'audience', c.audience,
                                 'plan', c.audience_plan, 'recipients', total,
                                 'excluded', left_out, 'per_minute', p_per_minute));
    RETURN jsonb_build_object(
        'communication_id', p_id, 'recipients', total, 'excluded', left_out,
        'per_minute', p_per_minute,
        'last_run_at', at_now + make_interval(secs => (total - 1) * 60.0 / p_per_minute));
END
$$;

-- Historial (solo propietario): de la más nueva a la más vieja, por páginas (created_at e
-- id, como el resto de listados). Sin el texto de los párrafos.
CREATE OR REPLACE FUNCTION admin_communications(
    p_session text, p_limit integer DEFAULT 50, p_after_key text DEFAULT NULL,
    p_after_id text DEFAULT NULL)
RETURNS TABLE (communication_id uuid, kind text, subject text, audience text,
               audience_plan text, audience_account uuid, state text,
               created_at timestamptz, created_by text, test_sent_at timestamptz,
               confirmed_at timestamptz, confirmed_by text, recipients integer,
               excluded integer, sent integer, failed integer, skipped integer,
               finished_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    who uuid;
BEGIN
    SELECT a.actor_id INTO who FROM public.admin_actor(p_session, ARRAY['propietario'], false) a;
    PERFORM public.admin_audit_write(who, 'communication_list_read', NULL, NULL, NULL, NULL);
    RETURN QUERY
    SELECT c.id, c.kind, c.subject, c.audience, c.audience_plan, c.audience_account, c.state,
           c.created_at, (SELECT u.username FROM public.admin_user u WHERE u.id = c.created_by),
           c.test_sent_at, c.confirmed_at,
           (SELECT u.username FROM public.admin_user u WHERE u.id = c.confirmed_by),
           c.recipients, c.excluded, c.sent_count, c.failed_count, c.skipped_count,
           c.finished_at
    FROM public.admin_communication c
    WHERE p_after_key IS NULL
       OR (c.created_at, c.id) < (p_after_key::timestamptz, p_after_id::uuid)
    ORDER BY c.created_at DESC, c.id DESC
    LIMIT least(greatest(coalesce(p_limit, 50), 1), 201);
END
$$;

-- El worker, al enviar cada correo: el texto y si esa cuenta todavía lo recibe (`deliver`
-- falso si se dio de baja de las comerciales, o de la cuenta, después de confirmar). Sin
-- cuenta: la prueba, a la persona del backoffice (también si ya se confirmó: la prueba se
-- encoló antes). NULL si la comunicación no existe.
CREATE OR REPLACE FUNCTION fichaitor_communication_mail(p_id uuid, p_account uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT jsonb_build_object(
        'kind', c.kind, 'subject', c.subject, 'preheader', c.preheader,
        'headline', c.headline, 'paragraphs', to_jsonb(c.paragraphs),
        'button_label', c.button_label, 'button_url', c.button_url,
        'deliver', CASE
            WHEN p_account IS NULL THEN true
            ELSE c.state <> 'borrador'
                 AND public.communication_account_ok(p_account)
                 AND NOT (c.kind = 'comercial' AND EXISTS (
                     SELECT 1 FROM public.communication_optout o WHERE o.account_id = p_account))
        END)
    FROM public.admin_communication c WHERE c.id = p_id
$$;

-- El worker, después de cada correo de una comunicación (no de las pruebas): suma uno a
-- enviados, fallidos u omitidos. Con el último, la comunicación queda 'enviada'.
CREATE OR REPLACE FUNCTION fichaitor_communication_result(p_id uuid, p_outcome text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF p_outcome IS NULL OR p_outcome NOT IN ('sent', 'failed', 'skipped') THEN
        RAISE EXCEPTION 'resultado desconocido' USING ERRCODE = 'check_violation';
    END IF;
    UPDATE public.admin_communication c
    SET sent_count = c.sent_count + (p_outcome = 'sent')::integer,
        failed_count = c.failed_count + (p_outcome = 'failed')::integer,
        skipped_count = c.skipped_count + (p_outcome = 'skipped')::integer
    WHERE c.id = p_id AND c.state = 'enviando'
      AND c.sent_count + c.failed_count + c.skipped_count < c.recipients;
    UPDATE public.admin_communication c
    SET state = 'enviada', finished_at = now()
    WHERE c.id = p_id AND c.state = 'enviando'
      AND c.sent_count + c.failed_count + c.skipped_count >= c.recipients;
END
$$;

-- La API: baja de las comunicaciones comerciales de una cuenta (el token ya comprobado
-- por el proceso). Idempotente: una segunda vez no cambia nada. Una cuenta que no existe
-- o está dada de baja no deja nada.
CREATE OR REPLACE FUNCTION fichaitor_communication_unsubscribe(p_account uuid, p_source text)
RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    INSERT INTO public.communication_optout (account_id, source)
    SELECT x.id, p_source FROM public.account x
    WHERE x.id = p_account AND x.deleted_at IS NULL
    ON CONFLICT (account_id) DO NOTHING
$$;

-- La retención (pasada diaria del worker): las comunicaciones de hace más de 12 meses.
-- Devuelve cuántas se borraron.
CREATE OR REPLACE FUNCTION fichaitor_communication_purge() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    gone integer;
BEGIN
    DELETE FROM public.admin_communication c WHERE c.created_at < now() - interval '12 months';
    GET DIAGNOSTICS gone = ROW_COUNT;
    RETURN gone;
END
$$;

-- Quién ejecuta qué: el backoffice, sus cinco pantallas (más el guardado); el worker, el
-- texto de cada correo, su resultado y la retención; la API, la baja. Las internas, nadie.
REVOKE ALL ON FUNCTION
    communication_account_ok(uuid),
    communication_recipients(uuid),
    communication_count(uuid),
    communication_json(uuid),
    admin_communication_save(text, uuid, text, text, text, text, text[], text, text, text,
                             text, uuid),
    admin_communication_get(text, uuid),
    admin_communication_audience(text, uuid),
    admin_communication_test(text, uuid),
    admin_communication_confirm(text, uuid, integer, integer),
    admin_communications(text, integer, text, text),
    fichaitor_communication_mail(uuid, uuid),
    fichaitor_communication_result(uuid, text),
    fichaitor_communication_unsubscribe(uuid, text),
    fichaitor_communication_purge()
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    admin_communication_save(text, uuid, text, text, text, text, text[], text, text, text,
                             text, uuid),
    admin_communication_get(text, uuid),
    admin_communication_audience(text, uuid),
    admin_communication_test(text, uuid),
    admin_communication_confirm(text, uuid, integer, integer),
    admin_communications(text, integer, text, text)
TO fichaitor_admin;
GRANT EXECUTE ON FUNCTION
    fichaitor_communication_mail(uuid, uuid),
    fichaitor_communication_result(uuid, text),
    fichaitor_communication_purge()
TO fichaitor_worker;
GRANT EXECUTE ON FUNCTION fichaitor_communication_unsubscribe(uuid, text) TO fichaitor_api;

-- ---------------------------------------------------------------------------------------
-- Migración 2026-11-13: oposición a las comunicaciones comerciales en el alta y en Ajustes
-- (LSSI art. 21.2; docs/contrato-web-api.md, «Comunicaciones comerciales: en el alta y en
-- Ajustes»). communication_optout sigue siendo la única fuente de verdad del envío: una
-- fila es una cuenta que no las recibe. Idempotente.
-- ---------------------------------------------------------------------------------------

-- Dos medios nuevos: 'signup' (la casilla del alta, en la misma transacción que crea la
-- cuenta) y 'settings' (el interruptor de Ajustes). En una base anterior la restricción
-- solo admitía 'enlace' y 'un_clic'.
ALTER TABLE communication_optout DROP CONSTRAINT IF EXISTS communication_optout_source_check;
ALTER TABLE communication_optout ADD CONSTRAINT communication_optout_source_check
    CHECK (source IN ('enlace', 'un_clic', 'signup', 'settings'));

-- La última vez que la cuenta retiró su oposición (volver a recibirlas desde Ajustes): la
-- prueba de que la retiró, si un día alguien dice que no. El envío NO la lee: solo
-- importa que ya no haya fila en communication_optout. Una fila por cuenta; se borra con
-- la cuenta (disparador communication_optout_on_leave y ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS communication_optout_withdrawal (
    account_id   uuid PRIMARY KEY REFERENCES account (id) ON DELETE CASCADE,
    withdrawn_at timestamptz NOT NULL DEFAULT now(),
    source       text NOT NULL CHECK (source IN ('settings'))
);

ALTER TABLE communication_optout_withdrawal ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON communication_optout_withdrawal
    FROM PUBLIC, fichaitor_api, fichaitor_worker, fichaitor_admin;
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_conversation') THEN
        REVOKE ALL ON communication_optout_withdrawal FROM fichaitor_conversation;
    END IF;
    IF EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_billing') THEN
        REVOKE ALL ON communication_optout_withdrawal FROM fichaitor_billing;
    END IF;
END
$$;

-- La API (GET /api/v1/me/communications): si la cuenta recibe las comerciales y su último
-- cambio. Opuesta: la fecha y el medio de la oposición. No opuesta: la última retirada,
-- o nada si nunca cambió (interés legítimo). Sin filas para una cuenta que no existe o
-- está dada de baja.
CREATE OR REPLACE FUNCTION fichaitor_communication_preference(p_account uuid)
RETURNS TABLE (commercial boolean, changed_at timestamptz, source text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT o.account_id IS NULL,
           CASE WHEN o.account_id IS NOT NULL THEN o.created_at ELSE w.withdrawn_at END,
           CASE WHEN o.account_id IS NOT NULL THEN o.source ELSE w.source END
    FROM public.account x
    LEFT JOIN public.communication_optout o ON o.account_id = x.id
    LEFT JOIN public.communication_optout_withdrawal w ON w.account_id = x.id
    WHERE x.id = p_account AND x.deleted_at IS NULL
$$;

-- La API (PUT /api/v1/me/communications con commercial = true): retira la oposición.
-- Idempotente: si la cuenta no estaba opuesta no cambia nada (ni la fecha de la última
-- retirada). La oposición, con fichaitor_communication_unsubscribe(p_account, 'settings').
CREATE OR REPLACE FUNCTION fichaitor_communication_resubscribe(p_account uuid, p_source text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    DELETE FROM public.communication_optout o
    WHERE o.account_id = p_account
      AND EXISTS (SELECT 1 FROM public.account x
                  WHERE x.id = p_account AND x.deleted_at IS NULL);
    IF FOUND THEN
        INSERT INTO public.communication_optout_withdrawal (account_id, withdrawn_at, source)
        VALUES (p_account, now(), p_source)
        ON CONFLICT (account_id) DO UPDATE
            SET withdrawn_at = EXCLUDED.withdrawn_at, source = EXCLUDED.source;
    END IF;
END
$$;

REVOKE ALL ON FUNCTION
    fichaitor_communication_preference(uuid),
    fichaitor_communication_resubscribe(uuid, text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    fichaitor_communication_preference(uuid),
    fichaitor_communication_resubscribe(uuid, text)
TO fichaitor_api;
