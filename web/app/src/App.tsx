import { useCallback, useEffect, useRef, useState } from "react";
import logo from "../../../brand/logo.svg";
import { api, ApiError, OAUTH_START, type Diagnosis, type Location, type Me } from "./api";
import { Resumen } from "./Resumen";

type Vista =
  | { tipo: "cargando" }
  | { tipo: "conectar"; aviso?: string }
  | { tipo: "pendiente" }
  | { tipo: "sin-fichas"; email: string }
  | { tipo: "elegir"; me: Me }
  | { tipo: "panel"; me: Me; ficha: Location }
  | { tipo: "error"; mensaje: string };

/** Mensajes para la vuelta de Google: /app?google=... (contrato web-API). */
const AVISOS: Record<string, string> = {
  cancelado: "Has cancelado la conexión con Google. No se ha cambiado nada.",
  sin_permiso:
    "Falta el permiso para gestionar tu ficha. En la pantalla de Google, marca la casilla de Google Business Profile: sin ella Fichaitor no puede ver tu ficha.",
  error: "No hemos podido conectar con Google. Prueba de nuevo.",
};

/** Lee ?google= y ?fichas= una vez y los quita de la barra de direcciones. */
function leerVuelta(): { google: string | null; fichas: string | null } {
  const q = new URLSearchParams(location.search);
  const vuelta = { google: q.get("google"), fichas: q.get("fichas") };
  if (vuelta.google !== null || vuelta.fichas !== null) {
    q.delete("google");
    q.delete("fichas");
    const resto = q.toString();
    history.replaceState(null, "", location.pathname + (resto ? `?${resto}` : ""));
  }
  return vuelta;
}

function vistaDe(me: Me, fichasPendientes: boolean): Vista {
  if (me.locations.length === 0) return fichasPendientes ? { tipo: "pendiente" } : { tipo: "sin-fichas", email: me.email };
  const ficha = me.locations.find((l) => l.id === me.selected_location_id);
  return ficha ? { tipo: "panel", me, ficha } : { tipo: "elegir", me };
}

export function App() {
  const [vista, setVista] = useState<Vista>({ tipo: "cargando" });
  const vuelta = useRef<ReturnType<typeof leerVuelta> | null>(null);

  const cargar = useCallback(async () => {
    setVista({ tipo: "cargando" });
    try {
      const me = await api.me();
      setVista(vistaDe(me, vuelta.current?.fichas === "pendiente"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setVista({ tipo: "conectar" });
      else setVista({ tipo: "error", mensaje: e instanceof Error ? e.message : "Algo ha fallado." });
    }
  }, []);

  useEffect(() => {
    if (vuelta.current === null) vuelta.current = leerVuelta();
    const aviso = vuelta.current.google ? AVISOS[vuelta.current.google] : undefined;
    if (aviso) setVista({ tipo: "conectar", aviso });
    else void cargar();
  }, [cargar]);

  const salir = useCallback(async () => {
    try {
      await api.salir();
    } finally {
      location.assign("/");
    }
  }, []);

  const conSesion = vista.tipo !== "cargando" && vista.tipo !== "conectar" && vista.tipo !== "error";

  return (
    <>
      <header className="barra">
        <a className="marca" href="/" aria-label="Fichaitor, inicio">
          <img src={logo} alt="" width={36} height={36} />
          <span>Fichaitor</span>
        </a>
        {conSesion && (
          <button className="enlace" type="button" onClick={() => void salir()}>
            Salir
          </button>
        )}
      </header>

      <main className="caja">
        {vista.tipo === "cargando" && <Cargando texto="Abriendo tu ficha" />}
        {vista.tipo === "conectar" && <Conectar {...(vista.aviso ? { aviso: vista.aviso } : {})} />}
        {vista.tipo === "pendiente" && <Pendiente onReintentar={() => void cargar()} />}
        {vista.tipo === "sin-fichas" && <SinFichas email={vista.email} />}
        {vista.tipo === "elegir" && <Elegir me={vista.me} onElegida={(me) => setVista(vistaDe(me, false))} />}
        {vista.tipo === "panel" && (
          <Panel
            ficha={vista.ficha}
            variasFichas={vista.me.locations.length > 1}
            onCambiar={() => setVista({ tipo: "elegir", me: vista.me })}
            onSinSesion={() => setVista({ tipo: "conectar" })}
          />
        )}
        {vista.tipo === "error" && (
          <section className="pantalla">
            <h1 className="cartel">
              Algo ha fallado<Punto />
            </h1>
            <p className="lead">{vista.mensaje}</p>
            <div className="acciones">
              <button className="btn" type="button" onClick={() => void cargar()}>
                Reintentar <Flecha />
              </button>
            </div>
          </section>
        )}
      </main>

      <footer className="pie caja">
        <a href="/privacidad/">Política de Privacidad</a>
        <a href="/condiciones-de-uso/">Condiciones de uso</a>
        <a href="mailto:soporte@fichaitor.es">soporte@fichaitor.es</a>
      </footer>
    </>
  );
}

const Punto = () => <span className="pt">.</span>;
const Flecha = () => (
  <span className="fl" aria-hidden="true">
    →
  </span>
);

function Cargando({ texto }: { texto: string }) {
  return (
    <section className="pantalla" role="status" aria-live="polite">
      <p className="cargando">
        <span className="ojo" aria-hidden="true" />
        {texto}…
      </p>
    </section>
  );
}

function Conectar({ aviso }: { aviso?: string }) {
  return (
    <section className="pantalla">
      {aviso && (
        <p className="aviso" role="alert">
          {aviso}
        </p>
      )}
      <h1 className="cartel">
        Conecta tu ficha<Punto />
      </h1>
      <p className="lead">
        Inicia sesión con la cuenta de Google que administra tu ficha. Fichaitor pide un único permiso: ver y gestionar tu ficha de
        Google Business Profile.
      </p>
      <div className="acciones">
        <a className="btn" href={OAUTH_START}>
          Conectar con Google <Flecha />
        </a>
        <a className="btn sec" href="/">
          Volver al inicio
        </a>
      </div>
      <ul className="lista-puntos">
        <li>No accede a tu correo, tus contactos, tu calendario ni tus archivos.</li>
        <li>Nunca cambia nada en tu ficha sin tu confirmación.</li>
        <li>Puedes retirar el acceso cuando quieras, desde aquí o desde tu cuenta de Google.</li>
      </ul>
    </section>
  );
}

function Pendiente({ onReintentar }: { onReintentar: () => void }) {
  return (
    <section className="pantalla">
      <h1 className="cartel">
        Conectado<Punto />
      </h1>
      <p className="lead">Tu cuenta de Google ya está conectada, pero todavía no hemos podido leer tus fichas. Vuelve a intentarlo en unos minutos.</p>
      <div className="acciones">
        <button className="btn" type="button" onClick={onReintentar}>
          Reintentar <Flecha />
        </button>
      </div>
    </section>
  );
}

function SinFichas({ email }: { email: string }) {
  return (
    <section className="pantalla">
      <h1 className="cartel">
        Sin fichas<Punto />
      </h1>
      <p className="lead">
        La cuenta <strong>{email}</strong> no administra ninguna ficha de Google Business Profile. Conecta la cuenta con la que gestionas tu
        negocio en Google.
      </p>
      <div className="acciones">
        <a className="btn" href={OAUTH_START}>
          Conectar otra cuenta <Flecha />
        </a>
      </div>
    </section>
  );
}

function Elegir({ me, onElegida }: { me: Me; onElegida: (me: Me) => void }) {
  const [enviando, setEnviando] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const elegir = async (id: string) => {
    setEnviando(id);
    setError(null);
    try {
      onElegida(await api.elegirFicha(id));
    } catch (e) {
      setError(e instanceof Error ? e.message : "No hemos podido elegir esa ficha.");
      setEnviando(null);
    }
  };

  return (
    <section className="pantalla">
      <h1 className="cartel">
        ¿Cuál es tu ficha<span className="pt">?</span>
      </h1>
      <p className="lead">
        {me.locations.length === 1 ? "Esta es la ficha que administra" : "Estas son las fichas que administra"} <strong>{me.email}</strong>.
        Elige con cuál quieres trabajar.
      </p>
      <ul className="fichas">
        {me.locations.map((l) => (
          <li key={l.id}>
            <button type="button" disabled={enviando !== null} onClick={() => void elegir(l.id)}>
              <span className="rotulo">{l.title}</span>
              <span className="sec">{l.verified ? "Verificada" : "Sin verificar"}</span>
              <span className="ir" aria-hidden="true">
                {enviando === l.id ? "…" : "→"}
              </span>
            </button>
          </li>
        ))}
      </ul>
      {error && (
        <p className="aviso" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}

const IMPACTO: Record<string, string> = { alto: "Impacto alto", medio: "Impacto medio", bajo: "Impacto bajo" };


function Panel({
  ficha,
  variasFichas,
  onCambiar,
  onSinSesion,
}: {
  ficha: Location;
  variasFichas: boolean;
  onCambiar: () => void;
  onSinSesion: () => void;
}) {
  type Estado =
    | { tipo: "cargando" }
    | { tipo: "ok"; d: Diagnosis }
    | { tipo: "agotado" }
    | { tipo: "reconectar" }
    | { tipo: "error"; mensaje: string };
  const [estado, setEstado] = useState<Estado>({ tipo: "cargando" });
  const [intento, setIntento] = useState(0);
  const cargar = useCallback(() => setIntento((n) => n + 1), []);

  // Mientras el servidor lee la ficha, la API responde 404 con `status: "pendiente"`:
  // se sondea cada 3 s, 10 intentos como máximo, igual que el resumen.
  useEffect(() => {
    let vivo = true;
    let temporizador: ReturnType<typeof setTimeout> | undefined;
    let sondeos = 0;

    const pedir = async () => {
      try {
        const d = await api.diagnostico();
        if (vivo) setEstado({ tipo: "ok", d });
      } catch (e) {
        if (!vivo) return;
        if (e instanceof ApiError && e.status === 401) onSinSesion();
        else if (e instanceof ApiError && e.status === 404 && e.estado === "pendiente") {
          sondeos += 1;
          if (sondeos >= 10) setEstado({ tipo: "agotado" });
          else temporizador = setTimeout(() => void pedir(), 3000);
        } else if (e instanceof ApiError && e.status === 404 && e.estado === "needs_reconnect") setEstado({ tipo: "reconectar" });
        else setEstado({ tipo: "error", mensaje: e instanceof Error ? e.message : "No hemos podido analizar tu ficha." });
      }
    };

    setEstado({ tipo: "cargando" });
    void pedir();
    return () => {
      vivo = false;
      if (temporizador) clearTimeout(temporizador);
    };
  }, [ficha.id, intento, onSinSesion]);

  return (
    <section className="pantalla panel">
      <div className="panel-cab">
        <div>
          <h1 className="cartel">
            {ficha.title}
            <Punto />
          </h1>
          {estado.tipo === "ok" && (
            <>
              <p className="sec">{[estado.d.category, estado.d.address].filter(Boolean).join(" · ")}</p>
              <p className="sec fuente">{!estado.d.attribution || estado.d.attribution === "Google Maps" ? "Datos de Google Maps" : estado.d.attribution}</p>
            </>
          )}
          {variasFichas && (
            <button className="enlace" type="button" onClick={onCambiar}>
              Cambiar de ficha
            </button>
          )}
        </div>
        {estado.tipo === "ok" && (
          <div className="nota-grande" data-nivel={estado.d.score < 60 ? "mal" : "bien"}>
            <p className="cartel">
              {estado.d.score}
              <small>/100</small>
            </p>
            <p className="rotulo">{estado.d.label}</p>
          </div>
        )}
      </div>

      <Resumen fichaId={ficha.id} onSinSesion={onSinSesion} />

      {estado.tipo === "cargando" && <Cargando texto="Analizando tu ficha" />}

      {estado.tipo === "agotado" && (
        <div className="bloque">
          <p className="lead">Todavía estamos analizando tu ficha; vuelve en un minuto.</p>
          <div className="acciones">
            <button className="btn sec" type="button" onClick={cargar}>
              Comprobar ahora
            </button>
          </div>
        </div>
      )}

      {estado.tipo === "reconectar" && (
        <div className="bloque">
          <p className="lead">Para analizar tu ficha, Fichaitor necesita que vuelvas a conectarla con Google.</p>
          <div className="acciones">
            <a className="btn" href={OAUTH_START}>
              Volver a conectar con Google <Flecha />
            </a>
          </div>
        </div>
      )}

      {estado.tipo === "error" && (
        <div className="bloque">
          <p className="aviso" role="alert">
            {estado.mensaje}
          </p>
          <div className="acciones">
            <button className="btn" type="button" onClick={cargar}>
              Reintentar <Flecha />
            </button>
          </div>
        </div>
      )}

      {estado.tipo === "ok" && (
        <div className="bloque">
          <h2 className="rotulo">Qué mejorar</h2>
          {(estado.d.status === "needs_reconnect" || estado.d.status === "error") && (
            <p className="sec">
              {estado.d.status === "needs_reconnect"
                ? "Este es el último análisis que pudimos hacer antes de que caducara el acceso a Google."
                : "No hemos podido actualizar el análisis ahora mismo. Este es el último que tenemos."}
            </p>
          )}
          {estado.d.findings.length === 0 ? (
            <p className="lead">No hemos encontrado nada que mejorar. Tu ficha está al día.</p>
          ) : (
            <ol className="hallazgos">
              {estado.d.findings.map((f) => (
                <li key={f.code}>
                  <b>{f.title}</b>
                  <span>{f.detail}</span>
                  <em>{IMPACTO[f.impact] ?? ""}</em>
                </li>
              ))}
            </ol>
          )}
          {estado.d.maps_url.startsWith("https://") && (
            <p className="sec">
              <a href={estado.d.maps_url} target="_blank" rel="noopener">
                Ver la ficha en Google Maps
              </a>
            </p>
          )}
        </div>
      )}

    </section>
  );
}
