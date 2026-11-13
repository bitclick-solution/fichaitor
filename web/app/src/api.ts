// Cliente de la API. Contrato: docs/contrato-web-api.md.

export type Impact = "alto" | "medio" | "bajo";

export interface Finding {
  code: string;
  title: string;
  detail: string;
  impact: Impact;
}

export interface LockedCheck {
  code: string;
  title: string;
}

export interface Diagnosis {
  place_id: string;
  name: string;
  address: string;
  category: string;
  score: number;
  label: string;
  findings: Finding[];
  locked_checks: LockedCheck[];
  maps_url: string;
  /** Origen de los datos, p. ej. «Datos de tu Perfil de Empresa de Google». */
  attribution?: string;
  /** Solo en /me/diagnosis. Con `needs_reconnect` o `error`, el cuerpo es el último diagnóstico bueno. */
  status?: "ok" | "needs_reconnect" | "error";
  synced_at?: string | null;
  stale?: boolean;
}

export type PlanFicha = "gratis" | "pro" | "pro_whatsapp";

export interface Location {
  id: string;
  title: string;
  place_id: string | null;
  verified: boolean;
  // ── Multinegocio (docs/multinegocio.md, 5.2). Ausentes en un backend anterior: entonces hay una sola ficha.
  /** Fichaitor la vigila y avisa de ella. Ver una ficha no pausa las demás. */
  active?: boolean;
  plan?: PlanFicha | null;
  active_since?: string | null;
  /** Solo en las activas. */
  /** Los mismos valores que el `status` del resumen; null si está inactiva. */
  status?: "ok" | "pendiente" | "error" | "needs_reconnect" | null;
  /** Reseñas sin responder y fechas clave por contestar. Solo en las activas. */
  pending?: { reviews: number; key_dates: number } | null;
  /** Respuestas con IA usadas. En Gratis, 25 por ficha para siempre (`lifetime: true`); `limit` null: sin límite. null si la ficha está inactiva. */
  usage?: { ai_replies: { used: number; limit: number | null; lifetime?: boolean } } | null;
  /** Qué incluye el plan de la ficha. null si está inactiva. */
  features?: { key_dates: boolean; whatsapp: boolean } | null;
}

/** Uso de los planes de la cuenta (docs/multinegocio.md, 5.2). */
export interface PlanesCuenta {
  active: number;
  free_available: boolean;
  available: { pro: number; pro_whatsapp: number };
}

/**
 * Precios (docs/multinegocio.md, «Precios en la respuesta»). Importes en céntimos; los totales los calcula
 * la API: la web no guarda ni calcula ninguna cifra. `charging: false` en la beta: precios de referencia.
 */
export interface Precios {
  currency: string;
  period: "month" | "year";
  charging: boolean;
  plans: Record<"pro" | "pro_whatsapp", { first: number; additional: number }>;
  /** Lo que paga hoy la cuenta por sus fichas activas. */
  total: number;
  /** Total resultante de activar una ficha más en cada plan. */
  if_activate: Partial<Record<"pro" | "pro_whatsapp", number>>;
  /** Total resultante de desactivar cada ficha activa. */
  if_deactivate: Record<string, number>;
}

export interface Me {
  email: string;
  locations: Location[];
  selected_location_id: string | null;
  /** Baja programada, si la hay. */
  pending_deletion?: BajaPendiente | null;
  /** Si la cuenta tiene Google conectado. */
  connected?: boolean;
  /** Multinegocio: uso de los planes. */
  plans?: PlanesCuenta;
  pricing?: Precios;
  /** Cobro (docs/admin-facturacion.md, 4.7). Ausente mientras el backend no lo tenga. */
  billing?: Cobro | null;
}

/** Plan de pago. En la interfaz, `pro_whatsapp` se llama «Pro+». */
export type PlanPago = "pro" | "pro_whatsapp";

/** Una plaza: un año pagado de un plan, asignado o no a una ficha. */
export interface Plaza {
  id: string;
  plan: PlanPago;
  location_id: string | null;
  status: string;
  renews_at: string;
  cancel_at_period_end: boolean;
  /** Precio de la siguiente renovación, en céntimos y sin IVA. */
  next_price: number;
  payment_method: string;
  collection: string;
}

export interface Cobro {
  profile_complete: boolean;
  blocked: { since: string; reason: string } | null;
  seats: Plaza[];
  /** Lo que se paga hoy por una plaza nueva de cada plan: un año, en céntimos y sin IVA. */
  new_seat_price: Record<PlanPago, number>;
}

export interface Direccion {
  line1: string;
  line2?: string | null;
  postal_code: string;
  city: string;
  province?: string | null;
  /** ISO 3166-1 alfa-2: ES, PT, FR… */
  country: string;
}

export interface PerfilFiscal {
  legal_name: string | null;
  tax_id: string | null;
  address: Direccion | null;
  invoice_email: string | null;
}

/** Petición al proceso de cobro (plaza nueva, portal, pago…). Con `url`, se abre Stripe. */
export interface PeticionCobro {
  state: "pendiente" | "lista" | "fallida";
  url?: string | null;
  code?: string | null;
  detail?: string | null;
}

/** Qué se da de baja: la ficha conectada o la cuenta entera. */
export type AlcanceBaja = "ficha" | "cuenta";

export interface BajaPendiente {
  scope: AlcanceBaja;
  /** Momento en que se borra si no se restaura antes (48 horas después de confirmar). */
  delete_at: string;
}

/** GET /api/v1/me/summary. Con `status: "pendiente"` todos los campos van a null, salvo `stale`. */
export interface Summary {
  status: "ok" | "pendiente" | "needs_reconnect" | "error";
  synced_at: string | null;
  review_count: number | null;
  average_rating: number | null;
  unanswered_count: number | null;
  unanswered_low_count: number | null;
  last_review_at: string | null;
  has_regular_hours: boolean | null;
  upcoming_special_hours: number | null;
  google_changed: boolean | null;
  google_changed_fields: string[] | null;
  google_changed_labels: string[] | null;
  stale: boolean;
}

/** Una reseña de la ficha conectada. Sin autor: se identifica por estrellas, fecha y texto. */
export interface Review {
  id: string;
  stars: number;
  /** Cadena vacía si la reseña solo tiene estrellas. */
  text: string;
  created_at: string;
  replied: boolean;
  reply: { text: string; updated_at: string } | null;
}

export interface ReviewsResponse {
  status: "ok" | "pendiente" | "needs_reconnect" | "error";
  synced_at: string | null;
  stale: boolean;
  total: number;
  reviews: Review[];
}

/** Acción sobre Google (hoy, publicar una respuesta). Un 202 no significa «publicada». */
export interface Action {
  id: string;
  type: string;
  /** null en los cambios de horario (fechas clave). */
  review_id: string | null;
  status: "pendiente" | "en_curso" | "hecha" | "fallida";
  message: string;
  error: "reconnect" | "rejected" | "review_gone" | "date_passed" | "no_regular_hours" | "suspended" | "unavailable" | "internal" | null;
  confirmed_at: string | null;
  finished_at: string | null;
}

/** GET /api/v1/me/sentiment (forma prevista). Sin nombres de autores en ningún campo. */
export interface Sentiment {
  status: "ok" | "pendiente" | "insuficiente" | "error";
  based_on: number | null;
  period: string | null;
  overall: "positivo" | "mixto" | "negativo" | null;
  score: number | null;
  summary: string | null;
  praised: { topic: string; mentions: number }[];
  complaints: { topic: string; mentions: number }[];
  suggestion: string | null;
  generated_at: string | null;
  /** Variación respecto al mes anterior, en puntos. Previsto, puede no venir. */
  delta: number | null;
  /** Nota del mes anterior. Previsto; si no viene `delta`, se calcula con ella. */
  previous_score: number | null;
  /** Notas de los últimos meses, de más antiguo a más reciente. Previsto, puede no venir. */
  history: { month: string; score: number }[];
  /** Reseñas con texto que entran en el análisis. */
  with_text: number | null;
  /** true con pocas reseñas con texto: la nota se muestra, con un aviso. */
  provisional: boolean;
  /** Frase lista para mostrar como pie. */
  next_update: string | null;
}

/** Una dirección por la que Fichaitor escribe al dueño. `address` llega ya enmascarada. */
export interface Canal {
  id: string;
  channel: "telegram" | "whatsapp" | "email";
  address: string;
  linked_at: string;
  /** Multinegocio: null si el chat ve todas las fichas activas (también las futuras); si no, las de su filtro. */
  locations?: string[] | null;
  /** Lo que recibe el chat, listo para mostrar tras «Recibe:» («Todos tus negocios», «A y B»…). */
  locations_label?: string;
}

/** Franja horaria «HH:MM». */
export interface Franja {
  open: string;
  close: string;
}

/** Fecha clave (docs/contrato-web-api.md, «Fechas clave»). */
export interface FechaClave {
  id: string;
  /** Día, «AAAA-MM-DD». */
  date: string;
  /** Nombre listo para mostrar: «Inmaculada Concepción», «Nochebuena». */
  name: string;
  /** Horario normal de ese día de la semana: null si la ficha no tiene; [] si ese día cierra. */
  regular_hours: Franja[] | null;
  status: "pendiente" | "en_curso" | "aplicada" | "fallida" | "descartada" | "caducada";
  answerable: boolean;
  /** Lo confirmado, con su texto listo para mostrar. */
  answer: { kind: "closed" | "regular" | "custom"; ranges: Franja[]; text: string } | null;
  /** El envío a Google, con los mismos estados que GET /me/actions/{id}. */
  action: Pick<Action, "id" | "status" | "message" | "error"> | null;
}

export type RespuestaFecha = { kind: "closed" } | { kind: "regular" } | { kind: "custom"; ranges: Franja[] };

/** Código de vinculación: 6 cifras, un solo uso, 10 minutos. Vale para cualquier canal. */
export interface CodigoVinculacion {
  code: string;
  expires_at: string;
  telegram_url: string | null;
  /** null mientras WhatsApp no esté disponible. */
  whatsapp_url?: string | null;
}

export const OAUTH_START = "/api/v1/google/oauth/start";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** Campo `status` del cuerpo del error, si lo trae (p. ej. «pendiente» en el 404 de /me/diagnosis). */
    readonly estado: string | null = null,
    /** Campo `code` (p. ej. «plan_required», «location_inactive»), si lo trae. */
    readonly codigo: string | null = null,
    /** Campo `feature` del 402 `plan_required`: «ai_replies» o «key_dates». */
    readonly funcion: string | null = null,
    /** Campo `request_id` del 409 `seat_pending`: la petición de pago que ya existe para esa ficha. */
    readonly peticion: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const GENERICO = "Algo ha fallado. Prueba de nuevo en un minuto.";


async function pedir<T>(method: "GET" | "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Requested-With"] = "fichaitor";
  if (body !== undefined) headers["content-type"] = "application/json";

  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (body !== undefined) init.body = JSON.stringify(body);

  let r: Response;
  try {
    // Con import() condicionado a una constante, la compilación de producción no incluye el simulador.
    r = import.meta.env.MODE === "mock" ? await (await import("./mock")).mockFetch(method, path, body) : await fetch(path, init);
  } catch {
    throw new ApiError(0, "No hay conexión con Fichaitor. Comprueba tu red y prueba de nuevo.");
  }

  if (r.status === 204) return undefined as T;
  if (r.ok) {
    // Un 202 puede llegar sin cuerpo: no es un error.
    const texto = await r.text();
    return (texto ? JSON.parse(texto) : undefined) as T;
  }

  // En los 422 automáticos, `detail` puede ser una lista: se muestra un mensaje genérico.
  let detalle = GENERICO;
  let estado: string | null = null;
  let codigo: string | null = null;
  let funcion: string | null = null;
  let peticion: string | null = null;
  try {
    const j: unknown = await r.json();
    if (typeof j === "object" && j !== null) {
      if ("detail" in j && typeof j.detail === "string") detalle = j.detail;
      if ("status" in j && typeof j.status === "string") estado = j.status;
      if ("code" in j && typeof j.code === "string") codigo = j.code;
      if ("feature" in j && typeof j.feature === "string") funcion = j.feature;
      if ("request_id" in j && typeof j.request_id === "string") peticion = j.request_id;
    }
  } catch {
    /* sin cuerpo */
  }
  throw new ApiError(r.status, detalle, estado, codigo, funcion, peticion);
}

/**
 * Lecturas de una ficha (docs/multinegocio.md, 5.1): `?location=` con la ficha que se ve. Si la respuesta
 * trae `location_id` y es de otra (el dueño cambió de negocio mientras cargaba), se descarta.
 */
function deFicha(ruta: string, ficha: string): string {
  return `${ruta}${ruta.includes("?") ? "&" : "?"}location=${encodeURIComponent(ficha)}`;
}
async function leerFicha<T>(ruta: string, ficha: string): Promise<T> {
  const r = await pedir<T & { location_id?: string }>("GET", deFicha(ruta, ficha));
  if (r && typeof r === "object" && typeof r.location_id === "string" && r.location_id !== ficha)
    throw new ApiError(409, "Esa información era de otra ficha. Vuelve a cargar.");
  return r;
}

export const api = {
  me: () => pedir<Me>("GET", "/api/v1/me"),
  elegirFicha: (locationId: string) => pedir<Me>("POST", "/api/v1/me/location", { location_id: locationId }),
  diagnostico: (ficha: string) => leerFicha<Diagnosis>("/api/v1/me/diagnosis", ficha),
  resumen: (ficha: string) => leerFicha<Summary>("/api/v1/me/summary", ficha),
  resenas: async (filtro: "unanswered" | "all", ficha: string): Promise<ReviewsResponse> => {
    const r = await leerFicha<Partial<ReviewsResponse> & Pick<ReviewsResponse, "status">>(`/api/v1/me/reviews?filter=${filtro}&limit=50`, ficha);
    return { status: r.status, synced_at: r.synced_at ?? null, stale: r.stale ?? false, total: r.total ?? 0, reviews: r.reviews ?? [] };
  },
  borrador: (id: string) => pedir<{ review_id: string; text: string; cached: boolean }>("POST", `/api/v1/me/reviews/${encodeURIComponent(id)}/draft`),
  /** El texto va tal cual: sin recortar ni normalizar. */
  responder: (id: string, text: string) => pedir<Action>("POST", `/api/v1/me/reviews/${encodeURIComponent(id)}/reply`, { text }),
  accion: (id: string) => pedir<Action>("GET", `/api/v1/me/actions/${encodeURIComponent(id)}`),
  sentimiento: async (ficha: string): Promise<Sentiment> => {
    const s = await leerFicha<Partial<Sentiment> & Pick<Sentiment, "status">>("/api/v1/me/sentiment", ficha);
    // Con `pendiente` o `insuficiente` pueden faltar campos: se normalizan para no romper la pantalla.
    return {
      status: s.status,
      based_on: s.based_on ?? null,
      period: s.period ?? null,
      overall: s.overall ?? null,
      score: s.score ?? null,
      summary: s.summary ?? null,
      praised: s.praised ?? [],
      complaints: s.complaints ?? [],
      suggestion: s.suggestion ?? null,
      generated_at: s.generated_at ?? null,
      delta: s.delta ?? null,
      previous_score: s.previous_score ?? null,
      history: Array.isArray(s.history) ? s.history.filter((h) => typeof h?.score === "number") : [],
      with_text: s.with_text ?? null,
      provisional: s.provisional === true,
      next_update: s.next_update ?? null,
    };
  },
  salir: () => pedir<void>("POST", "/api/v1/auth/logout"),
  // ── Canales (contrato previsto en docs/canales.md, «Paso 3»)
  canales: async () => (await pedir<{ channels: Canal[] }>("GET", "/api/v1/me/channels")).channels ?? [],
  /** Sin fichas (null), el chat verá todas las activas, también las que se activen después. */
  codigoVinculacion: (fichas: string[] | null = null) =>
    pedir<CodigoVinculacion>("POST", "/api/v1/channels/link-code", fichas ? { locations: fichas } : undefined),
  /** Cambia qué fichas ve un chat ya vinculado: null para todas. Responde con el canal. */
  filtrarCanal: (id: string, fichas: string[] | null) =>
    pedir<Canal>("PUT", `/api/v1/me/channels/${encodeURIComponent(id)}/locations`, { locations: fichas }),
  desvincularCanal: (id: string) => pedir<void>("DELETE", `/api/v1/me/channels/${encodeURIComponent(id)}`),
  // ── Multinegocio (docs/multinegocio.md, 5.3). Las dos responden con el cuerpo de GET /me.
  /** Sin plan, el servidor elige: gratis si está libre y, si no, el primer plan con hueco. 402: no queda hueco. */
  activarFicha: (id: string, plan?: PlanFicha) =>
    pedir<Me>("POST", `/api/v1/me/locations/${encodeURIComponent(id)}/activation`, plan ? { plan } : {}),
  desactivarFicha: (id: string) => pedir<Me>("DELETE", `/api/v1/me/locations/${encodeURIComponent(id)}/activation`),
  // ── Cobro (docs/admin-facturacion.md, 4.7)
  perfilFiscal: () => pedir<PerfilFiscal>("GET", "/api/v1/me/billing/profile"),
  guardarPerfilFiscal: (p: { legal_name: string; tax_id: string; address: Direccion; invoice_email: string }) =>
    pedir<PerfilFiscal>("PUT", "/api/v1/me/billing/profile", p),
  /** Plaza nueva para una ficha. Al cobrarse, el backend activa la ficha en ella. */
  pedirPlaza: (plan: PlanPago, ficha: string) => pedir<{ request_id: string }>("POST", "/api/v1/me/billing/seats", { plan, location_id: ficha }),
  peticionCobro: (id: string) => pedir<PeticionCobro>("GET", `/api/v1/me/billing/requests/${encodeURIComponent(id)}`),
  // ── Fechas clave. Con filter=all también las resueltas desde hoy, para enseñar el resultado.
  fechasClave: async (ficha: string) => (await leerFicha<{ dates: FechaClave[] }>("/api/v1/me/key-dates?filter=all", ficha)).dates ?? [],
  responderFecha: (id: string, r: RespuestaFecha) => pedir<FechaClave>("POST", `/api/v1/me/key-dates/${encodeURIComponent(id)}/answer`, r),
  descartarFecha: (id: string) => pedir<void>("POST", `/api/v1/me/key-dates/${encodeURIComponent(id)}/dismiss`),
  // ── Baja con código de un solo uso y 48 horas para restaurar (docs/contrato-web-api.md).
  pedirCodigoBaja: (scope: AlcanceBaja) => pedir<{ sent_to: string; expires_in: number }>("POST", "/api/v1/me/offboarding/code", { scope }),
  confirmarBaja: (scope: AlcanceBaja, code: string) => pedir<BajaPendiente>("POST", "/api/v1/me/offboarding/confirm", { scope, code }),
  restaurarBaja: () => pedir<void>("POST", "/api/v1/me/offboarding/restore"),
  /** Si el correo del último código ha salido: lo envía el worker después de responder al POST. */
  estadoCodigoBaja: () => pedir<{ status: "pendiente" | "enviado" | "fallido" | "ninguno" }>("GET", "/api/v1/me/offboarding/code"),
  /** Restauración desde el enlace del correo, sin sesión. 410 si el enlace no vale, está usado o ha caducado. */
  restaurarConToken: (token: string) => pedir<void>("POST", "/api/v1/public/offboarding/restore", { token }),
};

/** 402 por el plan de la ficha (`code: "plan_required"`), no por falta de hueco al activar. */
export function faltaPlan(e: unknown, funcion: "ai_replies" | "key_dates"): e is ApiError {
  return e instanceof ApiError && e.status === 402 && e.codigo === "plan_required" && e.funcion === funcion;
}
