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
CREATE OR REPLACE FUNCTION admin_find_accounts(p_session text, p_q text)
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
    ORDER BY x.created_at
    LIMIT 20;
END
$$;

-- Las fichas de una cuenta, con su nombre (dato de Google): solo aquí y con la referencia
-- del ticket, que se audita con la lectura (política de datos de usuario de Google:
-- lecturas humanas solo para soporte, seguridad u obligación legal).
CREATE OR REPLACE FUNCTION admin_account_detail(p_session text, p_account uuid, p_ticket_ref text)
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
    FROM public.location l WHERE l.account_id = p_account
    ORDER BY l.id;
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

-- Los 50 últimos jobs fallidos, con el error recortado y sin la carga.
CREATE OR REPLACE FUNCTION admin_failed_jobs(p_session text)
RETURNS TABLE (job_id uuid, kind text, attempts integer, finished_at timestamptz,
               last_error text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false);
    RETURN QUERY
    SELECT j.id, j.kind, j.attempts, j.finished_at, left(j.last_error, 200)
    FROM public.job j WHERE j.state = 'failed'
    ORDER BY j.finished_at DESC NULLS LAST LIMIT 50;
END
$$;

CREATE OR REPLACE FUNCTION admin_retention_runs(p_session text)
RETURNS TABLE (at timestamptz, deleted jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'propietario'], false);
    RETURN QUERY
    SELECT r.at, r.deleted FROM public.retention_run r ORDER BY r.at DESC LIMIT 10;
END
$$;

-- Estado de la sincronización de las fichas de una cuenta (o de todas con error), sin los
-- hechos de la ficha.
CREATE OR REPLACE FUNCTION admin_sync_status(p_session text, p_account uuid)
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
    WHERE (p_account IS NULL AND ps.status <> 'ok') OR a.id = p_account
    ORDER BY ps.status_at DESC LIMIT 200;
END
$$;

-- Los chats de una cuenta, sin direcciones.
CREATE OR REPLACE FUNCTION admin_channel_links(p_session text, p_account uuid)
RETURNS TABLE (identity_id text, channel text, verified_at timestamptz,
               last_inbound_at timestamptz, unlinked_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['soporte', 'finanzas', 'propietario'], false);
    RETURN QUERY
    SELECT c.id, c.channel, c.verified_at, c.last_inbound_at, c.unlinked_at
    FROM public.channel_identity c WHERE c.account_id = p_account
    ORDER BY c.verified_at;
END
$$;

-- ---------------------------------------------------------------------------
-- Lista de espera (sección 6.4), solo el rol propietario y solo en totales anónimos: por
-- semana (lunes, hora de Madrid), canal, tramo de nota y origen. Las celdas de menos de 5
-- personas no salen. No hay totales por fila ni por columna: con ellos, restando, se
-- deducirían las celdas suprimidas.
CREATE OR REPLACE FUNCTION admin_waitlist_totals(p_session text)
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
    ORDER BY t.week, t.channel, t.score_band, t.source;
END
$$;

-- ---------------------------------------------------------------------------
-- Personas y registro (sección 6.3: solo el rol propietario).

CREATE OR REPLACE FUNCTION admin_users(p_session text)
RETURNS TABLE (admin_user_id uuid, username text, role text, active boolean,
               must_enroll boolean, locked_until timestamptz, created_at timestamptz,
               password_changed_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM public.admin_actor(p_session, ARRAY['propietario'], false);
    RETURN QUERY
    SELECT u.id, u.username, u.role, u.active, u.must_enroll, u.locked_until, u.created_at,
           u.password_changed_at
    FROM public.admin_user u ORDER BY u.username;
END
$$;

-- El registro, de lo más nuevo a lo más viejo, por páginas (p_before: el id de la última
-- fila de la página anterior). La propia lectura queda registrada.
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
    LIMIT least(greatest(coalesce(p_limit, 100), 1), 500);
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
    admin_find_accounts(text, text),
    admin_account_detail(text, uuid, text),
    admin_email_matches(text, uuid, text, text),
    fichaitor_email_offboarding(text, uuid, text, text, text, boolean),
    admin_job_stats(text),
    admin_failed_jobs(text),
    admin_retention_runs(text),
    admin_sync_status(text, uuid),
    admin_channel_links(text, uuid),
    admin_waitlist_totals(text),
    admin_users(text),
    admin_audit_read(text, integer, bigint)
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
