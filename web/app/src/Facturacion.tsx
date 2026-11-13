import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Factura, type Me, type PerfilFiscal, type Plaza } from "./api";
import { Ajustes, Modal } from "./Ajustes";
import { esperarPeticion, euros, TARDA, urlDePago } from "./cobro";
import { Seccion } from "./movimiento";
import { DatosFiscales, NOMBRE_PAGO } from "./Paywall";

// «Facturación» (docs/contrato-web-api.md, «Cobro y paywall»): las plazas pagadas con su renovación,
// cancelar o reanudar, cambiar la tarjeta, los datos fiscales y las facturas de Odoo.
// Precios e importes salen de la API; aquí no se calcula ninguno.

const fechaLarga = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });

/** «14 de marzo de 2028» a partir de «2028-03-14» (o de una fecha y hora ISO). */
function fecha(iso: string): string {
  if (!iso) return "";
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : fechaLarga.format(d);
}

function estadoPlaza(p: Plaza, moneda: string): string {
  const fin = p.renews_at ? ` el ${fecha(p.renews_at)}` : "";
  if (p.status === "payment_failed" || p.status === "action_required" || p.status === "disputed") return "Tiene un pago pendiente.";
  // Contratada en la ventana de la beta: aún no se ha cobrado nada.
  if (p.trial_ends_at) {
    if (p.cancel_at_period_end || p.status === "canceled") return `Cancelada: no se cobrará nada. Sigue hasta el ${fecha(p.trial_ends_at)}.`;
    return `Primer cobro el ${fecha(p.trial_ends_at)}${p.next_price !== null ? `: ${euros(p.next_price, moneda)} + IVA` : ""}. Puedes cancelarla antes sin coste.`;
  }
  if (p.status === "canceled" || p.cancel_at_period_end) return `No se renueva: termina${fin}.`;
  // Sin ficha (o sin precio de renovación) no se renueva ni se cobra; se puede asignar una ficha antes.
  if (p.location_id === null || p.next_price === null) return `Sin ficha asignada: no se renueva ni se cobra. Termina${fin}, salvo que actives una ficha en ella antes.`;
  return `Se renueva${fin} por ${euros(p.next_price, moneda)} + IVA.`;
}

type Accion = { n: "nada" } | { n: "enviando"; que: string } | { n: "error"; mensaje: string };

export function Facturacion({
  me,
  onRecargar,
  onSinSesion,
  sinTarjeta = false,
}: {
  me: Me;
  /** En la pantalla de pausa, «Cambiar tarjeta» ya está arriba: aquí no se repite. */
  sinTarjeta?: boolean;
  /** Vuelve a leer la cuenta sin quitar la pantalla (sin pasar por «cargando»). */
  onRecargar: () => Promise<unknown> | void;
  onSinSesion: () => void;
}) {
  const cobro = me.billing;
  const moneda = me.pricing?.currency ?? "EUR";
  const plazas = cobro?.seats ?? [];
  const [accion, setAccion] = useState<Accion>({ n: "nada" });
  const [cancelando, setCancelando] = useState<Plaza | null>(null);
  const [perfil, setPerfil] = useState<PerfilFiscal | null>(null);
  const [editando, setEditando] = useState(false);
  const [guardandoDatos, setGuardandoDatos] = useState(false);
  // Confirmación de lo último que se ha hecho (cancelar, reanudar, guardar datos).
  const [hecho, setHecho] = useState<string | null>(null);
  const [facturas, setFacturas] = useState<Factura[] | null>(null);
  const vivo = useRef(true);
  useEffect(() => {
    vivo.current = true;
    return () => {
      vivo.current = false;
    };
  }, []);

  const visible = !!cobro && (me.pricing?.charging === true || (cobro.seats ?? []).length > 0);
  useEffect(() => {
    // En la beta, sin plazas, la sección no se enseña: no se pide nada.
    if (!cobro || !visible) return;
    api
      .facturas()
      .then((f) => vivo.current && setFacturas(f))
      .catch((e: unknown) => {
        if (!vivo.current) return;
        if (e instanceof ApiError && e.status === 401) onSinSesion();
        else setFacturas([]);
      });
    if (cobro.profile_complete)
      api
        .perfilFiscal()
        .then((p) => vivo.current && setPerfil(p))
        .catch(() => {
          /* el resumen de datos no se enseña */
        });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cobro?.profile_complete, visible]);

  if (!cobro || !visible) return null;

  const nombreFicha = (id: string | null) => (id === null ? null : (me.locations.find((l) => l.id === id)?.title ?? null));
  const ocupado = accion.n === "enviando";

  /** Pide algo al proceso de cobro y espera: con `url`, va a Stripe; sin ella, recarga la cuenta. */
  const lanzar = async (que: string, pedir: () => Promise<{ request_id: string }>, confirmacion?: string) => {
    setAccion({ n: "enviando", que });
    setHecho(null);
    try {
      const { request_id } = await pedir();
      const p = await esperarPeticion(request_id, () => vivo.current);
      if (!vivo.current) return;
      if (!p) return setAccion({ n: "error", mensaje: TARDA });
      if (p.state === "fallida") return setAccion({ n: "error", mensaje: p.detail ?? "No hemos podido hacerlo. Prueba otra vez." });
      if (p.url) {
        const url = urlDePago(p.url);
        if (!url) return setAccion({ n: "error", mensaje: "No hemos podido abrir la página de Stripe. Escríbenos a soporte@fichaitor.es." });
        location.assign(url);
        return;
      }
      setAccion({ n: "nada" });
      setCancelando(null);
      await onRecargar();
      if (vivo.current && confirmacion) setHecho(confirmacion);
    } catch (e) {
      if (!vivo.current) return;
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      else setAccion({ n: "error", mensaje: e instanceof Error ? e.message : "No hemos podido hacerlo. Prueba otra vez." });
    }
  };

  return (
    <Seccion id="facturacion" className="facturacion">
      <h2 className="rotulo">Facturación</h2>
      <p className="sec">Pagas con tarjeta, por años completos desde que activas cada ficha. Cada plaza se renueva sola en su fecha salvo que la canceles.</p>

      {/* Con el modal de cancelar abierto, el error sale solo dentro del modal. */}
      {accion.n === "error" && !cancelando && (
        <p className="aviso" role="alert">
          {accion.mensaje}
        </p>
      )}
      {hecho && (
        <p className="hecho" role="status">
          {hecho}
        </p>
      )}

      {plazas.length > 0 && (
        <ul className="plazas">
          {plazas.map((p) => {
            const ficha = nombreFicha(p.location_id);
            const puedeCancelar = !p.cancel_at_period_end && p.status === "active" && p.location_id !== null && p.next_price !== null;
            const puedeReanudar = p.cancel_at_period_end && p.status !== "canceled";
            return (
              <li key={p.id}>
                <div>
                  <p className="rotulo">
                    {NOMBRE_PAGO[p.plan]} · {ficha ?? "Sin ficha asignada"}
                  </p>
                  <p className="sec">{estadoPlaza(p, moneda)}</p>
                </div>
                <div className="acciones">
                  {puedeCancelar && (
                    <button className="enlace" type="button" disabled={ocupado} onClick={() => setCancelando(p)}>
                      {p.trial_ends_at ? "Cancelar la plaza" : "Cancelar la renovación"}
                    </button>
                  )}
                  {puedeReanudar && (
                    <button
                      className="btn sec"
                      type="button"
                      disabled={ocupado}
                      onClick={() => void lanzar(`reanudar-${p.id}`, () => api.reanudarPlaza(p.id), "Hecho: la plaza vuelve a renovarse en su fecha.")}
                    >
                      {accion.n === "enviando" && accion.que === `reanudar-${p.id}` ? "Reanudando…" : "Reanudar la renovación"}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <div className="ajustes-lista">
        {plazas.length > 0 && !sinTarjeta && (
          <div>
            <h3 className="rotulo">Tarjeta</h3>
            <p className="sec">La cambias en la página segura de Stripe. Fichaitor no ve los datos de tu tarjeta.</p>
            <button className="btn sec" type="button" disabled={ocupado} onClick={() => void lanzar("portal", api.portalCobro)}>
              {accion.n === "enviando" && accion.que === "portal" ? "Abriendo Stripe…" : "Cambiar tarjeta"}
            </button>
          </div>
        )}
        <div>
          <h3 className="rotulo">Datos de facturación</h3>
          {!cobro.profile_complete ? (
            <p className="sec">Te los pedimos al pagar por primera vez.</p>
          ) : perfil ? (
            <>
              <p className="sec datos-guardados">
                {perfil.legal_name}
                <br />
                {perfil.tax_id}
                {perfil.address && (
                  <>
                    <br />
                    {perfil.address.line1}
                    {perfil.address.line2 ? `, ${perfil.address.line2}` : ""}
                    <br />
                    {perfil.address.postal_code} {perfil.address.city}
                  </>
                )}
                <br />
                {perfil.invoice_email}
              </p>
              <button className="btn sec" type="button" disabled={ocupado} onClick={() => setEditando(true)}>
                Cambiar mis datos
              </button>
            </>
          ) : (
            <p className="sec">Abriendo tus datos…</p>
          )}
        </div>
      </div>

      <h3 className="rotulo">Facturas</h3>
      {facturas === null ? (
        <p className="sec">Abriendo tus facturas…</p>
      ) : facturas.length === 0 ? (
        <p className="sec">Todavía no hay facturas. Con cada cobro te llega también una por correo.</p>
      ) : (
        <ul className="facturas">
          {facturas.map((f) => (
            <li key={f.id}>
              <span>
                <strong>{f.number}</strong>
                {f.kind === "rectificativa" && " · Rectificativa"} · {fecha(f.issued_at)}
              </span>
              <span className="factura-total">{euros(f.amount_total, moneda)}</span>
              <a className="enlace" href={api.pdfFactura(f.id)} download>
                Descargar PDF
              </a>
            </li>
          ))}
        </ul>
      )}

      {cancelando && (
        <Modal
          titulo={cancelando.trial_ends_at ? "Cancelar la plaza" : "Cancelar la renovación"}
          onCerrar={() => {
            setCancelando(null);
            setAccion({ n: "nada" });
          }}
          bloqueado={ocupado}
        >
          <p>
            <strong>
              {NOMBRE_PAGO[cancelando.plan]} · {nombreFicha(cancelando.location_id) ?? "Sin ficha asignada"}
            </strong>
          </p>
          {cancelando.trial_ends_at ? (
            <p>Todavía no se ha cobrado nada y no se cobrará. Sigue como hasta ahora hasta el {fecha(cancelando.trial_ends_at)}. Puedes reanudarla antes de esa fecha.</p>
          ) : (
            <p>
              Sigue como hasta ahora{cancelando.renews_at ? ` hasta el ${fecha(cancelando.renews_at)}` : " hasta el final del año pagado"}. Después no se renovará ni se
              cobrará. Lo ya pagado no se devuelve, y puedes reanudar la renovación antes de esa fecha.
            </p>
          )}
          {accion.n === "error" && (
            <p className="aviso" role="alert">
              {accion.mensaje}
            </p>
          )}
          <div className="acciones">
            <button
              className="btn"
              type="button"
              disabled={ocupado}
              onClick={() =>
                void lanzar(`cancelar-${cancelando.id}`, () => api.cancelarPlaza(cancelando.id), "Hecho: la plaza no se renovará. Puedes reanudarla antes de su fecha.")
              }
            >
              {ocupado ? "Cancelando…" : cancelando.trial_ends_at ? "Cancelar la plaza" : "Cancelar la renovación"}
            </button>
            <button
              className="btn sec"
              type="button"
              disabled={ocupado}
              onClick={() => {
                setCancelando(null);
                setAccion({ n: "nada" });
              }}
            >
              Volver
            </button>
          </div>
        </Modal>
      )}

      {editando && (
        <Modal titulo="Datos de facturación" onCerrar={() => setEditando(false)} bloqueado={guardandoDatos}>
          <DatosFiscales
            email={me.email}
            etiqueta="Guardar"
            intro="Se usan en las próximas facturas. Las ya emitidas no cambian."
            onGuardando={setGuardandoDatos}
            onSinSesion={onSinSesion}
            onVolver={() => setEditando(false)}
            onGuardado={() => {
              setEditando(false);
              setHecho("Hecho: tus datos de facturación están guardados. Se usan desde la próxima factura.");
              api
                .perfilFiscal()
                .then((p) => vivo.current && setPerfil(p))
                .catch(() => undefined);
            }}
          />
        </Modal>
      )}
    </Seccion>
  );
}

// Detalle del motivo, debajo de la frase común «Tu cuenta está en pausa porque no hemos podido cobrar.»
const MOTIVO: Record<string, string> = {
  payment_failed: "La tarjeta ha sido rechazada.",
  action_required: "Tu banco pide que confirmes el pago y todavía no se ha confirmado.",
  dispute: "Se ha abierto una reclamación sobre un pago de tu cuenta.",
  manual_unpaid: "Hay una factura sin pagar.",
};

/**
 * Pantalla única con la cuenta en pausa por un impago (`billing.blocked`): el motivo, pagar, cambiar la
 * tarjeta, darse de baja y cerrar sesión. Las rutas de datos responden 402 `account_blocked` mientras dure.
 */
export function CuentaEnPausa({
  me,
  onComprobar,
  onTusNegocios,
  onSalir,
  onSinSesion,
  onDespedida,
}: {
  me: Me;
  /** Vuelve a leer la cuenta. Devuelve si sigue en pausa (si ya se ha pagado, App abre el panel). */
  onComprobar: () => Promise<boolean>;
  /** «Tus negocios», para desactivar fichas: funciona con la cuenta en pausa. */
  onTusNegocios: () => void;
  onSalir: () => void;
  onSinSesion: () => void;
  onDespedida: (borrado: string) => void;
}) {
  const [enviando, setEnviando] = useState<"pagar" | "portal" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comprobando, setComprobando] = useState(false);
  const [sigue, setSigue] = useState(false);
  const vivo = useRef(true);
  useEffect(() => {
    vivo.current = true;
    return () => {
      vivo.current = false;
    };
  }, []);
  const bloqueo = me.billing?.blocked;

  const comprobar = async () => {
    setComprobando(true);
    setSigue(false);
    const enPausa = await onComprobar().catch(() => true);
    if (!vivo.current) return;
    setComprobando(false);
    setSigue(enPausa);
  };

  const abrir = async (que: "pagar" | "portal") => {
    setEnviando(que);
    setError(null);
    try {
      const { request_id } = await (que === "pagar" ? api.pagarPendiente() : api.portalCobro());
      const p = await esperarPeticion(request_id, () => vivo.current);
      if (!vivo.current) return;
      if (!p) setError(TARDA);
      else if (p.state === "fallida") setError(p.detail ?? "No hemos podido abrir Stripe. Prueba otra vez.");
      else if (p.url && urlDePago(p.url)) {
        location.assign(urlDePago(p.url)!);
        return;
      } else setError("No hemos podido abrir la página de Stripe. Escríbenos a soporte@fichaitor.es.");
    } catch (e) {
      if (!vivo.current) return;
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      else setError(e instanceof Error ? e.message : "No hemos podido abrir Stripe. Prueba otra vez.");
    }
    setEnviando(null);
  };

  return (
    <section className="pantalla en-pausa">
      <h1 className="cartel">
        Cuenta en pausa<span className="pt">.</span>
      </h1>
      {/* La misma frase que el bot, la API y el correo. */}
      <p className="lead">Tu cuenta está en pausa porque no hemos podido cobrar. {bloqueo && MOTIVO[bloqueo.reason]}</p>
      <p>
        Hasta que se pague, Fichaitor no lee ninguna de tus fichas, tampoco la del plan Gratis, no te envía avisos ni aplica cambios. No se ha borrado nada y la pausa no
        borra nada: en cuanto se pague, todo vuelve a funcionar.
      </p>
      {error && (
        <p className="aviso" role="alert">
          {error}
        </p>
      )}
      <div className="acciones">
        <button className="btn" type="button" disabled={enviando !== null} onClick={() => void abrir("pagar")}>
          {enviando === "pagar" ? "Abriendo el pago…" : "Pagar ahora"}
        </button>
        <button className="btn sec" type="button" disabled={enviando !== null} onClick={() => void abrir("portal")}>
          {enviando === "portal" ? "Abriendo Stripe…" : "Cambiar tarjeta"}
        </button>
      </div>
      <p className="sec">
        ¿Ya has pagado? Stripe nos avisa en unos segundos.{" "}
        <button className="enlace" type="button" disabled={comprobando} onClick={() => void comprobar()}>
          {comprobando ? "Comprobando…" : "Comprobar ahora"}
        </button>
      </p>
      {sigue && (
        <p className="sec" role="status">
          Todavía no nos ha llegado el pago. Si acabas de pagar, espera unos segundos y vuelve a comprobarlo.
        </p>
      )}
      <Facturacion me={me} onRecargar={onComprobar} onSinSesion={onSinSesion} sinTarjeta />
      <div className="acciones">
        <button className="btn sec" type="button" onClick={onTusNegocios}>
          Tus negocios: desactivar fichas
        </button>
      </div>
      <Ajustes
        email={me.email}
        sinRecorrido
        variasFichas={me.locations.length > 1}
        onSinSesion={onSinSesion}
        onCambio={() => void onComprobar()}
        onDespedida={onDespedida}
      />
      <div className="acciones">
        <button className="btn sec" type="button" onClick={onSalir}>
          Cerrar sesión
        </button>
      </div>
    </section>
  );
}
