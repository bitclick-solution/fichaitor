-- Fichaitor: esquema del cobro con Stripe. Paso 2 de docs/admin-facturacion.md (sección 5.1),
-- con las decisiones del propietario de sus secciones 1.1, 9.0 y 9.0.1, que mandan.
-- Idempotente y aplicable sobre datos reales: solo crea lo que falta, añade columnas y
-- vuelve a crear funciones, disparadores, políticas y privilegios.
-- Se aplica con el rol propietario DESPUÉS de schema.sql, schema_worker.sql y
-- schema_channels.sql, y ANTES de schema_admin.sql (que concede al backoffice la lectura de
-- estas tablas):
--   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_billing.sql
--
-- Quién hace qué:
-- - fichaitor_api (la API): guarda el perfil fiscal, pide cosas al proceso de cobro
--   (billing_request) y apunta los eventos de Stripe que llegan firmados (solo sus
--   identificadores). Lee plazas, documentos, ajustes y si la cuenta está bloqueada. No
--   escribe derechos, plazas, cobros, documentos ni bloqueos, y no llama a Stripe.
-- - fichaitor_billing (el proceso fichaitor-billing, nuevo): el único que habla con Stripe.
--   Atiende las peticiones, vuelve a leer en Stripe cada evento, copia plazas y cobros,
--   crea los derechos 'pago', bloquea y desbloquea, y activa la ficha de una plaza nueva.
--   No lee conexiones de Google, reseñas, canales ni la lista de espera.
-- - fichaitor_worker y fichaitor_conversation: leen si una cuenta está bloqueada (paso 7);
--   el worker borra lo que vence (billing_request a 30 días, stripe_event a 90) y adjunta
--   el PDF de una factura a su correo (paso 9).
-- - Paso 9: la factura legal la emite el proceso de cobro en Odoo (billing_document) y la
--   envía el worker. Lo de más de 6 años lo borra billing_purge(), al final.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_billing') THEN
        CREATE ROLE fichaitor_billing NOLOGIN;
    END IF;
    -- Lo crea schema_admin.sql; aquí también, porque plan_entitlement_guard lo nombra.
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'fichaitor_admin') THEN
        CREATE ROLE fichaitor_admin NOLOGIN;
    END IF;
END
$$;

-- M6 de docs/seguridad/revision-2026-11-12-c.md: sin TEMPORARY, ni el rol ni sus usuarios
-- (fichaitor_billing_prod, fichaitor_billing_dev). schema.sql lo hace también en su lista.
DO $$
DECLARE
    who name;
BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
    FOR who IN
        SELECT 'fichaitor_billing'::name
        UNION
        SELECT m.rolname FROM pg_catalog.pg_auth_members am
        JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
        JOIN pg_catalog.pg_roles m ON m.oid = am.member
        WHERE g.rolname = 'fichaitor_billing' AND NOT m.rolsuper
    LOOP
        EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM %I', current_database(), who);
    END LOOP;
END
$$;

-- ¿Es quien escribe (session_user) miembro del rol, sin ser superusuario? Por nombre: no
-- falla si el rol no existe. Para los disparadores, que corren con los privilegios de
-- quien escribe o del propietario.
CREATE OR REPLACE FUNCTION fichaitor_session_in(role_name name) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles g, pg_catalog.pg_roles s
        WHERE g.rolname = role_name AND s.rolname = session_user AND NOT s.rolsuper
          AND pg_catalog.pg_has_role(s.oid, g.oid, 'MEMBER'))
$$;

-- ---------------------------------------------------------------------------
-- Ajustes del cobro (sección 4.6): una sola fila. launch_at vacío = beta: no se cobra
-- (pricing.charging es false y POST /me/billing/seats responde 409 beta_no_charge).
-- Solo la escribe el propietario.
CREATE TABLE IF NOT EXISTS billing_setting (
    id           boolean PRIMARY KEY DEFAULT true CHECK (id),
    launch_at    timestamptz,
    beta_ends_at timestamptz,
    CHECK (beta_ends_at IS NULL OR launch_at IS NULL OR beta_ends_at >= launch_at)
);
INSERT INTO billing_setting (id) VALUES (true) ON CONFLICT DO NOTHING;
-- beta_notice_for: el fin de la beta del que ya se avisó por correo (beta_ending, 30 días
-- antes de launch_at). Lo escribe el proceso de cobro; si el propietario cambia la fecha,
-- se vuelve a avisar.
ALTER TABLE billing_setting ADD COLUMN IF NOT EXISTS beta_notice_for timestamptz;

-- Ventana beta (decisión del propietario, 2026-11-13): las cuentas de la beta tienen 30 días
-- desde el lanzamiento para elegir plan, con sus plazas beta. beta_ends_at ya no se fija a
-- mano: es siempre launch_at + 30 días (app/domain/billing.py, BETA_WINDOW_DAYS), y vacío
-- sin launch_at. El propietario solo escribe launch_at.
CREATE OR REPLACE FUNCTION billing_setting_beta_end() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    NEW.beta_ends_at := NEW.launch_at + interval '30 days';
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS billing_setting_beta_end ON billing_setting;
CREATE TRIGGER billing_setting_beta_end BEFORE INSERT OR UPDATE ON billing_setting
    FOR EACH ROW EXECUTE FUNCTION billing_setting_beta_end();
-- Corrige la fila existente (una fecha puesta a mano antes de esta regla). Repetirlo no
-- cambia nada.
UPDATE billing_setting SET launch_at = launch_at
WHERE beta_ends_at IS DISTINCT FROM launch_at + interval '30 days';

-- ---------------------------------------------------------------------------
-- Datos fiscales de una cuenta (sección 4.8). Se conservan 6 años desde la última factura,
-- también después de borrar la cuenta (la cuenta nunca se borra: queda como lápida).
-- stripe_customer_id: lo pone una vez el proceso de cobro. El resto, la API (PUT del
-- perfil). updated_at lo pone la base.
CREATE TABLE IF NOT EXISTS billing_customer (
    account_id         uuid PRIMARY KEY REFERENCES account (id),
    stripe_customer_id text UNIQUE CHECK (stripe_customer_id ~ '^cus_[A-Za-z0-9]{1,250}$'),
    legal_name         text NOT NULL CHECK (length(legal_name) BETWEEN 1 AND 200),
    tax_id_type        text NOT NULL CHECK (tax_id_type IN ('es_cif', 'eu_vat')),
    tax_id_kind        text NOT NULL CHECK (tax_id_kind IN ('nif', 'nie', 'cif', 'eu_vat')),
    tax_id             text NOT NULL CHECK (tax_id ~ '^[A-Z0-9+*]{4,20}$'),
    address_line1      text NOT NULL CHECK (length(address_line1) BETWEEN 1 AND 200),
    address_line2      text CHECK (length(address_line2) <= 200),
    postal_code        text NOT NULL CHECK (length(postal_code) BETWEEN 1 AND 20),
    city               text NOT NULL CHECK (length(city) BETWEEN 1 AND 100),
    province           text CHECK (length(province) <= 100),
    country            text NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
    territory          text NOT NULL CHECK (territory IN ('peninsula_baleares', 'canarias', 'ceuta',
                                                          'melilla', 'ue')),
    tax_treatment      text NOT NULL CHECK (tax_treatment IN ('iva_21', 'no_sujeto', 'isp_ue')),
    invoice_email      text NOT NULL CHECK (length(invoice_email) BETWEEN 3 AND 254
                                            AND invoice_email LIKE '%_@_%'),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Riesgo residual de la sección 5.2: una API comprometida podría cambiar los datos fiscales
-- de otra cuenta. Cada alta o cambio de razón social o NIF deja su fila aquí (solo la lee
-- el propietario). Las facturas ya emitidas no cambian.
CREATE TABLE IF NOT EXISTS billing_customer_history (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    written_by name NOT NULL,
    account_id uuid NOT NULL,
    legal_name text NOT NULL,
    tax_id     text NOT NULL
);

CREATE OR REPLACE FUNCTION billing_customer_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF fichaitor_is_owner(TG_RELID) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        NEW.created_at := now();
        IF NEW.stripe_customer_id IS NOT NULL THEN
            RAISE EXCEPTION 'el cliente de Stripe lo pone el proceso de cobro'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    ELSE
        NEW.created_at := OLD.created_at;
        -- El cliente de Stripe se fija una vez y no cambia.
        IF OLD.stripe_customer_id IS NOT NULL
           AND NEW.stripe_customer_id IS DISTINCT FROM OLD.stripe_customer_id THEN
            RAISE EXCEPTION 'el cliente de Stripe no cambia' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS billing_customer_guard ON billing_customer;
CREATE TRIGGER billing_customer_guard BEFORE INSERT OR UPDATE ON billing_customer
    FOR EACH ROW EXECUTE FUNCTION billing_customer_guard();

CREATE OR REPLACE FUNCTION billing_customer_history_write() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.legal_name IS DISTINCT FROM OLD.legal_name
       OR NEW.tax_id IS DISTINCT FROM OLD.tax_id THEN
        INSERT INTO public.billing_customer_history (written_by, account_id, legal_name, tax_id)
        VALUES (session_user, NEW.account_id, NEW.legal_name, NEW.tax_id);
    END IF;
    RETURN NULL;
END
$$;

REVOKE ALL ON FUNCTION billing_customer_history_write() FROM PUBLIC;
DROP TRIGGER IF EXISTS billing_customer_history_write ON billing_customer;
CREATE TRIGGER billing_customer_history_write AFTER INSERT OR UPDATE ON billing_customer
    FOR EACH ROW EXECUTE FUNCTION billing_customer_history_write();

-- ---------------------------------------------------------------------------
-- Plazas (sección 3.2): un año pagado de un plan, con su suscripción de Stripe (una por
-- plaza) y su fecha de renovación. Una ficha activa de pago ocupa una plaza.
-- request_id: la petición de la web que la creó (para responder seat_pending).
-- review_reason: algo que revisar en el backoffice; 'duplicate_payment' si se cobró una
-- segunda plaza para una ficha que ya tenía la suya: queda libre en la cuenta, sin ficha.
CREATE TABLE IF NOT EXISTS billing_seat (
    id                     text PRIMARY KEY CHECK (id ~ '^seat_[0-9a-f]{8,32}$'),
    account_id             uuid NOT NULL REFERENCES account (id),
    plan                   text NOT NULL CHECK (plan IN ('pro', 'pro_whatsapp')),
    collection             text NOT NULL DEFAULT 'auto' CHECK (collection IN ('auto', 'manual')),
    stripe_subscription_id text UNIQUE CHECK (stripe_subscription_id ~ '^sub_[A-Za-z0-9]{1,250}$'),
    location_id            text,
    request_id             uuid,
    status                 text NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'active', 'payment_failed',
                                             'action_required', 'disputed', 'canceled')),
    current_period_start   timestamptz,
    current_period_end     timestamptz,
    cancel_at_period_end   boolean NOT NULL DEFAULT false,
    next_price_key         text CHECK (next_price_key IN ('pro_first_yearly', 'pro_additional_yearly',
                                                          'whatsapp_first_yearly',
                                                          'whatsapp_additional_yearly')),
    review_reason          text CHECK (review_reason ~ '^[a-z_]{1,40}$'),
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);

-- renewal_notice_for: el fin de periodo cuyo aviso de renovación (T-30) ya se envió, con el
-- precio congelado en Stripe (paso 8). Una vez por periodo.
ALTER TABLE billing_seat ADD COLUMN IF NOT EXISTS renewal_notice_for timestamptz;

-- Ventana beta: una plaza que una cuenta de la beta compra en sus 30 días tiene en Stripe una
-- prueba hasta beta_ends_at; su primer año se cobra ese día. trial_ends_at: el trial_end de
-- Stripe, copiado al volver a leer la suscripción. trial_notice_for: el trial_ends_at cuyo
-- aviso (trial_ending, 7 días antes) ya se envió.
ALTER TABLE billing_seat ADD COLUMN IF NOT EXISTS trial_ends_at timestamptz;
ALTER TABLE billing_seat ADD COLUMN IF NOT EXISTS trial_notice_for timestamptz;

CREATE INDEX IF NOT EXISTS billing_seat_account ON billing_seat (account_id);
CREATE INDEX IF NOT EXISTS billing_seat_location ON billing_seat (location_id)
    WHERE location_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_constraint WHERE conname = 'billing_seat_location_fk') THEN
        -- Si la retención borra la ficha, la plaza se queda sin ficha (pagada y libre).
        ALTER TABLE billing_seat ADD CONSTRAINT billing_seat_location_fk
            FOREIGN KEY (account_id, location_id) REFERENCES location (account_id, id)
            ON DELETE SET NULL (location_id);
    END IF;
END
$$;

-- Cada factura de Stripe de una plaza, copiada al volver a leerla (nunca de la carga del
-- evento). Importes en céntimos.
CREATE TABLE IF NOT EXISTS billing_charge (
    stripe_invoice_id text PRIMARY KEY CHECK (stripe_invoice_id ~ '^in_[A-Za-z0-9]{1,250}$'),
    account_id        uuid NOT NULL REFERENCES account (id),
    seat_id           text REFERENCES billing_seat (id),
    status            text CHECK (status ~ '^[a-z_]{1,40}$'),
    billing_reason    text CHECK (billing_reason ~ '^[a-z_]{1,60}$'),
    currency          text NOT NULL DEFAULT 'eur' CHECK (currency ~ '^[a-z]{3}$'),
    subtotal          integer NOT NULL DEFAULT 0,
    tax               integer NOT NULL DEFAULT 0,
    total             integer NOT NULL DEFAULT 0,
    amount_paid       integer NOT NULL DEFAULT 0,
    amount_refunded   integer NOT NULL DEFAULT 0,
    price_key         text,
    period_start      timestamptz,
    period_end        timestamptz,
    paid_at           timestamptz,
    payment_intent_id text CHECK (payment_intent_id ~ '^pi_[A-Za-z0-9]{1,250}$'),
    charge_id         text CHECK (charge_id ~ '^(ch|py)_[A-Za-z0-9]{1,250}$'),
    disputed          boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS billing_charge_account ON billing_charge (account_id);
CREATE INDEX IF NOT EXISTS billing_charge_intent ON billing_charge (payment_intent_id);

-- Factura legal en Odoo (paso 9, aún sin hacer: la tabla existe para que la web liste las
-- facturas y el backoffice la lea). Una por factura de Stripe y una rectificativa por
-- reembolso.
CREATE TABLE IF NOT EXISTS billing_document (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind              text NOT NULL CHECK (kind IN ('factura', 'rectificativa')),
    account_id        uuid NOT NULL REFERENCES account (id),
    stripe_invoice_id text CHECK (stripe_invoice_id ~ '^in_[A-Za-z0-9]{1,250}$'),
    stripe_refund_id  text CHECK (stripe_refund_id ~ '^re_[A-Za-z0-9]{1,250}$'),
    state             text NOT NULL DEFAULT 'pendiente'
                      CHECK (state IN ('pendiente', 'emitida', 'revision')),
    odoo_move_id      bigint,
    number            text CHECK (length(number) <= 60),
    amount_untaxed    integer,
    amount_tax        integer,
    amount_total      integer,
    verifactu_state   text CHECK (length(verifactu_state) <= 40),
    pdf               bytea,
    created_at        timestamptz NOT NULL DEFAULT now(),
    issued_at         timestamptz,
    CHECK ((kind = 'factura') = (stripe_invoice_id IS NOT NULL AND stripe_refund_id IS NULL)
           OR kind = 'rectificativa')
);

-- Paso 9: por qué una factura está en revisión (total distinto, Odoo la rechazó,
-- VERI*FACTU con errores) o qué encontró la conciliación diaria con Odoo. Sin datos
-- personales: un código.
ALTER TABLE billing_document
    ADD COLUMN IF NOT EXISTS review_reason text CHECK (review_reason ~ '^[a-z0-9_]{1,40}$');

CREATE UNIQUE INDEX IF NOT EXISTS billing_document_invoice ON billing_document (stripe_invoice_id)
    WHERE kind = 'factura';
CREATE UNIQUE INDEX IF NOT EXISTS billing_document_refund ON billing_document (stripe_refund_id)
    WHERE kind = 'rectificativa';
CREATE INDEX IF NOT EXISTS billing_document_account ON billing_document (account_id);

-- Reembolso excepcional (P2), que pide el backoffice (paso 12).
CREATE TABLE IF NOT EXISTS billing_refund_request (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        uuid NOT NULL REFERENCES account (id),
    stripe_invoice_id text NOT NULL CHECK (stripe_invoice_id ~ '^in_[A-Za-z0-9]{1,250}$'),
    amount            integer CHECK (amount > 0),
    reason            text NOT NULL CHECK (reason IN ('error_cobro', 'decision_comercial',
                                                      'cambio_tercero', 'disputa')),
    ticket_ref        text NOT NULL CHECK (length(ticket_ref) BETWEEN 1 AND 80),
    requested_by      text NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 80),
    state             text NOT NULL DEFAULT 'pendiente'
                      CHECK (state IN ('pendiente', 'hecho', 'fallido')),
    stripe_refund_id  text CHECK (stripe_refund_id ~ '^re_[A-Za-z0-9]{1,250}$'),
    created_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);

-- ---------------------------------------------------------------------------
-- Lo que la web pide al proceso de cobro (la API nunca llama a Stripe: sección 4.1). La
-- web consulta el resultado con GET /me/billing/requests/{id}.
-- - kind: seat (comprar una plaza para una ficha), cancel y resume (de una plaza), pay
--   (la factura pendiente, para desbloquear) y portal (cambiar la tarjeta).
-- - Columnas propias en vez de un jsonb de parámetros: cada una con su restricción.
-- - state: pendiente → lista (con url, siempre de *.stripe.com) o fallida (con code y
--   detail, listos para mostrar). Una sola vez: lo impone el disparador.
-- - expires_at: hasta cuándo vale el Checkout de una plaza (seat_pending).
-- Una sola petición de plaza pendiente por ficha (dos pestañas a la vez): el índice único.
CREATE TABLE IF NOT EXISTS billing_request (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  uuid NOT NULL REFERENCES account (id),
    kind        text NOT NULL CHECK (kind IN ('seat', 'cancel', 'resume', 'pay', 'portal')),
    location_id text CHECK (location_id ~ '^loc_[0-9a-z]{4,32}$'),
    plan        text CHECK (plan IN ('pro', 'pro_whatsapp')),
    seat_id     text CHECK (seat_id ~ '^seat_[0-9a-f]{8,32}$'),
    state       text NOT NULL DEFAULT 'pendiente' CHECK (state IN ('pendiente', 'lista', 'fallida')),
    url         text CHECK (length(url) <= 2000
                            AND url ~ '^https://([a-z0-9-]+\.)*stripe\.com/'),
    code        text CHECK (code ~ '^[a-z_]{1,40}$'),
    detail      text CHECK (length(detail) <= 300),
    expires_at  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CHECK ((kind = 'seat') = (location_id IS NOT NULL AND plan IS NOT NULL)),
    CHECK (kind NOT IN ('cancel', 'resume') OR seat_id IS NOT NULL),
    CHECK (state <> 'lista' OR url IS NOT NULL OR kind IN ('cancel', 'resume')),
    CHECK (state <> 'fallida' OR code IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS billing_request_account ON billing_request (account_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS billing_request_one_seat_pending ON billing_request (location_id)
    WHERE kind = 'seat' AND state = 'pendiente';

CREATE OR REPLACE FUNCTION billing_request_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF fichaitor_is_owner(TG_RELID) THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'pendiente' OR NEW.url IS NOT NULL OR NEW.code IS NOT NULL
           OR NEW.detail IS NOT NULL OR NEW.finished_at IS NOT NULL OR NEW.expires_at IS NOT NULL THEN
            RAISE EXCEPTION 'una petición nueva está pendiente' USING ERRCODE = 'check_violation';
        END IF;
        NEW.created_at := now();
        RETURN NEW;
    END IF;
    IF OLD.state <> 'pendiente' OR NEW.state = 'pendiente'
       OR (NEW.account_id, NEW.kind, NEW.location_id, NEW.plan, NEW.seat_id, NEW.created_at)
          IS DISTINCT FROM (OLD.account_id, OLD.kind, OLD.location_id, OLD.plan, OLD.seat_id,
                            OLD.created_at) THEN
        RAISE EXCEPTION 'una petición se resuelve una vez' USING ERRCODE = 'check_violation';
    END IF;
    NEW.finished_at := now();
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS billing_request_guard ON billing_request;
CREATE TRIGGER billing_request_guard BEFORE INSERT OR UPDATE ON billing_request
    FOR EACH ROW EXECUTE FUNCTION billing_request_guard();

-- ---------------------------------------------------------------------------
-- Eventos de Stripe (sección 4.2). La API apunta solo identificadores, nunca la carga (que
-- lleva nombre, dirección y correo), y solo si la firma es buena. El proceso de cobro
-- vuelve a leer el objeto en Stripe: un id inventado acaba en outcome = 'not_found'.
CREATE TABLE IF NOT EXISTS stripe_event (
    id             text PRIMARY KEY CHECK (id ~ '^evt_[A-Za-z0-9]{1,250}$'),
    type           text NOT NULL CHECK (type ~ '^[a-z_.]{1,100}$'),
    object_id      text CHECK (object_id ~ '^[a-z]{2,8}_[A-Za-z0-9_]{1,250}$'),
    livemode       boolean NOT NULL,
    stripe_created timestamptz,
    received_at    timestamptz NOT NULL DEFAULT now(),
    processed_at   timestamptz,
    outcome        text CHECK (outcome ~ '^[a-z_]{1,40}$')
);

CREATE INDEX IF NOT EXISTS stripe_event_unprocessed ON stripe_event (received_at)
    WHERE processed_at IS NULL;

-- ---------------------------------------------------------------------------
-- Bloqueo por impago (sección 3.6 y decisión 9.0, 2 y 3): toda la cuenta, sin límite de
-- tiempo, hasta que pague. Como mucho uno abierto por cuenta y factura.
CREATE TABLE IF NOT EXISTS account_billing_block (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id        uuid NOT NULL REFERENCES account (id),
    blocked_at        timestamptz NOT NULL DEFAULT now(),
    reason            text NOT NULL CHECK (reason IN ('payment_failed', 'action_required', 'dispute',
                                                      'manual_unpaid')),
    stripe_invoice_id text CHECK (stripe_invoice_id ~ '^in_[A-Za-z0-9]{1,250}$'),
    seat_id           text,
    unblocked_at      timestamptz,
    unblocked_by      text CHECK (unblocked_by IN ('payment', 'timeout', 'admin')),
    CHECK ((unblocked_at IS NULL) = (unblocked_by IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS account_billing_block_open
    ON account_billing_block (account_id, coalesce(stripe_invoice_id, ''))
    WHERE unblocked_at IS NULL;

-- La condición que miran todas las barreras (API, worker y conversación). Con los
-- privilegios de quien la llama: todos leen (account_id, unblocked_at).
CREATE OR REPLACE FUNCTION fichaitor_account_blocked(account uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.account_billing_block b
                   WHERE b.account_id = account AND b.unblocked_at IS NULL)
$$;

REVOKE ALL ON FUNCTION fichaitor_account_blocked(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fichaitor_account_blocked(uuid)
    TO fichaitor_api, fichaitor_worker, fichaitor_conversation, fichaitor_billing, fichaitor_admin;
REVOKE ALL ON FUNCTION fichaitor_session_in(name) FROM PUBLIC;
-- La llama plan_entitlement_guard con los privilegios de quien escribe un derecho.
GRANT EXECUTE ON FUNCTION fichaitor_session_in(name) TO fichaitor_billing, fichaitor_admin;

-- ---------------------------------------------------------------------------
-- Derechos de plan (sección 5.1). Un derecho 'pago' por factura de Stripe pagada
-- (external_ref = in_…), con su plaza. 'cortesia' y 'recomendacion', para el backoffice.
ALTER TABLE plan_entitlement DROP CONSTRAINT IF EXISTS plan_entitlement_source_check;
ALTER TABLE plan_entitlement ADD CONSTRAINT plan_entitlement_source_check
    CHECK (source IN ('beta', 'manual', 'pago', 'cortesia', 'recomendacion'));
ALTER TABLE plan_entitlement
    ADD COLUMN IF NOT EXISTS seat_id     text,
    ADD COLUMN IF NOT EXISTS provisional boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS granted_by  text CHECK (length(granted_by) BETWEEN 1 AND 80),
    ADD COLUMN IF NOT EXISTS note_code   text CHECK (note_code ~ '^[a-z_]{1,40}$');
CREATE UNIQUE INDEX IF NOT EXISTS plan_entitlement_paid_ref ON plan_entitlement (external_ref)
    WHERE source = 'pago';
CREATE INDEX IF NOT EXISTS plan_entitlement_seat ON plan_entitlement (seat_id)
    WHERE seat_id IS NOT NULL;

-- Quién escribe qué derecho (exime al propietario):
-- - 'pago', solo fichaitor_billing, con plaza y factura;
-- - 'manual', 'cortesia', 'recomendacion' y 'beta', solo fichaitor_admin, con granted_by,
--   10 como mucho y un fin de 2 años como mucho;
-- - starts_at a menos de un minuto de now(): nadie fecha un derecho en el pasado;
-- - en un UPDATE solo cambia ends_at, y solo hacia atrás (nadie alarga un derecho), salvo
--   que fichaitor_billing quite provisional.
CREATE OR REPLACE FUNCTION plan_entitlement_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    is_billing boolean;
    is_admin boolean;
BEGIN
    IF fichaitor_is_owner(TG_RELID) THEN
        RETURN NEW;
    END IF;
    is_billing := fichaitor_session_in('fichaitor_billing');
    is_admin := fichaitor_session_in('fichaitor_admin');
    IF TG_OP = 'INSERT' THEN
        IF NEW.source = 'pago' THEN
            IF NOT is_billing OR NEW.seat_id IS NULL OR NEW.external_ref IS NULL THEN
                RAISE EXCEPTION 'un derecho pagado lo crea el cobro, con plaza y factura'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        ELSIF NOT is_admin OR is_billing OR NEW.granted_by IS NULL OR NEW.quantity > 10
              OR NEW.ends_at IS NULL OR NEW.ends_at > NEW.starts_at + interval '2 years' THEN
            RAISE EXCEPTION 'derecho no permitido' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF abs(extract(epoch FROM NEW.starts_at - now())) > 60 THEN
            RAISE EXCEPTION 'un derecho empieza ahora' USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.id, NEW.account_id, NEW.plan, NEW.quantity, NEW.starts_at, NEW.source,
        NEW.external_ref, NEW.seat_id, NEW.granted_by, NEW.note_code, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.account_id, OLD.plan, OLD.quantity, OLD.starts_at, OLD.source,
        OLD.external_ref, OLD.seat_id, OLD.granted_by, OLD.note_code, OLD.created_at) THEN
        RAISE EXCEPTION 'de un derecho solo cambia el fin' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.ends_at IS NULL AND OLD.ends_at IS NOT NULL
       OR NEW.ends_at > coalesce(OLD.ends_at, 'infinity'::timestamptz) THEN
        RAISE EXCEPTION 'un derecho no se alarga' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.provisional IS DISTINCT FROM OLD.provisional
       AND NOT (is_billing AND OLD.provisional AND NOT NEW.provisional) THEN
        RAISE EXCEPTION 'provisional solo lo quita el cobro' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS plan_entitlement_guard ON plan_entitlement;
CREATE TRIGGER plan_entitlement_guard BEFORE INSERT OR UPDATE ON plan_entitlement
    FOR EACH ROW EXECUTE FUNCTION plan_entitlement_guard();

-- Derecho beta (sección 4.6): account_beta_entitlement, en schema.sql, ya mira
-- billing_setting (solo antes del lanzamiento, y hasta el fin de la beta).

-- ---------------------------------------------------------------------------
-- Privilegios (siempre REVOKE ALL primero). Los del backoffice, en schema_admin.sql.
REVOKE ALL ON billing_setting, billing_customer, billing_customer_history, billing_seat,
    billing_charge, billing_document, billing_refund_request, billing_request, stripe_event,
    account_billing_block
    FROM PUBLIC, fichaitor_api, fichaitor_worker, fichaitor_conversation, fichaitor_billing;

-- La API.
GRANT SELECT ON billing_setting, billing_seat, billing_document, billing_customer TO fichaitor_api;
GRANT INSERT (account_id, legal_name, tax_id_type, tax_id_kind, tax_id, address_line1,
              address_line2, postal_code, city, province, country, territory, tax_treatment,
              invoice_email)
    ON billing_customer TO fichaitor_api;
GRANT UPDATE (legal_name, tax_id_type, tax_id_kind, tax_id, address_line1, address_line2,
              postal_code, city, province, country, territory, tax_treatment, invoice_email)
    ON billing_customer TO fichaitor_api;
GRANT SELECT ON billing_request TO fichaitor_api;
GRANT INSERT (account_id, kind, location_id, plan, seat_id) ON billing_request TO fichaitor_api;
GRANT INSERT (id, type, object_id, livemode, stripe_created) ON stripe_event TO fichaitor_api;
GRANT SELECT (account_id, blocked_at, reason, unblocked_at) ON account_billing_block TO fichaitor_api;

-- El proceso de cobro: todo en sus tablas salvo borrar; de la cuenta y la ficha, lo justo
-- para activar la ficha de una plaza nueva (el disparador location_active_guard decide si
-- cabe y lee la cuenta con FOR UPDATE, que pide UPDATE en alguna columna de account: por
-- eso selected_location_id, que el cobro no cambia).
GRANT SELECT ON billing_setting TO fichaitor_billing;
GRANT SELECT, INSERT, UPDATE ON billing_seat, billing_charge, billing_document,
    billing_refund_request, account_billing_block TO fichaitor_billing;
GRANT SELECT ON billing_customer TO fichaitor_billing;
GRANT UPDATE (stripe_customer_id) ON billing_customer TO fichaitor_billing;
GRANT SELECT ON billing_request TO fichaitor_billing;
GRANT UPDATE (state, url, code, detail, expires_at) ON billing_request TO fichaitor_billing;
GRANT SELECT ON stripe_event TO fichaitor_billing;
GRANT UPDATE (processed_at, outcome) ON stripe_event TO fichaitor_billing;
GRANT SELECT (id, email, deleted_at) ON account TO fichaitor_billing;
GRANT UPDATE (selected_location_id) ON account TO fichaitor_billing;
GRANT SELECT (id, account_id, gbp_location, title, plan, active_since, removed_at)
    ON location TO fichaitor_billing;
GRANT UPDATE (active_since, plan) ON location TO fichaitor_billing;
GRANT SELECT (account_id, restored_at, effective_at) ON offboarding TO fichaitor_billing;
GRANT SELECT, INSERT ON plan_entitlement TO fichaitor_billing;
GRANT UPDATE (ends_at, provisional) ON plan_entitlement TO fichaitor_billing;
DROP POLICY IF EXISTS plan_entitlement_billing_read ON plan_entitlement;
CREATE POLICY plan_entitlement_billing_read ON plan_entitlement FOR SELECT TO fichaitor_billing
    USING (true);
DROP POLICY IF EXISTS plan_entitlement_billing_insert ON plan_entitlement;
CREATE POLICY plan_entitlement_billing_insert ON plan_entitlement FOR INSERT TO fichaitor_billing
    WITH CHECK (source = 'pago');
DROP POLICY IF EXISTS plan_entitlement_billing_update ON plan_entitlement;
CREATE POLICY plan_entitlement_billing_update ON plan_entitlement FOR UPDATE TO fichaitor_billing
    USING (source = 'pago') WITH CHECK (source = 'pago');

-- El worker y la conversación: si la cuenta está bloqueada (paso 7). El worker, además,
-- borra las peticiones y los eventos vencidos (retención).
GRANT SELECT (account_id, unblocked_at) ON account_billing_block
    TO fichaitor_worker, fichaitor_conversation;
GRANT SELECT (created_at) ON billing_request TO fichaitor_worker;
GRANT DELETE ON billing_request TO fichaitor_worker;
GRANT SELECT (received_at, processed_at) ON stripe_event TO fichaitor_worker;
GRANT DELETE ON stripe_event TO fichaitor_worker;
-- Paso 9: el worker adjunta el PDF de una factura emitida a su correo (send_mail). Solo
-- esas columnas: ni importes, ni números, ni la cuenta.
GRANT SELECT (id, state, pdf) ON billing_document TO fichaitor_worker;

-- Paso 8: el aviso del fin de la beta, una vez por fecha, y el embudo (checkout_started al
-- abrir un Checkout y paid con la primera plaza pagada). Del embudo, solo insertar.
GRANT UPDATE (beta_notice_for) ON billing_setting TO fichaitor_billing;
REVOKE ALL ON funnel_event FROM fichaitor_billing;
GRANT INSERT (kind, source) ON funnel_event TO fichaitor_billing;

-- ---------------------------------------------------------------------------
-- Cola de jobs (sección 5.1): el proceso de cobro toma y cierra solo sus tipos y encola
-- solo esos y el correo. La API encola además stripe_event y billing_request: están en
-- job_api_insert, en schema_channels.sql, con las demás políticas de la cola.
GRANT SELECT ON job TO fichaitor_billing;
GRANT INSERT (kind, payload, run_at, idempotency_key, max_attempts) ON job TO fichaitor_billing;
GRANT UPDATE (state, attempts, locked_by, locked_at, run_at, finished_at, last_error, payload)
    ON job TO fichaitor_billing;

DROP POLICY IF EXISTS job_billing ON job;
CREATE POLICY job_billing ON job FOR ALL TO fichaitor_billing
    USING (kind IN ('stripe_event', 'billing_request', 'billing_reconcile', 'billing_renewal_notice',
                    'billing_enforce', 'billing_refund', 'issue_invoice', 'issue_credit_note',
                    'billing_beta_notice', 'billing_purge'))
    WITH CHECK (kind IN ('stripe_event', 'billing_request', 'billing_reconcile',
                         'billing_renewal_notice', 'billing_enforce', 'billing_refund',
                         'issue_invoice', 'issue_credit_note', 'billing_beta_notice',
                         'billing_purge')
                AND job_key_fits(kind, idempotency_key));
DROP POLICY IF EXISTS job_billing_mail ON job;
CREATE POLICY job_billing_mail ON job FOR INSERT TO fichaitor_billing
    WITH CHECK (kind = 'send_mail' AND job_key_fits(kind, idempotency_key));

-- Solo en una base de pruebas: tipos propios ('pytest-billing-...'), como en schema_channels.sql.
DROP POLICY IF EXISTS job_pytest_billing ON job;
DO $$
BEGIN
    IF current_database() LIKE '%\_test' THEN
        CREATE POLICY job_pytest_billing ON job FOR ALL TO fichaitor_billing
            USING (kind LIKE 'pytest-%')
            WITH CHECK (kind LIKE 'pytest-%' AND job_key_fits(kind, idempotency_key));
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- Retención de las tablas de cobro (docs/retencion.md): 6 años (art. 30 del Código de
-- Comercio), también después de la baja. La llama a diario el proceso de cobro (job
-- billing_purge), que no tiene DELETE en ninguna tabla: la función borra solo lo que pasa
-- del plazo, y el plazo está aquí, no en un parámetro. Nunca un bloqueo abierto. Los datos
-- fiscales de una cuenta, solo cuando la cuenta está dada de baja y ya no le queda ninguna
-- factura, cobro ni plaza («6 años desde la última factura»).
CREATE OR REPLACE FUNCTION billing_purge() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cutoff timestamptz := now() - interval '6 years';
    counts jsonb := '{}'::jsonb;
    n bigint;
BEGIN
    DELETE FROM public.billing_document
    WHERE coalesce(issued_at, created_at) < cutoff;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('billing_document', n);

    DELETE FROM public.billing_refund_request
    WHERE coalesce(finished_at, created_at) < cutoff;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('billing_refund_request', n);

    DELETE FROM public.billing_charge
    WHERE coalesce(paid_at, period_end, created_at) < cutoff;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('billing_charge', n);

    DELETE FROM public.account_billing_block
    WHERE unblocked_at IS NOT NULL AND unblocked_at < cutoff;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('account_billing_block', n);

    DELETE FROM public.billing_seat s
    WHERE coalesce(s.current_period_end, s.created_at) < cutoff
      AND NOT EXISTS (SELECT 1 FROM public.billing_charge c WHERE c.seat_id = s.id);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('billing_seat', n);

    DELETE FROM public.billing_customer bc
    USING public.account a
    WHERE a.id = bc.account_id AND a.deleted_at IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM public.billing_document d WHERE d.account_id = bc.account_id)
      AND NOT EXISTS (SELECT 1 FROM public.billing_charge c WHERE c.account_id = bc.account_id)
      AND NOT EXISTS (SELECT 1 FROM public.billing_seat s WHERE s.account_id = bc.account_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('billing_customer', n);

    DELETE FROM public.billing_customer_history h
    WHERE h.at < cutoff
       OR NOT EXISTS (SELECT 1 FROM public.billing_customer bc WHERE bc.account_id = h.account_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('billing_customer_history', n);
    RETURN counts;
END
$$;

REVOKE ALL ON FUNCTION billing_purge() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION billing_purge() TO fichaitor_billing;

-- ---------------------------------------------------------------------------
-- Solo desarrollo local y pruebas: está copiado en db/dev-init.sql. En producción el
-- usuario se crea aparte (deploy/init-roles.sh: fichaitor_billing_prod).
--
-- DO $$
-- BEGIN
--     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'fichaitor_billing_dev') THEN
--         CREATE ROLE fichaitor_billing_dev LOGIN PASSWORD 'dev' IN ROLE fichaitor_billing;
--     END IF;
-- END
-- $$;
