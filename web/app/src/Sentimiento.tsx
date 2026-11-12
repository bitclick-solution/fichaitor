import { useEffect, useState } from "react";
import { api, ApiError, type Sentiment } from "./api";
import { Cuenta, Seccion } from "./movimiento";

// «Qué dicen de tu negocio»: lo primero que ve quien acaba de conectar su ficha.
// GET /api/v1/me/sentiment. La nota la calcula el servidor y es comparable de un mes a otro.
// El bloque solo desaparece si la petición falla (por ejemplo, si la ruta no existe).
// Con `error`, pocas reseñas o ninguna, se explica y se ofrece reintentar.

const INTERVALO = 3000;
const MAX_INTENTOS = 10;

const GENERAL: Record<NonNullable<Sentiment["overall"]>, string> = {
  positivo: "En general, positivo",
  mixto: "Opiniones repartidas",
  negativo: "En general, negativo",
};

const veces = (n: number) => (n === 1 ? "1 reseña" : `${n} reseñas`);

const nombreMes = new Intl.DateTimeFormat("es-ES", { month: "long" });
const mesYAnio = new Intl.DateTimeFormat("es-ES", { month: "long", year: "numeric" });

/** «2026-10» → «octubre» (o «octubre de 2026» si se pide el año). Lo que no encaje se muestra tal cual. */
function mes(valor: string, conAnio = false): string {
  const m = /^(\d{4})-(\d{2})$/.exec(valor);
  if (!m) return valor;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, 1);
  return Number.isNaN(d.getTime()) ? valor : (conAnio ? mesYAnio : nombreMes).format(d);
}

type Fase = "cargando" | "listo" | "agotado" | "oculto";

export function Sentimiento({ fichaId, version, onSinSesion }: { fichaId: string; version: number; onSinSesion: () => void }) {
  const [datos, setDatos] = useState<Sentiment | null>(null);
  const [fase, setFase] = useState<Fase>("cargando");
  const [intento, setIntento] = useState(0);
  const reintentar = () => {
    setFase("cargando");
    setIntento((n) => n + 1);
  };

  useEffect(() => {
    let vivo = true;
    let temporizador: ReturnType<typeof setTimeout> | undefined;
    let intentos = 0;

    const pedir = async () => {
      try {
        const s = await api.sentimiento();
        if (!vivo) return;
        if (s.status === "pendiente") {
          intentos += 1;
          if (intentos >= MAX_INTENTOS) setFase("agotado");
          else temporizador = setTimeout(() => void pedir(), INTERVALO);
          return;
        }
        setDatos(s);
        setFase("listo");
      } catch (e) {
        if (!vivo) return;
        if (e instanceof ApiError && e.status === 401) onSinSesion();
        // Cualquier otro fallo (incluido el 404 si la función no está desplegada): sin bloque.
        else setFase("oculto");
      }
    };

    // Al volver a pedir tras publicar una respuesta, se conserva lo que ya se veía.
    setFase((f) => (f === "listo" ? "listo" : "cargando"));
    void pedir();
    return () => {
      vivo = false;
      if (temporizador) clearTimeout(temporizador);
    };
  }, [fichaId, version, intento, onSinSesion]);

  // Al cambiar de ficha, se empieza de cero.
  useEffect(() => {
    setDatos(null);
    setFase("cargando");
  }, [fichaId]);

  if (fase === "oculto") return null;

  return (
    <Seccion id="opinion" className="sentimiento">
      <h2 className="rotulo">Qué dicen de tu negocio</h2>

      {fase === "cargando" && (
        <p className="cargando grande" role="status" aria-live="polite">
          <span className="ojo" aria-hidden="true" />
          Leyendo tus reseñas…
        </p>
      )}

      {fase === "agotado" && (
        <>
          <p className="lead">Todavía estamos leyendo tus reseñas; vuelve en un minuto.</p>
          <div className="acciones">
            <button className="btn sec" type="button" onClick={reintentar}>
              Comprobar ahora
            </button>
          </div>
        </>
      )}

      {fase === "listo" && datos?.status === "error" && (
        <>
          <p className="lead">No hemos podido analizar tus reseñas.</p>
          <div className="acciones">
            <button className="btn sec" type="button" onClick={reintentar}>
              Reintentar
            </button>
          </div>
        </>
      )}

      {fase === "listo" && datos?.status === "insuficiente" && (
        <>
          <p className="lead">{datos.summary || "Tu ficha todavía no tiene reseñas, así que no hay nada que resumir."}</p>
          {datos.suggestion && <Consejo texto={datos.suggestion} />}
        </>
      )}

      {fase === "listo" && datos?.status === "ok" && (
        <>
          <div className="sent-cab">
            {datos.score !== null && (
              <p className="cifra" data-alerta={datos.overall === "negativo"}>
                <Cuenta valor={Math.round(datos.score)} />
                <small>/100</small>
              </p>
            )}
            <div>
              {datos.overall && <p className="rotulo">{GENERAL[datos.overall]}</p>}
              <Variacion datos={datos} />
              {datos.summary && <p className="lead">{datos.summary}</p>}
              {datos.provisional && <p className="provisional">Con pocas reseñas, es una lectura provisional.</p>}
              {datos.based_on !== null && (
                <p className="sec">
                  A partir de {veces(datos.based_on)}
                  {datos.with_text !== null && datos.with_text !== datos.based_on ? `, ${datos.with_text} con texto` : ""}
                  {datos.period ? ` (${datos.period})` : ""}.
                </p>
              )}
            </div>
          </div>

          <Tendencia historia={datos.history} />

          {(datos.praised.length > 0 || datos.complaints.length > 0) && (
            <div className="sent-temas">
              <Temas titulo="Lo que más gusta" temas={datos.praised} />
              <Temas titulo="Las quejas que se repiten" temas={datos.complaints} queja />
            </div>
          )}

          {datos.suggestion && <Consejo texto={datos.suggestion} />}

          <p className="sec">
            Resumen redactado con inteligencia artificial a partir del texto de tus reseñas. Es orientativo.
            {datos.next_update ? ` ${datos.next_update}` : ""}
          </p>
        </>
      )}
    </Seccion>
  );
}

function Consejo({ texto }: { texto: string }) {
  return (
    <div className="sent-consejo">
      <p className="rotulo">Qué haría Fichaitor</p>
      <p>{texto}</p>
    </div>
  );
}

/** «4 puntos más que el mes pasado». Solo si la API da la variación o la nota anterior. */
function Variacion({ datos }: { datos: Sentiment }) {
  const d = datos.delta ?? (datos.score !== null && datos.previous_score !== null ? datos.score - datos.previous_score : null);
  if (d === null) return null;
  const n = Math.round(Math.abs(d));
  if (n === 0) return <p className="variacion">Igual que el mes pasado</p>;
  return (
    <p className="variacion" data-baja={d < 0}>
      {n} {n === 1 ? "punto" : "puntos"} {d > 0 ? "más" : "menos"} que el mes pasado
    </p>
  );
}

/** Línea de tendencia mínima con las notas de los últimos meses. Sin ejes: las cifras van en el texto alternativo. */
function Tendencia({ historia }: { historia: Sentiment["history"] }) {
  if (historia.length < 2) return null;
  const primero = historia[0];
  const ultimo = historia[historia.length - 1];
  if (!primero || !ultimo) return null;

  const ancho = 320, alto = 64, margen = 6;
  const paso = (ancho - 2 * margen) / (historia.length - 1);
  // La escala se ajusta al rango de las notas, con algo de aire, para que el cambio se vea.
  const notas = historia.map((h) => h.score);
  const bajo = Math.max(0, Math.min(...notas) - 8);
  const tope = Math.min(100, Math.max(...notas) + 8);
  const rango = Math.max(1, tope - bajo);
  const y = (nota: number) => alto - margen - ((Math.min(tope, Math.max(bajo, nota)) - bajo) / rango) * (alto - 2 * margen);
  const puntos = historia.map((h, i) => `${(margen + i * paso).toFixed(1)},${y(h.score).toFixed(1)}`).join(" ");
  const descripcion = `Evolución de la nota: de ${Math.round(primero.score)} en ${mes(primero.month, true)} a ${Math.round(ultimo.score)} en ${mes(ultimo.month, true)}.`;

  return (
    <figure className="tendencia">
      <svg viewBox={`0 0 ${ancho} ${alto}`} role="img" aria-label={descripcion} preserveAspectRatio="none">
        <polyline points={puntos} fill="none" stroke="currentColor" strokeWidth="3" strokeLinejoin="miter" vectorEffect="non-scaling-stroke" />
        <circle cx={margen + (historia.length - 1) * paso} cy={y(ultimo.score)} r="5" className="punto" />
      </svg>
      <figcaption className="sec">
        {mes(primero.month)} → {mes(ultimo.month)}
      </figcaption>
    </figure>
  );
}

function Temas({ titulo, temas, queja = false }: { titulo: string; temas: Sentiment["praised"]; queja?: boolean }) {
  return (
    <div>
      <h3 className="rotulo">{titulo}</h3>
      {temas.length === 0 ? (
        <p className="sec">{queja ? "Ninguna queja se repite." : "Nada destaca todavía."}</p>
      ) : (
        <ul className="temas" data-queja={queja}>
          {temas.map((t) => (
            <li key={t.topic}>
              <span>{t.topic}</span>
              <span className="sec">{veces(t.mentions)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
