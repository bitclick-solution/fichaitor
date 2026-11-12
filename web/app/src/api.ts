// Cliente de la API. Contrato: docs/contrato-web-api.md.
import { mockFetch } from "./mock";

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

export interface Location {
  id: string;
  title: string;
  place_id: string | null;
  verified: boolean;
}

export interface Me {
  email: string;
  locations: Location[];
  selected_location_id: string | null;
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
  review_id: string;
  status: "pendiente" | "en_curso" | "hecha" | "fallida";
  message: string;
  error: "reconnect" | "rejected" | "review_gone" | "unavailable" | "internal" | null;
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

export const OAUTH_START = "/api/v1/google/oauth/start";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** Campo `status` del cuerpo del error, si lo trae (p. ej. «pendiente» en el 404 de /me/diagnosis). */
    readonly estado: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const GENERICO = "Algo ha fallado. Prueba de nuevo en un minuto.";
const SIMULADO = import.meta.env.MODE === "mock";

async function pedir<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Requested-With"] = "fichaitor";
  if (body !== undefined) headers["content-type"] = "application/json";

  const init: RequestInit = { method, headers, credentials: "same-origin" };
  if (body !== undefined) init.body = JSON.stringify(body);

  let r: Response;
  try {
    r = SIMULADO ? await mockFetch(method, path, body) : await fetch(path, init);
  } catch {
    throw new ApiError(0, "No hay conexión con Fichaitor. Comprueba tu red y prueba de nuevo.");
  }

  if (r.status === 204) return undefined as T;
  if (r.ok) return (await r.json()) as T;

  // En los 422 automáticos, `detail` puede ser una lista: se muestra un mensaje genérico.
  let detalle = GENERICO;
  let estado: string | null = null;
  try {
    const j: unknown = await r.json();
    if (typeof j === "object" && j !== null) {
      if ("detail" in j && typeof j.detail === "string") detalle = j.detail;
      if ("status" in j && typeof j.status === "string") estado = j.status;
    }
  } catch {
    /* sin cuerpo */
  }
  throw new ApiError(r.status, detalle, estado);
}

export const api = {
  me: () => pedir<Me>("GET", "/api/v1/me"),
  elegirFicha: (locationId: string) => pedir<Me>("POST", "/api/v1/me/location", { location_id: locationId }),
  diagnostico: () => pedir<Diagnosis>("GET", "/api/v1/me/diagnosis"),
  resumen: () => pedir<Summary>("GET", "/api/v1/me/summary"),
  resenas: async (filtro: "unanswered" | "all"): Promise<ReviewsResponse> => {
    const r = await pedir<Partial<ReviewsResponse> & Pick<ReviewsResponse, "status">>("GET", `/api/v1/me/reviews?filter=${filtro}&limit=50`);
    return { status: r.status, synced_at: r.synced_at ?? null, stale: r.stale ?? false, total: r.total ?? 0, reviews: r.reviews ?? [] };
  },
  borrador: (id: string) => pedir<{ review_id: string; text: string; cached: boolean }>("POST", `/api/v1/me/reviews/${encodeURIComponent(id)}/draft`),
  /** El texto va tal cual: sin recortar ni normalizar. */
  responder: (id: string, text: string) => pedir<Action>("POST", `/api/v1/me/reviews/${encodeURIComponent(id)}/reply`, { text }),
  accion: (id: string) => pedir<Action>("GET", `/api/v1/me/actions/${encodeURIComponent(id)}`),
  sentimiento: async (): Promise<Sentiment> => {
    const s = await pedir<Partial<Sentiment> & Pick<Sentiment, "status">>("GET", "/api/v1/me/sentiment");
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
};
