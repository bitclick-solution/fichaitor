import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { irA, irArriba, pausarScroll, REDUCIDO } from "./movimiento";

// Recorrido de la primera visita: cinco paradas que señalan las partes del panel.
// Se muestra una vez por navegador (una marca en localStorage, sin datos personales) y se puede
// repetir desde Ajustes. Solo se detiene en las secciones que existen.

interface Parada {
  /** Selector del elemento que se señala. */
  donde: string;
  /** Sección a la que se desplaza (o «arriba»). */
  ir: string;
  titulo: string;
  texto: string;
}

const PARADAS: Parada[] = [
  {
    donde: ".panel-cab",
    ir: "arriba",
    titulo: "La nota de tu ficha",
    texto: "Mide lo completa y cuidada que está tu ficha de Google. Más abajo verás qué la sube.",
  },
  {
    donde: "#resumen",
    ir: "resumen",
    titulo: "Lo que pide tu atención",
    texto: "Reseñas sin responder, festivos sin horario y lo que Google ha cambiado en tu ficha sin avisarte.",
  },
  {
    donde: "#opinion",
    ir: "opinion",
    titulo: "Qué dicen de tu negocio",
    texto: "Un resumen de tus reseñas: lo que más gusta y las quejas que se repiten.",
  },
  {
    donde: "#resenas",
    ir: "resenas",
    titulo: "Responde en un minuto",
    texto: "Fichaitor te redacta la respuesta. La lees, la cambias si quieres y la publicas tú.",
  },
  {
    donde: "#avisos",
    ir: "avisos",
    titulo: "Que te avise Fichaitor",
    texto: "Conecta Telegram y te escribirá cuando haya algo que decidir. Nunca cambia nada sin tu confirmación.",
  },
];

const CLAVE = "fichaitor_recorrido";
export const EVENTO_RECORRIDO = "fichaitor:recorrido";

function yaVisto(): boolean {
  try {
    return localStorage.getItem(CLAVE) === "visto";
  } catch {
    return false;
  }
}

function marcarVisto(): void {
  try {
    localStorage.setItem(CLAVE, "visto");
  } catch {
    /* sin almacenamiento: se volverá a mostrar, sin más */
  }
}

export function Tour() {
  const [paradas, setParadas] = useState<Parada[] | null>(null);
  const [i, setI] = useState(0);

  const empezar = useCallback(() => {
    const presentes = PARADAS.filter((p) => document.querySelector(p.donde));
    if (presentes.length > 0) {
      setI(0);
      setParadas(presentes);
    }
  }, []);

  // Primera visita: espera a que el panel tenga sus secciones y arranca.
  useEffect(() => {
    if (yaVisto()) return;
    let vueltas = 0;
    const reloj = setInterval(() => {
      vueltas += 1;
      if (document.querySelector("#resumen") || vueltas > 40) {
        clearInterval(reloj);
        if (document.querySelector("#resumen")) setTimeout(empezar, 700);
      }
    }, 500);
    return () => clearInterval(reloj);
  }, [empezar]);

  // Repetirlo desde Ajustes.
  useEffect(() => {
    addEventListener(EVENTO_RECORRIDO, empezar);
    return () => removeEventListener(EVENTO_RECORRIDO, empezar);
  }, [empezar]);

  const terminar = useCallback(() => {
    marcarVisto();
    setParadas(null);
  }, []);

  if (!paradas) return null;
  const parada = paradas[i];
  if (!parada) return null;
  return (
    <Foco
      key={parada.donde}
      parada={parada}
      n={i + 1}
      total={paradas.length}
      onSiguiente={() => (i + 1 < paradas.length ? setI(i + 1) : terminar())}
      onAnterior={i > 0 ? () => setI(i - 1) : undefined}
      onSaltar={terminar}
    />
  );
}

function Foco({
  parada,
  n,
  total,
  onSiguiente,
  onAnterior,
  onSaltar,
}: {
  parada: Parada;
  n: number;
  total: number;
  onSiguiente: () => void;
  onAnterior?: (() => void) | undefined;
  onSaltar: () => void;
}) {
  const [rect, setRect] = useState<DOMRect | null>(null);
  const tarjeta = useRef<HTMLDivElement>(null);
  const ultima = n === total;

  // Lleva la sección a la vista y, mientras dura la parada, sigue su posición.
  useEffect(() => {
    if (parada.ir === "arriba") irArriba();
    else irA(parada.ir);
    let cuadro = 0;
    const medir = () => {
      const el = document.querySelector(parada.donde);
      if (el) setRect(el.getBoundingClientRect());
      cuadro = requestAnimationFrame(medir);
    };
    cuadro = requestAnimationFrame(medir);
    // Tras el desplazamiento se bloquea el scroll para que el foco no se pierda.
    let reanudar: (() => void) | null = null;
    const bloqueo = setTimeout(() => (reanudar = pausarScroll()), REDUCIDO ? 0 : 1150);
    return () => {
      cancelAnimationFrame(cuadro);
      clearTimeout(bloqueo);
      reanudar?.();
    };
  }, [parada]);

  useLayoutEffect(() => {
    tarjeta.current?.focus();
  }, [parada]);

  useEffect(() => {
    const tecla = (e: KeyboardEvent) => {
      if (e.key === "Escape") onSaltar();
      if (e.key === "ArrowRight") onSiguiente();
      if (e.key === "ArrowLeft") onAnterior?.();
    };
    addEventListener("keydown", tecla);
    return () => removeEventListener("keydown", tecla);
  }, [onSiguiente, onAnterior, onSaltar]);

  // El foco recorta la sección (limitado a la ventana); la tarjeta va debajo si cabe, si no encima.
  const margen = 10;
  const arriba = rect ? Math.max(8, rect.top - margen) : 0;
  const abajo = rect ? Math.min(innerHeight - 8, rect.bottom + margen) : 0;
  const hueco = rect
    ? { top: arriba, left: Math.max(8, rect.left - margen), width: Math.min(innerWidth - 16, rect.width + 2 * margen), height: Math.max(0, abajo - arriba) }
    : null;
  const debajo = hueco ? innerHeight - (hueco.top + hueco.height) > 230 : true;

  return createPortal(
    <div className="tour" aria-live="polite">
      {hueco && <div className="tour-foco" style={{ top: hueco.top, left: hueco.left, width: hueco.width, height: hueco.height }} />}
      <div
        className="tour-tarjeta"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tour-titulo"
        aria-describedby="tour-texto"
        tabIndex={-1}
        ref={tarjeta}
        data-lenis-prevent
        style={
          hueco && innerWidth > 760
            ? debajo
              ? { top: hueco.top + hueco.height + 16, left: Math.min(hueco.left, innerWidth - 436) }
              : { top: Math.max(16, hueco.top - 16 - 220), left: Math.min(hueco.left, innerWidth - 436) }
            : undefined
        }
      >
        <p className="sec">
          {n} de {total}
        </p>
        <h2 className="rotulo" id="tour-titulo">
          {parada.titulo}
        </h2>
        <p id="tour-texto">{parada.texto}</p>
        <div className="acciones">
          <button className="btn" type="button" onClick={onSiguiente}>
            {ultima ? "Empezar" : "Siguiente"}
          </button>
          {onAnterior && (
            <button className="btn sec" type="button" onClick={onAnterior}>
              Anterior
            </button>
          )}
          {!ultima && (
            <button className="enlace" type="button" onClick={onSaltar}>
              Saltar el recorrido
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
