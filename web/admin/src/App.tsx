import { useCallback, useEffect, useRef, useState } from "react";
import { api, ErrorAdmin, olvidarCsrf, siHaceFaltaCodigo, siSePierdeLaSesion, type Rol, type Sesion } from "./api";
import { Cargando, DialogoCodigo } from "./comun";
import { Cuentas } from "./Cuentas";
import { Alta, Entrar, Marca, nombreRol, Reenroll } from "./Entrar";
import { Espera, Metricas, Salud } from "./Paneles";
import { Revision } from "./Revision";
import { MiCuenta, Personas } from "./Personas";

// Backoffice de Fichaitor (docs/contrato-backoffice.md). Herramienta interna: solo por el túnel, en
// http://localhost:8090. Cada pantalla se enseña solo al rol que la puede usar (apartado 1.2); la base
// lo vuelve a comprobar en cada petición.

type Vista =
  | { n: "cargando" }
  | { n: "entrar"; aviso?: string | undefined }
  | { n: "alta"; token: string }
  | { n: "reenroll" }
  | { n: "dentro"; sesion: Sesion }
  | { n: "fuera-de-origen" };

type Pantalla = "cuentas" | "revision" | "salud" | "metricas" | "espera" | "personas" | "mi-cuenta";

const PANTALLAS: { id: Pantalla; nombre: string; roles: Rol[] }[] = [
  { id: "cuentas", nombre: "Cuentas", roles: ["soporte", "finanzas", "propietario"] },
  { id: "revision", nombre: "Cobro: revisión", roles: ["finanzas", "propietario"] },
  { id: "salud", nombre: "Salud", roles: ["soporte", "propietario"] },
  { id: "metricas", nombre: "Métricas", roles: ["finanzas", "propietario"] },
  { id: "espera", nombre: "Lista de espera", roles: ["propietario"] },
  { id: "personas", nombre: "Personas y auditoría", roles: ["propietario"] },
  { id: "mi-cuenta", nombre: "Mi cuenta", roles: ["soporte", "finanzas", "propietario"] },
];

/** Lee #alta=<token> una vez y lo quita de la barra de direcciones (y del historial). */
function leerAlta(): string | null {
  const f = new URLSearchParams(location.hash.replace(/^#/, ""));
  const token = f.get("alta");
  if (token !== null) history.replaceState(null, "", location.pathname + location.search);
  return token;
}

export function App() {
  const [vista, setVista] = useState<Vista>({ n: "cargando" });
  const [pantalla, setPantalla] = useState<Pantalla>("cuentas");
  const tokenAlta = useRef<string | null | undefined>(undefined);
  // Diálogo del código para una acción sensible: la promesa que espera la respuesta.
  const [pidiendo, setPidiendo] = useState<{ resolver: (c: string | null) => void; fallo: boolean; vez: number } | null>(null);
  const pendiente = useRef<((c: string | null) => void) | null>(null);
  /** Cancela la confirmación pendiente (al perder la sesión o salir): la acción no se repite después. */
  const cancelarCodigo = useCallback(() => {
    pendiente.current?.(null);
    pendiente.current = null;
    setPidiendo(null);
  }, []);

  const comprobar = useCallback(async () => {
    try {
      const s = await api.sesion();
      setVista(s.stage === "reenroll" ? { n: "reenroll" } : { n: "dentro", sesion: s });
    } catch (e) {
      if (e instanceof ErrorAdmin && e.codigo === "host_not_allowed") setVista({ n: "fuera-de-origen" });
      else setVista({ n: "entrar" });
    }
  }, []);

  useEffect(() => {
    siSePierdeLaSesion(() => {
      cancelarCodigo();
      setVista({ n: "entrar", aviso: "Tu sesión ha caducado. Vuelve a entrar." });
    });
    let vez = 0;
    siHaceFaltaCodigo(
      (fallo) =>
        new Promise<string | null>((resolver) => {
          pendiente.current = resolver;
          setPidiendo({ resolver, fallo, vez: ++vez });
        }),
    );
    if (tokenAlta.current === undefined) tokenAlta.current = leerAlta();
    if (tokenAlta.current) setVista({ n: "alta", token: tokenAlta.current });
    else void comprobar();

    // Un enlace de alta pegado en una pestaña ya abierta solo cambia el fragmento: también se atiende.
    const alCambiarFragmento = () => {
      const token = leerAlta();
      if (token) {
        cancelarCodigo();
        tokenAlta.current = token;
        setVista({ n: "alta", token });
      }
    };
    addEventListener("hashchange", alCambiarFragmento);
    return () => removeEventListener("hashchange", alCambiarFragmento);
  }, [comprobar, cancelarCodigo]);

  const salir = async () => {
    try {
      await api.salir();
    } catch {
      /* se sale igual */
    }
    cancelarCodigo();
    olvidarCsrf();
    setVista({ n: "entrar" });
  };

  const responderCodigo = (c: string | null) => {
    pidiendo?.resolver(c);
    pendiente.current = null;
    setPidiendo(null);
  };

  if (vista.n === "cargando") return <Cargando texto="Abriendo el backoffice…" />;
  if (vista.n === "fuera-de-origen")
    return (
      <main className="puerta">
        <div className="caja">
          <h1>Entra por el túnel</h1>
          <p>El backoffice solo responde en http://localhost:8090 o http://127.0.0.1:8090, a través del túnel SSH.</p>
        </div>
      </main>
    );
  if (vista.n === "alta")
    return (
      <Alta
        token={vista.token}
        onTerminar={() => {
          tokenAlta.current = null;
          setVista({ n: "entrar" });
        }}
      />
    );
  if (vista.n === "entrar") return <Entrar aviso={vista.aviso} onDentro={(s) => setVista({ n: "dentro", sesion: s })} onReenroll={() => setVista({ n: "reenroll" })} />;
  if (vista.n === "reenroll") return <Reenroll onHecho={() => void comprobar()} onSalir={() => void salir()} />;

  const { sesion } = vista;
  const visibles = PANTALLAS.filter((p) => p.roles.includes(sesion.role));
  const actual = visibles.some((p) => p.id === pantalla) ? pantalla : "cuentas";

  return (
    <>
      <header className="cabecera">
        <Marca />
        <div className="quien">
          <span>
            <strong>{sesion.username}</strong> <span className="rol">{nombreRol(sesion.role)}</span>
          </span>
          <button className="btn sec" type="button" onClick={() => void salir()}>
            Salir
          </button>
        </div>
      </header>
      <div className="marco">
        <nav className="menu" aria-label="Secciones">
          <ul>
            {visibles.map((p) => (
              <li key={p.id}>
                <button type="button" aria-current={actual === p.id ? "page" : undefined} onClick={() => setPantalla(p.id)}>
                  {p.nombre}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <main className="contenido">
          {actual === "cuentas" && <Cuentas rol={sesion.role} />}
          {actual === "revision" && <Revision rol={sesion.role} />}
          {actual === "salud" && <Salud />}
          {actual === "metricas" && <Metricas />}
          {actual === "espera" && <Espera />}
          {actual === "personas" && <Personas />}
          {actual === "mi-cuenta" && <MiCuenta sesion={sesion} />}
        </main>
      </div>
      {pidiendo && <DialogoCodigo key={pidiendo.vez} fallo={pidiendo.fallo} onCodigo={(c) => responderCodigo(c)} onCancelar={() => responderCodigo(null)} />}
    </>
  );
}
