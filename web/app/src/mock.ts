// Respuestas simuladas de las rutas con sesión, según docs/contrato-web-api.md.
// Solo se usan con `npm run dev:mock`. Nunca en la compilación de producción.
//
// Escenario por URL: /app/?mock=sin-sesion | sin-fichas | una | sin-resenas | caducado | falla | gratis (sin derecho beta) | cobro (precios con cobro) | varias (por defecto)

const CLAVE = "fichaitor_mock";

interface Estado {
  sesion: boolean;
  elegida: string | null;
  baja?: { scope: string; delete_at: string } | null;
  /** Multinegocio: plan de cada ficha activa. */
  planes?: Record<string, string>;
  /** «bloqueada»: ya se ha pagado lo pendiente. */
  pagado?: boolean;
  /** Cobro: datos fiscales guardados, plazas pagadas y un pago en curso (vuelta de Stripe simulada). */
  perfil?: { legal_name: string; tax_id: string; address: Record<string, string | null>; invoice_email: string } | null;
  plazas?: {
    id: string;
    plan: string;
    location_id: string;
    status: string;
    renews_at: string;
    cancel_at_period_end: boolean;
    next_price: number;
    payment_method: string;
    collection: string;
    trial_ends_at?: string | null;
  }[];
  pagoEnCurso?: { ficha: string; plan: string; vueltas: number } | null;
}

const FICHAS = [
  { id: "loc_3f2a", title: "Bar La Parra", place_id: "ChIJ_parra", verified: true },
  { id: "loc_81bc", title: "PUBLIEXPE | Regalos de empresa y merchandising personalizado", place_id: "ChIJ_rio", verified: true },
  { id: "loc_c07d", title: "Restaurante Asador El Rincón de la Abuela Carmen Granada Centro", place_id: null, verified: false },
];

function escenario(): string {
  return new URLSearchParams(location.search).get("mock") ?? "varias";
}

function leer(): Estado {
  try {
    const crudo = sessionStorage.getItem(CLAVE);
    if (crudo) return JSON.parse(crudo) as Estado;
  } catch {
    /* almacenamiento no disponible */
  }
  return { sesion: escenario() !== "sin-sesion", elegida: null };
}

function guardar(e: Estado): void {
  try {
    sessionStorage.setItem(CLAVE, JSON.stringify(e));
  } catch {
    /* almacenamiento no disponible */
  }
}

function fichas() {
  const e = escenario();
  if (e === "sin-fichas") return [];
  if (e === "una") return FICHAS.slice(0, 1);
  return FICHAS;
}

let llamadasSentimiento = 0;

// Canales: tras pedir un código, la segunda consulta ya lo ve conectado (como si se hubiera enviado).
// Cada chat guarda su filtro de fichas (null = todas); la etiqueta se compone al responder, como el backend.
const CANALES: { id: string; channel: string; address: string; linked_at: string; locations: string[] | null }[] = [];
// Peticiones al proceso de cobro: plaza nueva, cancelar, reanudar, pagar lo pendiente o el portal de Stripe.
/** «AAAA-MM-DD» en la hora local (toISOString la pasaría a UTC y podría restar un día). */
const fechaLocal = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

const PETICIONES: Record<string, { vueltas: number; tipo: "plaza" | "cancelar" | "reanudar" | "pagar" | "portal"; ficha?: string; plan?: string; plaza?: string }> = {};
const nuevaPeticion = (p: Omit<(typeof PETICIONES)[string], "vueltas">) => {
  const id = `req_${Date.now()}_${Object.keys(PETICIONES).length}`;
  PETICIONES[id] = { vueltas: 0, ...p };
  return json(202, { request_id: id });
};
let codigoPendiente: { consultas: number; locations: string[] | null } | null = null;

/** «A», «A y B», «A, B y C»; una ficha inactiva lleva « (desactivada)». */
function etiquetaFiltro(e: Estado, filtro: string[] | null): string {
  if (filtro === null) return "Todos tus negocios";
  const nombres = fichas()
    .filter((f) => filtro.includes(f.id))
    .map((f) => (e.planes?.[f.id] ? f.title : `${f.title} (desactivada)`));
  if (nombres.length === 0) return "Ninguno de tus negocios";
  return nombres.length === 1 ? nombres[0]! : `${nombres.slice(0, -1).join(", ")} y ${nombres[nombres.length - 1]}`;
}

/** Las reglas del filtro: fichas activas de la cuenta, de 1 a 50. Devuelve el error o la lista limpia. */
function validarFiltro(e: Estado, filtro: unknown): Response | string[] | null {
  if (e.baja) return json(409, { detail: "Tienes una baja programada. Restáurala para seguir usando Fichaitor." });
  if (filtro === null || filtro === undefined) return null;
  if (!Array.isArray(filtro) || filtro.length === 0 || filtro.length > 50 || filtro.some((x) => typeof x !== "string"))
    return json(422, { detail: [{ msg: "Lista no válida" }] });
  const ids = [...new Set(filtro as string[])];
  if (ids.some((id) => !fichas().some((f) => f.id === id))) return json(404, { detail: "Esa ficha no está en tu cuenta." });
  if (ids.some((id) => !e.planes?.[id])) return json(422, { detail: "Esa ficha no está activa." });
  return fichas()
    .filter((f) => ids.includes(f.id))
    .map((f) => f.id);
}

function canalVisto(e: Estado, c: (typeof CANALES)[number]) {
  return { ...c, locations_label: etiquetaFiltro(e, c.locations) };
}

interface ResenaSimulada {
  id: string;
  stars: number;
  text: string;
  created_at: string;
  replied: boolean;
  reply: { text: string; updated_at: string } | null;
}

const RESENAS: ResenaSimulada[] = [
  {
    id: "rev_1",
    stars: 2,
    text: "Tardaron mucho en servir y la mesa estaba sin limpiar. La comida, bien.",
    created_at: "2026-11-08T20:15:00Z",
    replied: false,
    reply: null,
  },
  { id: "rev_2", stars: 5, text: "El arroz, espectacular. Volveremos.", created_at: "2026-11-05T14:02:00Z", replied: false, reply: null },
  { id: "rev_3", stars: 4, text: "", created_at: "2026-10-28T21:40:00Z", replied: false, reply: null },
  {
    id: "rev_4",
    stars: 5,
    text: "Trato de diez y terraza muy agradable.",
    created_at: "2026-10-12T13:20:00Z",
    replied: true,
    reply: { text: "¡Gracias! Nos alegra que disfrutaras la terraza.", updated_at: "2026-10-13T09:00:00Z" },
  },
];

interface AccionSimulada {
  id: string;
  type: string;
  review_id: string;
  status: string;
  message: string;
  error: string | null;
  confirmed_at: string;
  finished_at: string | null;
  consultas: number;
  texto: string;
}

const ACCIONES: AccionSimulada[] = [];

/** La acción tal como la devuelve la API, sin los campos internos del simulador. */
function publica(a: AccionSimulada) {
  return {
    id: a.id,
    type: a.type,
    review_id: a.review_id,
    status: a.status,
    message: a.message,
    error: a.error,
    confirmed_at: a.confirmed_at,
    finished_at: a.finished_at,
  };
}

const json = (status: number, cuerpo: unknown) => new Response(JSON.stringify(cuerpo), { status, headers: { "content-type": "application/json" } });

let llamadasResumen = 0;
let consultasCodigoBaja = 0;
let llamadasDiagnostico = 0;

/** Bolsa de WhatsApp: 80 mensajes al mes por ficha Pro+; null sin ninguna. «whatsapp-agotado»: ya gastada. */
function bolsaDe(e: Estado) {
  const fichasPlus = Object.values(e.planes ?? {}).filter((p) => p === "pro_whatsapp").length;
  if (fichasPlus === 0) return null;
  const limit = 80 * fichasPlus;
  const used = escenario() === "whatsapp-agotado" ? limit : 12;
  return { month: new Date().toISOString().slice(0, 7), used, limit, remaining: limit - used };
}

/** Bloque `billing` de GET /me (docs/admin-facturacion.md, 4.7). */
function cobroDe(e: Estado) {
  const pagadas = (e.plazas ?? []).length;
  return {
    profile_complete: !!e.perfil,
    // «bloqueada»: un cobro rechazado hasta que se paga desde la pantalla de pausa.
    blocked: escenario() === "bloqueada" && !e.pagado ? { since: new Date(Date.now() - 86_400_000).toISOString(), reason: "payment_failed" } : null,
    seats: e.plazas ?? [],
    // Sin IVA y en céntimos; el 5 % desde la segunda ficha de pago.
    new_seat_price: { pro: pagadas > 0 ? 3325 : 3500, pro_whatsapp: pagadas > 0 ? 5605 : 5900 },
  };
}

const yo = (e: Estado) => {
  // Con la baja de la ficha efectiva, el backend responde sin fichas y con connected: false.
  const desconectada = escenario() === "desconectada";
  // «sin-acceso»: Google niega la ficha elegida; sigue elegida, pero desaparece de la lista.
  if (escenario() === "sin-acceso")
    return {
      email: "dueno@example.com",
      locations: conPlanes(e, FICHAS.slice(1)),
      selected_location_id: FICHAS[0]!.id,
      pending_deletion: null,
      connected: true,
      plans: planesCuenta(e),
      pricing: precios(e),
      billing: cobroDe(e),
      whatsapp: bolsaDe(e),
    };
  return {
    email: "dueno@example.com",
    plans: planesCuenta(e),
    pricing: precios(e),
    billing: cobroDe(e),
    whatsapp: bolsaDe(e),
    locations: desconectada ? [] : conPlanes(e, fichas()),
    selected_location_id: desconectada ? null : e.elegida,
    pending_deletion: e.baja ?? null,
    connected: !desconectada,
  };
};

// Fechas clave (docs/contrato-web-api.md, «Fechas clave»). Se calculan desde hoy para que siempre estén
// por venir. La tercera cierra ese día de la semana (regular_hours: []).
function enDias(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
type FranjaSim = { open: string; close: string };
interface FechaSim {
  id: string;
  date: string;
  name: string;
  regular_hours: FranjaSim[] | null;
  status: string;
  answerable: boolean;
  answer: { kind: string; ranges: FranjaSim[]; text: string } | null;
  action: { id: string; status: string; message: string; error: string | null } | null;
  /** Consultas desde la confirmación: la acción pasa a «hecha» a la tercera. */
  vueltas: number;
}
const pendiente = (id: string, dias: number, name: string, regular_hours: FranjaSim[] | null): FechaSim => ({
  id,
  date: enDias(dias),
  name,
  regular_hours,
  status: "pendiente",
  answerable: true,
  answer: null,
  action: null,
  vueltas: 0,
});
let FECHAS: FechaSim[] | null = null;
const fechasClave = () =>
  (FECHAS ??= [
    pendiente("kd_1", 12, "Inmaculada Concepción", [
      { open: "09:00", close: "14:00" },
      { open: "17:00", close: "20:30" },
    ]),
    pendiente("kd_2", 30, "Nochebuena", null),
    pendiente("kd_3", 37, "Nochevieja", []),
  ]);
const textoFranjas = (r: FranjaSim[]) => r.map((x) => `de ${x.open} a ${x.close}`).join(" y ");
const sinVueltas = ({ vueltas: _v, ...f }: FechaSim) => f;

// Multinegocio (docs/multinegocio.md, 5.2 y 5.3). Al empezar no hay ninguna ficha activa: elegir la
// primera la activa en Gratis. El derecho beta da 3 fichas Pro; con ?mock=gratis no hay derecho.
// Los precios del catálogo son los decididos por el propietario (apartado 15); el catálogo es una
// propuesta de la web al backend.
// Plan Gratis: 25 respuestas con IA por ficha, para siempre. Bar La Parra empieza en 23 para ver el tope en dos borradores.
const LIMITE_GRATIS = 25;
const USO_IA: Record<string, number> = { loc_3f2a: 23 };
// La ficha que se está viendo: la del último ?location= de una lectura (los borradores y las fechas no lo llevan).
let fichaVista: string | null = null;
const planDe = (e: Estado, id: string | null) => (id ? (e.planes?.[id] ?? null) : null);

const PENDIENTES: Record<string, { reviews: number; key_dates: number }> = {
  loc_3f2a: { reviews: 3, key_dates: 2 },
  loc_81bc: { reviews: 1, key_dates: 0 },
  loc_c07d: { reviews: 0, key_dates: 1 },
};
const BETA_PRO = 3;
function conPlanes(e: Estado, lista: typeof FICHAS) {
  return lista.map((f) => {
    const plan = e.planes?.[f.id] ?? null;
    return {
      ...f,
      active: plan !== null,
      plan,
      active_since: plan ? "2026-11-12T10:00:00Z" : null,
      // La tercera, sin verificar, sale con error para ver el aviso del selector.
      status: plan ? (f.id === "loc_c07d" ? "error" : "ok") : null,
      pending: plan ? (PENDIENTES[f.id] ?? { reviews: 0, key_dates: 0 }) : null,
      usage: plan ? { ai_replies: { used: USO_IA[f.id] ?? 0, limit: plan === "gratis" ? LIMITE_GRATIS : null, lifetime: plan === "gratis" } } : null,
      features: plan ? { key_dates: plan !== "gratis", whatsapp: plan === "pro_whatsapp" } : null,
    };
  });
}
function planesCuenta(e: Estado) {
  const usados = Object.values(e.planes ?? {});
  // Con cobro («cobro») ya no hay plazas de la beta: las de pago se compran en el paywall.
  const beta = ["gratis", "cobro", "bloqueada", "lanzamiento"].includes(escenario()) ? 0 : BETA_PRO;
  return {
    active: usados.length,
    free_available: !usados.includes("gratis"),
    available: { pro: Math.max(0, beta - usados.filter((p) => p === "pro").length), pro_whatsapp: 0 },
  };
}
// Precios de la API (docs/multinegocio.md, «Precios en la respuesta»): el descuento se aplica desde la
// segunda ficha de pago; la gratuita no cuenta. Beta: charging false.
const TARIFAS = { pro: { first: 3500, additional: 3325 }, pro_whatsapp: { first: 5900, additional: 5605 } } as const;
function totalDe(planes: string[]): number {
  const pago = planes.filter((p): p is "pro" | "pro_whatsapp" => p === "pro" || p === "pro_whatsapp");
  // La de precio completo es la de plan más caro.
  pago.sort((a, b) => TARIFAS[b].first - TARIFAS[a].first);
  return pago.reduce((s, p, i) => s + (i === 0 ? TARIFAS[p].first : TARIFAS[p].additional), 0);
}
function precios(e: Estado) {
  const actuales = e.planes ?? {};
  const usados = Object.values(actuales);
  return {
    currency: "EUR",
    period: "year",
    charging: ["cobro", "bloqueada", "lanzamiento"].includes(escenario()),
    // «lanzamiento»: cuenta de la beta con 18 días de su ventana para elegir plan sin pagar.
    trial_days: escenario() === "lanzamiento" ? 18 : null,
    plans: TARIFAS,
    total: totalDe(usados),
    if_activate: { pro: totalDe([...usados, "pro"]), pro_whatsapp: totalDe([...usados, "pro_whatsapp"]) },
    if_deactivate: Object.fromEntries(
      Object.keys(actuales).map((id) => [
        id,
        totalDe(
          Object.entries(actuales)
            .filter(([k]) => k !== id)
            .map(([, v]) => v),
        ),
      ]),
    ),
  };
}

let correosMock: { commercial: boolean; changed_at: string | null; source: string | null } = { commercial: true, changed_at: null, source: null };

export async function mockFetch(method: string, path: string, body: unknown): Promise<Response> {
  await new Promise((r) => setTimeout(r, 350));
  const e = leer();
  // Las lecturas llevan ?location= (multinegocio). El simulador sirve lo mismo para todas: se quita.
  {
    const u = new URL(path, "http://simulador");
    if (u.searchParams.get("location")) fichaVista = u.searchParams.get("location");
    u.searchParams.delete("location");
    path = u.pathname + u.search;
  }

  // Restauración por el enlace del correo, sin sesión. Token válido en el simulador: «bueno»
  if (method === "POST" && path === "/api/v1/public/offboarding/restore") {
    // «falla»: el servidor no responde; el enlace sigue valiendo y se ofrece reintentar.
    if ((body as { token?: string }).token === "falla") return json(503, { detail: "El servicio no responde ahora mismo." });
    if ((body as { token?: string }).token !== "bueno") return json(410, { detail: "Ese enlace ya no sirve: se ha usado o ha caducado." });
    e.baja = null;
    e.sesion = true;
    guardar(e);
    return new Response(null, { status: 204 });
  }

  if (path === "/api/v1/auth/logout") {
    guardar({ sesion: false, elegida: null });
    return new Response(null, { status: 204 });
  }
  if (!e.sesion) return json(401, { detail: "No has iniciado sesión." });

  if (method === "GET" && path === "/api/v1/me") {
    // Vuelta de Stripe simulada: la plaza se activa a la segunda consulta, como si el webhook tardara.
    if (e.pagoEnCurso && ++e.pagoEnCurso.vueltas >= 2) {
      const { ficha, plan } = e.pagoEnCurso;
      e.planes = { ...(e.planes ?? {}), [ficha]: plan };
      const hoy = new Date();
      e.plazas = [
        ...(e.plazas ?? []),
        {
          id: `seat_${(e.plazas ?? []).length + 1}`,
          plan,
          location_id: ficha,
          status: "active",
          renews_at: new Date(hoy.getFullYear() + 1, hoy.getMonth(), hoy.getDate()).toISOString().slice(0, 10),
          cancel_at_period_end: false,
          next_price: plan === "pro" ? 3500 : 5900,
          payment_method: "card",
          collection: "auto",
          // En la ventana de la beta, el primer cobro llega al terminarla.
          trial_ends_at: escenario() === "lanzamiento" ? fechaLocal(new Date(hoy.getFullYear(), hoy.getMonth(), hoy.getDate() + 18)) : null,
        },
      ];
      e.pagoEnCurso = null;
    }
    guardar(e);
    return json(200, yo(e));
  }

  // ── Cobro (docs/admin-facturacion.md, 4.7). NIF válido en el simulador: 8 cifras y una letra (12345678Z) o B88994777.
  // Países: una muestra de los tres tipos (la API real trae todos los del mundo menos los bloqueados).
  if (method === "GET" && path === "/api/v1/me/billing/countries") {
    const ue = [
      "AT",
      "BE",
      "BG",
      "CY",
      "CZ",
      "DE",
      "DK",
      "EE",
      "FI",
      "FR",
      "GR",
      "HR",
      "HU",
      "IE",
      "IT",
      "LT",
      "LU",
      "LV",
      "MC",
      "MT",
      "NL",
      "PL",
      "PT",
      "RO",
      "SE",
      "SI",
      "SK",
    ];
    const fuera = ["AD", "AR", "AU", "BR", "CA", "CH", "CL", "CO", "GB", "JP", "MA", "MX", "NO", "PE", "US"];
    const lista = [{ code: "ES", region: "es" }, ...ue.map((code) => ({ code, region: "ue" })), ...fuera.map((code) => ({ code, region: "fuera_ue" }))];
    return json(200, { countries: lista.sort((a, b) => a.code.localeCompare(b.code)) });
  }
  if (path === "/api/v1/me/billing/profile") {
    if (method === "GET") return json(200, e.perfil ?? { legal_name: null, tax_id: null, address: null, invoice_email: null, complete: false });
    const p = body as NonNullable<Estado["perfil"]>;
    const pais = String(p.address?.["country"] ?? "");
    const cp = String(p.address?.["postal_code"] ?? "");
    const ue = [
      "AT",
      "BE",
      "BG",
      "CY",
      "CZ",
      "DE",
      "DK",
      "EE",
      "FI",
      "FR",
      "GR",
      "HR",
      "HU",
      "IE",
      "IT",
      "LT",
      "LU",
      "LV",
      "MC",
      "MT",
      "NL",
      "PL",
      "PT",
      "RO",
      "SE",
      "SI",
      "SK",
    ];
    if (["CU", "IR", "KP", "SY", "RU", "BY"].includes(pais) || !/^[A-Z]{2}$/.test(pais))
      return json(422, { detail: "No facturamos en ese país.", code: "country_not_supported" });
    const region = pais === "ES" ? "es" : ue.includes(pais) ? "ue" : "fuera_ue";
    if (region === "es" && !/^([0-9]{8}[A-Z]|[A-Z][0-9]{7}[0-9A-Z])$/.test(p.tax_id))
      return json(422, { detail: "Ese NIF no es válido. Revísalo.", code: "tax_id_invalid" });
    if (region === "ue" && !new RegExp(`^${pais === "MC" ? "FR" : pais}[0-9A-Z]{8,12}$`).test(p.tax_id))
      return json(422, { detail: "Ese NIF-IVA no tiene el formato de ese país. Escríbelo con su prefijo (por ejemplo, FR40303265045).", code: "tax_id_invalid" });
    if (region === "fuera_ue" && (!/^[0-9A-ZÑ&\-./ ]{3,32}$/.test(p.tax_id) || !/[0-9]/.test(p.tax_id)))
      return json(422, { detail: "Esa identificación fiscal no es válida. Escríbela tal como aparece en los documentos de tu empresa.", code: "tax_id_invalid" });
    if (region === "es" && !/^[0-9]{5}$/.test(cp)) return json(422, { detail: "El código postal debe tener 5 cifras.", code: "postal_code_invalid" });
    if (region === "ue" && !cp) return json(422, { detail: "Falta el código postal.", code: "postal_code_invalid" });
    const tax_treatment = region === "fuera_ue" ? "extracomunitario" : region === "ue" ? "isp_ue" : "iva_21";
    e.perfil = { ...p, tax_treatment } as NonNullable<Estado["perfil"]>;
    guardar(e);
    return json(200, { ...e.perfil, complete: true });
  }
  if (method === "POST" && path === "/api/v1/me/billing/seats") {
    if (!e.perfil) return json(409, { detail: "Faltan tus datos para la factura.", code: "profile_incomplete" });
    const b = body as { plan: string; location_id: string };
    return nuevaPeticion({ tipo: "plaza", ficha: b.location_id, plan: b.plan });
  }
  const accionPlaza = /^\/api\/v1\/me\/billing\/seats\/([^/]+)\/(cancel|resume)$/.exec(path);
  if (method === "POST" && accionPlaza) {
    if (!(e.plazas ?? []).some((s) => s.id === accionPlaza[1])) return json(404, { detail: "Esa plaza ya no se puede cambiar." });
    return nuevaPeticion({ tipo: accionPlaza[2] === "cancel" ? "cancelar" : "reanudar", plaza: accionPlaza[1]! });
  }
  if (method === "POST" && path === "/api/v1/me/billing/pay") return nuevaPeticion({ tipo: "pagar" });
  if (method === "POST" && path === "/api/v1/me/billing/portal") return nuevaPeticion({ tipo: "portal" });
  if (method === "GET" && path === "/api/v1/me/billing/invoices")
    return json(200, {
      invoices: (e.plazas ?? []).map((s, i) => ({
        id: `inv_${i + 1}`,
        kind: "factura",
        number: `FCH/2026/${String(i + 1).padStart(5, "0")}`,
        amount_total: Math.round(s.next_price * 1.21),
        issued_at: new Date().toISOString(),
      })),
    });
  const peticion = /^\/api\/v1\/me\/billing\/requests\/([^/]+)$/.exec(path);
  if (method === "GET" && peticion) {
    const r = PETICIONES[peticion[1]!];
    if (!r) return json(404, { detail: "No encontramos esa petición." });
    if (escenario() === "falla") return json(200, { state: "fallida", code: "stripe_error", detail: "Stripe no responde ahora mismo. Prueba otra vez en un minuto." });
    if (++r.vueltas < 2) return json(200, { state: "pendiente" });
    if (r.tipo === "cancelar" || r.tipo === "reanudar") {
      const s = (e.plazas ?? []).find((x) => x.id === r.plaza);
      if (s) s.cancel_at_period_end = r.tipo === "cancelar";
      guardar(e);
      return json(200, { state: "lista", url: null });
    }
    if (r.tipo === "pagar") {
      // «Factura alojada en Stripe» del simulador: queda pagada y se vuelve a la web.
      e.pagado = true;
      guardar(e);
      return json(200, { state: "lista", url: `/app/?mock=${escenario()}` });
    }
    if (r.tipo === "portal") return json(200, { state: "lista", url: `/app/?mock=${escenario()}` });
    // «Stripe Checkout» del simulador: vuelve directamente con el pago hecho.
    e.pagoEnCurso = { ficha: r.ficha!, plan: r.plan!, vueltas: 0 };
    guardar(e);
    return json(200, { state: "lista", url: `/app/?mock=${escenario()}&pago=ok&ficha=${encodeURIComponent(r.ficha!)}` });
  }

  if (method === "POST" && path === "/api/v1/channels/link-code") {
    if (body && Object.keys(body as object).some((k) => k !== "locations")) return json(422, { detail: [{ msg: "Campo de más" }] });
    const filtro = validarFiltro(e, (body as { locations?: unknown } | undefined)?.locations);
    if (filtro instanceof Response) return filtro;
    codigoPendiente = { consultas: 0, locations: filtro };
    return json(200, {
      code: "482913",
      expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
      // Como el backend: el enlace lleva un testigo de 43 caracteres, nunca las 6 cifras.
      telegram_url: "https://t.me/FichaitorBot?start=Qm3v0X5k1y2Zt8pW-aR4sLd9_fHj6cNe7uBoVgIiT0Q",
      // Como el backend: el enlace de WhatsApp solo existe si está configurado; en el simulador, con alguna ficha Pro+.
      whatsapp_url: Object.values(e.planes ?? {}).includes("pro_whatsapp")
        ? "https://wa.me/34600000000?text=Vincular%20Fichaitor%20Qm3v0X5k1y2Zt8pW-aR4sLd9_fHj6cNe7uBoVgIiT0Q"
        : null,
    });
  }
  const activacion = /^\/api\/v1\/me\/locations\/([^/]+)\/activation$/.exec(path);
  if (activacion && (method === "POST" || method === "DELETE")) {
    const id = activacion[1]!;
    if (!fichas().some((f) => f.id === id)) return json(404, { detail: "Esa ficha no está en tu cuenta." });
    e.planes ??= {};
    if (method === "POST") {
      const p = planesCuenta(e);
      const pedido = (body as { plan?: string } | undefined)?.plan;
      // Como el servidor: en la beta (sin cobro), Pro primero mientras queden plazas.
      const beta = escenario() !== "cobro";
      const plan = pedido ?? (beta && p.available.pro > 0 ? "pro" : p.free_available ? "gratis" : p.available.pro > 0 ? "pro" : null);
      const hueco = plan === "gratis" ? p.free_available : plan === "pro" ? p.available.pro > 0 : false;
      if (!plan || !hueco) return json(402, { detail: "No te quedan fichas libres en tu plan." });
      e.planes[id] = plan;
      e.elegida ??= id;
    } else {
      delete e.planes[id];
      if (e.elegida === id) e.elegida = Object.keys(e.planes)[0] ?? null;
    }
    guardar(e);
    return json(200, yo(e));
  }
  if (method === "GET" && path.startsWith("/api/v1/me/key-dates")) {
    if (escenario() === "sin-fichas") return json(200, { dates: [] });
    // En Gratis no hay propuestas de festivos: la lista sale vacía.
    if (planDe(e, fichaVista ?? e.elegida) === "gratis") return json(200, { dates: [] });
    // Cada consulta avanza el envío a Google: en cola, enviándose y, a la tercera, enviado.
    for (const f of fechasClave()) {
      if (!f.action || f.action.status === "hecha") continue;
      f.vueltas += 1;
      if (f.vueltas >= 3) {
        f.status = "aplicada";
        f.action = { ...f.action, status: "hecha", message: "Enviado a Google. Puede tardar unos minutos en verse en tu ficha." };
      } else if (f.vueltas >= 1) f.action = { ...f.action, status: "en_curso", message: "Estamos enviando tu cambio de horario a Google." };
    }
    const todas = path.includes("filter=all");
    return json(200, {
      dates: fechasClave()
        .filter((f) => todas || f.status === "pendiente")
        .map(sinVueltas),
    });
  }
  const fecha = /^\/api\/v1\/me\/key-dates\/([^/]+)\/(answer|dismiss)$/.exec(path);
  if (method === "POST" && fecha) {
    if (planDe(e, fichaVista ?? e.elegida) === "gratis")
      return json(402, {
        detail: "Los festivos y las fechas clave están en Pro. Pásate a Pro para que te preguntemos por ellos y cambiemos el horario en Google.",
        code: "plan_required",
        feature: "key_dates",
      });
    const f = fechasClave().find((x) => x.id === fecha[1]);
    if (!f) return json(404, { detail: "No encontramos esa fecha." });
    if (f.status !== "pendiente") return json(409, { detail: "Esa fecha ya está resuelta." });
    if (fecha[2] === "dismiss") {
      f.status = "descartada";
      f.answerable = false;
      return new Response(null, { status: 204 });
    }
    const r = body as { kind?: string; ranges?: FranjaSim[] } | undefined;
    // Como el servidor: 422 con el mensaje para los campos. En el simulador, cualquier franja que empiece a medianoche.
    if (r?.kind === "custom" && r.ranges?.some((x) => x.open === "00:00")) return json(422, { detail: "Google no acepta franjas que empiezan a medianoche." });
    if (escenario() === "falla") return json(503, { detail: "No hemos podido guardarlo. Prueba otra vez en un minuto." });
    const ranges = r?.kind === "custom" ? (r.ranges ?? []) : r?.kind === "regular" ? (f.regular_hours ?? []) : [];
    f.status = "en_curso";
    f.answerable = false;
    f.answer = { kind: r?.kind ?? "closed", ranges, text: ranges.length > 0 ? textoFranjas(ranges) : "" };
    f.action = { id: `act_${f.id}`, status: "pendiente", message: "Tu cambio de horario está en cola para enviarse a Google.", error: null };
    return json(202, sinVueltas(f));
  }
  if (method === "GET" && path === "/api/v1/me/channels") {
    if (codigoPendiente && ++codigoPendiente.consultas >= 2) {
      CANALES.push({
        id: `ch_${CANALES.length + 1}`,
        // «whatsapp»: lo que se vincula es un WhatsApp; en los demás, un Telegram.
        channel: escenario() === "whatsapp" ? "whatsapp" : "telegram",
        address: `Telegram de ••••${7342 + CANALES.length}`,
        linked_at: new Date().toISOString(),
        locations: codigoPendiente.locations,
      });
      codigoPendiente = null;
    }
    return json(200, { channels: CANALES.map((c) => canalVisto(e, c)) });
  }
  const filtroCanal = /^\/api\/v1\/me\/channels\/([^/]+)\/locations$/.exec(path);
  if (method === "PUT" && filtroCanal) {
    const c = CANALES.find((x) => x.id === filtroCanal[1]);
    if (!c) return json(404, { detail: "Ese canal no está vinculado a tu cuenta." });
    const cuerpo = body as { locations?: unknown } | undefined;
    if (!cuerpo || !("locations" in cuerpo) || Object.keys(cuerpo).length !== 1) return json(422, { detail: [{ msg: "Falta locations o sobra un campo" }] });
    const filtro = validarFiltro(e, cuerpo.locations);
    if (filtro instanceof Response) return filtro;
    c.locations = filtro;
    return json(200, canalVisto(e, c));
  }
  if (method === "DELETE" && path.startsWith("/api/v1/me/channels/")) {
    const id = path.split("/").pop();
    const k = CANALES.findIndex((c) => c.id === id);
    if (k >= 0) CANALES.splice(k, 1);
    return new Response(null, { status: 204 });
  }

  // ── Baja con código de un solo uso (propuesta de contrato). Código válido en el simulador: 123456
  // Como el backend: el correo sale después de responder al POST; las dos primeras consultas lo ven «pendiente».
  if (method === "GET" && path === "/api/v1/me/offboarding/code")
    return json(200, { status: escenario() === "sin-correo" ? "fallido" : ++consultasCodigoBaja <= 2 ? "pendiente" : "enviado" });
  if (method === "POST" && path === "/api/v1/me/offboarding/code") {
    consultasCodigoBaja = 0;
    await new Promise((ok) => setTimeout(ok, 500));
    return json(202, { sent_to: "d***o@example.com", expires_in: 600 });
  }
  if (method === "POST" && path === "/api/v1/me/offboarding/confirm") {
    const { scope, code } = body as { scope?: string; code?: string };
    if (code !== "123456") return json(422, { detail: "Ese código no es correcto. Te quedan 4 intentos." });
    e.baja = { scope: scope ?? "ficha", delete_at: new Date(Date.now() + 48 * 3600 * 1000).toISOString() };
    // Al borrar la cuenta se cierran todas las sesiones.
    if (scope === "cuenta") e.sesion = false;
    guardar(e);
    return json(200, e.baja);
  }
  if (method === "POST" && path === "/api/v1/me/offboarding/restore") {
    e.baja = null;
    guardar(e);
    return new Response(null, { status: 204 });
  }

  // Comunicaciones comerciales: en memoria (se pierde al recargar). Idempotente, como el backend.
  if (path === "/api/v1/me/communications" && (method === "GET" || method === "PUT")) {
    if (method === "PUT") {
      const c = (body as { commercial?: unknown } | undefined)?.commercial;
      if (typeof c !== "boolean") return json(422, { detail: "commercial tiene que ser true o false." });
      if (c !== correosMock.commercial) correosMock = { commercial: c, changed_at: new Date().toISOString(), source: "settings" };
    }
    return json(200, correosMock);
  }

  if (method === "POST" && path === "/api/v1/me/location") {
    const id = (body as { location_id?: string }).location_id;
    if (!fichas().some((f) => f.id === id)) return json(404, { detail: "Esa ficha no es de tu cuenta." });
    e.elegida = id ?? null;
    e.planes ??= {};
    if (id && Object.keys(e.planes).length === 0) e.planes[id] = "gratis";
    guardar(e);
    return json(200, yo(e));
  }

  if (method === "GET" && path === "/api/v1/me/summary") {
    if (!e.elegida) return json(409, { detail: "Elige primero una ficha." });
    llamadasResumen += 1;
    const vacio = {
      synced_at: null,
      review_count: null,
      average_rating: null,
      unanswered_count: null,
      unanswered_low_count: null,
      last_review_at: null,
      has_regular_hours: null,
      upcoming_special_hours: null,
      google_changed: null,
      google_changed_fields: null,
      google_changed_labels: null,
      stale: false,
    };
    // Las dos primeras llamadas responden `pendiente`, como tras elegir ficha.
    if (llamadasResumen <= 2) return json(200, { status: "pendiente", ...vacio });
    return json(200, {
      status: escenario() === "caducado" ? "needs_reconnect" : "ok",
      synced_at: new Date().toISOString(),
      review_count: 13,
      average_rating: 4.7,
      unanswered_count: 13,
      unanswered_low_count: 1,
      last_review_at: "2025-02-06",
      has_regular_hours: true,
      upcoming_special_hours: 0,
      google_changed: true,
      google_changed_fields: ["specialHours"],
      google_changed_labels: ["Horarios especiales"],
      stale: false,
    });
  }

  if (method === "GET" && path === "/api/v1/me/sentiment") {
    if (!e.elegida) return json(409, { detail: "Elige primero una ficha." });
    llamadasSentimiento += 1;
    if (llamadasSentimiento <= 1) return json(200, { status: "pendiente" });
    const pie = "Se actualiza con 3 reseñas nuevas, con una de 1 o 2 estrellas o a los 30 días.";
    if (escenario() === "sin-resenas") {
      return json(200, {
        status: "insuficiente",
        based_on: 0,
        summary: "Tu ficha todavía no tiene reseñas.",
        suggestion: "Pide a tus clientes de confianza que te dejen las primeras reseñas.",
      });
    }
    if (escenario() === "una") {
      return json(200, {
        status: "ok",
        based_on: 3,
        with_text: 2,
        period: "últimos 12 meses",
        overall: "positivo",
        score: 80,
        provisional: true,
        summary: "Las pocas reseñas que hay hablan bien del trato.",
        praised: [],
        complaints: [],
        suggestion: "Con unas cuantas reseñas más, este resumen será más fiable.",
        delta: null,
        history: [],
        next_update: pie,
        generated_at: new Date().toISOString(),
      });
    }
    return json(200, {
      status: "ok",
      based_on: 13,
      with_text: 11,
      period: "últimos 12 meses",
      overall: "positivo",
      score: 86,
      provisional: false,
      summary: "La gente destaca el trato y la rapidez. La queja que se repite es la espera en hora punta.",
      praised: [
        { topic: "Trato del personal", mentions: 7 },
        { topic: "El arroz", mentions: 4 },
        { topic: "Relación calidad-precio", mentions: 3 },
      ],
      complaints: [{ topic: "Tiempo de espera", mentions: 2 }],
      suggestion: "Responde a las 2 reseñas que mencionan la espera explicando qué habéis cambiado.",
      delta: 4,
      previous_score: 82,
      history: [
        { month: "2026-06", score: 74 },
        { month: "2026-07", score: 78 },
        { month: "2026-08", score: 77 },
        { month: "2026-09", score: 81 },
        { month: "2026-10", score: 82 },
        { month: "2026-11", score: 86 },
      ],
      next_update: pie,
      generated_at: new Date().toISOString(),
    });
  }

  // ── Reseñas y respuestas
  if (method === "GET" && path.startsWith("/api/v1/me/reviews")) {
    if (!e.elegida) return json(409, { detail: "Elige primero una ficha." });
    const todas = new URLSearchParams(path.split("?")[1] ?? "").get("filter") === "all";
    const lista = RESENAS.filter((r) => todas || !r.replied).sort((a, b) => Number(a.replied) - Number(b.replied) || a.stars - b.stars);
    return json(200, { status: "ok", synced_at: new Date().toISOString(), stale: false, total: lista.length, reviews: lista });
  }

  const partes = path.split("/");
  // /api/v1/me/reviews/{id}/draft | reply
  if (method === "POST" && partes[4] === "reviews" && partes[6] === "draft") {
    const r = RESENAS.find((x) => x.id === partes[5]);
    if (!r) return json(404, { detail: "Esa reseña ya no existe." });
    if (!r.text) return json(422, { detail: "Esta reseña no tiene texto. Escribe tú la respuesta." });
    const vista = fichaVista ?? e.elegida;
    if (vista && planDe(e, vista) === "gratis" && (USO_IA[vista] ?? 0) >= LIMITE_GRATIS)
      return json(402, {
        detail: "Has usado tus 25 respuestas con IA gratuitas. Pásate a Pro para tenerlas ilimitadas. Puedes seguir escribiendo tus respuestas.",
        code: "plan_required",
        feature: "ai_replies",
      });
    if (vista) USO_IA[vista] = (USO_IA[vista] ?? 0) + 1;
    await new Promise((ok) => setTimeout(ok, 900));
    const texto =
      r.stars <= 2
        ? "Sentimos que la espera fuera larga. Ese día nos faltó gente en sala y no es excusa. Nos gustaría que volvieras para atenderte como mereces."
        : "Muchas gracias por tu reseña. Nos alegra que lo disfrutaras. Te esperamos pronto.";
    return json(200, { review_id: r.id, text: texto, cached: false });
  }
  if (method === "POST" && partes[4] === "reviews" && partes[6] === "reply") {
    const r = RESENAS.find((x) => x.id === partes[5]);
    if (!r) return json(404, { detail: "Esa reseña ya no existe." });
    const texto = (body as { text?: string }).text ?? "";
    if (!texto.trim()) return json(422, { detail: "Escribe una respuesta antes de publicarla." });
    const accion: AccionSimulada = {
      id: `act_${Date.now()}`,
      type: "review_reply",
      review_id: r.id,
      status: "pendiente",
      message: "Enviando tu respuesta a Google…",
      error: null,
      confirmed_at: new Date().toISOString(),
      finished_at: null,
      consultas: 0,
      texto,
    };
    ACCIONES.push(accion);
    return json(202, publica(accion));
  }
  // /api/v1/me/actions/{id}
  if (method === "GET" && partes[4] === "actions" && partes[5]) {
    const a = ACCIONES.find((x) => x.id === partes[5]);
    if (!a) return json(404, { detail: "Esa acción no existe." });
    a.consultas += 1;
    if (a.consultas === 1) a.status = "en_curso";
    if (a.consultas >= 2 && a.status !== "hecha" && a.status !== "fallida") {
      if (escenario() === "falla") {
        a.status = "fallida";
        a.error = "unavailable";
        a.message = "Google no ha aceptado la respuesta ahora mismo. No se ha publicado.";
      } else {
        a.status = "hecha";
        a.message = "Enviada a Google. Puede tardar unos minutos en verse en tu ficha.";
        const r = RESENAS.find((x) => x.id === a.review_id);
        if (r) {
          r.replied = true;
          r.reply = { text: a.texto, updated_at: new Date().toISOString() };
        }
      }
      a.finished_at = new Date().toISOString();
    }
    return json(200, publica(a));
  }

  if (method === "GET" && path === "/api/v1/me/diagnosis") {
    const f = fichas().find((x) => x.id === e.elegida);
    if (!f) return json(409, { detail: "Elige primero una ficha." });
    // Las dos primeras llamadas responden 404 «pendiente», como mientras el servidor lee la ficha.
    llamadasDiagnostico += 1;
    if (llamadasDiagnostico <= 2) return json(404, { status: "pendiente", detail: "Estamos leyendo tu ficha." });
    if (escenario() === "caducado") return json(404, { status: "needs_reconnect", detail: "El acceso a Google ha caducado." });
    return json(200, {
      status: "ok",
      synced_at: new Date().toISOString(),
      stale: false,
      place_id: f.place_id ?? "",
      name: f.title,
      address: "Calle Elvira 1, Granada",
      category: "Bar",
      score: 58,
      label: "Necesita atención",
      findings: [
        { code: "unanswered_reviews", title: "Tienes 13 reseñas sin responder", detail: "Responder muestra que hay alguien al otro lado.", impact: "alto" },
        { code: "no_holiday_hours", title: "Sin horario para el festivo del 8 de diciembre", detail: "Quien busque ese día no sabrá si abres.", impact: "alto" },
        { code: "google_changed", title: "Google ha cambiado datos de tu ficha", detail: "Revisa que los horarios especiales sigan siendo correctos.", impact: "medio" },
        { code: "no_description", title: "Tu ficha no tiene descripción", detail: "Es tu sitio para contar qué te hace distinto.", impact: "bajo" },
      ],
      locked_checks: [],
      maps_url: "",
      attribution: "Datos de tu Perfil de Empresa de Google",
    });
  }

  return json(404, { detail: "Ruta no simulada." });
}
