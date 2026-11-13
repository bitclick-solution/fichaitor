import { useEffect, useId, useRef, useState } from "react";
import { api, ApiError, type Location, type Me, type PaisFacturacion, type PerfilFiscal, type PlanPago, type RegionFiscal } from "./api";
import { Modal } from "./Ajustes";
import { finDePrueba, urlDePago } from "./cobro";

// Paywall: «Pásate a Pro». Contrato: docs/admin-facturacion.md, 4.7.
//  - Compara Pro y Pro+ (en la API, `pro_whatsapp`) con los precios de la API: nunca escritos aquí.
//  - Beta (`pricing.charging: false`): sin Stripe. La ficha se activa en Pro con una plaza de la beta.
//  - Con cobro: datos fiscales si faltan, plaza nueva (POST /me/billing/seats), sondeo de la petición y
//    Stripe Checkout. Al volver, App sondea /me hasta ver la ficha en su plan nuevo.
// Se puede cerrar siempre: cerrar no cambia nada.

/** Por qué se abre. Con «whatsapp» solo se ofrece Pro+, el único plan con WhatsApp. */
export type MotivoPaywall = "ai_replies" | "key_dates" | "no_seat" | "mejorar" | "whatsapp";

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
  pro: ["Respuestas con IA ilimitadas", "Festivos y fechas clave"],
  pro_whatsapp: ["Todo lo del plan Pro", "Por WhatsApp", "80 mensajes de Fichaitor al mes"],
};

const INTRO: Record<MotivoPaywall, string> = {
  ai_replies: "Ya has usado las respuestas con IA gratuitas de esta ficha. Con Pro no tienen límite.",
  key_dates: "Los festivos y las fechas clave están en Pro: te preguntamos si abres y cambiamos el horario en Google cuando lo confirmas.",
  no_seat: "Para activar esta ficha necesitas una plaza de pago.",
  mejorar: "Pasa esta ficha a un plan de pago.",
  whatsapp: "WhatsApp está en el plan Pro+: Fichaitor te escribe al WhatsApp de tu negocio y le respondes desde ahí.",
};

function euros(me: Me, centimos: number): string {
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: me.pricing?.currency ?? "EUR",
    minimumFractionDigits: centimos % 100 === 0 ? 0 : 2,
  }).format(centimos / 100);
}

// Respaldo si GET /me/billing/countries no responde: España y la UE, que ya se facturaban antes.
const PAISES_RESPALDO: PaisFacturacion[] = [
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
  "MT",
  "NL",
  "PL",
  "PT",
  "RO",
  "SE",
  "SI",
  "SK",
].map((code) => ({ code, region: "ue" as const }));

// Países que usan el euro (zona euro y los que lo usan por acuerdo): fuera de ellos se avisa de la conversión.
const EURO = new Set([
  "AT",
  "BE",
  "HR",
  "CY",
  "EE",
  "FI",
  "FR",
  "DE",
  "GR",
  "IE",
  "IT",
  "LV",
  "LT",
  "LU",
  "MT",
  "NL",
  "PT",
  "SK",
  "SI",
  "ES",
  "AD",
  "MC",
  "SM",
  "VA",
  "ME",
  "XK",
]);
export const usaEuro = (pais: string | null | undefined) => !pais || EURO.has(pais);

/**
 * « + IVA» detrás de un precio, solo si se suma el IVA español del 21 %: sin datos fiscales (lo normal en
 * España) o con `iva_21`. A la UE (inversión del sujeto pasivo), Canarias, Ceuta, Melilla y fuera de la UE
 * no se les suma IVA español: el precio va sin el añadido.
 */
export const masIva = (perfil: Pick<PerfilFiscal, "tax_treatment"> | null | undefined) => (!perfil?.tax_treatment || perfil.tax_treatment === "iva_21" ? " + IVA" : "");

const nombresPais = (() => {
  try {
    return new Intl.DisplayNames(["es"], { type: "region" });
  } catch {
    return null;
  }
})();
export const nombrePais = (code: string) => nombresPais?.of(code) ?? code;

/** España primero; el resto, por su nombre en español. */
function ordenar(paises: PaisFacturacion[]): { code: string; region: RegionFiscal; nombre: string }[] {
  const con = paises.map((x) => ({ ...x, nombre: nombrePais(x.code) }));
  const es = con.filter((x) => x.code === "ES");
  const resto = con.filter((x) => x.code !== "ES").sort((a, b) => a.nombre.localeCompare(b.nombre, "es"));
  return [...es, ...resto];
}

const ETIQUETA_FISCAL: Record<RegionFiscal, string> = {
  es: "NIF",
  ue: "NIF-IVA (con el prefijo del país)",
  fuera_ue: "Identificación fiscal de tu empresa (Tax ID / EIN / RFC…)",
};

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
  // Cuenta de la beta dentro de su ventana tras el lanzamiento: contrata ahora y no paga hasta el final.
  // Solo con `trial_days` de la API: a una cuenta nueva nunca se le anuncia una prueba.
  const diasPrueba = cobro && typeof me.pricing?.trial_days === "number" && me.pricing.trial_days > 0 ? me.pricing.trial_days : null;
  const hastaCuando = diasPrueba !== null ? finDePrueba(diasPrueba) : null;
  const libres = me.plans?.available ?? { pro: 0, pro_whatsapp: 0 };
  // En la beta solo hay plazas Pro; con cobro, los dos planes se pueden comprar.
  const disponible = (p: PlanPago) => cobro || libres[p] > 0;
  // Planes que se ofrecen: para WhatsApp, solo Pro+ (Pro no lo incluye).
  const planes: PlanPago[] = motivo === "whatsapp" ? ["pro_whatsapp"] : ["pro", "pro_whatsapp"];
  const [plan, setPlan] = useState<PlanPago>(motivo === "whatsapp" ? "pro_whatsapp" : disponible("pro") || !disponible("pro_whatsapp") ? "pro" : "pro_whatsapp");
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

  // Los datos fiscales ya guardados deciden si al precio se le suma el IVA español y si hay que avisar del euro.
  const [fiscal, setFiscal] = useState<PerfilFiscal | null>(null);
  useEffect(() => {
    if (!me.billing?.profile_complete) return;
    let sigue = true;
    api
      .perfilFiscal()
      .then((p) => sigue && setFiscal(p))
      .catch(() => undefined);
    return () => {
      sigue = false;
    };
  }, [me.billing?.profile_complete]);
  const iva = masIva(fiscal);

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

  const titulo = paso.n === "datos" ? "Datos para la factura" : motivo === "whatsapp" ? "Pásate a Pro+" : "Pásate a Pro";
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
              <div className={planes.length > 1 ? "opciones opciones-dos planes-pago" : "opciones planes-pago plan-unico"}>
                {planes.map((p) => {
                  const euro = precio(p);
                  return (
                    <label className="opcion" key={p}>
                      <input type="radio" name={`${id}-plan`} checked={plan === p} disabled={!disponible(p) || ocupado} onChange={() => setPlan(p)} />
                      <span>
                        {/* Nombre del plan grande; el «+» de Pro+ en rojo, como el punto de la marca. */}
                        <span className="plan-nombre">
                          {p === "pro_whatsapp" ? (
                            <>
                              Pro<span className="mas">+</span>
                            </>
                          ) : (
                            NOMBRE_PAGO[p]
                          )}
                        </span>
                        {/* El precio, lo primero que se mira: grande, con «+ IVA al año» al lado. */}
                        {cobro ? (
                          euro !== null && (
                            <>
                              <span className="plan-cifra">
                                {euros(me, euro)}
                                <small>{iva} al año</small>
                              </span>
                              {conDescuento(p) && <small className="plan-precio">5 % menos por ser tu segunda ficha de pago o siguientes</small>}
                              {hastaCuando && <small className="plan-precio plan-prueba">No pagas nada hasta el {hastaCuando}</small>}
                            </>
                          )
                        ) : disponible(p) ? (
                          <>
                            <span className="plan-cifra">
                              {euros(me, 0)}
                              <small> durante la beta</small>
                            </span>
                            {euro !== null && <small className="plan-precio">Después, {euros(me, euro)} + IVA al año</small>}
                          </>
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

            {cobro && fiscal?.address && !usaEuro(fiscal.address.country) && <p className="sec">Precios en euros. Tu banco hace la conversión.</p>}
            {cobro ? (
              precio(plan) !== null && (
                <p className="sec">
                  {hastaCuando ? (
                    <>
                      Esta ficha necesita una plaza {NOMBRE_PAGO[plan]}: {euros(me, precio(plan)!)}
                      {iva} por un año. Como estás en la beta, no pagas nada hasta el {hastaCuando}: hoy solo guardas la tarjeta en Stripe, ese día se cobra el primer año
                      y puedes cancelarla antes sin coste. Te avisamos 7 días antes. Después se renueva sola cada año salvo que la canceles. No se devuelven importes.
                    </>
                  ) : (
                    <>
                      Esta ficha necesita una plaza {NOMBRE_PAGO[plan]}: {euros(me, precio(plan)!)}
                      {iva} por un año desde hoy. Se renueva sola salvo que la canceles. No se devuelven importes. Pagas con tarjeta en Stripe; el periodo exacto va en la
                      factura.
                    </>
                  )}
                </p>
              )
            ) : !disponible(plan) ? (
              <p className="aviso">
                {plan === "pro_whatsapp"
                  ? "Pro+ con WhatsApp todavía no está disponible en la beta. Te avisaremos cuando lo esté."
                  : "Ya usas todas las plazas Pro de la beta. Desactiva otra ficha Pro en «Tus negocios» para usar su plaza."}
              </p>
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
                      ? hastaCuando
                        ? "Guardar la tarjeta"
                        : "Continuar al pago"
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
type Clave = Campo | "line2" | "province";

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
  // Los países a los que se factura, de la API (sin los bloqueados). Si no responde, España y la UE.
  const [paises, setPaises] = useState<PaisFacturacion[]>([{ code: "ES", region: "es" }, ...PAISES_RESPALDO]);

  useEffect(() => {
    let vivo = true;
    api
      .paisesFacturacion()
      .then((r) => vivo && r.countries.length > 0 && setPaises(r.countries))
      .catch(() => undefined);
    return () => {
      vivo = false;
    };
  }, []);

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

  const poner = (k: keyof typeof datos) => (e: { target: { value: string } }) => {
    setDatos((d) => ({ ...d, [k]: e.target.value }));
    // Al cambiar de país cambian la etiqueta y las reglas: el error de antes ya no vale.
    if (k === "country") setError(null);
  };
  const opciones = ordenar(paises);
  const region: RegionFiscal = paises.find((x) => x.code === datos.country)?.region ?? (datos.country === "ES" ? "es" : "fuera_ue");
  const fuera = region === "fuera_ue";
  // Fuera de la UE el código postal es opcional; en España y la UE, obligatorio.
  const OBLIGATORIOS: Campo[] = ["legal_name", "tax_id", "line1", ...(fuera ? [] : (["postal_code"] as Campo[])), "city", "invoice_email"];
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
        // En España y la UE se quitan espacios, puntos y guiones; fuera de la UE se respetan sus separadores (12-3456789).
        tax_id: fuera ? datos.tax_id.trim().toUpperCase() : datos.tax_id.replace(/[\s.-]/g, "").toUpperCase(),
        address: {
          line1: datos.line1.trim(),
          line2: datos.line2.trim() || null,
          postal_code: datos.postal_code.trim() || null,
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

  const errorDe = (k: Clave) =>
    error?.campo === k ? (
      <small className="error-campo" id={`${id}-error`} role="alert">
        {error.mensaje}
      </small>
    ) : null;

  const campo = (k: Clave, etiqueta: string, extra: { type?: string; autoComplete?: string; opcional?: boolean; ayuda?: string } = {}) => {
    const conError = error?.campo === k;
    const ayuda = extra.ayuda ? `${id}-${k}-ayuda` : undefined;
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
          aria-describedby={[conError ? `${id}-error` : "", ayuda ?? ""].filter(Boolean).join(" ") || undefined}
        />
        {extra.ayuda && (
          <small className="ayuda-campo" id={ayuda}>
            {extra.ayuda}
          </small>
        )}
        {errorDe(k)}
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
      {campo("legal_name", fuera ? "Nombre de tu empresa" : "Razón social o nombre", { autoComplete: "organization" })}
      <label className="campo-fiscal" htmlFor={`${id}-country`}>
        <span>País</span>
        <select
          id={`${id}-country`}
          value={datos.country}
          onChange={poner("country")}
          disabled={guardando}
          autoComplete="country"
          aria-invalid={error?.campo === "country"}
          aria-describedby={error?.campo === "country" ? `${id}-error` : undefined}
        >
          {/* Un país guardado que ya no está en la lista se conserva para que se vea (la API dirá por qué). */}
          {!opciones.some((x) => x.code === datos.country) && <option value={datos.country}>{nombrePais(datos.country)}</option>}
          {opciones.map((x) => (
            <option key={x.code} value={x.code}>
              {x.nombre}
            </option>
          ))}
        </select>
        {!usaEuro(datos.country) && <small className="ayuda-campo">Precios en euros. Tu banco hace la conversión.</small>}
        {errorDe("country")}
      </label>
      {campo("tax_id", ETIQUETA_FISCAL[region], {
        autoComplete: "off",
        ...(fuera ? { ayuda: "Fuera de la UE solo facturamos a empresas. Escribe la identificación fiscal de tu empresa tal como aparece en tus documentos." } : {}),
      })}
      {campo("line1", "Dirección", { autoComplete: "address-line1" })}
      {campo("line2", "Piso, puerta…", { autoComplete: "address-line2", opcional: true })}
      <div className="fila-fiscal">
        {campo("postal_code", "Código postal", { autoComplete: "postal-code", opcional: fuera })}
        {campo("city", "Población", { autoComplete: "address-level2" })}
      </div>
      {campo("province", fuera ? "Provincia, estado o región" : "Provincia", { autoComplete: "address-level1", opcional: true })}
      {campo("invoice_email", "Correo para las facturas", { type: "email", autoComplete: "email" })}
      {error && error.campo === null && (
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
