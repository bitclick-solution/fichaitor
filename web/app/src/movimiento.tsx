// Movimiento del panel. Reglas en brand/DESIGN.md, apartado 7: firme y mecánico, sin rebotes.
// Con «movimiento reducido» en el sistema, todo se muestra completo y estático.
import { useEffect, useRef, useState, type ReactNode } from "react";
import Lenis from "lenis";

export const REDUCIDO = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

let lenis: Lenis | null = null;

/** Scroll suave, como en la portada. Se arranca una vez, al cargar la aplicación. */
export function iniciarScrollSuave(): void {
  if (REDUCIDO || lenis) return;
  lenis = new Lenis({ lerp: 0.09 });
  const paso = (t: number) => {
    lenis?.raf(t);
    requestAnimationFrame(paso);
  };
  requestAnimationFrame(paso);
}

/** Desplaza hasta una sección, dejando sitio a la barra fija. */
/** Distancia entre el borde superior de la ventana y el de una sección tras ir a ella (la cabecera fija). */
export const ALTO_CABECERA = 76;

/**
 * Salto sin animación. Lenis ignora un scrollTo cuyo destino coincide con el del desplazamiento en curso,
 * aunque no haya llegado, y puede tener una idea vieja de dónde está la página. Parar y arrancar lo
 * resincroniza con la posición real (y cancela lo pendiente); después, el salto.
 */
function saltar(y: number): void {
  if (!lenis) {
    scrollTo({ top: y, behavior: "auto" });
    return;
  }
  const estabaParado = lenis.isStopped;
  // La altura de la página puede haber cambiado (secciones que terminan de cargar): sin esto,
  // Lenis recortaría el salto a la altura que conocía.
  lenis.resize();
  lenis.stop();
  lenis.start();
  lenis.scrollTo(y, { immediate: true, force: true });
  if (estabaParado) lenis.stop();
}

export function irA(id: string, inmediato = false): void {
  const destino = document.getElementById(id);
  if (!destino) return;
  const y = destino.getBoundingClientRect().top + scrollY - ALTO_CABECERA;
  if (inmediato) saltar(y);
  else if (lenis) lenis.scrollTo(y, { duration: 1.1 });
  else scrollTo({ top: y, behavior: REDUCIDO ? "auto" : "smooth" });
}

/** Detiene el scroll de la página mientras hay una ventana modal. Devuelve la función que lo reanuda. */
export function pausarScroll(): () => void {
  lenis?.stop();
  const antes = document.documentElement.style.overflow;
  document.documentElement.style.overflow = "hidden";
  return () => {
    document.documentElement.style.overflow = antes;
    lenis?.start();
  };
}

/** Sube al principio de la página: el nombre del negocio y su nota. */
export function irArriba(inmediato = false): void {
  if (inmediato) saltar(0);
  else if (lenis) lenis.scrollTo(0, { duration: 1.1 });
  else scrollTo({ top: 0, behavior: REDUCIDO ? "auto" : "smooth" });
}

/** true desde que el elemento entra en pantalla por primera vez. */
// Sin margen: basta con que asome un píxel. Con margen negativo, en pantallas bajas
// quedaba un hueco en blanco al pie hasta que se hacía scroll.
export function useEnVista<T extends Element>(margen = "0px") {
  const ref = useRef<T>(null);
  const [visto, setVisto] = useState(REDUCIDO);

  useEffect(() => {
    const el = ref.current;
    if (!el || visto) return;
    if (typeof IntersectionObserver !== "function") {
      setVisto(true);
      return;
    }
    const io = new IntersectionObserver(
      (entradas) => {
        if (entradas.some((e) => e.isIntersecting)) {
          setVisto(true);
          io.disconnect();
        }
      },
      { rootMargin: margen },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visto, margen]);

  return { ref, visto };
}

/** Número que cuenta desde 0 hasta su valor cuando entra en pantalla. */
export function Cuenta({ valor, decimales = 0, duracion = 1100 }: { valor: number; decimales?: number; duracion?: number }) {
  const { ref, visto } = useEnVista<HTMLSpanElement>();
  const [actual, setActual] = useState(REDUCIDO ? valor : 0);

  useEffect(() => {
    if (!visto) return;
    if (REDUCIDO || valor === 0) {
      setActual(valor);
      return;
    }
    let cuadro = 0;
    const inicio = performance.now();
    const paso = (t: number) => {
      const p = Math.min(1, (t - inicio) / duracion);
      setActual(valor * (1 - Math.pow(1 - p, 3)));
      if (p < 1) cuadro = requestAnimationFrame(paso);
    };
    cuadro = requestAnimationFrame(paso);
    return () => cancelAnimationFrame(cuadro);
  }, [visto, valor, duracion]);

  const formato = new Intl.NumberFormat("es-ES", { minimumFractionDigits: decimales, maximumFractionDigits: decimales });
  return (
    <span ref={ref} className="cuenta">
      {/* El lector de pantalla recibe el valor final, no el recuento. */}
      <span aria-hidden="true">{formato.format(actual)}</span>
      <span className="sr">{formato.format(valor)}</span>
    </span>
  );
}

/** Bloque del panel que entra desde abajo la primera vez que se ve. */
export function Seccion({ id, className = "", children }: { id?: string; className?: string; children: ReactNode }) {
  const { ref, visto } = useEnVista<HTMLDivElement>();
  return (
    <div ref={ref} id={id} className={`bloque seccion ${className}`.trim()} data-visto={visto}>
      {children}
    </div>
  );
}

/**
 * Valoración real de la ficha sobre 5, como el sello de estrellas de la portada:
 * las cinco estrellas saltan una a una y después se rellenan hasta la media.
 */
export function SelloEstrellas({ media, total }: { media: number; total: number | null }) {
  const { ref, visto } = useEnVista<HTMLDivElement>("0px");
  const v = Math.min(5, Math.max(0, media));
  const texto = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(v);
  const estrella = "M12 1.6l3.2 6.6 7.2 1-5.2 5.1 1.2 7.2L12 18.1l-6.4 3.4 1.2-7.2L1.6 9.2l7.2-1z";
  const fila = (clase: string) => (
    <span className={clase} aria-hidden="true">
      {[0, 1, 2, 3, 4].map((i) => (
        <svg key={i} viewBox="0 0 24 24" style={{ animationDelay: `${0.15 + i * 0.09}s` }}>
          <path d={estrella} />
        </svg>
      ))}
    </span>
  );

  return (
    <div ref={ref} className="sello" data-visto={visto} role="img" aria-label={`${texto} de 5 estrellas${total !== null ? `, con ${total} ${total === 1 ? "reseña" : "reseñas"}` : ""}`}>
      <span className="sello-estrellas">
        {fila("base")}
        <span className="llenas" style={{ width: visto ? `${(v / 5) * 100}%` : "0%" }}>
          {fila("fila")}
        </span>
      </span>
      <span className="sello-media" aria-hidden="true">
        <Cuenta valor={v} decimales={1} duracion={1400} />
      </span>
    </div>
  );
}
