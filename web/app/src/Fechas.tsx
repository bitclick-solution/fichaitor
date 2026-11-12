import { useEffect, useId, useRef, useState } from "react";
import { api, ApiError, type FechaClave, type Franja, type RespuestaFecha } from "./api";
import { Seccion } from "./movimiento";

// «Festivos»: Fichaitor pregunta con antelación si el negocio abre en una fecha clave y con qué horario.
// El dueño elige cerrado, su horario normal u otro horario (una o dos franjas) y lo confirma: esa
// pulsación es su confirmación, y el worker envía el cambio a Google. También puede descartarla.
// La misma pregunta llega por Telegram; se responde en uno de los dos sitios.
//
// Contrato: docs/contrato-web-api.md, «Fechas clave». La lista se pide con filter=all para enseñar
// también lo ya confirmado (y cómo va su envío a Google) hasta que pasa el día.

const semana = new Intl.DateTimeFormat("es-ES", { weekday: "long", timeZone: "UTC" });
const diaMes = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", timeZone: "UTC" });

/** «martes 24 de noviembre», sin la coma que pone Intl tras el día de la semana. */
function dia(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) ? iso : `${semana.format(d)} ${diaMes.format(d)}`;
}

/** Días que faltan, contados en la fecha local del navegador. */
function faltan(iso: string): number | null {
  const objetivo = Date.parse(`${iso}T00:00:00Z`);
  if (Number.isNaN(objetivo)) return null;
  const hoy = new Date();
  const hoyUTC = Date.UTC(hoy.getFullYear(), hoy.getMonth(), hoy.getDate());
  return Math.round((objetivo - hoyUTC) / 86_400_000);
}

const franjasTexto = (f: Franja[]) => f.map((x) => `de ${x.open} a ${x.close}`).join(" y ");

/** Mientras haya un envío a Google sin terminar, la lista se vuelve a pedir. */
const enMarcha = (f: FechaClave) => f.status === "en_curso" || f.action?.status === "pendiente" || f.action?.status === "en_curso";
/** Las descartadas y las caducadas no se enseñan: no hay nada que hacer con ellas. */
const visible = (f: FechaClave) => f.status !== "descartada" && f.status !== "caducada";

export function Fechas({ fichaId, onSinSesion }: { fichaId: string; onSinSesion: () => void }) {
  const [fechas, setFechas] = useState<FechaClave[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const vueltas = useRef(0);

  const cargar = async () => {
    try {
      setFechas(await api.fechasClave(fichaId));
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 404: la ruta no existe en ese entorno. 409: baja programada o sin ficha elegida. La sección no aparece.
      else if (e instanceof ApiError && (e.status === 404 || e.status === 409)) setFechas([]);
      else setError(e instanceof Error ? e.message : "No hemos podido leer tus fechas.");
    }
  };

  useEffect(() => {
    void cargar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fichaId]);

  // Sondeo del envío a Google: cada 3 s al principio y cada minuto pasados 20 intentos.
  // Con la pestaña oculta no se pregunta; al volver, el siguiente cambio de estado lo reprograma.
  const hayEnvio = (fechas ?? []).some(enMarcha);
  useEffect(() => {
    if (!hayEnvio) {
      vueltas.current = 0;
      return;
    }
    const reloj = setTimeout(
      () => {
        vueltas.current += 1;
        if (document.hidden) setFechas((xs) => (xs ? [...xs] : xs));
        else void cargar();
      },
      vueltas.current < 20 ? 3000 : 60000,
    );
    return () => clearTimeout(reloj);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fechas, hayEnvio]);

  const lista = (fechas ?? []).filter(visible);
  if (!error && lista.length === 0) return null;

  const cambiar = (nueva: FechaClave) => setFechas((xs) => (xs ?? []).map((x) => (x.id === nueva.id ? nueva : x)));

  return (
    <Seccion id="festivos" className="festivos">
      <h2 className="rotulo">Festivos</h2>
      <p className="sec">Dinos si abres y lo ponemos en tu ficha de Google. Así nadie se encuentra la puerta cerrada.</p>
      {error && (
        <p className="aviso" role="alert">
          {error}
        </p>
      )}
      {lista.length > 0 && (
        <ul className="fechas">
          {lista.map((f) => (
            <li key={f.id}>
              {f.status === "pendiente" && f.answerable ? (
                <Fecha fecha={f} onSinSesion={onSinSesion} onRespondida={cambiar} onCambio={() => void cargar()} />
              ) : (
                <Resuelta fecha={f} />
              )}
            </li>
          ))}
        </ul>
      )}
    </Seccion>
  );
}

function Cabecera({ fecha }: { fecha: FechaClave }) {
  const quedan = faltan(fecha.date);
  return (
    <div className="fecha-cab">
      <p className="rotulo">
        {fecha.name}
        <span className="pt">.</span>
      </p>
      <p className="sec">
        <span className="fecha-dia">{dia(fecha.date)}</span>
        {quedan !== null && quedan >= 0 && <> · {quedan === 0 ? "es hoy" : quedan === 1 ? "es mañana" : `faltan ${quedan} días`}</>}
      </p>
    </div>
  );
}

/** Ya confirmada: qué se dijo y cómo va su envío a Google. Nunca «publicado»: Google revisa los cambios. */
function Resuelta({ fecha }: { fecha: FechaClave }) {
  const a = fecha.action;
  const fallo = fecha.status === "fallida" || a?.status === "fallida";
  return (
    <div className="fecha">
      <Cabecera fecha={fecha} />
      {fecha.answer && <p className="lead">{fecha.answer.kind === "closed" || !fecha.answer.text ? "Cierras." : `Abres ${fecha.answer.text}.`}</p>}
      {a?.message &&
        (fallo ? (
          <p className="aviso" role="alert">
            {a.message}
          </p>
        ) : (
          <p className={a.status === "hecha" ? "sec" : "cargando"} role="status" aria-live="polite">
            {a.status !== "hecha" && <span className="ojo" aria-hidden="true" />}
            {a.message}
          </p>
        ))}
    </div>
  );
}

type Opcion = RespuestaFecha["kind"];
type Paso = "elegir" | "confirmar" | "enviando";

const VACIA: Franja = { open: "", close: "" };

function Fecha({
  fecha,
  onSinSesion,
  onRespondida,
  onCambio,
}: {
  fecha: FechaClave;
  onSinSesion: () => void;
  /** 202: el elemento ya confirmado, con su acción. */
  onRespondida: (f: FechaClave) => void;
  /** La fecha ya no está pendiente (respondida por Telegram, descartada o pasada): se recarga la lista. */
  onCambio: () => void;
}) {
  const id = useId();
  const [opcion, setOpcion] = useState<Opcion | null>(null);
  const [franjas, setFranjas] = useState<Franja[]>([{ open: "10:00", close: "14:00" }]);
  const [paso, setPaso] = useState<Paso>("elegir");
  const [error, setError] = useState<string | null>(null);
  const [descartando, setDescartando] = useState(false);

  const normal = fecha.regular_hours;
  // [] : la ficha cierra ese día de la semana, así que «Horario normal» significa cerrado.
  const normalCierra = normal !== null && normal.length === 0;

  // Las mismas reglas que el servidor (que responde 422 si no se cumplen): apertura antes que cierre,
  // sin cruzar la medianoche, como mucho dos franjas, en orden y sin solaparse.
  const fallo = (): string | null => {
    if (opcion !== "custom") return null;
    for (const f of franjas) if (!f.open || !f.close) return "Completa las horas de apertura y de cierre.";
    for (const f of franjas) if (f.open >= f.close) return "La hora de cierre tiene que ser posterior a la de apertura, el mismo día.";
    if (franjas.length === 2 && franjas[1]!.open <= franjas[0]!.close) return "La segunda franja tiene que empezar después de que acabe la primera.";
    return null;
  };

  const resumen = (): string => {
    if (opcion === "closed" || (opcion === "regular" && normalCierra)) return "cierras";
    if (opcion === "regular") return normal && normal.length > 0 ? `abres con tu horario normal, ${franjasTexto(normal)}` : "abres con tu horario normal";
    return `abres ${franjasTexto(franjas)}`;
  };

  const seguir = () => {
    const f = fallo();
    setError(f);
    if (!f) setPaso("confirmar");
  };

  const enviar = async () => {
    if (!opcion) return;
    setPaso("enviando");
    setError(null);
    try {
      const hecha = await api.responderFecha(fecha.id, opcion === "custom" ? { kind: "custom", ranges: franjas } : { kind: opcion });
      if (hecha) onRespondida(hecha);
      else onCambio();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 404 o 409: ya no está pendiente. No se reintenta: se recarga la lista.
      else if (e instanceof ApiError && (e.status === 404 || e.status === 409)) onCambio();
      // 422: el servidor no acepta las franjas. Se vuelve a los campos con su mensaje debajo.
      else if (e instanceof ApiError && e.status === 422) {
        setError(e.message);
        setPaso("elegir");
      } else {
        // 503 u otro fallo: no se guardó nada y se puede repetir.
        setError(e instanceof Error ? e.message : "No hemos podido guardarlo. Prueba otra vez.");
        setPaso("confirmar");
      }
    }
  };

  const descartar = async () => {
    setDescartando(true);
    setError(null);
    try {
      await api.descartarFecha(fecha.id);
      onCambio();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      else if (e instanceof ApiError && (e.status === 404 || e.status === 409)) onCambio();
      else setError(e instanceof Error ? e.message : "No hemos podido descartarla.");
      setDescartando(false);
    }
  };

  if (paso === "confirmar" || paso === "enviando") {
    return (
      <div className="fecha">
        <Cabecera fecha={fecha} />
        <p className="lead">
          Pondremos en tu ficha de Google que el {dia(fecha.date)} {resumen()}.
        </p>
        {error && (
          <p className="aviso" role="alert">
            {error}
          </p>
        )}
        <div className="acciones">
          <button className="btn" type="button" disabled={paso === "enviando"} onClick={() => void enviar()}>
            {paso === "enviando" ? "Guardando…" : "Confirmar"}
          </button>
          <button className="btn sec" type="button" disabled={paso === "enviando"} onClick={() => setPaso("elegir")}>
            Cambiar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fecha">
      <Cabecera fecha={fecha} />
      <fieldset className="fecha-opciones">
        <legend className="lead">¿Abres ese día?</legend>
        <div className="opciones">
          <label className="opcion">
            <input type="radio" name={`${id}-opcion`} checked={opcion === "closed"} onChange={() => setOpcion("closed")} />
            <span>Cerrado</span>
          </label>
          <label className="opcion">
            <input type="radio" name={`${id}-opcion`} checked={opcion === "regular"} onChange={() => setOpcion("regular")} />
            <span>
              Horario normal
              {normal && normal.length > 0 && <small>{franjasTexto(normal)}</small>}
              {normalCierra && <small>ese día de la semana cierras</small>}
            </span>
          </label>
          <label className="opcion">
            <input type="radio" name={`${id}-opcion`} checked={opcion === "custom"} onChange={() => setOpcion("custom")} />
            <span>Otro horario</span>
          </label>
        </div>
      </fieldset>

      {opcion === "custom" && (
        <div className="franjas">
          {franjas.map((f, i) => (
            <div className="franja" key={i}>
              <label>
                <span className="sec">{franjas.length === 2 ? (i === 0 ? "Mañana, abre" : "Tarde, abre") : "Abre"}</span>
                <input
                  type="time"
                  value={f.open}
                  step={900}
                  onChange={(e) => setFranjas((xs) => xs.map((x, j) => (j === i ? { ...x, open: e.target.value } : x)))}
                />
              </label>
              <label>
                <span className="sec">Cierra</span>
                <input
                  type="time"
                  value={f.close}
                  step={900}
                  onChange={(e) => setFranjas((xs) => xs.map((x, j) => (j === i ? { ...x, close: e.target.value } : x)))}
                />
              </label>
            </div>
          ))}
          {franjas.length === 1 ? (
            <button className="enlace" type="button" onClick={() => setFranjas((xs) => [...xs, { ...VACIA }])}>
              Añadir otra franja (por ejemplo, de tarde)
            </button>
          ) : (
            <button className="enlace" type="button" onClick={() => setFranjas((xs) => xs.slice(0, 1))}>
              Quitar la segunda franja
            </button>
          )}
        </div>
      )}

      {error && (
        <p className="aviso" role="alert">
          {error}
        </p>
      )}

      <div className="acciones">
        <button className="btn" type="button" disabled={!opcion} onClick={seguir}>
          Seguir
        </button>
        <button className="enlace" type="button" disabled={descartando} onClick={() => void descartar()}>
          {descartando ? "Descartando…" : "No preguntar por esta fecha"}
        </button>
      </div>
    </div>
  );
}
