-- Fichaitor: esquema de base de datos. Idempotente: se puede aplicar las veces que haga falta.
-- Lo aplica el rol propietario. La API se conecta con un usuario que hereda de fichaitor_api.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_api') THEN
        CREATE ROLE fichaitor_api NOLOGIN;
    END IF;
    -- El disparador de la baja de cuentas (más abajo) distingue al worker de la API.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_worker') THEN
        CREATE ROLE fichaitor_worker NOLOGIN;
    END IF;
END
$$;

-- M6 de docs/seguridad/revision-2026-11-12-c.md: ningún rol de la aplicación crea tablas
-- temporales. Una tabla temporal tapaba a las de verdad dentro de las funciones de la base
-- (pg_temp va el primero del search_path si no se nombra) y con eso se saltaban los
-- disparadores de guardia. La aplicación no usa tablas temporales. Es la barrera de toda la
-- clase; la otra es que cada función fija search_path = pg_catalog, public, pg_temp.
-- Sobre la base en la que se aplica (fichaitor, las _test o la de producción), quitado a
-- PUBLIC y, por si alguien se lo concedió, a los roles de la aplicación y a sus usuarios.
-- El propietario lo conserva. De paso, nadie más que el propietario crea objetos en public
-- (PostgreSQL 15 ya lo hace así en las bases nuevas; aquí se asegura en las anteriores).
-- Idempotente: repetirlo no cambia nada.
DO $$
DECLARE
    who name;
BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    FOR who IN
        SELECT r.rolname FROM pg_catalog.pg_roles r
        WHERE r.rolname IN ('fichaitor_api', 'fichaitor_worker', 'fichaitor_conversation',
                            'fichaitor_billing', 'fichaitor_admin')
        UNION
        SELECT m.rolname FROM pg_catalog.pg_auth_members am
        JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
        JOIN pg_catalog.pg_roles m ON m.oid = am.member
        WHERE g.rolname IN ('fichaitor_api', 'fichaitor_worker', 'fichaitor_conversation',
                            'fichaitor_billing', 'fichaitor_admin')
          AND NOT m.rolsuper
    LOOP
        EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM %I', current_database(), who);
    END LOOP;
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
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
    CHECK (kind IN ('visit', 'diagnosis', 'not_found', 'signup', 'connected',
                    'screenshot_diagnosis', 'screenshot_rejected',
                    -- El cobro (db/schema_billing.sql): Checkout abierto y primera plaza pagada.
                    'checkout_started', 'paid'));

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
-- B14 de docs/seguridad/revision-2026-11-12-c.md: tampoco elige la fecha de la conexión
-- (connected_at toma now()), así que no puede colar una conexión «más nueva» que las que
-- vengan después, ni crearla revocada (eso lo impide además google_connection_guard).
REVOKE ALL ON waitlist_entry, funnel_event, google_connection FROM fichaitor_api;
GRANT INSERT ON waitlist_entry, funnel_event TO fichaitor_api;
GRANT INSERT (google_sub, email, refresh_token_enc, key_version, scopes, locations)
    ON google_connection TO fichaitor_api;

-- Cuentas. Una cuenta es una identidad de Google: no hay contraseñas.
-- Datos personales: el correo. Se toma de Google al conectar.
CREATE TABLE IF NOT EXISTS account (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    google_sub           text NOT NULL UNIQUE CHECK (length(google_sub) BETWEEN 1 AND 255),
    email                text NOT NULL CHECK (length(email) <= 254),
    created_at           timestamptz NOT NULL DEFAULT now(),
    selected_location_id text
);

-- Baja (2026-11-12). Consulta docs/retencion.md.
-- disconnected_at: desde cuándo la cuenta está sin conexión con Google (baja de la ficha
-- o de la cuenta ya efectiva). Lo pone el worker al hacer efectiva la baja; al volver a
-- conectar, la API lo deja a NULL. De esta fecha cuentan los 12 meses del registro de
-- respuestas.
-- deleted_at: la cuenta está dada de baja. No se puede volver a entrar con ella.
ALTER TABLE account ADD COLUMN IF NOT EXISTS disconnected_at timestamptz;
ALTER TABLE account ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- Reglas de la baja, en la base y no solo en el código:
-- - Al darse de baja una cuenta (deleted_at), en la misma escritura se vacía su correo,
--   deja de tener ficha elegida y su google_sub pasa a 'deleted:<id>': la identidad de
--   Google queda libre, así que un inicio de sesión posterior crea una cuenta nueva que
--   no hereda nada.
-- - Una cuenta dada de baja no vuelve a cambiar.
-- - Solo el worker (o el propietario) puede fechar una desconexión o una baja. La API
--   solo puede quitar disconnected_at, que es lo que hace al volver a conectar.
CREATE OR REPLACE FUNCTION account_baja_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.deleted_at IS NOT NULL THEN
        NEW.google_sub := OLD.google_sub;
        NEW.email := OLD.email;
        NEW.deleted_at := OLD.deleted_at;
        NEW.disconnected_at := OLD.disconnected_at;
        NEW.selected_location_id := NULL;
        RETURN NEW;
    END IF;
    IF NOT pg_has_role(current_user, 'fichaitor_worker', 'MEMBER')
       AND (NEW.deleted_at IS NOT NULL
            OR (NEW.disconnected_at IS NOT NULL
                AND NEW.disconnected_at IS DISTINCT FROM OLD.disconnected_at)) THEN
        RAISE EXCEPTION 'solo el worker puede fechar una baja'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN
        NEW.google_sub := 'deleted:' || OLD.id::text;
        NEW.email := '';
        NEW.selected_location_id := NULL;
        NEW.disconnected_at := coalesce(NEW.disconnected_at, NEW.deleted_at);
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS account_baja_guard ON account;
CREATE TRIGGER account_baja_guard BEFORE UPDATE ON account
    FOR EACH ROW EXECUTE FUNCTION account_baja_guard();

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
-- Cerrar sesión marca revoked_at. La tarea de retención del worker borra las sesiones
-- caducadas o cerradas hace más de 30 días.
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
-- disconnected_at: solo puede quitarlo (al volver a conectar); lo impone el disparador.
-- El borrado físico lo hace el worker: sus privilegios están en schema_worker.sql.
REVOKE ALL ON account, location, session FROM fichaitor_api;
GRANT SELECT, INSERT ON account, location, session TO fichaitor_api;
GRANT UPDATE (email, selected_location_id, disconnected_at) ON account TO fichaitor_api;
GRANT UPDATE (gbp_account, title, place_id, verified, updated_at, removed_at)
    ON location TO fichaitor_api;
GRANT UPDATE (revoked_at) ON session TO fichaitor_api;

-- Topes de llamadas de pago (2026-11-12, B2 de docs/seguridad/revision-2026-11-12.md):
-- las capturas y los diagnósticos públicos de cada día, para todos los clientes juntos, y
-- los borradores de respuesta de cada cuenta. Un contador por ámbito y ventana fija.
-- En la base, y no en la memoria de la API, para que un reinicio, un despliegue o una
-- segunda réplica no los pongan a cero.
-- scope: una palabra fija y, si es por cuenta, el id de la cuenta. Nunca una IP ni un texto
-- tecleado. La tarea de retención del worker borra las ventanas vencidas.
CREATE TABLE IF NOT EXISTS usage_counter (
    scope        text NOT NULL CHECK (scope ~ '^[a-z_]{1,40}(:[0-9a-z_-]{1,64})?$'),
    window_start timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL,
    hits         integer NOT NULL CHECK (hits >= 0),
    PRIMARY KEY (scope, window_start),
    CHECK (expires_at > window_start)
);

CREATE INDEX IF NOT EXISTS usage_counter_expires ON usage_counter (expires_at);

-- Multinegocio (2026-11-12): el tope de borradores pasa a ser por ficha
-- ("review_draft:loc_..."), así que el ámbito admite también letras y '_'. Bases
-- anteriores: se recrea la restricción con la expresión nueva, que acepta todo lo que
-- aceptaba la antigua.
ALTER TABLE usage_counter DROP CONSTRAINT IF EXISTS usage_counter_scope_check;
ALTER TABLE usage_counter ADD CONSTRAINT usage_counter_scope_check
    CHECK (scope ~ '^[a-z_]{1,40}(:[0-9a-z_-]{1,64})?$');

-- La API cuenta (INSERT ... ON CONFLICT DO UPDATE en una sola sentencia) y lee la cuenta.
-- No puede borrar un contador, cambiarle el ámbito o la ventana, ni ponerlo a cero.
REVOKE ALL ON usage_counter FROM PUBLIC, fichaitor_api;
GRANT SELECT (scope, window_start, hits) ON usage_counter TO fichaitor_api;
GRANT INSERT (scope, window_start, expires_at, hits) ON usage_counter TO fichaitor_api;
GRANT UPDATE (hits) ON usage_counter TO fichaitor_api;

-- Quién no está sujeto a los disparadores de guardia: el propietario de la tabla (aplica
-- los esquemas y prepara las pruebas) y un superusuario. Los roles de la aplicación, sí.
-- pg_has_role no sirve aquí: un superusuario es "miembro" de todos los roles.
-- M6 de docs/seguridad/revision-2026-11-12-c.md: catálogos cualificados y pg_temp el
-- último del search_path. Sin eso, una tabla temporal "pg_roles" con rolsuper = true
-- la tapaba y todos los disparadores eximían a quien la creara.
CREATE OR REPLACE FUNCTION fichaitor_is_owner(rel regclass) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT coalesce(bool_or(r.rolsuper OR c.relowner = r.oid), false)
    FROM pg_catalog.pg_roles r, pg_catalog.pg_class c
    WHERE r.rolname = current_user AND c.oid = rel
$$;

-- B11 de docs/seguridad/revision-2026-11-12-b.md: un contador solo sube, y de uno en uno.
-- Es lo único que hace take_quota (hits = hits + 1). Con UPDATE (hits), la API y la
-- conversación podían ponerlo a cero y el tope dejaba de serlo. Un contador nuevo empieza
-- en 1, y su ámbito, su ventana y su fin no cambian.
CREATE OR REPLACE FUNCTION usage_counter_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF fichaitor_is_owner(TG_RELID) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.hits <> 1 THEN
            RAISE EXCEPTION 'un contador nuevo empieza en 1' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.hits <> OLD.hits + 1
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.window_start IS DISTINCT FROM OLD.window_start
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'un contador solo suma 1' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS usage_counter_guard ON usage_counter;
CREATE TRIGGER usage_counter_guard BEFORE INSERT OR UPDATE ON usage_counter
    FOR EACH ROW EXECUTE FUNCTION usage_counter_guard();

-- ---------------------------------------------------------------------------
-- Respuestas con IA del plan Gratis (decisión del propietario, 2026-11-13): 25 por ficha de
-- Google, una sola vez y para siempre. No se recargan cada mes ni vuelven al borrar la
-- cuenta o retirar el acceso y conectar otra vez, aunque sea con otra cuenta de Google que
-- administre la misma ficha. Por eso la clave sobrevive a la baja:
-- - key: HMAC-SHA256 de gbp_location ("locations/<id>") con FICHAITOR_ALLOWANCE_PEPPER (un
--   secreto del servidor), en hexadecimal. Sin el secreto no se puede volver a la ficha
--   probando identificadores. Nunca se guarda el id en claro, la cuenta ni un correo.
-- - used: cuántas se han gastado. first_used_at y last_used_at: la primera y la última.
--   last_used_at hace falta para el plazo (3 años desde el último uso).
-- Ni la baja, ni la retención de 30 días ni la purga por revocación la tocan. La borra la
-- retención del worker 3 años después del último uso (docs/retencion.md: interés legítimo
-- en prevenir el abuso del plan gratuito); el disparador lo impone antes de ese plazo.
-- Sustituye a la ventana mensual de usage_counter ("ai_replies:loc_..."), que se borra.
CREATE TABLE IF NOT EXISTS free_allowance (
    key           text PRIMARY KEY CHECK (key ~ '^[0-9a-f]{64}$'),
    used          integer NOT NULL CHECK (used >= 0),
    first_used_at timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS free_allowance_last_used ON free_allowance (last_used_at);

-- Igual que usage_counter (B11): fuera del propietario, una fila nueva empieza en 1 y cada
-- cambio suma exactamente 1, sin tocar la clave ni la primera fecha. Las fechas las pone la
-- base. Borrar, solo pasados 3 años del último uso.
CREATE OR REPLACE FUNCTION free_allowance_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF fichaitor_is_owner(TG_RELID) THEN
        RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.last_used_at > now() - interval '3 years' THEN
            RAISE EXCEPTION 'free_allowance: solo se borra a los 3 años del último uso'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.used <> 1 THEN
            RAISE EXCEPTION 'un contador nuevo empieza en 1' USING ERRCODE = 'check_violation';
        END IF;
        NEW.first_used_at := now();
        NEW.last_used_at := now();
        RETURN NEW;
    END IF;
    IF NEW.used <> OLD.used + 1 OR NEW.key IS DISTINCT FROM OLD.key
       OR NEW.first_used_at IS DISTINCT FROM OLD.first_used_at THEN
        RAISE EXCEPTION 'un contador solo suma 1' USING ERRCODE = 'check_violation';
    END IF;
    NEW.last_used_at := now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS free_allowance_guard ON free_allowance;
CREATE TRIGGER free_allowance_guard BEFORE INSERT OR UPDATE OR DELETE ON free_allowance
    FOR EACH ROW EXECUTE FUNCTION free_allowance_guard();

-- La API cuenta (INSERT ... ON CONFLICT DO UPDATE en una sentencia) y lee lo gastado. La
-- conversación, lo mismo (schema_channels.sql); el worker, solo borrar (schema_worker.sql).
REVOKE ALL ON free_allowance FROM PUBLIC, fichaitor_api;
GRANT SELECT (key, used) ON free_allowance TO fichaitor_api;
GRANT INSERT (key, used) ON free_allowance TO fichaitor_api;
GRANT UPDATE (used) ON free_allowance TO fichaitor_api;

-- La ventana mensual anterior ya no se usa. Repetirlo no cambia nada.
DELETE FROM usage_counter WHERE scope LIKE 'ai\_replies:%';

-- ---------------------------------------------------------------------------
-- Multinegocio (2026-11-12): varias fichas activas por cuenta. Consulta
-- docs/multinegocio.md, secciones 3 y 4, y su sección 15 (decisiones del propietario).

-- La ficha activa: el estado actual de cada ficha va en su fila de location.
-- - active_since: desde cuándo está activa. NULL = inactiva. Activa quiere decir que se
--   sincroniza, se propone por los chats y el panel la sirve.
-- - plan: con qué plan está activa. NULL si está inactiva.
-- - deactivated_at: desde cuándo está inactiva una ficha que estuvo activa. De aquí
--   cuentan los 30 días de retención. Lo pone el disparador, nunca la API.
ALTER TABLE location
    ADD COLUMN IF NOT EXISTS active_since   timestamptz,
    ADD COLUMN IF NOT EXISTS plan           text,
    ADD COLUMN IF NOT EXISTS deactivated_at timestamptz;

ALTER TABLE location DROP CONSTRAINT IF EXISTS location_plan_check;
ALTER TABLE location ADD CONSTRAINT location_plan_check
    CHECK (plan IN ('gratis', 'pro', 'pro_whatsapp'));
ALTER TABLE location DROP CONSTRAINT IF EXISTS location_active_has_plan;
ALTER TABLE location ADD CONSTRAINT location_active_has_plan
    CHECK ((active_since IS NULL) = (plan IS NULL));
ALTER TABLE location DROP CONSTRAINT IF EXISTS location_removed_not_active;
ALTER TABLE location ADD CONSTRAINT location_removed_not_active
    CHECK (removed_at IS NULL OR active_since IS NULL);

CREATE INDEX IF NOT EXISTS location_active ON location (account_id)
    WHERE active_since IS NOT NULL;

-- Decisión 5 del propietario: una ficha de Google solo puede estar activa en Gratis en
-- una cuenta de Fichaitor. gbp_location ("locations/<id>") es único en todo Google.
-- El disparador de abajo lo comprueba antes con un mensaje propio (gratis_ocupada); este
-- índice es la barrera final, también con dos activaciones a la vez en dos cuentas.
CREATE UNIQUE INDEX IF NOT EXISTS location_gratis_one_account ON location (gbp_location)
    WHERE plan = 'gratis';

-- Derechos de plan: "esta cuenta puede tener N fichas activas en este plan hasta esta
-- fecha". Preparan el cobro sin construirlo; cada suscripción será un derecho 'pago'.
-- El plan gratis no necesita derecho: como mucho una ficha gratis activa por cuenta.
-- Solo los escribe el propietario (y el disparador de las cuentas nuevas, abajo). La API
-- los lee; el worker los borra en la retención.
-- ON DELETE CASCADE: una cuenta no se borra nunca (queda como lápida), así que en
-- producción no actúa; deja que se borre a mano o en las pruebas sin tocar esta tabla.
CREATE TABLE IF NOT EXISTS plan_entitlement (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   uuid NOT NULL REFERENCES account (id) ON DELETE CASCADE,
    plan         text NOT NULL CHECK (plan IN ('pro', 'pro_whatsapp')),
    quantity     smallint NOT NULL CHECK (quantity BETWEEN 1 AND 100),
    starts_at    timestamptz NOT NULL,
    ends_at      timestamptz CHECK (ends_at IS NULL OR ends_at > starts_at),
    source       text NOT NULL CHECK (source IN ('beta', 'manual', 'pago')),
    external_ref text CHECK (length(external_ref) BETWEEN 1 AND 200),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS plan_entitlement_account ON plan_entitlement (account_id);

-- Registro de activaciones: de solo inserción, lo escribe el disparador (no se puede
-- olvidar) y sirve para auditar y, con el cobro, para prorratear. Sin clave ajena: dura
-- más que la fila de location, que la retención borra. Sin datos personales.
-- written_by: el usuario con el que se conectó quien hizo el cambio (session_user).
-- reason: owner (el dueño, desde la API), removed (la ficha se retiró), migration (el
-- relleno de abajo) u otro código que pida el propietario con fichaitor.activation_reason.
-- Si lo pide otro rol, untrusted («no fiable»): B16 de
-- docs/seguridad/revision-2026-11-12-c.md.
CREATE TABLE IF NOT EXISTS location_activation_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    written_by  name NOT NULL DEFAULT current_user,
    account_id  uuid NOT NULL,
    location_id text NOT NULL CHECK (length(location_id) <= 64),
    event       text NOT NULL CHECK (event IN ('activated', 'deactivated', 'plan_changed')),
    plan        text CHECK (plan IN ('gratis', 'pro', 'pro_whatsapp')),
    reason      text CHECK (reason ~ '^[a-z_]{1,40}$')
);

CREATE INDEX IF NOT EXISTS location_activation_log_account
    ON location_activation_log (account_id);

-- Reglas de la ficha activa, en la base y no solo en el código:
-- 1. Al retirar una ficha (removed_at) deja de estar activa, la retire quien la retire
--    (sync_locations de la API, record_denied y begin_offboarding del worker).
-- 2. Al activar o cambiar de plan, fuera del propietario: la cuenta se bloquea (dos
--    activaciones a la vez se cuentan una detrás de otra), no está dada de baja ni tiene
--    una baja programada, active_since está a menos de un minuto del reloj de la base y
--    hay hueco en el plan: gratis, una por cuenta y esa ficha de Google sin otra cuenta
--    que la tenga en gratis; pro y pro_whatsapp, tantas como la suma de los derechos
--    vigentes. Si no, check_violation con un mensaje fijo que la API traduce:
--      limite_fichas (402), gratis_ocupada (409 free_taken), baja_programada (409
--      offboarding_pending), cuenta_de_baja, fecha_activacion y ficha_retirada.
-- 3. Al desactivar, deactivated_at = now(); al activar otra vez, NULL.
-- Con los privilegios de quien escribe (no SECURITY DEFINER): así fichaitor_is_owner ve
-- al propietario de verdad. Quien activa es la API, que lee todo lo que se consulta aquí.
-- Los privilegios de columna se comprueban sobre las del SET, no sobre las que cambia el
-- disparador: el worker sigue retirando fichas con solo UPDATE (removed_at).
CREATE OR REPLACE FUNCTION location_active_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    is_owner boolean := fichaitor_is_owner(TG_RELID);
    was_active boolean := false;
    gone timestamptz;
    used integer;
    allowed integer;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        was_active := OLD.active_since IS NOT NULL;
    ELSIF NOT is_owner THEN
        NEW.deactivated_at := NULL;
    END IF;
    -- 1. Retirada. Activar una ficha que ya estaba retirada es un error, no un no-op.
    IF NEW.removed_at IS NOT NULL THEN
        IF NOT is_owner AND NEW.active_since IS NOT NULL
           AND (TG_OP = 'INSERT' OR OLD.removed_at IS NOT NULL) THEN
            RAISE EXCEPTION 'ficha_retirada' USING ERRCODE = 'check_violation';
        END IF;
        IF was_active THEN
            NEW.deactivated_at := now();
        END IF;
        NEW.active_since := NULL;
        NEW.plan := NULL;
        RETURN NEW;
    END IF;
    -- 3. Desactivar (o seguir inactiva). Un plan sin active_since lo rechaza la
    -- restricción location_active_has_plan.
    IF NEW.active_since IS NULL THEN
        IF was_active THEN
            NEW.plan := NULL;
            NEW.deactivated_at := now();
        END IF;
        RETURN NEW;
    END IF;
    -- Activa y sin cambios en lo que decide el plan: nada que comprobar.
    IF was_active AND NEW.active_since = OLD.active_since
       AND NEW.plan IS NOT DISTINCT FROM OLD.plan THEN
        RETURN NEW;
    END IF;
    -- 2. Activar o cambiar de plan.
    NEW.deactivated_at := NULL;
    IF is_owner OR NEW.plan IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT a.deleted_at INTO gone FROM public.account a WHERE a.id = NEW.account_id FOR UPDATE;
    IF NOT FOUND OR gone IS NOT NULL THEN
        RAISE EXCEPTION 'cuenta_de_baja' USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM public.offboarding o
               WHERE o.account_id = NEW.account_id
                 AND o.restored_at IS NULL AND o.effective_at IS NULL) THEN
        RAISE EXCEPTION 'baja_programada' USING ERRCODE = 'check_violation';
    END IF;
    IF (NOT was_active OR NEW.active_since <> OLD.active_since)
       AND abs(extract(epoch FROM NEW.active_since - now())) > 60 THEN
        RAISE EXCEPTION 'fecha_activacion' USING ERRCODE = 'check_violation';
    END IF;
    SELECT count(*) INTO used FROM public.location l
    WHERE l.account_id = NEW.account_id AND l.id <> NEW.id AND l.plan = NEW.plan;
    IF NEW.plan = 'gratis' THEN
        allowed := 1;
    ELSE
        SELECT coalesce(sum(e.quantity), 0) INTO allowed FROM public.plan_entitlement e
        WHERE e.account_id = NEW.account_id AND e.plan = NEW.plan
          AND e.starts_at <= now() AND (e.ends_at IS NULL OR e.ends_at > now());
    END IF;
    IF used + 1 > allowed THEN
        RAISE EXCEPTION 'limite_fichas' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.plan = 'gratis' AND EXISTS (
        SELECT 1 FROM public.location l
        WHERE l.gbp_location = NEW.gbp_location AND l.account_id <> NEW.account_id
          AND l.plan = 'gratis') THEN
        RAISE EXCEPTION 'gratis_ocupada' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS location_active_guard ON location;
CREATE TRIGGER location_active_guard BEFORE INSERT OR UPDATE ON location
    FOR EACH ROW EXECUTE FUNCTION location_active_guard();

-- 4. Cada activación, desactivación y cambio de plan deja su fila en el registro, también
-- las del propietario (el relleno queda registrado). SECURITY DEFINER: nadie más tiene
-- INSERT en el registro. written_by es session_user: dentro de esta función current_user
-- es el propietario.
-- Además, al desactivar a mano la ficha que el panel tenía elegida, la preferencia pasa
-- a la activa más antigua, o a NULL si no queda ninguna (docs/multinegocio.md,
-- «Aclaraciones para la web», 3). Al retirarla no: el código antiguo la deja elegida para
-- enseñar «sin acceso», o la vacía él.
CREATE OR REPLACE FUNCTION location_activation_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    happened text;
    why text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.active_since IS NULL THEN
            RETURN NULL;
        END IF;
        happened := 'activated';
    ELSIF OLD.active_since IS NULL AND NEW.active_since IS NOT NULL THEN
        happened := 'activated';
    ELSIF OLD.active_since IS NOT NULL AND NEW.active_since IS NULL THEN
        happened := 'deactivated';
    ELSIF NEW.active_since IS NOT NULL AND NEW.plan IS DISTINCT FROM OLD.plan THEN
        happened := 'plan_changed';
    ELSE
        RETURN NULL;
    END IF;
    why := nullif(current_setting('fichaitor.activation_reason', true), '');
    -- B16 de docs/seguridad/revision-2026-11-12-c.md: el motivo pedido solo vale si lo
    -- pide el propietario (o un superusuario), que es quien rellena. Aquí current_user es
    -- el propietario (SECURITY DEFINER): se mira session_user, el usuario con el que se
    -- conectó quien escribe. La aplicación nunca lo pide; si otro rol lo hace, queda
    -- 'untrusted' («no fiable») y no lo que pidió.
    IF why IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles r, pg_catalog.pg_class c
        WHERE r.rolname = session_user AND c.oid = TG_RELID
          AND (r.rolsuper OR c.relowner = r.oid)) THEN
        why := 'untrusted';
    END IF;
    -- El proceso de cobro (fichaitor_billing, db/schema_billing.sql) activa la ficha de una
    -- plaza recién pagada: reason = 'billing' (docs/admin-facturacion.md, 4.6). Por nombre y
    -- sin superusuarios, que son «miembros» de todo.
    IF why IS NULL AND NEW.removed_at IS NULL AND EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles g, pg_catalog.pg_roles s
        WHERE g.rolname = 'fichaitor_billing' AND s.rolname = session_user AND NOT s.rolsuper
          AND pg_catalog.pg_has_role(s.oid, g.oid, 'MEMBER')) THEN
        why := 'billing';
    END IF;
    IF why IS NULL OR why !~ '^[a-z_]{1,40}$' THEN
        why := CASE WHEN NEW.removed_at IS NOT NULL THEN 'removed' ELSE 'owner' END;
    END IF;
    INSERT INTO public.location_activation_log
        (written_by, account_id, location_id, event, plan, reason)
    VALUES (session_user, NEW.account_id, NEW.id, happened,
            CASE WHEN happened = 'deactivated' THEN OLD.plan ELSE NEW.plan END, why);
    IF happened = 'deactivated' AND NEW.removed_at IS NULL THEN
        UPDATE public.account a SET selected_location_id = (
            SELECT l.id FROM public.location l
            WHERE l.account_id = a.id AND l.active_since IS NOT NULL
            ORDER BY l.active_since, l.id LIMIT 1)
        WHERE a.id = NEW.account_id AND a.selected_location_id = NEW.id;
    END IF;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION location_activation_audit() FROM PUBLIC;
DROP TRIGGER IF EXISTS location_activation_audit ON location;
CREATE TRIGGER location_activation_audit AFTER INSERT OR UPDATE ON location
    FOR EACH ROW EXECUTE FUNCTION location_activation_audit();

-- Derecho beta (decisión 4 del propietario): 3 fichas Pro por cuenta hasta el
-- lanzamiento, ampliable a mano con otro derecho ('manual'). Cada cuenta nueva lo recibe
-- al crearse; las existentes, con el relleno de abajo.
-- Cobro (docs/admin-facturacion.md, 4.6): solo antes del lanzamiento y hasta el fin de la
-- beta, si están fijados en billing_setting (db/schema_billing.sql). Sin esa tabla, como
-- siempre. Aquí y no en schema_billing.sql: volver a aplicar este archivo solo no debe
-- devolver el derecho beta después del lanzamiento.
CREATE OR REPLACE FUNCTION account_beta_entitlement() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    launch timestamptz;
    beta_end timestamptz;
BEGIN
    IF pg_catalog.to_regclass('public.billing_setting') IS NOT NULL THEN
        EXECUTE 'SELECT launch_at, beta_ends_at FROM public.billing_setting LIMIT 1'
            INTO launch, beta_end;
    END IF;
    IF (launch IS NULL OR now() < launch) AND (beta_end IS NULL OR beta_end > now()) THEN
        INSERT INTO public.plan_entitlement (account_id, plan, quantity, starts_at, ends_at, source)
        VALUES (NEW.id, 'pro', 3, now(), beta_end, 'beta');
    END IF;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION account_beta_entitlement() FROM PUBLIC;
DROP TRIGGER IF EXISTS account_beta_entitlement ON account;
CREATE TRIGGER account_beta_entitlement AFTER INSERT ON account
    FOR EACH ROW EXECUTE FUNCTION account_beta_entitlement();

-- Privilegios. La API activa, desactiva y cambia de plan (UPDATE de active_since y plan:
-- el disparador decide si cabe) y lee los derechos. No fija deactivated_at, no escribe
-- derechos ni el registro y no borra nada. El worker solo borra, en la retención.
-- Las columnas nuevas de location para el worker y la conversación se conceden en
-- schema_worker.sql y schema_channels.sql.
REVOKE ALL ON plan_entitlement, location_activation_log FROM PUBLIC, fichaitor_api, fichaitor_worker;
GRANT UPDATE (active_since, plan) ON location TO fichaitor_api;
GRANT SELECT ON plan_entitlement TO fichaitor_api;
GRANT SELECT (account_id) ON plan_entitlement, location_activation_log TO fichaitor_worker;
GRANT DELETE ON plan_entitlement, location_activation_log TO fichaitor_worker;

-- Relleno (docs/multinegocio.md, 3.8). Se puede repetir sin efecto, y no cambia nada
-- que no sea de las columnas y las tablas nuevas.
-- a) El derecho beta de las cuentas que ya existían.
INSERT INTO plan_entitlement (account_id, plan, quantity, starts_at, source)
SELECT a.id, 'pro', 3, now(), 'beta' FROM account a
WHERE a.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM plan_entitlement e
                  WHERE e.account_id = a.id AND e.source = 'beta');

-- b) La ficha elegida pasa a activa en gratis, con reason = 'migration' en el registro.
-- Solo si está elegida, no está retirada, la cuenta no está dada de baja y la cuenta no
-- tiene ninguna fila en el registro: esa es la marca de "ya migrada", para no reactivar
-- al volver a aplicar el esquema una ficha que el dueño desactivó.
-- Si la misma ficha de Google está elegida en varias cuentas, solo puede quedar gratis
-- en una (decisión 5): la de la cuenta más antigua. Las demás quedan inactivas.
DO $$
BEGIN
    PERFORM set_config('fichaitor.activation_reason', 'migration', true);
    WITH candidate AS (
        SELECT l.id, l.gbp_location, a.created_at, a.id AS account_id
        FROM location l
        JOIN account a ON a.id = l.account_id AND a.selected_location_id = l.id
        WHERE l.removed_at IS NULL AND l.active_since IS NULL AND a.deleted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM location_activation_log g WHERE g.account_id = a.id)
          AND NOT EXISTS (SELECT 1 FROM location o
                          WHERE o.gbp_location = l.gbp_location AND o.plan = 'gratis')
    ), first_per_google AS (
        SELECT DISTINCT ON (gbp_location) id FROM candidate
        ORDER BY gbp_location, created_at, account_id
    )
    UPDATE location l SET active_since = now(), plan = 'gratis'
    WHERE l.id IN (SELECT id FROM first_per_google);
    PERFORM set_config('fichaitor.activation_reason', '', true);
END
$$;
