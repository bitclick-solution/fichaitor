import { useEffect, useId, useRef, useState } from "react";
import { api, ApiError, type Location, type Me, type PerfilFiscal, type PlanPago } from "./api";
import { Modal } from "./Ajustes";
import { urlDePago } from "./cobro";

// Paywall: «Pásate a Pro». Contrato: docs/admin-facturacion.md, 4.7.
//  - Compara Pro y Pro+ (en la API, `pro_whatsapp`) con los precios de la API: nunca escritos aquí.
//  - Beta (`pricing.charging: false`): sin Stripe. La ficha se activa en Pro con una plaza de la beta.
//  - Con cobro: datos fiscales si faltan, plaza nueva (POST /me/billing/seats), sondeo de la petición y
//    Stripe Checkout. Al volver, App sondea /me hasta ver la ficha en su plan nuevo.
// Se puede cerrar siempre: cerrar no cambia nada.

export type MotivoPaywall = "ai_replies" | "key_dates" | "no_seat" | "mejorar";

/**
 * La ficha cuyo pago acaba de volver de Stripe y aún se está activando. Mientras tanto, el paywall de esa
 * ficha no deja pagar otra vez (un webhook lento no debe acabar en dos plazas). Lo fija App.
 */
let fichaPagando: string | null = null;
export function marcarPagoEnCurso(ficha: string | null): void {
  fichaPagando = ficha;
}

/** Nombre visible de cada plan de pago. En la API, Pro+ es `pro_whatsapp`. */
export const NOMBRE_PAGO: Record<PlanPago, string> = { pro: "Pro", pro_whatsapp: "Pro+" };

const INCLUYE: Record<PlanPago, string[]> = {
  pro: ["Respuestas con IA ilimitadas", "Festivos y fechas clave", "Modo automático, si lo activas"],
  pro_whatsapp: ["Todo lo del plan Pro", "Por WhatsApp", "80 mensajes de Fichaitor al mes"],
};

const INTRO: Record<MotivoPaywall, string> = {
  ai_replies: "Ya has usado las respuestas con IA gratuitas de esta ficha. Con Pro no tienen límite.",
  key_dates: "Los festivos y las fechas clave están en Pro: te preguntamos si abres y cambiamos el horario en Google cuando lo confirmas.",
  no_seat: "Para activar esta ficha necesitas una plaza de pago.",
  mejorar: "Pasa esta ficha a un plan de pago.",
};

function euros(me: Me, centimos: number): string {
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: me.pricing?.currency ?? "EUR",
    minimumFractionDigits: centimos % 100 === 0 ? 0 : 2,
  }).format(centimos / 100);
}

// Países de la UE para el NIF-IVA. España incluye Canarias, Ceuta y Melilla.
const PAISES: [string, string][] = [
  ["ES", "España"],
  ["DE", "Alemania"],
  ["AT", "Austria"],
  ["BE", "Bélgica"],
  ["BG", "Bulgaria"],
  ["CY", "Chipre"],
  ["HR", "Croacia"],
  ["DK", "Dinamarca"],
  ["SK", "Eslovaquia"],
  ["SI", "Eslovenia"],
  ["EE", "Estonia"],
  ["FI", "Finlandia"],
  ["FR", "Francia"],
  ["GR", "Grecia"],
  ["HU", "Hungría"],
  ["IE", "Irlanda"],
  ["IT", "Italia"],
  ["LV", "Letonia"],
  ["LT", "Lituania"],
  ["LU", "Luxemburgo"],
  ["MT", "Malta"],
  ["NL", "Países Bajos"],
  ["PL", "Polonia"],
  ["PT", "Portugal"],
  ["CZ", "República Checa"],
  ["RO", "Rumanía"],
  ["SE", "Suecia"],
];

type Paso = { n: "elegir" } | { n: "datos" } | { n: "pagando" } | { n: "activando" } | { n: "error"; mensaje: string };

export function Paywall({
  me,
  ficha,
  motivo,
  onCerrar,
  onActivada,
  onSinSesion,
}: {
  me: Me;
  ficha: Location;
  motivo: MotivoPaywall;
  onCerrar: () => void;
  /** Beta: la ficha ya está en Pro. Llega el cuerpo de GET /me actualizado. */
  onActivada: (me: Me) => void;
  onSinSesion: () => void;
}) {
  const id = useId();
  const cobro = me.pricing?.charging === true;
  const libres = me.plans?.available ?? { pro: 0, pro_whatsapp: 0 };
  // En la beta solo hay plazas Pro; con cobro, los dos planes se pueden comprar.
  const disponible = (p: PlanPago) => cobro || libres[p] > 0;
  const [plan, setPlan] = useState<PlanPago>(disponible("pro") || !disponible("pro_whatsapp") ? "pro" : "pro_whatsapp");
  const [paso, setPaso] = useState<Paso>({ n: "elegir" });
  // Mientras se guardan los datos fiscales, el modal no se cierra: después sale la petición de pago.
  const [guardandoDatos, setGuardandoDatos] = useState(false);
  // La petición de pago que ya se pidió: si tarda y se reintenta, se sigue esa misma, sin crear otra plaza.
  const peticion = useRef<string | null>(null);
  const yaPagando = fichaPagando === ficha.id;
  const vivo = useRef(true);
  // Al cambiar de paso, el botón pulsado desaparece: el foco pasa al contenido nuevo para no salirse del modal.
  const contenido = useRef<HTMLDivElement>(null);
  const primerPaso = useRef(true);
  useEffect(() => {
    if (primerPaso.current) {
      primerPaso.current = false;
      return;
    }
    if (paso.n === "elegir" || paso.n === "datos") contenido.current?.focus();
  }, [paso.n]);
  useEffect(() => {
    // En desarrollo React monta dos veces: hay que volver a marcarlo como vivo en el segundo montaje.
    vivo.current = true;
    return () => {
      vivo.current = false;
    };
  }, []);

  const precio = (p: PlanPago): number | null => me.billing?.new_seat_price?.[p] ?? me.pricing?.plans[p]?.first ?? null;
  // El 5 % se anuncia solo si el precio de hoy es exactamente el de «segunda ficha y siguientes».
  const conDescuento = (p: PlanPago) => {
    const hoy = precio(p);
    const plan = me.pricing?.plans[p];
    return hoy !== null && plan !== undefined && plan.additional < plan.first && hoy === plan.additional;
  };

  // ── Beta: activar en Pro con la plaza de la beta ──
  const activarBeta = async () => {
    setPaso({ n: "activando" });
    try {
      onActivada(await api.activarFicha(ficha.id, plan));
    } catch (e) {
      if (!vivo.current) return;
      if (e instanceof ApiError && e.status === 401) {
        setPaso({ n: "elegir" });
        onSinSesion();
      } else if (e instanceof ApiError && e.status === 402)
        setPaso({ n: "error", mensaje: "Ya usas todas las plazas Pro de la beta. Desactiva otra ficha Pro en «Tus negocios» para usar su plaza." });
      else setPaso({ n: "error", mensaje: e instanceof Error ? e.message : "No hemos podido activarla. Prueba otra vez." });
    }
  };

  // ── Con cobro: plaza nueva, sondeo de la petición y Stripe Checkout ──
  // Plaza nueva o, si ya hay una petición pendiente para esta ficha (409 `seat_pending`), esa misma.
  const pedirOPendiente = async (): Promise<string> => {
    try {
      return (await api.pedirPlaza(plan, ficha.id)).request_id;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.codigo === "seat_pending" && e.peticion) return e.peticion;
      throw e;
    }
  };

  const pagar = async () => {
    if (!vivo.current || yaPagando) return;
    setPaso({ n: "pagando" });
    try {
      const request_id = peticion.current ?? (await pedirOPendiente());
      peticion.current = request_id;
      for (let i = 0; i < 20; i++) {
        await new Promise((ok) => setTimeout(ok, 2000));
        if (!vivo.current) return;
        const p = await api.peticionCobro(request_id);
        if (!vivo.current) return;
        if (p.state === "lista" && p.url) {
          // La URL la da el servidor (Stripe Checkout). Solo se abre si es de Stripe.
          const url = urlDePago(p.url);
          if (!url) {
            peticion.current = null;
            setPaso({ n: "error", mensaje: "No hemos podido abrir la página de pago. Escríbenos a soporte@fichaitor.es." });
            return;
          }
          location.assign(url);
          return;
        }
        if (p.state === "fallida") {
          peticion.current = null;
          setPaso({ n: "error", mensaje: p.detail ?? "No hemos podido preparar el pago. Prueba otra vez." });
          return;
        }
      }
      if (vivo.current) setPaso({ n: "error", mensaje: "Preparar el pago está tardando más de lo normal. Prueba otra vez en un minuto." });
    } catch (e) {
      if (!vivo.current) return;
      peticion.current = null;
      if (e instanceof ApiError && e.status === 401) {
        setPaso({ n: "elegir" });
        onSinSesion();
      } else if (e instanceof ApiError && e.codigo === "profile_incomplete") setPaso({ n: "datos" });
      // Mientras no se cobra, el servidor no vende plazas: la ficha pasa a Pro con la plaza de la beta.
      else if (e instanceof ApiError && e.codigo === "beta_no_charge") void activarBeta();
      else setPaso({ n: "error", mensaje: e instanceof Error ? e.message : "No hemos podido preparar el pago. Prueba otra vez." });
    }
  };

  const continuar = () => {
    if (!cobro) void activarBeta();
    else if (me.billing?.profile_complete) void pagar();
    else setPaso({ n: "datos" });
  };

  const titulo = paso.n === "datos" ? "Datos para la factura" : "Pásate a Pro";
  const ocupado = paso.n === "pagando" || paso.n === "activando";

  return (
    <Modal titulo={titulo} onCerrar={onCerrar} bloqueado={ocupado || guardandoDatos}>
      <div className="paso-modal" ref={contenido} tabIndex={-1}>
        {(paso.n === "elegir" || paso.n === "error" || ocupado) && (
          <>
            <p>
              {INTRO[motivo]} Ficha: <strong>{ficha.title}</strong>.
            </p>
            <fieldset className="fecha-opciones">
              <legend className="sr">Elige el plan</legend>
              <div className="opciones opciones-dos planes-pago">
                {(["pro", "pro_whatsapp"] as const).map((p) => {
                  const euro = precio(p);
                  return (
                    <label className="opcion" key={p}>
                      <input type="radio" name={`${id}-plan`} checked={plan === p} disabled={!disponible(p) || ocupado} onChange={() => setPlan(p)} />
                      <span>
                        {NOMBRE_PAGO[p]}
                        {cobro ? (
                          euro !== null && (
                            <small className="plan-precio">
                              {euros(me, euro)} + IVA al año{conDescuento(p) && " · 5 % menos por ser tu segunda ficha de pago o siguientes"}
                            </small>
                          )
                        ) : disponible(p) ? (
                          <small className="plan-precio">Gratis durante la beta{euro !== null && `. Después, ${euros(me, euro)} + IVA al año`}</small>
                        ) : (
                          <small className="plan-precio">Sin plazas en la beta</small>
                        )}
                        <ul className="plan-incluye">
                          {INCLUYE[p].map((x) => (
                            <li key={x}>{x}</li>
                          ))}
                        </ul>
                      </span>
                    </label>
                  );
                })}
              </div>
            </fieldset>

            {cobro ? (
              precio(plan) !== null && (
                <p className="sec">
                  Esta ficha necesita una plaza {NOMBRE_PAGO[plan]}: {euros(me, precio(plan)!)} + IVA por un año desde hoy. Se renueva sola salvo que la canceles. No se
                  devuelven importes. Pagas con tarjeta en Stripe; el periodo exacto va en la factura.
                </p>
              )
            ) : !disponible("pro") ? (
              <p className="aviso">Ya usas todas las plazas Pro de la beta. Desactiva otra ficha Pro en «Tus negocios» para usar su plaza.</p>
            ) : (
              <p className="sec">Durante la beta no cobramos. Cuando termine, te avisaremos con 30 días de antelación para que elijas.</p>
            )}

            {yaPagando && (
              <p className="aviso" role="status">
                Acabamos de recibir el pago de esta ficha y la estamos activando. No hace falta volver a pagar.
              </p>
            )}
            {paso.n === "error" && (
              <p className="aviso" role="alert">
                {paso.mensaje}
              </p>
            )}

            <div className="acciones">
              <button className="btn" type="button" disabled={ocupado || yaPagando || !disponible(plan)} onClick={continuar}>
                {paso.n === "pagando"
                  ? "Preparando el pago…"
                  : paso.n === "activando"
                    ? "Activando…"
                    : cobro
                      ? "Continuar al pago"
                      : `Activar ${NOMBRE_PAGO[plan]} (beta)`}
              </button>
              <button className="btn sec" type="button" disabled={ocupado} onClick={onCerrar}>
                Ahora no
              </button>
            </div>
          </>
        )}

        {paso.n === "datos" && (
          <DatosFiscales
            email={me.email}
            onVolver={() => setPaso({ n: "elegir" })}
            onSinSesion={onSinSesion}
            onGuardando={setGuardandoDatos}
            onGuardado={() => void pagar()}
          />
        )}
      </div>
    </Modal>
  );
}

type Campo = "legal_name" | "tax_id" | "line1" | "postal_code" | "city" | "country" | "invoice_email";

/** Datos fiscales que se piden antes del primer pago (GET y PUT /me/billing/profile). */
export function DatosFiscales({
  email,
  onVolver,
  onGuardado,
  onGuardando,
  onSinSesion,
  etiqueta = "Guardar y pagar",
  intro = "Los necesitamos para hacerte la factura. Solo los pedimos una vez.",
}: {
  /** Texto del botón: «Guardar y pagar» en el paywall, «Guardar» en Facturación. */
  etiqueta?: string;
  intro?: string;
  email: string;
  onVolver: () => void;
  onGuardado: () => void;
  /** Mientras se guardan, el modal no se puede cerrar. */
  onGuardando: (si: boolean) => void;
  onSinSesion: () => void;
}) {
  const id = useId();
  const [datos, setDatos] = useState({
    legal_name: "",
    tax_id: "",
    line1: "",
    line2: "",
    postal_code: "",
    city: "",
    province: "",
    country: "ES",
    invoice_email: email,
  });
  const [cargando, setCargando] = useState(true);
  const [guardando, setGuardando] = useState(false);
  const [error, setError] = useState<{ campo: Campo | null; mensaje: string } | null>(null);

  // Si ya hay algo guardado, se parte de ello.
  useEffect(() => {
    let vivo = true;
    api
      .perfilFiscal()
      .then((p: PerfilFiscal) => {
        if (!vivo) return;
        setDatos((d) => ({
          legal_name: p.legal_name ?? d.legal_name,
          tax_id: p.tax_id ?? d.tax_id,
          line1: p.address?.line1 ?? d.line1,
          line2: p.address?.line2 ?? d.line2,
          postal_code: p.address?.postal_code ?? d.postal_code,
          city: p.address?.city ?? d.city,
          province: p.address?.province ?? d.province,
          country: p.address?.country ?? d.country,
          invoice_email: p.invoice_email ?? d.invoice_email,
        }));
      })
      .catch((e: unknown) => {
        if (vivo && e instanceof ApiError && e.status === 401) onSinSesion();
      })
      .finally(() => {
        if (vivo) setCargando(false);
      });
    return () => {
      vivo = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const poner = (k: keyof typeof datos) => (e: { target: { value: string } }) => setDatos((d) => ({ ...d, [k]: e.target.value }));
  const OBLIGATORIOS = ["legal_name", "tax_id", "line1", "postal_code", "city", "invoice_email"] as const;
  const enfocar = (k: string) => setTimeout(() => document.getElementById(`${id}-${k}`)?.focus(), 0);

  const guardar = async () => {
    const vacio = OBLIGATORIOS.find((k) => datos[k].trim() === "");
    if (vacio) {
      setError({ campo: vacio, mensaje: "Rellena los campos que faltan." });
      enfocar(vacio);
      return;
    }
    setGuardando(true);
    onGuardando(true);
    setError(null);
    try {
      await api.guardarPerfilFiscal({
        legal_name: datos.legal_name.trim(),
        tax_id: datos.tax_id.replace(/[\s.-]/g, "").toUpperCase(),
        address: {
          line1: datos.line1.trim(),
          line2: datos.line2.trim() || null,
          postal_code: datos.postal_code.trim(),
          city: datos.city.trim(),
          province: datos.province.trim() || null,
          country: datos.country,
        },
        invoice_email: datos.invoice_email.trim(),
      });
      onGuardando(false);
      onGuardado();
    } catch (e) {
      onGuardando(false);
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      else {
        const campo: Campo | null =
          e instanceof ApiError
            ? e.codigo === "tax_id_invalid"
              ? "tax_id"
              : e.codigo === "country_not_supported"
                ? "country"
                : e.codigo === "postal_code_invalid"
                  ? "postal_code"
                  : null
            : null;
        setError({ campo, mensaje: e instanceof Error ? e.message : "No hemos podido guardar los datos." });
        if (campo) enfocar(campo);
      }
      setGuardando(false);
    }
  };

  if (cargando)
    return (
      <p className="cargando" role="status">
        <span className="ojo" aria-hidden="true" />
        Abriendo tus datos…
      </p>
    );

  const campo = (k: Campo | "line2" | "province", etiqueta: string, extra: { type?: string; autoComplete?: string; opcional?: boolean } = {}) => {
    const conError = error?.campo === k;
    return (
      <label className="campo-fiscal" htmlFor={`${id}-${k}`}>
        <span>
          {etiqueta}
          {extra.opcional && <small> (opcional)</small>}
        </span>
        <input
          id={`${id}-${k}`}
          type={extra.type ?? "text"}
          autoComplete={extra.autoComplete}
          value={datos[k]}
          onChange={poner(k)}
          disabled={guardando}
          required={!extra.opcional}
          aria-invalid={conError}
          aria-describedby={conError ? `${id}-error` : undefined}
        />
      </label>
    );
  };

  return (
    <form
      noValidate
      className="datos-fiscales"
      onSubmit={(e) => {
        e.preventDefault();
        void guardar();
      }}
    >
      <p>{intro}</p>
      {campo("legal_name", "Razón social o nombre", { autoComplete: "organization" })}
      {campo("tax_id", "NIF (o NIF-IVA si no estás en España)")}
      {campo("line1", "Dirección", { autoComplete: "address-line1" })}
      {campo("line2", "Piso, puerta…", { autoComplete: "address-line2", opcional: true })}
      <div className="fila-fiscal">
        {campo("postal_code", "Código postal", { autoComplete: "postal-code" })}
        {campo("city", "Población", { autoComplete: "address-level2" })}
      </div>
      <div className="fila-fiscal">
        {campo("province", "Provincia", { autoComplete: "address-level1", opcional: true })}
        <label className="campo-fiscal" htmlFor={`${id}-country`}>
          <span>País</span>
          <select
            id={`${id}-country`}
            value={datos.country}
            onChange={poner("country")}
            disabled={guardando}
            aria-invalid={error?.campo === "country"}
            aria-describedby={error?.campo === "country" ? `${id}-error` : undefined}
          >
            {PAISES.map(([c, n]) => (
              <option key={c} value={c}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>
      {campo("invoice_email", "Correo para las facturas", { type: "email", autoComplete: "email" })}
      {error && (
        <p className="aviso" role="alert" id={`${id}-error`}>
          {error.mensaje}
        </p>
      )}
      <div className="acciones">
        <button className="btn" type="submit" disabled={guardando}>
          {guardando ? "Guardando…" : etiqueta}
        </button>
        <button className="btn sec" type="button" disabled={guardando} onClick={onVolver}>
          Volver
        </button>
      </div>
    </form>
  );
}
