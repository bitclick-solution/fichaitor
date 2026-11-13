import { api, type PeticionCobro } from "./api";

// Peticiones al proceso de cobro (docs/contrato-web-api.md, «Cobro y paywall»). Cada acción responde
// 202 {request_id}; la web consulta la petición hasta que esté `lista` (con `url` si hay que ir a Stripe)
// o `fallida` (con `detail` listo para mostrar).

/** Solo se abre una URL de Stripe (o, en el simulador, una de esta misma web). */
export function urlDePago(url: string): string | null {
  try {
    const u = new URL(url, location.origin);
    if (u.protocol === "https:" && (u.hostname === "stripe.com" || u.hostname.endsWith(".stripe.com"))) return u.href;
    if (import.meta.env.MODE === "mock" && u.origin === location.origin) return u.href;
  } catch {
    /* no es una URL */
  }
  return null;
}

/**
 * Espera a que una petición termine: cada 2 s, 20 veces como mucho. Devuelve la petición `lista` o
 * `fallida`, o null si se agota el tiempo o `sigue()` dice que ya no hace falta (el componente se fue).
 */
export async function esperarPeticion(id: string, sigue: () => boolean): Promise<PeticionCobro | null> {
  for (let i = 0; i < 20; i++) {
    await new Promise((ok) => setTimeout(ok, 2000));
    if (!sigue()) return null;
    const p = await api.peticionCobro(id);
    if (!sigue()) return null;
    if (p.state !== "pendiente") return p;
  }
  return null;
}

const fechaLarga = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", year: "numeric" });

/** El día en que acaba la ventana de la beta, a partir de los días que quedan («31 de marzo de 2027»). */
export function finDePrueba(dias: number): string {
  const d = new Date();
  d.setDate(d.getDate() + dias);
  return fechaLarga.format(d);
}

export const TARDA = "Está tardando más de lo normal. Prueba otra vez en un minuto.";

/** Importe en euros a partir de céntimos: «35 €», «33,25 €». */
export function euros(centimos: number, moneda = "EUR"): string {
  return new Intl.NumberFormat("es-ES", { style: "currency", currency: moneda, minimumFractionDigits: centimos % 100 === 0 ? 0 : 2 }).format(
    centimos / 100,
  );
}
