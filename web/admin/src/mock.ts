// Simulador del backoffice para `npm run dev:mock`. Nunca entra en el build de producción: api.ts lo
// importa de forma dinámica solo cuando import.meta.env.MODE === "mock".
//
// Usuarios de prueba (contraseña de todos: «contraseña-de-prueba»): dueno (propietario), lucia (finanzas),
// pablo (soporte). Código TOTP: 123456. Código de recuperación: k7m2-q9xa-4tpz. Enlace de alta: #alta=prueba.
// La confirmación TOTP caduca a los 60 s en el simulador para poder ver el diálogo «Confirma con tu código».

import type { Rol } from "./api";

const CONTRASENA = "contraseña-de-prueba";
const CODIGO = "123456";
const RECUPERACION = "k7m2q9xa4tpz";
const FRESCO_MS = 60_000;

const USUARIOS: Record<string, Rol> = { dueno: "propietario", lucia: "finanzas", pablo: "soporte" };

type Estado = { fase: "nada" } | { fase: "totp"; usuario: string } | { fase: "reenroll"; usuario: string } | { fase: "ok"; usuario: string; frescoHasta: number };
let estado: Estado = { fase: "nada" };

const ahora = () => new Date();
const hace = (min: number) => new Date(Date.now() - min * 60_000).toISOString();
const dentroDe = (dias: number) => new Date(Date.now() + dias * 86_400_000).toISOString();
const csrf = () => `mock-${Math.random().toString(36).slice(2, 10)}`;

function responder(status: number, cuerpo: unknown): Response {
  return new Response(JSON.stringify(cuerpo), { status, headers: { "Content-Type": "application/json" } });
}
const error = (status: number, code: string, message: string, fields?: string[]) => responder(status, fields ? { code, message, fields } : { code, message });

function sesion() {
  if (estado.fase !== "ok") return null;
  return { stage: "ok" as const, username: estado.usuario, role: USUARIOS[estado.usuario]!, totp_fresh_until: new Date(estado.frescoHasta).toISOString() };
}

const PUEDEN: [RegExp, Rol[]][] = [
  [/^GET \/accounts\/[^/]+\/sync$/, ["soporte", "propietario"]],
  [/^POST \/accounts\/[^/]+\/offboarding/, ["soporte", "propietario"]],
  [/^GET \/health\//, ["soporte", "propietario"]],
  [/^GET \/accounts\/[^/]+\/billing$/, ["finanzas", "propietario"]],
  [/^POST \/accounts\/[^/]+\/billing\/contact$/, ["finanzas", "propietario"]],
  [/^GET \/billing\/review(\/lists\/\w+)?$/, ["finanzas", "propietario"]],
  [/^GET \/accounts\/[^/]+\/lists\/(billing_seats|charges|documents|refunds|billing_blocks)$/, ["finanzas", "propietario"]],
  [/^POST \/refunds$/, ["finanzas", "propietario"]],
  [/^GET \/metrics/, ["finanzas", "propietario"]],
  [/^POST \/accounts\/[^/]+\/entitlements$/, ["propietario"]],
  [/^POST \/entitlements\//, ["propietario"]],
  [/^POST \/blocks\//, ["propietario"]],
  [/^GET \/waitlist$/, ["propietario"]],
  [/^GET \/users$/, ["propietario"]],
  [/^GET \/audit/, ["propietario"]],
];
const SENSIBLE = [
  /^POST \/refunds$/,
  /^POST \/accounts\/[^/]+\/entitlements$/,
  /^POST \/entitlements\//,
  /^POST \/blocks\//,
  /^POST \/accounts\/[^/]+\/offboarding$/,
  /^POST \/password$/,
  /^POST \/totp\/start$/,
];

const ticketMal = (t: unknown, max = 500) => typeof t !== "string" || t.trim().length < 3 || t.length > max || /[\r\n]/.test(t) || /@/.test(t);

// ── Datos ──

const ID1 = "4b7c1e2a-0f3d-4c8e-9a51-1d2e3f4a5b6c";
const ID2 = "9e8d7c6b-5a4f-4e3d-8c2b-1a0f9e8d7c6b";
const ID3 = "c0ffee00-1234-4abc-8def-00000000beef";

const cuentas = [
  { account_id: ID1, email_masked: "ma***@g***.com", created_at: hace(60 * 24 * 40), deleted_at: null, disconnected_at: null, locations: 3, active_locations: 2 },
  { account_id: ID2, email_masked: "bo***@h***.es", created_at: hace(60 * 24 * 12), deleted_at: null, disconnected_at: null, locations: 1, active_locations: 1 },
  {
    account_id: ID3,
    email_masked: "ca***@o***.com",
    created_at: hace(60 * 24 * 200),
    deleted_at: hace(60 * 24 * 3),
    disconnected_at: hace(60 * 24 * 5),
    locations: 1,
    active_locations: 0,
  },
];

const derechos = [
  {
    id: "ent-1",
    plan: "pro",
    quantity: 1,
    starts_at: hace(60 * 24 * 30),
    ends_at: dentroDe(335),
    source: "pago",
    seat_id: "seat-1",
    provisional: false,
    granted_by: null,
    note_code: null,
    current: true,
  },
  {
    id: "ent-2",
    plan: "pro_whatsapp",
    quantity: 1,
    starts_at: hace(60 * 24 * 10),
    ends_at: dentroDe(80),
    source: "cortesia",
    seat_id: null,
    provisional: false,
    granted_by: "dueno",
    note_code: "feria_hosteleria",
    current: true,
  },
  {
    id: "ent-3",
    plan: "pro",
    quantity: 2,
    starts_at: hace(60 * 24 * 90),
    ends_at: hace(60 * 24 * 30),
    source: "beta",
    seat_id: null,
    provisional: false,
    granted_by: "dueno",
    note_code: null,
    current: false,
  },
];

const bloqueos = [
  {
    id: 7,
    blocked_at: hace(60 * 26),
    reason: "invoice_unpaid",
    seat_id: "seat-2",
    unblocked_at: null as string | null,
    unblocked_by: null as string | null,
    stripe_invoice_id: "in_1QmockUnpaid",
  },
];

function detalle(id: string) {
  const c = cuentas.find((x) => x.account_id === id);
  if (!c) return null;
  const primera = id === ID1;
  return {
    account: { id, email_masked: c.email_masked, created_at: c.created_at, deleted_at: c.deleted_at, disconnected_at: c.disconnected_at },
    blocked: primera && bloqueos.some((b) => !b.unblocked_at),
    locations: primera
      ? [
          { id: "loc-1", plan: "pro", active_since: hace(60 * 24 * 30), deactivated_at: null, removed_at: null, verified: true },
          { id: "loc-2", plan: "pro_whatsapp", active_since: hace(60 * 24 * 10), deactivated_at: null, removed_at: null, verified: true },
          { id: "loc-3", plan: null, active_since: null, deactivated_at: hace(60 * 24 * 20), removed_at: null, verified: false },
        ]
      : [
          {
            id: "loc-9",
            plan: id === ID2 ? "pro" : null,
            active_since: id === ID2 ? hace(60 * 24 * 11) : null,
            deactivated_at: null,
            removed_at: id === ID3 ? hace(60 * 24 * 3) : null,
            verified: true,
          },
        ],
    entitlements: primera ? derechos : [],
    seats: primera
      ? [
          {
            id: "seat-1",
            plan: "pro",
            collection: "charge_automatically",
            location_id: "loc-1",
            status: "active",
            current_period_start: hace(60 * 24 * 30),
            current_period_end: dentroDe(335),
            cancel_at_period_end: false,
            next_price_key: "pro_first_yearly",
            trial_ends_at: null,
            review_reason: null,
            created_at: hace(60 * 24 * 30),
          },
          {
            id: "seat-2",
            plan: "pro",
            collection: "charge_automatically",
            location_id: "loc-3",
            status: "past_due",
            current_period_start: hace(60 * 24 * 2),
            current_period_end: dentroDe(363),
            cancel_at_period_end: true,
            next_price_key: "pro_additional_yearly",
            trial_ends_at: null,
            review_reason: null,
            created_at: hace(60 * 24 * 2),
          },
        ]
      : [],
    blocks: primera ? bloqueos : [],
    activations: primera
      ? [
          { at: hace(60 * 24 * 30), location_id: "loc-1", event: "activated", plan: "pro", reason: null },
          { at: hace(60 * 24 * 20), location_id: "loc-3", event: "deactivated", plan: null, reason: "seat_unpaid" },
        ]
      : [],
    offboardings:
      id === ID3
        ? [
            {
              id: "off-1",
              scope: "cuenta",
              confirmed_at: hace(60 * 24 * 5),
              delete_at: hace(60 * 24 * 3),
              restored_at: null,
              effective_at: hace(60 * 24 * 3),
              reason: "email",
            },
          ]
        : [],
  };
}

const auditoria = Array.from({ length: 140 }, (_, i) => ({
  id: 1000 - i,
  at: hace(i * 37),
  written_by: i % 9 === 0 ? "sistema" : "admin",
  username: i % 9 === 0 ? null : ["dueno", "lucia", "pablo"][i % 3]!,
  role: i % 9 === 0 ? null : ["propietario", "finanzas", "soporte"][i % 3]!,
  action: ["login", "account.read", "locations.read", "refund.request", "entitlement.grant", "block.close"][i % 6]!,
  account_id: i % 2 ? ID1 : null,
  target: i % 4 === 0 ? `ent-${i}` : null,
  ticket_ref: i % 2 ? `HD-${2000 + i}` : null,
  detail: i % 5 === 0 ? { plan: "pro", quantity: 1 } : {},
}));

// ── Rutas ──

export async function simular(method: string, path: string, cuerpo: unknown): Promise<Response> {
  await new Promise((r) => setTimeout(r, 250));
  const url = new URL(path, "http://localhost:8090");
  const ruta = url.pathname.replace(/^\/admin\/api/, "");
  const clave = `${method} ${ruta}`;
  const b = (cuerpo ?? {}) as Record<string, unknown>;

  // Sesión y alta, sin sesión completa
  if (clave === "GET /session") {
    if (estado.fase === "reenroll") return responder(200, { stage: "reenroll", csrf_token: csrf() });
    const s = sesion();
    return s ? responder(200, { ...s, csrf_token: csrf() }) : error(401, "session_invalid", "La sesión ha caducado. Vuelve a entrar.");
  }
  if (clave === "POST /login") {
    if (typeof b.username === "string" && USUARIOS[b.username] && b.password === CONTRASENA) {
      estado = { fase: "totp", usuario: b.username };
      return responder(200, { stage: "totp", csrf_token: csrf() });
    }
    return error(401, "invalid_credentials", "Usuario o contraseña incorrectos.");
  }
  if (clave === "POST /session/totp") {
    if (estado.fase !== "totp" && estado.fase !== "ok") return error(401, "session_invalid", "La sesión ha caducado. Vuelve a entrar.");
    if (b.code !== CODIGO) return error(401, "totp_invalid", "El código no vale o ya se ha usado.");
    estado = { fase: "ok", usuario: estado.usuario, frescoHasta: Date.now() + FRESCO_MS };
    return responder(200, { ...sesion(), csrf_token: csrf() });
  }
  if (clave === "POST /login/recovery") {
    if (estado.fase !== "totp") return error(401, "session_invalid", "La sesión ha caducado. Vuelve a entrar.");
    if (typeof b.code !== "string" || b.code.replace(/-/g, "").toLowerCase() !== RECUPERACION)
      return error(401, "recovery_invalid", "El código de recuperación no vale o ya se ha usado.");
    estado = { fase: "reenroll", usuario: estado.usuario };
    return responder(200, { stage: "reenroll", csrf_token: csrf() });
  }
  if (clave === "POST /logout") {
    estado = { fase: "nada" };
    return responder(200, { ok: true });
  }
  if (clave === "POST /enrollment/start") {
    if (b.token !== "prueba") return error(404, "enrollment_invalid", "El enlace de alta no vale, ha caducado o ya se ha usado.");
    return responder(200, { ...totp("ana"), username: "ana", role: "soporte" });
  }
  if (clave === "POST /enrollment/finish") {
    if (b.token !== "prueba") return error(404, "enrollment_invalid", "El enlace de alta no vale, ha caducado o ya se ha usado.");
    const p = contrasenaMal(b.password, "ana");
    if (p) return p;
    if (b.code !== CODIGO) return error(422, "totp_invalid", "El código no vale. Prueba con el siguiente.");
    return responder(200, { username: "ana", recovery_codes: codigosRecuperacion() });
  }
  if (estado.fase === "reenroll") {
    if (clave === "POST /totp/start") return responder(200, totp(estado.usuario));
    if (clave === "POST /totp/finish") {
      if (b.code !== CODIGO) return error(422, "totp_invalid", "El código no vale. Prueba con el siguiente.");
      estado = { fase: "ok", usuario: estado.usuario, frescoHasta: Date.now() + FRESCO_MS };
      return responder(200, { recovery_codes: codigosRecuperacion() });
    }
    return error(401, "session_invalid", "Termina primero el alta de tu app de autenticación.");
  }

  // Desde aquí, sesión completa
  if (estado.fase !== "ok") return error(401, "session_invalid", "La sesión ha caducado. Vuelve a entrar.");
  const rol = USUARIOS[estado.usuario]!;
  const regla = PUEDEN.find(([re]) => re.test(clave));
  if (regla && !regla[1].includes(rol)) return error(403, "role_not_allowed", "Tu rol no permite hacer esto.");
  if (SENSIBLE.some((re) => re.test(clave)) && estado.frescoHasta < Date.now()) return error(403, "totp_required", "Confirma la acción con un código de tu app.");

  let m: RegExpMatchArray | null;

  if (clave === "POST /totp/start") return responder(200, totp(estado.usuario));
  if (clave === "POST /totp/finish") {
    if (b.code !== CODIGO) return error(422, "totp_invalid", "El código no vale. Prueba con el siguiente.");
    return responder(200, { recovery_codes: codigosRecuperacion() });
  }
  if (clave === "POST /password") {
    if (b.current_password !== CONTRASENA) return error(401, "invalid_credentials", "La contraseña actual no es correcta.");
    return contrasenaMal(b.new_password, estado.usuario) ?? responder(200, { ok: true, changed_at: ahora().toISOString() });
  }

  if (clave === "GET /accounts") {
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    if (q.length < 3) return error(422, "validation", "Escribe al menos 3 caracteres.", ["q"]);
    const r = q.includes("nadie") ? [] : q.includes("@") || q.length < 8 ? cuentas : cuentas.filter((c) => c.account_id.startsWith(q));
    return pagina(url, { accounts: r }, "accounts");
  }
  if ((m = clave.match(/^GET \/accounts\/([^/]+)$/))) {
    const d = detalle(decodeURIComponent(m[1]!));
    return d ? responder(200, primeras(d, ["locations", "entitlements", "seats", "blocks", "activations", "offboardings"])) : error(404, "not_found", "No existe.");
  }
  if ((m = clave.match(/^POST \/accounts\/([^/]+)\/locations$/))) {
    if (ticketMal(b.ticket_ref)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    const d = detalle(decodeURIComponent(m[1]!));
    if (!d) return error(404, "not_found", "No existe.");
    const titulos = ["Bar La Esquina", "Restaurante El Puerto (WhatsApp)", "La Esquina · Terraza"];
    return pagina(
      url,
      {
        locations: d.locations.map((l, i) => ({
          location_id: l.id,
          title: titulos[i] ?? "Cafetería Centro",
          active_since: l.active_since,
          plan: l.plan,
          deactivated_at: l.deactivated_at,
          removed_at: l.removed_at,
          verified: l.verified,
        })),
      },
      "locations",
    );
  }
  if ((m = clave.match(/^GET \/accounts\/([^/]+)\/sync$/))) {
    const d = detalle(decodeURIComponent(m[1]!));
    if (!d) return error(404, "not_found", "No existe.");
    return pagina(
      url,
      {
        sync: d.locations.map((l, i) => ({ location_id: l.id, status: i === 2 ? "needs_reconnect" : "ok", status_at: hace(30 + i * 90), synced_at: hace(30 + i * 600) })),
      },
      "sync",
    );
  }
  if ((m = clave.match(/^GET \/accounts\/([^/]+)\/channels$/))) {
    return pagina(
      url,
      {
        channels:
          m[1] === ID1
            ? [
                { identity_id: "id-1", channel: "whatsapp", verified_at: hace(60 * 24 * 9), last_inbound_at: hace(45), unlinked_at: null },
                { identity_id: "id-2", channel: "telegram", verified_at: hace(60 * 24 * 30), last_inbound_at: hace(60 * 24 * 4), unlinked_at: hace(60 * 24 * 2) },
              ]
            : [],
      },
      "channels",
    );
  }
  if ((m = clave.match(/^POST \/accounts\/([^/]+)\/offboarding\/check$/))) {
    const raw = typeof b.raw_message === "string" ? b.raw_message : "";
    if (raw.length < 20) return error(422, "validation", "Pega el mensaje original completo, con las cabeceras.", ["raw_message"]);
    if (/dmarc=fail/i.test(raw)) return error(422, "dmarc_failed", "DMARC no pasa: dmarc=fail en Authentication-Results.");
    return responder(200, { dmarc: "pass", from_domain: "gmail.com", matches_account: !/otro/i.test(raw) });
  }
  if ((m = clave.match(/^POST \/accounts\/([^/]+)\/offboarding$/))) {
    if (ticketMal(b.ticket_ref)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    return responder(200, { offboarding_id: "off-nueva", delete_at: dentroDe(2) });
  }
  if ((m = clave.match(/^GET \/accounts\/([^/]+)\/billing$/))) {
    const id = decodeURIComponent(m[1]!);
    return responder(200, primeras(cobroDe(id), ["seats", "charges", "documents", "refunds", "blocks"]));
  }
  if ((m = clave.match(/^POST \/accounts\/([^/]+)\/billing\/contact$/))) {
    if (ticketMal(b.ticket_ref)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    return responder(200, {
      legal_name: "Hostelería La Esquina S.L.",
      tax_id: "B12345678",
      address_line1: "Calle Mayor 1",
      address_line2: null,
      postal_code: "39001",
      city: "Santander",
      province: "Cantabria",
      country: "ES",
      invoice_email: "facturas@example.com",
    });
  }
  if (clave === "POST /refunds") {
    if (ticketMal(b.ticket_ref, 80)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    return responder(200, { refund_request_id: "ref-nuevo" });
  }
  if ((m = clave.match(/^POST \/accounts\/([^/]+)\/entitlements$/))) {
    if (ticketMal(b.ticket_ref)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    const nuevo = {
      id: `ent-${derechos.length + 1}`,
      plan: String(b.plan),
      quantity: Number(b.quantity),
      starts_at: ahora().toISOString(),
      ends_at: String(b.ends_at),
      source: String(b.source),
      seat_id: null,
      provisional: false,
      granted_by: estado.usuario,
      note_code: typeof b.note_code === "string" ? b.note_code : null,
      current: true,
    };
    derechos.unshift(nuevo as (typeof derechos)[number]);
    return responder(200, { entitlement_id: nuevo.id });
  }
  if ((m = clave.match(/^POST \/entitlements\/([^/]+)\/trim$/))) {
    if (ticketMal(b.ticket_ref)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    const d = derechos.find((x) => x.id === decodeURIComponent(m![1]!));
    if (!d) return error(404, "not_found", "No existe.");
    if (d.source === "pago") return error(422, "invalid", "Los derechos de pago no se recortan: se gestionan con la plaza en Stripe.");
    d.ends_at = typeof b.ends_at === "string" ? b.ends_at : ahora().toISOString();
    if (b.ends_at === null) d.current = false;
    return responder(200, { ok: true });
  }
  if ((m = clave.match(/^POST \/blocks\/(\d+)\/close$/))) {
    if (ticketMal(b.ticket_ref)) return error(422, "validation", "La referencia del ticket no vale.", ["ticket_ref"]);
    const x = bloqueos.find((y) => y.id === Number(m![1]));
    if (!x || x.unblocked_at) return error(404, "not_found", "No existe o ya está cerrado.");
    x.unblocked_at = ahora().toISOString();
    x.unblocked_by = estado.usuario;
    return responder(200, { account_id: ID1 });
  }

  if (clave === "GET /billing/review") {
    return responder(200, primeras(revisionDatos(), ["documents", "seats", "refunds", "events", "paid_without_document", "blocks"]));
  }
  if (clave === "GET /health/overview") {
    return responder(200, primeras(saludDatos(), ["failed_jobs", "retention_runs", "sync_errors"]));
  }
  if ((m = clave.match(/^GET \/accounts\/([^/]+)\/lists\/(\w+)$/))) {
    const id = decodeURIComponent(m[1]!);
    const nombre = m[2]!;
    const d = detalle(id);
    if (!d) return error(404, "not_found", "No existe.");
    const c = cobroDe(id);
    const fuentes: Record<string, unknown[]> = {
      locations: d.locations,
      entitlements: d.entitlements,
      seats: d.seats,
      blocks: d.blocks,
      activations: d.activations,
      offboardings: d.offboardings,
      billing_seats: c.seats,
      charges: c.charges,
      documents: c.documents,
      refunds: c.refunds,
      billing_blocks: c.blocks,
    };
    const todas = fuentes[nombre];
    if (!todas) return error(404, "not_found", "Ese listado no existe.");
    return siguientes(url, todas);
  }
  if ((m = clave.match(/^GET \/billing\/review\/lists\/(\w+)$/))) {
    const r = revisionDatos() as unknown as Record<string, unknown>;
    const todas = r[m[1]!];
    if (!Array.isArray(todas) || m[1] === "links") return error(404, "not_found", "Ese listado no existe.");
    const resp = siguientes(url, todas, { links: r["links"], odoo_link: r["odoo_link"] });
    return resp;
  }
  if ((m = clave.match(/^GET \/health\/(failed-jobs|retention-runs|sync-errors)$/))) {
    const clave2 = m[1]!.replace("-", "_");
    return pagina(url, { [clave2]: (saludDatos() as unknown as Record<string, unknown[]>)[clave2]! }, clave2);
  }
  if (clave === "GET /metrics") {
    const desde = url.searchParams.get("from") ?? "";
    const hasta = url.searchParams.get("to") ?? "";
    if (!desde || !hasta || hasta <= desde) return error(422, "validation", "El periodo no vale.", ["from", "to"]);
    return responder(200, {
      period: { from: desde, to: hasta },
      revenue: { arr_cents: 1_234_500, mrr_cents: 102_875, estimated_seats: 2 },
      seats_in_force: [
        { plan: "pro", next_price_key: "pro_first_yearly", seats: 21 },
        { plan: "pro", next_price_key: "pro_additional_yearly", seats: 9 },
        { plan: "pro_whatsapp", next_price_key: "whatsapp_first_yearly", seats: 6 },
        { plan: "pro_whatsapp", next_price_key: null, seats: 2 },
      ],
      signups: { accounts: 57, first_seats: 18, new_seats: 26 },
      churn: { seats_canceled: 3, seats_to_cancel: 4, account_leaves: 2 },
      blocks: { opened: 4, closed: 3, open_now: bloqueos.filter((x) => !x.unblocked_at).length },
      conversion: { beta_accounts: 30, beta_paid: 12, free_accounts: 27, free_paid: 6 },
      funnel: { visit: 4210, diagnosis: 40, screenshot_diagnosis: 380, signup: 57, connected: 44, checkout_started: 25, paid: 18 },
    });
  }
  if (clave === "GET /waitlist") {
    return pagina(
      url,
      {
        min_cell: 5,
        cells: [
          { week: "2026-10-26", channel: "email", score_band: "50-69", source: "landing", people: 14 },
          { week: "2026-10-26", channel: "whatsapp", score_band: "70-89", source: "landing", people: 6 },
          { week: "2026-11-02", channel: "email", score_band: "0-49", source: "instagram", people: 9 },
          { week: "2026-11-02", channel: "email", score_band: "sin_nota", source: "landing", people: 21 },
        ],
      },
      "cells",
    );
  }
  if (clave === "GET /users") {
    return pagina(
      url,
      {
        users: [
          {
            admin_user_id: "u1",
            username: "dueno",
            role: "propietario",
            active: true,
            must_enroll: false,
            locked_until: null,
            created_at: hace(60 * 24 * 60),
            password_changed_at: hace(60 * 24 * 60),
          },
          {
            admin_user_id: "u2",
            username: "lucia",
            role: "finanzas",
            active: true,
            must_enroll: false,
            locked_until: null,
            created_at: hace(60 * 24 * 20),
            password_changed_at: hace(60 * 24 * 19),
          },
          {
            admin_user_id: "u3",
            username: "pablo",
            role: "soporte",
            active: true,
            must_enroll: false,
            locked_until: dentroDe(0.01),
            created_at: hace(60 * 24 * 10),
            password_changed_at: hace(60 * 24 * 10),
          },
          { admin_user_id: "u4", username: "ana", role: "soporte", active: true, must_enroll: true, locked_until: null, created_at: hace(60), password_changed_at: null },
          {
            admin_user_id: "u5",
            username: "antiguo",
            role: "finanzas",
            active: false,
            must_enroll: false,
            locked_until: null,
            created_at: hace(60 * 24 * 300),
            password_changed_at: hace(60 * 24 * 290),
          },
        ],
      },
      "users",
    );
  }
  if (clave === "GET /audit") {
    return pagina(url, { rows: auditoria }, "rows", 100);
  }

  return error(404, "not_found", `El simulador no conoce ${clave}.`);
}

function totp(usuario: string) {
  const secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
  return { otpauth_uri: `otpauth://totp/Fichaitor%20backoffice:${usuario}?secret=${secret}&issuer=Fichaitor%20backoffice`, secret, state: "v1.mock" };
}

function codigosRecuperacion(): string[] {
  const a = "abcdefghjkmnpqrstuvwxyz23456789";
  const g = () => Array.from({ length: 4 }, () => a[Math.floor(Math.random() * a.length)]).join("");
  return Array.from({ length: 10 }, () => `${g()}-${g()}-${g()}`);
}

function contrasenaMal(p: unknown, usuario: string): Response | null {
  if (typeof p !== "string" || p.length < 12) return error(422, "password_too_short", "La contraseña tiene que tener al menos 12 caracteres.");
  if (p.length > 128) return error(422, "password_too_long", "La contraseña no puede pasar de 128 caracteres.");
  if (p.toLowerCase().includes(usuario)) return error(422, "password_has_username", "La contraseña no puede contener tu usuario.");
  if (/^(.)\1+$/.test(p) || p.toLowerCase().startsWith("123456789")) return error(422, "password_common", "Esa contraseña es demasiado común.");
  return null;
}

// ── Páginas en el simulador ──
// Páginas cortas (2 filas, o el `limit` que se pida) para ver «Cargar más». El cursor es opaco para la
// interfaz; aquí es «p<posición>» en base64.

const POR_PAGINA = 2;

function trozo<T>(todas: T[], url: URL, porDefecto = POR_PAGINA): { items: T[]; next_cursor: string | null } | null {
  const limit = Number(url.searchParams.get("limit") ?? porDefecto);
  const cursor = url.searchParams.get("cursor");
  let desde = 0;
  if (cursor) {
    let claro = "";
    try {
      claro = atob(cursor);
    } catch {
      return null;
    }
    if (!/^p\d+$/.test(claro)) return null;
    desde = Number(claro.slice(1));
  }
  const items = todas.slice(desde, desde + limit);
  return { items, next_cursor: desde + limit < todas.length ? btoa(`p${desde + limit}`) : null };
}

const cursorMal = () => error(422, "invalid_cursor", "Esa página ya no vale. Vuelve a empezar.");

/** Una respuesta con un listado (`clave`) por páginas y su `next_cursor`. */
function pagina(url: URL, cuerpo: Record<string, unknown>, clave: string, porDefecto = POR_PAGINA): Response {
  const t = trozo(cuerpo[clave] as unknown[], url, porDefecto);
  if (!t) return cursorMal();
  return responder(200, { ...cuerpo, [clave]: t.items, next_cursor: t.next_cursor });
}

/** Las páginas siguientes de un listado de una pantalla: {items, next_cursor}. */
function siguientes(url: URL, todas: unknown[], extra: Record<string, unknown> = {}): Response {
  const t = trozo(todas, url);
  return t ? responder(200, { ...t, ...extra }) : cursorMal();
}

/** La primera página de cada listado de una pantalla, con `next_cursors`. */
function primeras<T extends object>(cuerpo: T, claves: string[]): T & { next_cursors: Record<string, string | null> } {
  const sin = new URL("http://localhost:8090/");
  const nuevo = { ...cuerpo } as Record<string, unknown>;
  const cursores: Record<string, string | null> = {};
  for (const k of claves) {
    const t = trozo(nuevo[k] as unknown[], sin)!;
    nuevo[k] = t.items;
    cursores[k] = t.next_cursor;
  }
  return { ...(nuevo as T), next_cursors: cursores };
}

function cobroDe(id: string) {
  if (id !== ID1)
    return {
      livemode: false,
      customer: null,
      seats: [] as unknown[],
      charges: [] as unknown[],
      documents: [] as unknown[],
      refunds: [] as unknown[],
      blocks: [] as unknown[],
    };
  return {
    livemode: false,
    customer: {
      stripe_customer_id: "cus_Qmock123",
      legal_name: "Hostelería La Esquina S.L.",
      tax_id_type: "es_cif",
      tax_id: "B1•••••78",
      country: "ES",
      territory: "peninsula",
      tax_treatment: "standard",
      updated_at: hace(60 * 24 * 30),
      stripe_url: "https://dashboard.stripe.com/test/customers/cus_Qmock123",
    },
    seats: [
      {
        id: "seat-1",
        plan: "pro",
        collection: "charge_automatically",
        stripe_subscription_id: "sub_Qmock1",
        location_id: "loc-1",
        status: "active",
        current_period_end: dentroDe(335),
        cancel_at_period_end: false,
        next_price_key: "pro_first_yearly",
        review_reason: null,
        stripe_url: "https://dashboard.stripe.com/test/subscriptions/sub_Qmock1",
      },
      {
        id: "seat-2",
        plan: "pro",
        collection: "charge_automatically",
        stripe_subscription_id: "sub_Qmock2",
        location_id: "loc-3",
        status: "past_due",
        current_period_end: dentroDe(363),
        cancel_at_period_end: true,
        next_price_key: "pro_additional_yearly",
        review_reason: null,
        stripe_url: "https://dashboard.stripe.com/test/subscriptions/sub_Qmock2",
      },
    ],
    charges: [
      {
        stripe_invoice_id: "in_1QmockPaid",
        seat_id: "seat-1",
        status: "paid",
        billing_reason: "subscription_create",
        currency: "eur",
        subtotal: 3500,
        tax: 735,
        total: 4235,
        amount_paid: 4235,
        amount_refunded: 0,
        price_key: "pro_first_yearly",
        period_start: hace(60 * 24 * 30),
        period_end: dentroDe(335),
        paid_at: hace(60 * 24 * 30),
        disputed: false,
        stripe_url: "https://dashboard.stripe.com/test/invoices/in_1QmockPaid",
        payment_url: null,
      },
      {
        stripe_invoice_id: "in_1QmockUnpaid",
        seat_id: "seat-2",
        status: "open",
        billing_reason: "subscription_create",
        currency: "eur",
        subtotal: 2500,
        tax: 525,
        total: 3025,
        amount_paid: 0,
        amount_refunded: 0,
        price_key: "pro_additional_yearly",
        period_start: hace(60 * 24 * 2),
        period_end: dentroDe(363),
        paid_at: null,
        disputed: false,
        stripe_url: "https://dashboard.stripe.com/test/invoices/in_1QmockUnpaid",
        payment_url: "https://invoice.stripe.com/i/mock",
      },
    ],
    documents: [
      {
        id: "doc-1",
        kind: "invoice",
        stripe_invoice_id: "in_1QmockPaid",
        state: "issued",
        number: "F2026-000123",
        amount_total: 4235,
        verifactu_state: "accepted",
        review_reason: null,
        created_at: hace(60 * 24 * 30),
        issued_at: hace(60 * 24 * 30),
        odoo_url: null,
        stripe_url: null,
      },
    ],
    refunds: [],
    blocks: bloqueos,
  };
}

function revisionDatos() {
  return {
    livemode: false,
    counts: {
      events_unprocessed: 1,
      documents_pending: 2,
      documents_overdue: 1,
      documents_review: 1,
      blocks_open: bloqueos.filter((x) => !x.unblocked_at).length,
      refunds_pending: 1,
      paid_without_document: 1,
    },
    documents: [
      {
        id: "doc-7",
        kind: "invoice",
        stripe_invoice_id: "in_1QmockRev",
        state: "review",
        number: null,
        amount_total: 4235,
        verifactu_state: null,
        review_reason: "tax_id_invalid",
        created_at: hace(60 * 5),
        account_id: ID1,
        odoo_move_id: 812,
      },
      {
        id: "doc-8",
        kind: "credit_note",
        stripe_invoice_id: "in_1QmockPaid",
        state: "pending",
        number: null,
        amount_total: -4235,
        verifactu_state: null,
        review_reason: null,
        created_at: hace(90),
        account_id: ID2,
        odoo_move_id: null,
      },
    ],
    seats: [{ id: "seat-5", account_id: ID2, plan: "pro_whatsapp", status: "incomplete", stripe_subscription_id: "sub_Qmock5", review_reason: "price_mismatch" }],
    refunds: [
      {
        id: "ref-1",
        stripe_invoice_id: "in_1QmockPaid",
        amount: 1000,
        reason: "service_issue",
        requested_by: "lucia",
        state: "pending",
        created_at: hace(20),
        account_id: ID1,
      },
    ],
    events: [
      { id: "evt_1Qmock", type: "invoice.paid", object_id: "in_1QmockLost", received_at: hace(80), processed_at: null, outcome: null },
      { id: "evt_2Qmock", type: "customer.subscription.updated", object_id: "sub_QmockX", received_at: hace(95), processed_at: hace(94), outcome: "orphan" },
      { id: "evt_3Qmock", type: "invoice.payment_failed", object_id: "in_QmockY", received_at: hace(130), processed_at: hace(129), outcome: "orphan" },
    ],
    paid_without_document: [{ entitlement_id: "ent-9", account_id: ID2, stripe_invoice_id: "in_1QmockNoDoc", seat_id: "seat-5", starts_at: hace(60 * 3) }],
    blocks: bloqueos.filter((x) => !x.unblocked_at).map((x) => ({ ...x, account_id: ID1 })),
    links: {
      customer: "https://dashboard.stripe.com/test/customers/{id}",
      invoice: "https://dashboard.stripe.com/test/invoices/{id}",
      subscription: "https://dashboard.stripe.com/test/subscriptions/{id}",
      payment: "https://dashboard.stripe.com/test/payments/{id}",
    },
    odoo_link: "https://odoo.example.com/odoo/action-account.action_move_out_invoice_type/{id}",
  };
}

function saludDatos() {
  return {
    summaries: { ok: 41, needs_reconnect: 2, error: 1 },
    summaries_stale: 3,
    channels: { whatsapp: { linked: 12, unlinked: 2 }, telegram: { linked: 5, unlinked: 1 } },
    jobs_stuck: 1,
    jobs_failed_24h: 2,
    jobs: [
      { kind: "sync_location", state: "queued", jobs: 4, oldest_run_at: hace(3), stuck: 0 },
      { kind: "sync_location", state: "running", jobs: 1, oldest_run_at: hace(48), stuck: 1 },
      { kind: "send_digest", state: "queued", jobs: 12, oldest_run_at: hace(1), stuck: 0 },
    ],
    failed_jobs: [
      { job_id: "job-1", kind: "sync_location", attempts: 5, finished_at: hace(70), last_error: "google_api: 403 PERMISSION_DENIED" },
      { job_id: "job-2", kind: "send_whatsapp", attempts: 3, finished_at: hace(300), last_error: "meta: 131047 re-engagement window" },
      { job_id: "job-3", kind: "send_mail", attempts: 3, finished_at: hace(900), last_error: "smtp: 451 temporary failure" },
    ],
    retention_runs: [
      { run_id: 32, at: hace(60 * 6), deleted: { events: 1203, sessions: 40 } },
      { run_id: 31, at: hace(60 * 30), deleted: {} },
    ],
    sync_errors: [{ account_id: ID1, location_id: "loc-3", status: "needs_reconnect", status_at: hace(60 * 30), synced_at: hace(60 * 24 * 3) }],
  };
}
