import { useEffect, useId, useState } from "react";
import { api, ErrorAdmin, type BorradorComunicacion, type Comunicacion, type FilaComunicacion, type Publico, type VistaPrevia } from "./api";
import { Cargando, cuando, Error, Mas, mensajeDe, numero, useLista, Ventana } from "./comun";

// «Comunicaciones» (docs/contrato-backoffice.md, apartado 10). Solo el rol propietario. Flujo: redactar →
// guardar el borrador → vista previa → prueba a tu dirección → recuento → confirmar con el código → el worker
// envía en cola. Ninguna respuesta trae direcciones de clientes: solo recuentos.

const TIPO: Record<string, string> = { servicio: "Servicio", comercial: "Comercial" };
const PLAN_PUBLICO: [string, string][] = [
  ["gratis", "Gratis"],
  ["pro", "Pro"],
  ["pro_whatsapp", "Pro+"],
];
const ESTADO: Record<string, [string, string]> = { borrador: ["Borrador", "neutra"], enviando: ["Enviando", "mal"], enviada: ["Enviada", "bien"] };

function publico(c: Pick<FilaComunicacion, "audience" | "audience_plan" | "audience_account">): string {
  if (c.audience === "todas") return "Todas las cuentas activas";
  if (c.audience === "beta") return "Cuentas de la beta";
  if (c.audience === "plan") return `Cuentas con una ficha en ${PLAN_PUBLICO.find(([v]) => v === c.audience_plan)?.[1] ?? c.audience_plan ?? "—"}`;
  return `Una cuenta: ${c.audience_account ?? "—"}`;
}

/** Cuánto tarda el envío, en palabras. */
function duracion(n: number, porMinuto: number): string {
  const min = Math.ceil(n / Math.max(1, porMinuto));
  return min <= 1 ? "un minuto o menos" : `unos ${min} minutos`;
}

export function Comunicaciones() {
  const [abierta, setAbierta] = useState<string | "nueva" | null>(null);
  const [version, setVersion] = useState(0);
  const lista = useLista<FilaComunicacion>((cursor) => api.comunicaciones(cursor).then((r) => ({ items: r.communications, next_cursor: r.next_cursor })));
  const { empezar } = lista;
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (abierta) return;
    setError(null);
    api
      .comunicaciones()
      .then((r) => empezar(r.communications, r.next_cursor))
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, [abierta, version, empezar]);

  if (abierta)
    return (
      <Editor
        id={abierta === "nueva" ? null : abierta}
        onVolver={() => {
          setAbierta(null);
          setVersion((v) => v + 1);
        }}
        onCreada={(id) => setAbierta(id)}
      />
    );

  const filas = lista.filas;
  return (
    <>
      <h1>Comunicaciones</h1>
      <p className="sec">
        Correos a los clientes con la plantilla de Fichaitor. Solo a cuentas: nunca a la lista de espera ni a direcciones escritas a mano. Las de servicio llegan a todo
        el público; las comerciales, no a quien se dio de baja de ellas.
      </p>
      <div className="acciones">
        <button className="btn" type="button" onClick={() => setAbierta("nueva")}>
          Nueva comunicación
        </button>
      </div>
      <Error texto={error} />
      {!filas && !error && <Cargando />}
      {filas &&
        (filas.length === 0 ? (
          <p className="sec">Todavía no hay ninguna.</p>
        ) : (
          <section className="bloque">
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Asunto</th>
                    <th>Tipo</th>
                    <th>Público</th>
                    <th>Estado</th>
                    <th className="num">Enviados</th>
                    <th>Creada</th>
                  </tr>
                </thead>
                <tbody>
                  {filas.map((c) => {
                    const [estado, clase] = ESTADO[c.state] ?? [c.state, "neutra"];
                    return (
                      <tr key={c.communication_id}>
                        <td>
                          <button className="enlace" type="button" onClick={() => setAbierta(c.communication_id)}>
                            {c.subject}
                          </button>
                        </td>
                        <td>{TIPO[c.kind] ?? c.kind}</td>
                        <td>{publico(c)}</td>
                        <td>
                          <span className={`etiqueta ${clase}`}>{estado}</span>
                        </td>
                        <td className="num">
                          {c.recipients === null ? "—" : `${numero(c.sent)} de ${numero(c.recipients)}`}
                          {c.failed > 0 && <span className="etiqueta mal">{c.failed} fallidos</span>}
                        </td>
                        <td>
                          {cuando(c.created_at)}
                          {c.created_by && <span className="sec"> · {c.created_by}</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <Mas lista={lista} />
          </section>
        ))}
    </>
  );
}

const VACIO: BorradorComunicacion = {
  kind: "servicio",
  subject: "",
  preheader: "",
  headline: "",
  paragraphs: [""],
  button_label: null,
  button_url: null,
  audience: "todas",
  audience_plan: null,
  audience_account: null,
};

const UNA_LINEA = /[\r\n]/;
const ENLACE_PROPIO = /^https:\/\/fichaitor\.es\/[^\s"'<>@]*$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Qué le falta al borrador para guardarse, con las reglas del contrato; null si vale. */
function problema(b: BorradorComunicacion, conBoton: boolean): string | null {
  if (!b.subject.trim() || b.subject.length > 90 || UNA_LINEA.test(b.subject)) return "El asunto: de 1 a 90 caracteres, en una línea.";
  if (!b.preheader.trim() || b.preheader.length > 120 || UNA_LINEA.test(b.preheader)) return "El preencabezado: de 1 a 120 caracteres, en una línea.";
  if (!b.headline.trim() || b.headline.length > 40 || UNA_LINEA.test(b.headline)) return "El titular: de 1 a 40 caracteres, en una línea.";
  if (/[.!?…:;,]\s*$/.test(b.headline)) return "El titular va sin punto final: la plantilla ya pone el punto rojo.";
  const parrafos = b.paragraphs.filter((p) => p.trim());
  if (parrafos.length < 1 || parrafos.length > 8) return "De 1 a 8 párrafos.";
  if (parrafos.some((p) => p.length > 1000)) return "Cada párrafo, hasta 1000 caracteres.";
  if (conBoton) {
    if (!b.button_label?.trim() || b.button_label.length > 30) return "La etiqueta del botón: de 1 a 30 caracteres.";
    if (!b.button_url || !ENLACE_PROPIO.test(b.button_url)) return "El enlace del botón tiene que empezar por https://fichaitor.es/ (sin espacios ni comillas).";
  }
  if (b.audience === "plan" && !b.audience_plan) return "Elige el plan.";
  if (b.audience === "cuenta" && !UUID.test(b.audience_account ?? "")) return "Escribe el id de la cuenta (un UUID).";
  return null;
}

/** El cuerpo que se envía: sin párrafos vacíos, sin botón si no se usa y sin los campos de público que no tocan. */
function limpio(b: BorradorComunicacion, conBoton: boolean): BorradorComunicacion {
  return {
    ...b,
    subject: b.subject.trim(),
    preheader: b.preheader.trim(),
    headline: b.headline.trim(),
    paragraphs: b.paragraphs.map((p) => p.trim()).filter(Boolean),
    button_label: conBoton ? (b.button_label?.trim() ?? null) : null,
    button_url: conBoton ? (b.button_url?.trim() ?? null) : null,
    audience_plan: b.audience === "plan" ? b.audience_plan : null,
    audience_account: b.audience === "cuenta" ? (b.audience_account?.trim().toLowerCase() ?? null) : null,
  };
}

function Editor({ id, onVolver, onCreada }: { id: string | null; onVolver: () => void; onCreada: (id: string) => void }) {
  const fid = useId();
  const [c, setC] = useState<Comunicacion | null>(null);
  const [b, setB] = useState<BorradorComunicacion>(VACIO);
  const [conBoton, setConBoton] = useState(false);
  const [cambiado, setCambiado] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [guardando, setGuardando] = useState(false);
  const [version, setVersion] = useState(0);
  const [hecho, setHecho] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let vivo = true;
    api
      .comunicacion(id)
      .then((r) => {
        if (!vivo) return;
        setC(r);
        setB({
          kind: r.kind,
          subject: r.subject,
          preheader: r.preheader,
          headline: r.headline,
          paragraphs: r.paragraphs.length ? r.paragraphs : [""],
          button_label: r.button_label,
          button_url: r.button_url,
          audience: r.audience,
          audience_plan: r.audience_plan,
          audience_account: r.audience_account,
        });
        setConBoton(Boolean(r.button_label));
        setCambiado(false);
      })
      .catch((e: unknown) => vivo && setError(mensajeDe(e)));
    return () => {
      vivo = false;
    };
  }, [id, version]);

  const editable = !id || c?.state === "borrador";
  const cargado = !id || c !== null;
  const falta = problema(b, conBoton);
  const pon = (cambios: Partial<BorradorComunicacion>) => {
    setB((x) => ({ ...x, ...cambios }));
    setCambiado(true);
    setHecho(null);
  };

  const guardar = async () => {
    if (falta) return;
    setGuardando(true);
    setError(null);
    try {
      const cuerpo = limpio(b, conBoton);
      if (!id) {
        const r = await api.crearComunicacion(cuerpo);
        onCreada(r.communication_id);
      } else {
        await api.guardarComunicacion(id, cuerpo);
        setHecho("Borrador guardado. Si ya habías enviado una prueba, envía otra: la anterior era de la versión de antes.");
        setVersion((v) => v + 1);
      }
    } catch (e) {
      setError(mensajeDe(e));
    } finally {
      setGuardando(false);
    }
  };

  if (!cargado)
    return (
      <>
        <Volver onVolver={onVolver} />
        <Error texto={error} />
        {!error && <Cargando />}
      </>
    );

  return (
    <>
      <Volver onVolver={onVolver} />
      <h1>{id ? (c?.subject ?? "Comunicación") : "Nueva comunicación"}</h1>
      {c && (
        <p className="sec">
          <span className={`etiqueta ${ESTADO[c.state]?.[1] ?? "neutra"}`}>{ESTADO[c.state]?.[0] ?? c.state}</span> · creada {cuando(c.created_at)}
          {c.created_by && ` por ${c.created_by}`}
          {c.state === "borrador" && ` · guardada ${cuando(c.updated_at)}`}
        </p>
      )}
      {hecho && (
        <p className="ok" role="status">
          {hecho}
        </p>
      )}

      {c && c.state !== "borrador" && <Envio c={c} onActualizar={() => setVersion((v) => v + 1)} />}

      <form
        className="bloque"
        onSubmit={(e) => {
          e.preventDefault();
          void guardar();
        }}
      >
        <h2>{editable ? "1. Redactar" : "Lo que se envió"}</h2>
        <fieldset className="campo" disabled={!editable}>
          <legend>Tipo</legend>
          <label className="casilla">
            <input type="radio" name={`${fid}-t`} checked={b.kind === "servicio"} onChange={() => pon({ kind: "servicio" })} />
            <span>
              <strong>De servicio:</strong> cambios en el producto o en las condiciones, incidencias, fin de la beta. Llega a todo el público, sin enlace de baja.
            </span>
          </label>
          <label className="casilla">
            <input type="radio" name={`${fid}-t`} checked={b.kind === "comercial"} onChange={() => pon({ kind: "comercial" })} />
            <span>
              <strong>Comercial:</strong> ofertas, novedades para vender, boletín. No llega a quien se dio de baja de las comerciales y lleva el enlace de baja.
            </span>
          </label>
          <small>Si dudas, es comercial: un correo de servicio que vende algo deja de ser de servicio (LSSI, art. 21).</small>
        </fieldset>

        <div className="fila">
          <label className="campo" htmlFor={`${fid}-p`}>
            <span>Público</span>
            <select id={`${fid}-p`} value={b.audience} disabled={!editable} onChange={(e) => pon({ audience: e.target.value as Publico })}>
              <option value="todas">Todas las cuentas activas</option>
              <option value="beta">Cuentas de la beta</option>
              <option value="plan">Cuentas con una ficha en un plan</option>
              <option value="cuenta">Una cuenta (por su id)</option>
            </select>
          </label>
          {b.audience === "plan" && (
            <label className="campo" htmlFor={`${fid}-pl`}>
              <span>Plan</span>
              <select id={`${fid}-pl`} value={b.audience_plan ?? ""} disabled={!editable} onChange={(e) => pon({ audience_plan: e.target.value || null })}>
                <option value="">Elige un plan</option>
                {PLAN_PUBLICO.map(([v, n]) => (
                  <option key={v} value={v}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
          )}
          {b.audience === "cuenta" && (
            <label className="campo" htmlFor={`${fid}-c`}>
              <span>Id de la cuenta</span>
              <input
                id={`${fid}-c`}
                type="text"
                value={b.audience_account ?? ""}
                disabled={!editable}
                onChange={(e) => pon({ audience_account: e.target.value })}
                spellCheck={false}
                autoComplete="off"
              />
            </label>
          )}
        </div>

        <Texto id={`${fid}-a`} etiqueta="Asunto" valor={b.subject} max={90} editable={editable} onCambio={(v) => pon({ subject: v })} />
        <Texto
          id={`${fid}-pe`}
          etiqueta="Preencabezado (lo que se ve tras el asunto en la bandeja)"
          valor={b.preheader}
          max={120}
          editable={editable}
          onCambio={(v) => pon({ preheader: v })}
        />
        <Texto
          id={`${fid}-h`}
          etiqueta="Titular (sin punto final: la plantilla pone el punto rojo)"
          valor={b.headline}
          max={40}
          editable={editable}
          onCambio={(v) => pon({ headline: v })}
        />

        <fieldset className="campo" disabled={!editable}>
          <legend>Párrafos (de 1 a 8)</legend>
          {b.paragraphs.map((p, i) => (
            <div className="parrafo" key={i}>
              <textarea
                aria-label={`Párrafo ${i + 1}`}
                value={p}
                maxLength={1000}
                rows={4}
                onChange={(e) => pon({ paragraphs: b.paragraphs.map((x, j) => (j === i ? e.target.value : x)) })}
              />
              <small>
                {p.length}/1000
                {editable && b.paragraphs.length > 1 && (
                  <>
                    {" · "}
                    <button className="enlace" type="button" onClick={() => pon({ paragraphs: b.paragraphs.filter((_, j) => j !== i) })}>
                      Quitar este párrafo
                    </button>
                  </>
                )}
              </small>
            </div>
          ))}
          {editable && b.paragraphs.length < 8 && (
            <div className="acciones">
              <button className="btn sec" type="button" onClick={() => pon({ paragraphs: [...b.paragraphs, ""] })}>
                Añadir un párrafo
              </button>
            </div>
          )}
          <small>Texto, no HTML. Cada párrafo es un bloque: un salto de línea dentro de un párrafo no se ve en el correo.</small>
        </fieldset>

        <fieldset className="campo" disabled={!editable}>
          <legend>Botón (opcional)</legend>
          <label className="casilla">
            <input
              type="checkbox"
              checked={conBoton}
              onChange={(e) => {
                setConBoton(e.target.checked);
                setCambiado(true);
              }}
            />
            <span>Con un botón, detrás del primer párrafo</span>
          </label>
          {conBoton && (
            <div className="fila">
              <Texto id={`${fid}-be`} etiqueta="Texto del botón" valor={b.button_label ?? ""} max={30} editable={editable} onCambio={(v) => pon({ button_label: v })} />
              <Texto
                id={`${fid}-bu`}
                etiqueta="Enlace (solo https://fichaitor.es/…)"
                valor={b.button_url ?? ""}
                max={500}
                editable={editable}
                onCambio={(v) => pon({ button_url: v })}
              />
            </div>
          )}
        </fieldset>

        {editable && (
          <>
            {falta && (cambiado || !id) && <p className="sec">{falta}</p>}
            <Error texto={error} />
            <div className="acciones">
              <button className="btn" type="submit" disabled={guardando || Boolean(falta) || (Boolean(id) && !cambiado)}>
                {guardando ? "Guardando…" : id ? "Guardar los cambios" : "Guardar el borrador"}
              </button>
            </div>
          </>
        )}
      </form>

      {id &&
        c &&
        c.state === "borrador" &&
        (cambiado ? (
          <p className="aviso">Hay cambios sin guardar: guárdalos para ver la vista previa y enviar la prueba de esta versión.</p>
        ) : (
          <Envio c={c} onActualizar={() => setVersion((v) => v + 1)} />
        ))}
    </>
  );
}

function Volver({ onVolver }: { onVolver: () => void }) {
  return (
    <p>
      <button className="enlace" type="button" onClick={onVolver}>
        ← Volver a las comunicaciones
      </button>
    </p>
  );
}

function Texto({
  id,
  etiqueta,
  valor,
  max,
  editable,
  onCambio,
}: {
  id: string;
  etiqueta: string;
  valor: string;
  max: number;
  editable: boolean;
  onCambio: (v: string) => void;
}) {
  return (
    <label className="campo" htmlFor={id}>
      <span>{etiqueta}</span>
      <input
        id={id}
        type="text"
        value={valor}
        maxLength={max}
        disabled={!editable}
        onChange={(e) => onCambio(e.target.value.replace(/[\r\n]/g, " "))}
        autoComplete="off"
      />
      <small>
        {valor.length}/{max}
      </small>
    </label>
  );
}

/** Vista previa, prueba, recuento y confirmación de un borrador guardado; o el avance del envío. */
function Envio({ c, onActualizar }: { c: Comunicacion; onActualizar: () => void }) {
  const [vista, setVista] = useState<VistaPrevia | null>(null);
  const [errorVista, setErrorVista] = useState<string | null>(null);
  const [cuantos, setCuantos] = useState<{ recipients: number; excluded: number; per_minute: number } | null>(null);
  const [errorCuantos, setErrorCuantos] = useState<string | null>(null);
  const [probando, setProbando] = useState(false);
  const [prueba, setPrueba] = useState<string | null>(null);
  const [errorPrueba, setErrorPrueba] = useState<string | null>(null);
  const [confirmando, setConfirmando] = useState(false);
  const borrador = c.state === "borrador";

  useEffect(() => {
    let vivo = true;
    setVista(null);
    setErrorVista(null);
    api
      .vistaPrevia(c.communication_id)
      .then((v) => vivo && setVista(v))
      .catch((e: unknown) => vivo && setErrorVista(mensajeDe(e)));
    return () => {
      vivo = false;
    };
  }, [c.communication_id, c.updated_at]);

  // La vista previa la sirve el backoffice en preview.html, con su propia CSP. En el simulador no hay
  // servidor: se enseña el HTML del JSON desde un Blob (solo en `npm run dev:mock`; no entra en el build).
  const [src, setSrc] = useState<string | undefined>(import.meta.env.MODE === "mock" ? undefined : api.urlVistaPrevia(c.communication_id, c.updated_at));
  useEffect(() => {
    if (import.meta.env.MODE !== "mock") {
      setSrc(api.urlVistaPrevia(c.communication_id, c.updated_at));
      return;
    }
    if (!vista) return;
    const url = URL.createObjectURL(new Blob([vista.html], { type: "text/html;charset=utf-8" }));
    setSrc(url);
    return () => URL.revokeObjectURL(url);
  }, [vista, c.communication_id, c.updated_at]);

  const contar = () => {
    setErrorCuantos(null);
    api
      .destinatarios(c.communication_id)
      .then(setCuantos)
      .catch((e: unknown) => setErrorCuantos(mensajeDe(e)));
  };
  useEffect(() => {
    if (borrador) contar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [c.communication_id, c.updated_at, borrador]);

  return (
    <>
      {!borrador && (
        <section className="bloque">
          <h2>Envío</h2>
          <dl className="dl">
            <dt>Confirmada</dt>
            <dd>
              {cuando(c.confirmed_at)}
              {c.confirmed_by && ` por ${c.confirmed_by}`}
            </dd>
            <dt>Destinatarios</dt>
            <dd>
              {numero(c.recipients ?? 0)}
              {(c.excluded ?? 0) > 0 && <span className="sec"> · {numero(c.excluded ?? 0)} fuera por haberse dado de baja de las comerciales</span>}
            </dd>
            <dt>Enviados</dt>
            <dd>
              {numero(c.sent)}
              {c.failed > 0 && <span className="etiqueta mal">{numero(c.failed)} fallidos</span>}
              {c.skipped > 0 && <span className="sec"> · {numero(c.skipped)} se dieron de baja antes de su turno</span>}
            </dd>
            <dt>Terminó</dt>
            <dd>{c.finished_at ? cuando(c.finished_at) : "Todavía en cola"}</dd>
          </dl>
          {c.state === "enviando" && (
            <div className="acciones">
              <button className="btn sec" type="button" onClick={onActualizar}>
                Actualizar
              </button>
            </div>
          )}
        </section>
      )}

      <section className="bloque">
        <h2>{borrador ? "2. Vista previa" : "Vista previa"}</h2>
        <p className="sec">Hecha con el mismo código que usa el envío. El enlace de baja de la vista previa no da de baja a nadie.</p>
        <iframe className="vista-correo" title="Vista previa del correo" sandbox="" referrerPolicy="no-referrer" src={src} />
        <Error texto={errorVista} />
        {vista && (
          <details className="detalles">
            <summary>Texto plano y cabeceras</summary>
            <p className="sec">
              Asunto: <strong>{vista.subject}</strong>
              <br />
              Preencabezado: {vista.preheader}
            </p>
            {vista.headers.length > 0 && <pre className="texto-plano">{vista.headers.map((h) => `${h.name}: ${h.value}`).join("\n")}</pre>}
            <pre className="texto-plano">{vista.text}</pre>
          </details>
        )}
      </section>

      {borrador && (
        <>
          <section className="bloque">
            <h2>3. Prueba</h2>
            <p className="sec">
              Te llega a ti, con «[Prueba]» delante del asunto. Es obligatoria antes de confirmar, y vale solo para esta versión: si cambias algo, hay que enviar otra.
              Como mucho, 10 por hora.
            </p>
            {c.test_is_current ? (
              <p className="ok">Hay una prueba de esta versión ({cuando(c.test_sent_at)}).</p>
            ) : (
              <p className="aviso">Falta la prueba de esta versión.</p>
            )}
            {prueba && <p className="ok">Enviada a {prueba}. Revísala en tu correo antes de confirmar.</p>}
            <Error texto={errorPrueba} />
            <div className="acciones">
              <button
                className="btn sec"
                type="button"
                disabled={probando}
                onClick={() => {
                  setProbando(true);
                  setErrorPrueba(null);
                  setPrueba(null);
                  api
                    .probar(c.communication_id)
                    .then((r) => {
                      setPrueba(r.sent_to);
                      onActualizar();
                    })
                    .catch((e: unknown) => setErrorPrueba(mensajeDe(e)))
                    .finally(() => setProbando(false));
                }}
              >
                {probando ? "Enviando…" : c.test_is_current ? "Enviar otra prueba" : "Enviarme la prueba"}
              </button>
            </div>
          </section>

          <section className="bloque">
            <h2>4. Destinatarios y envío</h2>
            <Error texto={errorCuantos} />
            {!cuantos && !errorCuantos && <Cargando texto="Contando…" />}
            {cuantos && (
              <>
                <p>
                  Si la confirmas ahora, le llegará a <strong>{numero(cuantos.recipients)}</strong> {cuantos.recipients === 1 ? "cuenta" : "cuentas"}
                  {cuantos.excluded > 0 && <>. Quedan fuera {numero(cuantos.excluded)} que se dieron de baja de las comerciales</>}. Al ritmo de{" "}
                  {numero(cuantos.per_minute)} por minuto, tarda {duracion(cuantos.recipients, cuantos.per_minute)}.
                </p>
                <div className="acciones">
                  <button className="btn" type="button" disabled={!c.test_is_current || cuantos.recipients === 0} onClick={() => setConfirmando(true)}>
                    Enviar a {numero(cuantos.recipients)} {cuantos.recipients === 1 ? "cuenta" : "cuentas"}
                  </button>
                  <button className="btn sec" type="button" onClick={contar}>
                    Volver a contar
                  </button>
                </div>
                {!c.test_is_current && <p className="sec">Envía antes la prueba de esta versión.</p>}
                {cuantos.recipients === 0 && <p className="sec">Ninguna cuenta del público la recibiría.</p>}
              </>
            )}
          </section>
        </>
      )}

      {confirmando && cuantos && (
        <Confirmar
          c={c}
          cuantos={cuantos.recipients}
          onCerrar={() => setConfirmando(false)}
          onCambioDePublico={() => {
            setConfirmando(false);
            contar();
          }}
          onHecho={() => {
            setConfirmando(false);
            onActualizar();
          }}
        />
      )}
    </>
  );
}

function Confirmar({
  c,
  cuantos,
  onCerrar,
  onCambioDePublico,
  onHecho,
}: {
  c: Comunicacion;
  cuantos: number;
  onCerrar: () => void;
  onCambioDePublico: () => void;
  onHecho: () => void;
}) {
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cambio, setCambio] = useState<string | null>(null);
  return (
    <Ventana titulo="Confirmar el envío" onCerrar={onCerrar} bloqueada={enviando}>
      <p>
        <strong>{c.subject}</strong>
        <br />
        {TIPO[c.kind]} · {publico(c)}
      </p>
      <p className="aviso">
        Se enviará a {numero(cuantos)} {cuantos === 1 ? "cuenta" : "cuentas"}. Una vez confirmada no se puede cambiar, parar ni volver a enviar.
      </p>
      {cambio && (
        <>
          <p className="error" role="alert">
            {cambio}
          </p>
          <div className="acciones">
            <button className="btn sec" type="button" onClick={onCambioDePublico}>
              Volver a contar
            </button>
          </div>
        </>
      )}
      <Error texto={error} />
      {!cambio && (
        <div className="acciones">
          <button
            className="btn peligro"
            type="button"
            disabled={enviando}
            onClick={() => {
              setEnviando(true);
              setError(null);
              api
                .confirmar(c.communication_id, cuantos)
                .then(onHecho)
                .catch((e: unknown) => {
                  // 422 invalid: el público ha cambiado (el mensaje trae el número nuevo), falta la prueba o ya no es un borrador.
                  if (e instanceof ErrorAdmin && e.status === 422) setCambio(e.message);
                  else setError(mensajeDe(e));
                })
                .finally(() => setEnviando(false));
            }}
          >
            {enviando ? "Confirmando…" : "Confirmar y enviar"}
          </button>
          <button className="btn sec" type="button" onClick={onCerrar} disabled={enviando}>
            Cancelar
          </button>
        </div>
      )}
    </Ventana>
  );
}
