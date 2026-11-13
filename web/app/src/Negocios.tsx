import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Location, type Me, type PlanFicha, type Precios } from "./api";
import { Modal } from "./Ajustes";
import { Paywall, type MotivoPaywall } from "./Paywall";

// Multinegocio (docs/multinegocio.md, apartado 12): una cuenta con varias fichas activas a la vez.
//  - SelectorNegocio: en la barra de arriba. Las fichas activas primero, con su plan, sus pendientes y
//    un aviso si algo falla; debajo, las demás fichas de la cuenta de Google, con «Activar».
//    Ver una ficha no pausa las demás: Fichaitor sigue vigilando y avisando de todas las activas.
//  - TusNegocios: la pantalla para activar y desactivar fichas.
// Si el backend aún no manda `active` en las fichas, App no pinta nada de esto.

export const esMultinegocio = (me: Me) => me.locations.some((l) => typeof l.active === "boolean");

const NOMBRE_PLAN: Record<PlanFicha, string> = { gratis: "Gratis", pro: "Pro", pro_whatsapp: "Pro+" };

/** «Gratis», «Pro» o «Pro+» (en la API, `pro_whatsapp`); mientras no hay cobro (beta), «Pro (beta)», sin precio. */
function nombrePlan(plan: PlanFicha, precios: Precios | undefined): string {
  return plan !== "gratis" && precios && !precios.charging ? `${NOMBRE_PLAN[plan]} (beta)` : NOMBRE_PLAN[plan];
}

function importe(precios: Precios, centimos: number): string {
  const n = new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: precios.currency,
    minimumFractionDigits: centimos % 100 === 0 ? 0 : 2,
  }).format(centimos / 100);
  return `${n}/${precios.period === "month" ? "mes" : "año"}`;
}

/** Aviso de una ficha activa que no se está leyendo bien. */
function avisoDe(l: Location): string | null {
  if (l.status === "error") return "No hemos podido leerla";
  if (l.status === "needs_reconnect") return "Vuelve a conectar Google";
  return null;
}

function Pendientes({ ficha }: { ficha: Location }) {
  const p = ficha.pending;
  const aviso = avisoDe(ficha);
  const partes = [
    p && p.reviews > 0 ? `${p.reviews} ${p.reviews === 1 ? "reseña" : "reseñas"} sin responder` : null,
    p && p.key_dates > 0 ? `${p.key_dates} ${p.key_dates === 1 ? "festivo" : "festivos"} por contestar` : null,
  ].filter(Boolean);
  if (!aviso && partes.length === 0) return null;
  return (
    <span className="pendientes">
      {aviso && <span className="pendiente-aviso">{aviso}</span>}
      {partes.map((x) => (
        <span key={x}>{x}</span>
      ))}
    </span>
  );
}

// ───── Selector de la barra ─────

export function SelectorNegocio({
  me,
  actual,
  onElegir,
  onActivar,
  onTusNegocios,
}: {
  me: Me;
  actual: string;
  onElegir: (id: string) => Promise<void>;
  /** Lleva a «Tus negocios» con la confirmación de activar esa ficha abierta. */
  onActivar: (id: string) => void;
  onTusNegocios: () => void;
}) {
  const [abierto, setAbierto] = useState(false);
  const [cambiando, setCambiando] = useState<string | null>(null);
  const caja = useRef<HTMLDivElement>(null);
  const activas = me.locations.filter((l) => l.active);
  const otras = me.locations.filter((l) => !l.active);
  const vista = me.locations.find((l) => l.id === actual);

  useEffect(() => {
    if (!abierto) return;
    const fuera = (e: PointerEvent) => {
      if (caja.current && !caja.current.contains(e.target as Node)) setAbierto(false);
    };
    const tecla = (e: KeyboardEvent) => {
      if (e.key === "Escape") setAbierto(false);
    };
    addEventListener("pointerdown", fuera);
    addEventListener("keydown", tecla);
    return () => {
      removeEventListener("pointerdown", fuera);
      removeEventListener("keydown", tecla);
    };
  }, [abierto]);

  // Con una sola ficha activa y ninguna más, el selector es solo el título del panel.
  if (activas.length <= 1 && otras.length === 0) return null;

  const elegir = async (id: string) => {
    if (id === actual) {
      setAbierto(false);
      return;
    }
    setCambiando(id);
    try {
      await onElegir(id);
      setAbierto(false);
    } finally {
      setCambiando(null);
    }
  };

  // Lo que queda pendiente en las demás fichas activas, para verlo sin abrir la lista.
  const enOtras = activas.filter((l) => l.id !== actual).reduce((n, l) => n + (l.pending?.reviews ?? 0) + (l.pending?.key_dates ?? 0), 0);

  return (
    <div className="selector" ref={caja}>
      <button className="selector-boton" type="button" aria-expanded={abierto} aria-haspopup="true" onClick={() => setAbierto((a) => !a)}>
        <span className="selector-nombre">{vista?.title ?? "Tus negocios"}</span>
        {enOtras > 0 && (
          <span className="selector-marca" aria-label={`${enOtras} pendientes en tus otros negocios`}>
            {enOtras}
          </span>
        )}
        <span className="selector-flecha" aria-hidden="true">
          ▾
        </span>
      </button>
      {abierto && (
        <div className="selector-lista">
          <p className="sec">Tus negocios activos</p>
          <ul>
            {activas.map((l) => (
              <li key={l.id}>
                <button type="button" aria-current={l.id === actual} disabled={cambiando !== null} onClick={() => void elegir(l.id)}>
                  <span className="rotulo">{l.title}</span>
                  <span className="sec">
                    {l.plan ? nombrePlan(l.plan, me.pricing) : "Activa"}
                    {cambiando === l.id && " · Cambiando…"}
                  </span>
                  <Pendientes ficha={l} />
                </button>
              </li>
            ))}
          </ul>
          {otras.length > 0 && (
            <>
              <p className="sec">Otras fichas de tu cuenta de Google</p>
              <ul className="selector-otras">
                {otras.map((l) => (
                  <li key={l.id}>
                    <span className="rotulo">{l.title}</span>
                    <button
                      className="enlace"
                      type="button"
                      onClick={() => {
                        setAbierto(false);
                        onActivar(l.id);
                      }}
                    >
                      Activar
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
          <button
            className="enlace"
            type="button"
            onClick={() => {
              setAbierto(false);
              onTusNegocios();
            }}
          >
            Tus negocios: activar y desactivar
          </button>
        </div>
      )}
    </div>
  );
}

// ───── Pantalla «Tus negocios» ─────

export type PreguntaNegocio = { n: "activar" | "desactivar"; id: string };

/**
 * Qué plan recibe una ficha al activarla sin elegir uno (docs/multinegocio.md, 5.3). Durante la beta (sin
 * cobro), Pro mientras queden plazas, para que se pruebe todo; si no, Gratis primero y después los de pago.
 */
export function planAlActivar(me: Me): PlanFicha | null {
  const planes = me.plans;
  if (!planes) return null;
  const enBeta = me.pricing ? !me.pricing.charging : false;
  if (enBeta && planes.available.pro > 0) return "pro";
  if (planes.free_available) return "gratis";
  if (planes.available.pro > 0) return "pro";
  if (planes.available.pro_whatsapp > 0) return "pro_whatsapp";
  return null;
}

/**
 * El plan que se envía al activar. Solo se pide de forma explícita Pro en la beta, para que la ficha quede
 * en el plan que dice la pantalla aunque el servidor elija Gratis primero. En el resto de casos se deja
 * elegir al servidor, que sabe pasar a un plan de pago si la ficha ya está en Gratis en otra cuenta.
 */
export function planPedido(me: Me): PlanFicha | undefined {
  return me.pricing && !me.pricing.charging && planAlActivar(me) === "pro" ? "pro" : undefined;
}

// Texto fijado en docs/multinegocio.md, apartado 12.
const AL_DESACTIVAR =
  "Dejaremos de leer esta ficha y de avisarte de ella. Las propuestas pendientes se cancelan. Si no la vuelves a activar en 30 días, borraremos lo que tenemos de ella. Para retirar el acceso de Fichaitor a tu cuenta de Google, usa Desconectar Google.";

export function TusNegocios({
  me,
  preguntaInicial,
  onCambio,
  onVer,
  onVolver,
  onSinSesion,
}: {
  me: Me;
  preguntaInicial?: PreguntaNegocio | undefined;
  /** Las rutas de activación responden con el cuerpo de GET /me. */
  onCambio: (me: Me) => void;
  /** Elige la ficha y va a su panel. */
  onVer: (id: string) => Promise<void>;
  onVolver: (() => void) | null;
  onSinSesion: () => void;
}) {
  // Si llega a activar una ficha (desde el selector) y no queda hueco en ningún plan, se abre el
  // paywall en lugar de una confirmación sin salida.
  const sinHuecoAlEntrar = preguntaInicial?.n === "activar" && planAlActivar(me) === null;
  const [pregunta, setPregunta] = useState<PreguntaNegocio | null>(sinHuecoAlEntrar ? null : (preguntaInicial ?? null));
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Paywall para una ficha: sin hueco al activarla, o para pasar una ficha Gratis a Pro.
  const [paywall, setPaywall] = useState<{ id: string; motivo: MotivoPaywall } | null>(
    sinHuecoAlEntrar && preguntaInicial ? { id: preguntaInicial.id, motivo: "no_seat" } : null,
  );
  const fichaPaywall = paywall ? me.locations.find((l) => l.id === paywall.id) : undefined;
  const precios = me.pricing;
  const ficha = pregunta ? me.locations.find((l) => l.id === pregunta.id) : undefined;

  const planSiguiente = planAlActivar(me);

  const cerrar = () => {
    setPregunta(null);
    setError(null);
  };

  const responder = async () => {
    if (!pregunta) return;
    setEnviando(true);
    setError(null);
    try {
      const r = pregunta.n === "activar" ? await api.activarFicha(pregunta.id, planPedido(me)) : await api.desactivarFicha(pregunta.id);
      onCambio(r);
      cerrar();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 402: no queda hueco en ningún plan. El paywall ofrece una plaza de pago para esa ficha.
      // Los 409 (code free_taken, offboarding_pending, location_removed) traen su `detail`, que se muestra tal cual.
      else if (e instanceof ApiError && e.status === 402 && e.codigo !== "account_blocked") {
        const id = pregunta.id;
        cerrar();
        setPaywall({ id, motivo: "no_seat" });
      }
      else setError(e instanceof Error ? e.message : "No hemos podido cambiarlo. Prueba otra vez.");
    } finally {
      setEnviando(false);
    }
  };

  return (
    <section className="pantalla negocios">
      <h1 className="cartel">
        Tus negocios<span className="pt">.</span>
      </h1>
      <p className="lead">
        Las fichas que administra <strong>{me.email}</strong> en Google. Fichaitor vigila las activas y te avisa de todas, aunque estés viendo otra.
      </p>

      {precios && (
        <div className="negocios-plan">
          {!precios.charging && <p className="negocios-beta">Durante la beta no cobramos. Estos son los precios de referencia:</p>}
          <p>
            <strong>Gratis:</strong> una ficha.
          </p>
          <p>
            <strong>Pro:</strong> {importe(precios, precios.plans.pro.first)}; desde la segunda ficha de pago, {importe(precios, precios.plans.pro.additional)}.
          </p>
          <p>
            <strong>Pro+ (con WhatsApp):</strong> {importe(precios, precios.plans.pro_whatsapp.first)}; desde la segunda ficha de pago,{" "}
            {importe(precios, precios.plans.pro_whatsapp.additional)}.
          </p>
          {precios.charging && (
            <p className="negocios-total">
              Pagas hoy: <strong>{importe(precios, precios.total)}</strong>
            </p>
          )}
        </div>
      )}

      <ul className="negocios-lista">
        {me.locations.map((l) => (
          <li key={l.id} data-activo={l.active === true}>
            <div>
              <p className="rotulo">{l.title}</p>
              <p className="negocios-estado">{l.active ? (l.plan ? `Activa · ${nombrePlan(l.plan, precios)}` : "Activa") : "Sin activar"}</p>
              {l.active && <Pendientes ficha={l} />}
              {!l.verified && <p className="sec">Sin verificar en Google</p>}
            </div>
            <div className="acciones">
              {l.active ? (
                <>
                  <button className="btn sec" type="button" onClick={() => void onVer(l.id)}>
                    Ver su panel
                  </button>
                  {l.plan === "gratis" && (
                    <button className="btn" type="button" onClick={() => setPaywall({ id: l.id, motivo: "mejorar" })}>
                      Pásate a Pro
                    </button>
                  )}
                  {/* También la última: la cuenta vuelve a «activa tu primera ficha». */}
                  <button className="enlace" type="button" onClick={() => setPregunta({ n: "desactivar", id: l.id })}>
                    Desactivar
                  </button>
                </>
              ) : (
                <button
                  className="btn"
                  type="button"
                  // Sin hueco en ningún plan, directamente el paywall: no hay nada que confirmar.
                  onClick={() => (planSiguiente === null ? setPaywall({ id: l.id, motivo: "no_seat" }) : setPregunta({ n: "activar", id: l.id }))}
                >
                  Activar
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>

      {onVolver && (
        <div className="acciones">
          <button className="btn sec" type="button" onClick={onVolver}>
            Volver al panel
          </button>
        </div>
      )}

      {pregunta && ficha && (
        // Título corto: los nombres de ficha pueden ser muy largos. El nombre va en el cuerpo.
        <Modal titulo={pregunta.n === "activar" ? "Activar esta ficha" : "Desactivar esta ficha"} onCerrar={cerrar} bloqueado={enviando}>
          <p className="rotulo">{ficha.title}</p>
          {pregunta.n === "activar" ? (
            planSiguiente === null ? (
              <p>Ya usas todas las fichas de tu plan.</p>
            ) : (
              <>
                <p>Fichaitor empezará a leer esta ficha y a avisarte de ella, junto a las demás.</p>
                <p>
                  Se activa en el plan <strong>{nombrePlan(planSiguiente, precios)}</strong>.
                  {/* Con cobro, el total resultante lo da la API (if_activate). */}
                  {precios?.charging && planSiguiente !== "gratis" && precios.if_activate[planSiguiente] !== undefined && (
                    <> Tu total pasará a {importe(precios, precios.if_activate[planSiguiente]!)}.</>
                  )}
                </p>
              </>
            )
          ) : (
            <>
              <p>{AL_DESACTIVAR}</p>
              {precios?.charging && precios.if_deactivate[pregunta.id] !== undefined && (
                <p>
                  Tu total pasará a <strong>{importe(precios, precios.if_deactivate[pregunta.id]!)}</strong>.
                </p>
              )}
            </>
          )}
          {error && (
            <p className="aviso" role="alert">
              {error}
            </p>
          )}
          <div className="acciones">
            {!(pregunta.n === "activar" && planSiguiente === null) && (
              <button className="btn" type="button" disabled={enviando} onClick={() => void responder()}>
                {enviando ? "Guardando…" : pregunta.n === "activar" ? "Activar" : "Desactivar"}
              </button>
            )}
            <button className="btn sec" type="button" disabled={enviando} onClick={cerrar}>
              {pregunta.n === "activar" && planSiguiente === null ? "Entendido" : "Cancelar"}
            </button>
          </div>
        </Modal>
      )}

      {paywall && fichaPaywall && (
        <Paywall
          me={me}
          ficha={fichaPaywall}
          motivo={paywall.motivo}
          onCerrar={() => setPaywall(null)}
          onSinSesion={onSinSesion}
          onActivada={(nuevo) => {
            setPaywall(null);
            onCambio(nuevo);
          }}
        />
      )}
    </section>
  );
}
