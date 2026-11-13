import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { irA, irArriba } from "./movimiento";

// Índice lateral del panel, como el de un documento: va en el margen, no encima del contenido
// (brand/DESIGN.md, apartado 12, «Ruido»). Solo aparece cuando hay margen donde ponerlo.

export interface EntradaIndice {
  id: string;
  nombre: string;
  /** Si es true, al pulsar se sube al principio del panel (el nombre del negocio), no al inicio de la sección. */
  arriba?: boolean;
}

export function Indice({ secciones }: { secciones: EntradaIndice[] }) {
  const [presentes, setPresentes] = useState<string[]>([]);
  const [activa, setActiva] = useState<string | null>(null);
  // La sección pulsada en el índice. Las últimas son cortas y el scroll no llega a subirlas hasta la
  // línea: mientras se vea, se marca la pulsada. Se olvida en cuanto el usuario desplaza por su cuenta.
  const pedida = useRef<string | null>(null);

  useEffect(() => {
    let cuadro = 0;

    const medir = () => {
      cuadro = 0;
      const linea = innerHeight * 0.35; // la sección activa es la última cuyo inicio ha pasado esta línea
      const vistas: string[] = [];
      let actual: string | null = null;
      let ultimaVisible: string | null = null;
      let pedidaVisible = false;
      for (const s of secciones) {
        const el = document.getElementById(s.id);
        if (!el) continue;
        vistas.push(s.id);
        const caja = el.getBoundingClientRect();
        const seVe = caja.top < innerHeight && caja.bottom > 0;
        if (caja.top <= linea) actual = s.id;
        if (seVe) ultimaVisible = s.id;
        if (seVe && s.id === pedida.current) pedidaVisible = true;
      }
      // Al final de la página, la última sección que se ve, aunque su inicio no llegue a la línea.
      const alFinal = scrollY + innerHeight >= document.documentElement.scrollHeight - 2;
      if (pedidaVisible) actual = pedida.current;
      else if (alFinal && ultimaVisible) actual = ultimaVisible;
      setPresentes((antes) => (antes.join() === vistas.join() ? antes : vistas));
      setActiva(actual ?? vistas[0] ?? null);
    };
    const porSuCuenta = () => {
      pedida.current = null;
    };
    const pedir = () => {
      if (!cuadro) cuadro = requestAnimationFrame(medir);
    };

    medir();
    addEventListener("scroll", pedir, { passive: true });
    addEventListener("resize", pedir);
    addEventListener("wheel", porSuCuenta, { passive: true });
    addEventListener("touchmove", porSuCuenta, { passive: true });
    addEventListener("keydown", porSuCuenta);
    // Las secciones aparecen según llegan sus datos: se vuelve a mirar de vez en cuando.
    const reloj = setInterval(pedir, 800);
    return () => {
      removeEventListener("scroll", pedir);
      removeEventListener("resize", pedir);
      removeEventListener("wheel", porSuCuenta);
      removeEventListener("touchmove", porSuCuenta);
      removeEventListener("keydown", porSuCuenta);
      clearInterval(reloj);
      if (cuadro) cancelAnimationFrame(cuadro);
    };
  }, [secciones]);

  const visibles = secciones.filter((s) => presentes.includes(s.id));
  if (visibles.length < 2) return null;

  // Se pinta en <body>: dentro del panel, la animación de entrada (con transform) lo ataría
  // a ese bloque y dejaría de estar fijo respecto a la ventana.
  return createPortal(
    <nav className="indice" aria-label="Secciones del panel">
      <ol>
        {visibles.map((s) => (
          <li key={s.id}>
            <button
              type="button"
              aria-current={activa === s.id ? "true" : undefined}
              onClick={() => {
                pedida.current = s.id;
                setActiva(s.id);
                if (s.arriba) irArriba();
                else irA(s.id);
              }}
            >
              {s.nombre}
            </button>
          </li>
        ))}
      </ol>
    </nav>,
    document.body,
  );
}
