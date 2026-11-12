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
  salir: () => pedir<void>("POST", "/api/v1/auth/logout"),
};
