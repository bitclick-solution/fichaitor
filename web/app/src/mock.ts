// Respuestas simuladas de las rutas con sesión, según docs/contrato-web-api.md.
// Solo se usan con `npm run dev:mock`. Nunca en la compilación de producción.
//
// Escenario por URL: /app/?mock=sin-sesion | sin-fichas | una | caducado | varias (por defecto)

const CLAVE = "fichaitor_mock";

interface Estado {
  sesion: boolean;
  elegida: string | null;
}

const FICHAS = [
  { id: "loc_3f2a", title: "Bar La Parra", place_id: "ChIJ_parra", verified: true },
  { id: "loc_81bc", title: "Bar La Parra · Terraza del Río", place_id: "ChIJ_rio", verified: true },
  { id: "loc_c07d", title: "Obrador La Parra", place_id: null, verified: false },
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

const json = (status: number, cuerpo: unknown) =>
  new Response(JSON.stringify(cuerpo), { status, headers: { "content-type": "application/json" } });

let llamadasResumen = 0;
let llamadasDiagnostico = 0;

const yo = (e: Estado) => ({ email: "dueno@example.com", locations: fichas(), selected_location_id: e.elegida });

export async function mockFetch(method: string, path: string, body: unknown): Promise<Response> {
  await new Promise((r) => setTimeout(r, 350));
  const e = leer();

  if (path === "/api/v1/auth/logout") {
    guardar({ sesion: false, elegida: null });
    return new Response(null, { status: 204 });
  }
  if (!e.sesion) return json(401, { detail: "No has iniciado sesión." });

  if (method === "GET" && path === "/api/v1/me") return json(200, yo(e));

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
