// Respuestas simuladas de las rutas con sesión, según docs/contrato-web-api.md.
// Solo se usan con `npm run dev:mock`. Nunca en la compilación de producción.
//
// Escenario por URL: /app/?mock=sin-sesion | sin-fichas | una | sin-resenas | caducado | falla | varias (por defecto)

const CLAVE = "fichaitor_mock";

interface Estado {
  sesion: boolean;
  elegida: string | null;
  baja?: { scope: string; delete_at: string } | null;
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
const CANALES: { id: string; channel: string; address: string; linked_at: string }[] = [];
let codigoPendiente: { consultas: number } | null = null;

interface ResenaSimulada {
  id: string;
  stars: number;
  text: string;
  created_at: string;
  replied: boolean;
  reply: { text: string; updated_at: string } | null;
}

const RESENAS: ResenaSimulada[] = [
  { id: "rev_1", stars: 2, text: "Tardaron mucho en servir y la mesa estaba sin limpiar. La comida, bien.", created_at: "2026-11-08T20:15:00Z", replied: false, reply: null },
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
  return { id: a.id, type: a.type, review_id: a.review_id, status: a.status, message: a.message, error: a.error, confirmed_at: a.confirmed_at, finished_at: a.finished_at };
}

const json = (status: number, cuerpo: unknown) =>
  new Response(JSON.stringify(cuerpo), { status, headers: { "content-type": "application/json" } });

let llamadasResumen = 0;
let llamadasDiagnostico = 0;

const yo = (e: Estado) => ({ email: "dueno@example.com", locations: fichas(), selected_location_id: e.elegida, pending_deletion: e.baja ?? null });

export async function mockFetch(method: string, path: string, body: unknown): Promise<Response> {
  await new Promise((r) => setTimeout(r, 350));
  const e = leer();

  // Restauración por el enlace del correo, sin sesión. Token válido en el simulador: «bueno»
  if (method === "POST" && path === "/api/v1/public/offboarding/restore") {
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

  if (method === "GET" && path === "/api/v1/me") return json(200, yo(e));

  if (method === "POST" && path === "/api/v1/channels/link-code") {
    codigoPendiente = { consultas: 0 };
    return json(200, {
      code: "482913",
      expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
      telegram_url: "https://t.me/FichaitorBot?start=482913",
      whatsapp_url: null,
    });
  }
  if (method === "GET" && path === "/api/v1/me/channels") {
    if (codigoPendiente && ++codigoPendiente.consultas >= 2) {
      CANALES.push({ id: `ch_${CANALES.length + 1}`, channel: "telegram", address: "Telegram de ••••7342", linked_at: new Date().toISOString() });
      codigoPendiente = null;
    }
    return json(200, { channels: CANALES });
  }
  if (method === "DELETE" && path.startsWith("/api/v1/me/channels/")) {
    const id = path.split("/").pop();
    const k = CANALES.findIndex((c) => c.id === id);
    if (k >= 0) CANALES.splice(k, 1);
    return new Response(null, { status: 204 });
  }

  // ── Baja con código de un solo uso (propuesta de contrato). Código válido en el simulador: 123456
  if (method === "POST" && path === "/api/v1/me/offboarding/code") {
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

  if (method === "POST" && path === "/api/v1/me/location") {
    const id = (body as { location_id?: string }).location_id;
    if (!fichas().some((f) => f.id === id)) return json(404, { detail: "Esa ficha no es de tu cuenta." });
    e.elegida = id ?? null;
    guardar(e);
    return json(200, yo(e));
  }

  if (method === "GET" && path === "/api/v1/me/summary") {
    if (!e.elegida) return json(409, { detail: "Elige primero una ficha." });
    llamadasResumen += 1;
    const vacio = {
      synced_at: null, review_count: null, average_rating: null, unanswered_count: null, unanswered_low_count: null,
      last_review_at: null, has_regular_hours: null, upcoming_special_hours: null, google_changed: null,
      google_changed_fields: null, google_changed_labels: null, stale: false,
    };
    // Las dos primeras llamadas responden `pendiente`, como tras elegir ficha.
    if (llamadasResumen <= 2) return json(200, { status: "pendiente", ...vacio });
    return json(200, {
      status: escenario() === "caducado" ? "needs_reconnect" : "ok",
      synced_at: new Date().toISOString(),
      review_count: 13, average_rating: 4.7, unanswered_count: 13, unanswered_low_count: 1,
      last_review_at: "2025-02-06", has_regular_hours: true, upcoming_special_hours: 0,
      google_changed: true, google_changed_fields: ["specialHours"], google_changed_labels: ["Horarios especiales"],
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
