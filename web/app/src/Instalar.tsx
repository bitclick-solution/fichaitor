import { useEffect, useState } from "react";
import { Seccion } from "./movimiento";

// «En tu móvil»: instalar la aplicación en la pantalla de inicio.
//
// Sección propia, no dentro de «Avisos»: instalar no es un canal, y así no depende de que la API de
// canales responda. Solo se muestra en móviles donde todavía no está instalada (en el ordenador o
// dentro de la app instalada sería ruido; brand/DESIGN.md, apartado 12).
//
// Chrome, Edge y Samsung Internet emiten «beforeinstallprompt» cuando la app cumple los requisitos
// (manifiesto, HTTPS y algo de uso). Puede llegar antes de que la sección se pinte, así que se escucha
// desde que carga el módulo. No se llama a preventDefault(): el navegador conserva su propia barra de
// instalación y el botón es un camino más. iOS no emite el evento: allí se instala desde Compartir.

type AvisoInstalacion = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: "accepted" | "dismissed" }> };

const CLAVE = "fichaitor_instalada";

let avisoGuardado: AvisoInstalacion | null = null;
const oyentes = new Set<() => void>();
const avisar = () => oyentes.forEach((f) => f());

/** Una marca local, sin datos personales: el navegador no avisa al volver a una pestaña normal. */
function recordarInstalada(): void {
  try {
    localStorage.setItem(CLAVE, "si");
  } catch {
    /* sin almacenamiento: solo se pierde el recuerdo */
  }
}
function constaInstalada(): boolean {
  try {
    return localStorage.getItem(CLAVE) === "si";
  } catch {
    return false;
  }
}

window.addEventListener("beforeinstallprompt", (e) => {
  avisoGuardado = e as AvisoInstalacion;
  avisar();
});
window.addEventListener("appinstalled", () => {
  avisoGuardado = null;
  recordarInstalada();
  avisar();
});

const modoApp = window.matchMedia("(display-mode: standalone)");
const enApp = () => modoApp.matches || (navigator as Navigator & { standalone?: boolean }).standalone === true;
// iPadOS se presenta como un Mac; se distingue por la pantalla táctil.
const esIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const esMovil = () => esIOS() || window.matchMedia("(pointer: coarse)").matches;

type Instalacion = "oculta" | "lista" | "ios" | "menu";

function estadoInstalacion(): Instalacion {
  if (enApp()) {
    recordarInstalada();
    return "oculta";
  }
  if (constaInstalada() || !esMovil()) return "oculta";
  // El botón solo en móvil: en el ordenador, Chrome también ofrece el aviso, pero instalaría en el PC.
  if (avisoGuardado) return "lista";
  return esIOS() ? "ios" : "menu";
}

export function Instalar() {
  const [estado, setEstado] = useState<Instalacion>(estadoInstalacion);
  const [abriendo, setAbriendo] = useState(false);

  useEffect(() => {
    const actualizar = () => setEstado(estadoInstalacion());
    oyentes.add(actualizar);
    modoApp.addEventListener("change", actualizar);
    actualizar();
    return () => {
      oyentes.delete(actualizar);
      modoApp.removeEventListener("change", actualizar);
    };
  }, []);

  const instalar = async () => {
    const aviso = avisoGuardado;
    if (!aviso) return;
    setAbriendo(true);
    try {
      await aviso.prompt();
      const { outcome } = await aviso.userChoice;
      if (outcome === "accepted") recordarInstalada();
    } catch {
      /* el aviso ya se usó o el navegador lo rechazó: queda el camino del menú */
    } finally {
      // El aviso solo se puede lanzar una vez.
      avisoGuardado = null;
      setAbriendo(false);
      avisar();
    }
  };

  if (estado === "oculta") return null;

  return (
    <Seccion id="movil" className="movil">
      <h2 className="rotulo">En tu móvil</h2>
      <p className="sec">Abre tu ficha desde la pantalla de inicio, como una app. Es gratis.</p>
      {estado === "lista" && (
        <div className="acciones">
          <button className="btn sec" type="button" disabled={abriendo} onClick={() => void instalar()}>
            Instalar Fichaitor
          </button>
        </div>
      )}
      {estado === "ios" && <p>Pulsa Compartir y luego «Añadir a pantalla de inicio».</p>}
      {estado === "menu" && <p>En el menú del navegador, elige «Instalar aplicación» o «Añadir a pantalla de inicio».</p>}
    </Seccion>
  );
}
