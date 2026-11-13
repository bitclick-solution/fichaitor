// Cliente del backoffice (docs/contrato-backoffice.md).
//
// Reglas que cumple siempre:
//  - Rutas relativas, mismo origen: /admin/api/…
//  - Todo lo que no es GET lleva `X-Requested-With: fichaitor-admin` y, con sesión, `X-CSRF-Token`.
//  - El token CSRF vive solo en memoria y se renueva con cada respuesta que lo trae.
//  - Nada se guarda en localStorage ni en sessionStorage.

export type Rol = "soporte" | "finanzas" | "propietario";

export interface Sesion {
  stage: "ok";
  username: string;
  role: Rol;
  totp_fresh_until: string;
}

export type EstadoSesion = Sesion | { stage: "reenroll" };

export class ErrorAdmin extends Error {
  constructor(
    readonly status: number,
    readonly codigo: string,
    message: string,
    /** En un 422 `validation`, los campos que fallan (nunca sus valores). */
    readonly campos: string[] = [],
  ) {
    super(message);
    this.name = "ErrorAdmin";
  }
}

let csrf: string | null = null;

/** Se llama cuando la sesión deja de valer (401 `session_invalid`): App vuelve a «Entrar». */
let alPerderSesion: () => void = () => undefined;
export function siSePierdeLaSesion(f: () => void): void {
  alPerderSesion = f;
}

/**
 * Cuando una acción sensible responde 403 `totp_required`, se pide un código con esta función (la pone
 * App, que pinta el diálogo) y se repite la petición una vez. null: la persona ha cancelado.
 */
let pedirCodigo: (fallo: boolean) => Promise<string | null> = async () => null;
/** `fallo`: el código anterior no valía (para avisarlo en el diálogo). */
export function siHaceFaltaCodigo(f: (fallo: boolean) => Promise<string | null>): void {
  pedirCodigo = f;
}

type Metodo = "GET" | "POST" | "PUT";

const GENERICO = "Algo ha fallado. Prueba de nuevo.";

async function una<T>(method: Metodo, ruta: string, cuerpo?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") {
    headers["X-Requested-With"] = "fichaitor-admin";
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  if (cuerpo !== undefined) headers["Content-Type"] = "application/json";
  const init: RequestInit = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (cuerpo !== undefined) init.body = JSON.stringify(cuerpo);

  let r: Response;
  try {
    r = import.meta.env.MODE === "mock" ? await (await import("./mock")).simular(method, `/admin/api${ruta}`, cuerpo) : await fetch(`/admin/api${ruta}`, init);
  } catch {
    throw new ErrorAdmin(0, "network", "No hay conexión con el backoffice. ¿Está abierto el túnel?");
  }

  let datos: unknown = null;
  const texto = await r.text();
  if (texto) {
    try {
      datos = JSON.parse(texto);
    } catch {
      datos = null;
    }
  }
  if (r.ok) {
    if (datos && typeof datos === "object" && "csrf_token" in datos && typeof datos.csrf_token === "string") csrf = datos.csrf_token;
    return datos as T;
  }
  let codigo = "error";
  let mensaje = GENERICO;
  let campos: string[] = [];
  if (datos && typeof datos === "object") {
    if ("code" in datos && typeof datos.code === "string") codigo = datos.code;
    if ("message" in datos && typeof datos.message === "string" && datos.message) mensaje = datos.message;
    if ("fields" in datos && Array.isArray(datos.fields)) campos = datos.fields.filter((x): x is string => typeof x === "string");
  }
  if (r.status === 401 && codigo === "session_invalid") {
    csrf = null;
    alPerderSesion();
  }
  throw new ErrorAdmin(r.status, codigo, mensaje, campos);
}

/**
 * Confirmación con código en curso. Si dos acciones sensibles piden código a la vez, comparten la misma
 * confirmación: un solo diálogo, y las dos se repiten (o se cancelan) con su resultado.
 */
let confirmando: Promise<void> | null = null;

async function confirmarConCodigo(): Promise<void> {
  for (let fallo = false; ; fallo = true) {
    const code = await pedirCodigo(fallo);
    if (code === null) throw new ErrorAdmin(403, "cancelled", "Acción cancelada: falta el código.");
    try {
      await una("POST", "/session/totp", { code });
      return;
    } catch (e) {
      // Código incorrecto o ya usado: se vuelve a pedir. Otro error: se corta.
      if (!(e instanceof ErrorAdmin) || e.codigo !== "totp_invalid") throw e;
    }
  }
}

/** Una petición; si es sensible y el TOTP ha caducado, pide el código y la repite una vez. */
async function pedir<T>(method: Metodo, ruta: string, cuerpo?: unknown): Promise<T> {
  try {
    return await una<T>(method, ruta, cuerpo);
  } catch (e) {
    if (!(e instanceof ErrorAdmin) || e.status !== 403 || e.codigo !== "totp_required") throw e;
    if (!confirmando)
      confirmando = confirmarConCodigo().finally(() => {
        confirmando = null;
      });
    await confirmando;
    return await una<T>(method, ruta, cuerpo);
  }
}

export function olvidarCsrf(): void {
  csrf = null;
}

// ── Tipos de las respuestas ──

export interface CuentaEncontrada {
  account_id: string;
  email_masked: string;
  created_at: string;
  deleted_at: string | null;
  disconnected_at: string | null;
  locations: number;
  active_locations: number;
}

export interface Derecho {
  id: string;
  plan: string;
  quantity: number;
  starts_at: string;
  ends_at: string | null;
  source: "beta" | "manual" | "pago" | "cortesia" | "recomendacion";
  seat_id: string | null;
  provisional: boolean;
  granted_by: string | null;
  note_code: string | null;
  current: boolean;
}

export interface Bloqueo {
  id: number;
  blocked_at: string;
  reason: string;
  seat_id: string | null;
  unblocked_at: string | null;
  unblocked_by: string | null;
  stripe_invoice_id?: string | null;
}

export interface DetalleCuenta {
  account: { id: string; email_masked: string; created_at: string; deleted_at: string | null; disconnected_at: string | null };
  blocked: boolean;
  locations: { id: string; plan: string | null; active_since: string | null; deactivated_at: string | null; removed_at: string | null; verified: boolean }[];
  entitlements: Derecho[];
  seats: {
    id: string;
    plan: string;
    collection: string;
    location_id: string | null;
    status: string;
    current_period_start: string | null;
    current_period_end: string | null;
    cancel_at_period_end: boolean;
    next_price_key: string | null;
    trial_ends_at: string | null;
    review_reason: string | null;
    created_at: string;
  }[];
  blocks: Bloqueo[];
  activations: { at: string; location_id: string; event: string; plan: string | null; reason: string | null }[];
  offboardings: { id: string; scope: string; confirmed_at: string; delete_at: string; restored_at: string | null; effective_at: string | null; reason: string | null }[];
  next_cursors: Cursores;
}

// ── Paginación (contrato, 1.3): cursor opaco; null cuando no hay más ──

export type Cursor = string | null;
export type Cursores = Record<string, Cursor>;
export interface Pagina<T> {
  items: T[];
  next_cursor: Cursor;
}

/** «?limit=…&cursor=…» o «&…», sin parámetros vacíos. */
function paginas(cursor: Cursor | undefined, limit?: number, primero = "?"): string {
  const p = new URLSearchParams();
  if (limit) p.set("limit", String(limit));
  if (cursor) p.set("cursor", cursor);
  const s = p.toString();
  return s ? `${primero}${s}` : "";
}

/** Listados de una cuenta con páginas siguientes en /accounts/{id}/lists/{nombre}. */
export type ListaCuenta =
  "locations" | "entitlements" | "seats" | "blocks" | "activations" | "offboardings" | "billing_seats" | "charges" | "documents" | "refunds" | "billing_blocks";
/** Listados de la revisión del cobro con páginas siguientes en /billing/review/lists/{nombre}. */
export type ListaRevision = "documents" | "seats" | "refunds" | "events" | "paid_without_document" | "blocks";

export interface NombreFicha {
  location_id: string;
  title: string;
  active_since: string | null;
  plan: string | null;
  deactivated_at: string | null;
  removed_at: string | null;
  verified: boolean;
}

export interface Cargo {
  stripe_invoice_id: string;
  seat_id: string | null;
  status: string;
  billing_reason: string | null;
  currency: string;
  subtotal: number;
  tax: number;
  total: number;
  amount_paid: number;
  amount_refunded: number;
  price_key: string | null;
  period_start: string | null;
  period_end: string | null;
  paid_at: string | null;
  disputed: boolean;
  stripe_url: string | null;
  payment_url: string | null;
}

export interface DocumentoLegal {
  id: string;
  kind: string;
  stripe_invoice_id: string | null;
  state: string;
  number: string | null;
  amount_total: number | null;
  verifactu_state: string | null;
  review_reason: string | null;
  created_at: string;
  issued_at?: string | null;
  odoo_url?: string | null;
  stripe_url?: string | null;
  account_id?: string;
  odoo_move_id?: number | null;
}

export interface Reembolso {
  id: string;
  stripe_invoice_id: string;
  amount: number | null;
  reason: string;
  ticket_ref?: string;
  requested_by: string;
  state: string;
  created_at: string;
  finished_at?: string | null;
  account_id?: string;
}

export interface CobroCuenta {
  livemode: boolean;
  customer: {
    stripe_customer_id: string;
    legal_name: string;
    tax_id_type: string;
    tax_id: string;
    country: string;
    territory: string;
    tax_treatment: string;
    updated_at: string;
    stripe_url: string | null;
  } | null;
  seats: {
    id: string;
    plan: string;
    collection: string;
    stripe_subscription_id: string | null;
    location_id: string | null;
    status: string;
    current_period_end: string | null;
    cancel_at_period_end: boolean;
    next_price_key: string | null;
    review_reason: string | null;
    stripe_url: string | null;
  }[];
  charges: Cargo[];
  documents: DocumentoLegal[];
  refunds: Reembolso[];
  blocks: Bloqueo[];
  next_cursors: Cursores;
}

export interface ContactoFiscal {
  legal_name: string;
  tax_id: string;
  address_line1: string;
  address_line2: string | null;
  postal_code: string;
  city: string;
  province: string | null;
  country: string;
  invoice_email: string;
}

export interface Revision {
  livemode: boolean;
  counts: Record<string, number>;
  documents: DocumentoLegal[];
  seats: { id: string; account_id: string; plan: string; status: string; stripe_subscription_id: string | null; review_reason: string | null }[];
  refunds: Reembolso[];
  events: { id: string; type: string; object_id: string | null; received_at: string; processed_at: string | null; outcome: string | null }[];
  paid_without_document: { entitlement_id: string; account_id: string; stripe_invoice_id: string; seat_id: string | null; starts_at: string }[];
  blocks: (Bloqueo & { account_id: string })[];
  links: EnlacesRevision;
  odoo_link: string;
  next_cursors: Cursores;
}

export interface EnlacesRevision {
  customer: string;
  invoice: string;
  subscription: string;
  payment: string;
}

export type FalloTrabajo = { job_id: string; kind: string; attempts: number; finished_at: string | null; last_error: string | null };
export type PasadaRetencion = { run_id?: number; at: string; deleted: Record<string, number> };
export type FilaSync = { account_id?: string; location_id: string; status: string; status_at: string | null; synced_at: string | null };
export type Chat = { identity_id: string; channel: string; verified_at: string | null; last_inbound_at: string | null; unlinked_at: string | null };

export interface Salud {
  summaries: Record<string, number>;
  summaries_stale: number;
  channels: Record<string, { linked: number; unlinked: number }>;
  jobs_stuck: number;
  jobs_failed_24h: number;
  jobs: { kind: string; state: string; jobs: number; oldest_run_at: string | null; stuck: number }[];
  failed_jobs: FalloTrabajo[];
  retention_runs: PasadaRetencion[];
  sync_errors: (FilaSync & { account_id: string })[];
  next_cursors: Cursores;
}

export interface Metricas {
  period: { from: string; to: string };
  revenue: { arr_cents: number; mrr_cents: number; estimated_seats: number };
  seats_in_force: { plan: string; next_price_key: string | null; seats: number }[];
  signups: { accounts: number; first_seats: number; new_seats: number };
  churn: { seats_canceled: number; seats_to_cancel: number; account_leaves: number };
  blocks: { opened: number; closed: number; open_now: number };
  conversion: { beta_accounts: number; beta_paid: number; free_accounts: number; free_paid: number };
  funnel: Record<string, number>;
}

export interface CeldaEspera {
  week: string;
  channel: string;
  score_band: string;
  source: string;
  people: number;
}

export interface Persona {
  admin_user_id: string;
  username: string;
  role: Rol;
  active: boolean;
  must_enroll: boolean;
  locked_until: string | null;
  created_at: string;
  password_changed_at: string | null;
}

export interface FilaAuditoria {
  id: number;
  at: string;
  written_by: string;
  username: string | null;
  role: string | null;
  action: string;
  account_id: string | null;
  target: string | null;
  ticket_ref: string | null;
  detail: Record<string, unknown>;
}

// ── Comunicaciones por correo (contrato, apartado 10) ──

export type TipoComunicacion = "servicio" | "comercial";
export type Publico = "todas" | "beta" | "plan" | "cuenta";

/** Lo que se redacta: el cuerpo de POST y PUT /communications. */
export interface BorradorComunicacion {
  kind: TipoComunicacion;
  subject: string;
  preheader: string;
  headline: string;
  paragraphs: string[];
  button_label: string | null;
  button_url: string | null;
  audience: Publico;
  audience_plan: string | null;
  audience_account: string | null;
}

export interface Comunicacion extends BorradorComunicacion {
  communication_id: string;
  state: "borrador" | "enviando" | "enviada";
  created_at: string;
  updated_at: string;
  created_by: string | null;
  test_sent_at: string | null;
  test_is_current: boolean;
  confirmed_at: string | null;
  confirmed_by: string | null;
  per_minute: number | null;
  recipients: number | null;
  excluded: number | null;
  sent: number;
  failed: number;
  skipped: number;
  finished_at: string | null;
}

/** Una fila del historial: sin los párrafos. */
export type FilaComunicacion = Omit<
  Comunicacion,
  "paragraphs" | "preheader" | "headline" | "button_label" | "button_url" | "updated_at" | "test_is_current" | "per_minute"
>;

export interface VistaPrevia {
  subject: string;
  preheader: string;
  html: string;
  text: string;
  headers: { name: string; value: string }[];
}

export interface AltaTotp {
  otpauth_uri: string;
  secret: string;
  state: string;
}

const enc = encodeURIComponent;

export const api = {
  // ── Sesión
  sesion: () => una<EstadoSesion>("GET", "/session"),
  entrar: (username: string, password: string) => una<{ stage: "totp" }>("POST", "/login", { username, password }),
  codigo: (code: string) => una<Sesion>("POST", "/session/totp", { code }),
  recuperacion: (code: string) => una<{ stage: "reenroll" }>("POST", "/login/recovery", { code }),
  salir: () => una<{ ok: true }>("POST", "/logout"),
  // TOTP nuevo: tras un código de recuperación o voluntario (este, acción con TOTP reciente).
  totpEmpezar: () => pedir<AltaTotp>("POST", "/totp/start"),
  totpTerminar: (state: string, code: string) => pedir<{ recovery_codes: string[] }>("POST", "/totp/finish", { state, code }),
  // Tras un código de recuperación no hay app con la que confirmar: sin el reintento con código.
  totpEmpezarObligatorio: () => una<AltaTotp>("POST", "/totp/start"),
  totpTerminarObligatorio: (state: string, code: string) => una<{ recovery_codes: string[] }>("POST", "/totp/finish", { state, code }),
  cambiarContrasena: (current_password: string, new_password: string) => pedir<{ ok: true; changed_at: string }>("POST", "/password", { current_password, new_password }),
  // ── Alta por enlace
  altaEmpezar: (token: string) => una<AltaTotp & { username: string; role: Rol }>("POST", "/enrollment/start", { token }),
  altaTerminar: (token: string, password: string, state: string, code: string) =>
    una<{ username: string; recovery_codes: string[] }>("POST", "/enrollment/finish", { token, password, state, code }),
  // ── Cuentas
  buscar: (q: string, cursor?: Cursor) => una<{ accounts: CuentaEncontrada[]; next_cursor: Cursor }>("GET", `/accounts?q=${enc(q)}${paginas(cursor, undefined, "&")}`),
  cuenta: (id: string) => una<DetalleCuenta>("GET", `/accounts/${enc(id)}`),
  /** Página siguiente de un listado del resumen o del cobro de una cuenta. */
  listaCuenta: <T>(id: string, nombre: ListaCuenta, cursor: string) => una<Pagina<T>>("GET", `/accounts/${enc(id)}/lists/${nombre}${paginas(cursor)}`),
  // Cada página de nombres es una lectura con ticket, y se audita.
  nombresFichas: (id: string, ticket_ref: string, cursor?: Cursor) =>
    una<{ locations: NombreFicha[]; next_cursor: Cursor }>("POST", `/accounts/${enc(id)}/locations${paginas(cursor)}`, { ticket_ref }),
  sincronizacion: (id: string, cursor?: Cursor) => una<{ sync: FilaSync[]; next_cursor: Cursor }>("GET", `/accounts/${enc(id)}/sync${paginas(cursor)}`),
  chats: (id: string, cursor?: Cursor) => una<{ channels: Chat[]; next_cursor: Cursor }>("GET", `/accounts/${enc(id)}/channels${paginas(cursor)}`),
  comprobarCorreo: (id: string, raw_message: string) =>
    una<{ dmarc: string; from_domain: string; matches_account: boolean }>("POST", `/accounts/${enc(id)}/offboarding/check`, { raw_message }),
  bajaPorCorreo: (id: string, scope: "ficha" | "cuenta", location_id: string | null, ticket_ref: string) =>
    pedir<{ offboarding_id: string; delete_at: string }>(
      "POST",
      `/accounts/${enc(id)}/offboarding`,
      location_id ? { scope, location_id, ticket_ref } : { scope, ticket_ref },
    ),
  // ── Cobro
  cobro: (id: string) => una<CobroCuenta>("GET", `/accounts/${enc(id)}/billing`),
  contactoFiscal: (id: string, ticket_ref: string) => una<ContactoFiscal>("POST", `/accounts/${enc(id)}/billing/contact`, { ticket_ref }),
  revision: () => una<Revision>("GET", "/billing/review"),
  listaRevision: <T>(nombre: ListaRevision, cursor: string) =>
    una<Pagina<T> & { links: EnlacesRevision; odoo_link: string }>("GET", `/billing/review/lists/${nombre}${paginas(cursor)}`),
  reembolsar: (stripe_invoice_id: string, amount: number | null, reason: string, ticket_ref: string) =>
    pedir<{ refund_request_id: string }>(
      "POST",
      "/refunds",
      amount === null ? { stripe_invoice_id, reason, ticket_ref } : { stripe_invoice_id, amount, reason, ticket_ref },
    ),
  // ── Derechos y bloqueos
  conceder: (id: string, cuerpo: { plan: string; quantity: number; source: string; ends_at: string; note_code?: string; ticket_ref: string }) =>
    pedir<{ entitlement_id: string }>("POST", `/accounts/${enc(id)}/entitlements`, cuerpo),
  recortar: (id: string, ends_at: string | null, ticket_ref: string) => pedir<{ ok: true }>("POST", `/entitlements/${enc(id)}/trim`, { ends_at, ticket_ref }),
  cerrarBloqueo: (id: number, ticket_ref: string) => pedir<{ account_id: string }>("POST", `/blocks/${id}/close`, { ticket_ref }),
  // ── Salud, métricas, lista de espera, personas y auditoría
  salud: () => una<Salud>("GET", "/health/overview"),
  saludFallidos: (cursor: string) => una<{ failed_jobs: FalloTrabajo[]; next_cursor: Cursor }>("GET", `/health/failed-jobs${paginas(cursor)}`),
  saludRetencion: (cursor: string) => una<{ retention_runs: PasadaRetencion[]; next_cursor: Cursor }>("GET", `/health/retention-runs${paginas(cursor)}`),
  saludErroresSync: (cursor: string) => una<{ sync_errors: (FilaSync & { account_id: string })[]; next_cursor: Cursor }>("GET", `/health/sync-errors${paginas(cursor)}`),
  metricas: (desde: string, hasta: string) => una<Metricas>("GET", `/metrics?from=${enc(desde)}&to=${enc(hasta)}`),
  espera: (cursor?: Cursor) => una<{ min_cell: number; cells: CeldaEspera[]; next_cursor: Cursor }>("GET", `/waitlist${paginas(cursor)}`),
  personas: (cursor?: Cursor) => una<{ users: Persona[]; next_cursor: Cursor }>("GET", `/users${paginas(cursor)}`),
  // ── Comunicaciones (solo propietario)
  comunicaciones: (cursor?: Cursor) => una<{ communications: FilaComunicacion[]; next_cursor: Cursor; per_minute: number }>("GET", `/communications${paginas(cursor)}`),
  comunicacion: (id: string) => una<Comunicacion>("GET", `/communications/${enc(id)}`),
  crearComunicacion: (b: BorradorComunicacion) => una<{ communication_id: string }>("POST", "/communications", b),
  guardarComunicacion: (id: string, b: BorradorComunicacion) => una<{ communication_id: string }>("PUT", `/communications/${enc(id)}`, b),
  vistaPrevia: (id: string) => una<VistaPrevia>("GET", `/communications/${enc(id)}/preview`),
  /** La URL de la vista previa en HTML, para un <iframe sandbox> (no se pasa por fetch). */
  urlVistaPrevia: (id: string, version: string) => `/admin/api/communications/${enc(id)}/preview.html?v=${enc(version)}`,
  destinatarios: (id: string) => una<{ recipients: number; excluded: number; per_minute: number }>("GET", `/communications/${enc(id)}/audience`),
  probar: (id: string) => una<{ sent_to: string }>("POST", `/communications/${enc(id)}/test`),
  confirmar: (id: string, expected_recipients: number) =>
    pedir<{ communication_id: string; recipients: number; excluded: number; per_minute: number; last_run_at: string }>("POST", `/communications/${enc(id)}/confirm`, {
      expected_recipients,
    }),
  auditoria: (cursor?: Cursor) => una<{ rows: FilaAuditoria[]; next_cursor: Cursor }>("GET", `/audit${paginas(cursor, 100)}`),
};
