import { useCallback, useEffect, useRef, useState } from "react";
import logo from "../../../brand/logo.svg";
import { api, ApiError, EVENTO_BLOQUEO, OAUTH_START, type BajaPendiente, type Diagnosis, type Location, type Me, type Summary } from "./api";
import { Ajustes, AvisoBaja, cuando } from "./Ajustes";
import { Canales } from "./Canales";
import { CuentaEnPausa, Facturacion } from "./Facturacion";
import { Fechas } from "./Fechas";
import { Indice } from "./Indice";
import { esMultinegocio, planPedido, SelectorNegocio, TusNegocios, type PreguntaNegocio } from "./Negocios";
import { Instalar } from "./Instalar";
import { marcarPagoEnCurso, NOMBRE_PAGO, Paywall, type MotivoPaywall } from "./Paywall";
import { Tour } from "./Tour";
import { Cuenta, Seccion, SelloEstrellas } from "./movimiento";
import { Resumen } from "./Resumen";
import { Resenas } from "./Resenas";
import { Sentimiento } from "./Sentimiento";

type Vista =
  | { tipo: "cargando" }
  | { tipo: "conectar"; aviso?: string }
  | { tipo: "pendiente" }
  | { tipo: "sin-fichas"; email: string }
  | { tipo: "elegir"; me: Me }
  | { tipo: "panel"; me: Me; ficha: Location }
  // Multinegocio (docs/multinegocio.md, 12): activar y desactivar fichas; puede llegar con una pregunta abierta.
  | { tipo: "negocios"; me: Me; pregunta?: PreguntaNegocio }
  // Baja programada: solo el aviso con la fecha y «Restaurar». No se piden datos (la API responde 409).
  | { tipo: "baja"; baja: BajaPendiente }
  // La baja de la ficha ya es efectiva y la cuenta sigue: solo un estado vacío, sin pedir datos.
  | { tipo: "desconectada"; me: Me }
  // Google niega el acceso a la ficha elegida: sigue elegida, pero ya no está en `locations`.
  | { tipo: "sin-acceso"; me: Me }
  // Cuenta en pausa por un impago (`billing.blocked`): una pantalla única para pagar o darse de baja.
  | { tipo: "bloqueada"; me: Me }
  // Tras confirmar el borrado de la cuenta: las sesiones ya están cerradas.
  | { tipo: "despedida"; borrado: string }
  // Llegada desde el enlace del correo: /app?restaurar=<token>, sin sesión.
  // «caducado»: el servidor dice que el enlace ya no vale (410). «fallo»: cualquier otro error; el enlace
  // puede seguir valiendo y se ofrece reintentar.
  | { tipo: "restaurar"; estado: "enviando" | "ok" | "caducado" | "fallo"; mensaje?: string | undefined }
  | { tipo: "error"; mensaje: string };

/** Mensajes para la vuelta de Google: /app?google=... (contrato web-API). */
const AVISOS: Record<string, string> = {
  cancelado: "Has cancelado la conexión con Google. No se ha cambiado nada.",
  sin_permiso:
    "Falta el permiso para gestionar tu ficha. En la pantalla de Google, marca la casilla de Google Business Profile: sin ella Fichaitor no puede ver tu ficha.",
  error: "No hemos podido conectar con Google. Prueba de nuevo.",
};

/** Lee ?google=, ?fichas= y ?restaurar= una vez y los quita de la barra de direcciones. */
function leerVuelta(): { google: string | null; fichas: string | null; restaurar: string | null; pago: string | null; fichaPago: string | null } {
  const q = new URLSearchParams(location.search);
  // El enlace de restauración puede venir en el fragmento (#restaurar=...): el fragmento no se envía
  // al servidor, así que no queda en ningún registro. Se acepta también en la consulta.
  const fragmento = new URLSearchParams(location.hash.replace(/^#/, ""));
  const vuelta = {
    google: q.get("google"),
    fichas: q.get("fichas"),
    restaurar: fragmento.get("restaurar") ?? q.get("restaurar"),
    // Vuelta de Stripe Checkout (docs/admin-facturacion.md, 4.7).
    pago: q.get("pago"),
    fichaPago: q.get("ficha"),
  };
  if (fragmento.has("restaurar")) history.replaceState(null, "", location.pathname + location.search);
  if (vuelta.google !== null || vuelta.fichas !== null || q.has("restaurar") || vuelta.pago !== null) {
    q.delete("google");
    q.delete("fichas");
    q.delete("pago");
    q.delete("ficha");
    // El token de restauración no debe quedarse en la barra de direcciones ni en el historial.
    q.delete("restaurar");
    const resto = q.toString();
    history.replaceState(null, "", location.pathname + (resto ? `?${resto}` : ""));
  }
  return vuelta;
}

function vistaDe(me: Me, fichasPendientes: boolean): Vista {
  if (me.pending_deletion) return { tipo: "baja", baja: me.pending_deletion };
  if (me.billing?.blocked) return { tipo: "bloqueada", me };
  if (me.connected === false) return { tipo: "desconectada", me };
  const ficha = me.locations.find((l) => l.id === me.selected_location_id);
  // Antes que «sin fichas»: si era la única, `locations` llega vacía pero la ficha sigue elegida.
  if (me.selected_location_id !== null && !ficha) return { tipo: "sin-acceso", me };
  if (me.locations.length === 0) return fichasPendientes ? { tipo: "pendiente" } : { tipo: "sin-fichas", email: me.email };
  if (esMultinegocio(me)) {
    // Sin ninguna activa, «elegir» es activar la primera. Con activas, el panel enseña la preferida si
    // está activa y, si no, la primera activa.
    const activas = me.locations.filter((l) => l.active);
    if (activas.length === 0) return { tipo: "elegir", me };
    return { tipo: "panel", me, ficha: ficha?.active ? ficha : activas[0]! };
  }
  return ficha ? { tipo: "panel", me, ficha } : { tipo: "elegir", me };
}

export function App() {
  const [vista, setVista] = useState<Vista>({ tipo: "cargando" });
  const vuelta = useRef<ReturnType<typeof leerVuelta> | null>(null);
  // Al volver de Stripe: qué se sabe del pago de esa ficha.
  const [avisoPago, setAvisoPago] = useState<{ estado: "esperando" | "hecho" | "tarde" | "cancelado"; texto: string } | null>(null);
  const restaurando = useRef(false);
  // El token, solo en memoria mientras haga falta para reintentar: ni en la URL ni en localStorage.
  const tokenRestaurar = useRef<string | null>(null);

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

  // Vuelve a leer la cuenta sin pasar por «cargando»: la pantalla se queda y solo cambia lo que cambie.
  // Devuelve si la cuenta sigue en pausa.
  const refrescar = useCallback(async (): Promise<boolean> => {
    const me = await api.me();
    setVista(vistaDe(me, false));
    return !!me.billing?.blocked;
  }, []);

  // Una ruta ha respondido 402 `account_blocked` (la cuenta se ha bloqueado a mitad de sesión).
  // Al volver atrás desde Stripe, el navegador puede restaurar la página con los botones en «Abriendo…».
  useEffect(() => {
    const alBloquear = () => void refrescar().catch(() => undefined);
    const alVolver = (e: PageTransitionEvent) => {
      if (e.persisted) location.reload();
    };
    addEventListener(EVENTO_BLOQUEO, alBloquear);
    addEventListener("pageshow", alVolver);
    return () => {
      removeEventListener(EVENTO_BLOQUEO, alBloquear);
      removeEventListener("pageshow", alVolver);
    };
  }, [refrescar]);

  const restaurar = useCallback(() => {
    const token = tokenRestaurar.current;
    if (!token) return;
    setVista({ tipo: "restaurar", estado: "enviando" });
    api
      .restaurarConToken(token)
      .then(() => {
        tokenRestaurar.current = null;
        setVista({ tipo: "restaurar", estado: "ok" });
      })
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 410) {
          tokenRestaurar.current = null;
          setVista({ tipo: "restaurar", estado: "caducado" });
        } else setVista({ tipo: "restaurar", estado: "fallo", mensaje: e instanceof Error ? e.message : undefined });
      });
  }, []);

  useEffect(() => {
    if (vuelta.current === null) vuelta.current = leerVuelta();
    // Si ya se está atendiendo un enlace de restauración, este efecto no hace nada más:
    // en desarrollo React lo ejecuta dos veces, y la segunda cargaría el panel por encima.
    if (restaurando.current) return;
    const token = vuelta.current.restaurar;
    if (token) {
      // El token se usa una sola vez.
      restaurando.current = true;
      vuelta.current.restaurar = null;
      tokenRestaurar.current = token;
      restaurar();
      return;
    }
    const aviso = vuelta.current.google ? AVISOS[vuelta.current.google] : undefined;
    if (aviso) setVista({ tipo: "conectar", aviso });
    else void cargar();
  }, [cargar, restaurar]);

  // Vuelta de Stripe Checkout. El cobro lo confirma Stripe al backend por su cuenta: aquí solo se
  // pregunta por la cuenta hasta ver la ficha en su plan nuevo (un minuto como mucho).
  useEffect(() => {
    // Sin guardia de «ya empezado»: en desarrollo React monta dos veces, y la limpieza del primer
    // montaje para su sondeo; el segundo empieza otro.
    const v = vuelta.current ?? (vuelta.current = leerVuelta());
    if (!v.pago) return;
    if (v.pago !== "ok") {
      setAvisoPago({ estado: "cancelado", texto: "Has salido del pago sin terminarlo. No se ha cobrado nada." });
      return;
    }
    const ficha = v.fichaPago;
    const NEUTRO = "Si has completado el pago, tu plaza se activará en unos minutos y te llegará la factura por correo.";
    if (!ficha) {
      setAvisoPago({ estado: "tarde", texto: NEUTRO });
      return;
    }
    marcarPagoEnCurso(ficha);
    setAvisoPago({ estado: "esperando", texto: "Pago recibido. Estamos activando tu plaza…" });
    let vivo = true;
    let reloj: ReturnType<typeof setTimeout> | undefined;
    const mirar = async (vez: number) => {
      try {
        const me = await api.me();
        if (!vivo) return;
        const l = me.locations.find((x) => x.id === ficha);
        if (!l) {
          // La ficha no es de esta cuenta (o la vuelta no es de verdad): nada que comprobar.
          marcarPagoEnCurso(null);
          setAvisoPago({ estado: "tarde", texto: NEUTRO });
          return;
        }
        if (l.active && (l.plan === "pro" || l.plan === "pro_whatsapp")) {
          marcarPagoEnCurso(null);
          setAvisoPago({ estado: "hecho", texto: `Ya está: ${l.title} está en ${NOMBRE_PAGO[l.plan]}. La factura te llega por correo.` });
          setVista(vistaDe(me, false));
          return;
        }
      } catch {
        /* se reintenta */
      }
      if (!vivo) return;
      if (vez >= 20) {
        setAvisoPago({ estado: "tarde", texto: "Pago recibido. Tu plaza se activará en unos minutos; te llegará la factura por correo." });
        return;
      }
      reloj = setTimeout(() => void mirar(vez + 1), 3000);
    };
    void mirar(1);
    return () => {
      vivo = false;
      if (reloj) clearTimeout(reloj);
    };
  }, []);

  /** Cambia la ficha que se ve. Las demás activas siguen vigiladas. */
  const verNegocio = useCallback(async (id: string) => {
    try {
      setVista(vistaDe(await api.elegirFicha(id), false));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setVista({ tipo: "conectar" });
      else setVista({ tipo: "error", mensaje: e instanceof Error ? e.message : "No hemos podido abrir esa ficha." });
    }
  }, []);

  const salir = useCallback(async () => {
    try {
      await api.salir();
    } finally {
      location.assign("/");
    }
  }, []);

  const SIN_SESION: Vista["tipo"][] = ["cargando", "conectar", "error", "despedida", "restaurar"];
  const conSesion = !SIN_SESION.includes(vista.tipo);

  return (
    <>
      <header className="barra">
        <a className="marca" href="/" aria-label="Fichaitor, inicio">
          <img src={logo} alt="" width={36} height={36} />
          <span>Fichaitor</span>
        </a>
        {conSesion && (
          <div className="barra-derecha">
            {vista.tipo === "panel" && esMultinegocio(vista.me) && (
              <SelectorNegocio
                me={vista.me}
                actual={vista.ficha.id}
                onElegir={verNegocio}
                onActivar={(id) => setVista({ tipo: "negocios", me: vista.me, pregunta: { n: "activar", id } })}
                onTusNegocios={() => setVista({ tipo: "negocios", me: vista.me })}
              />
            )}
            <button className="enlace" type="button" onClick={() => void salir()}>
              Salir
            </button>
          </div>
        )}
      </header>

      <main className="caja">
        {avisoPago && (
          <div className="aviso-pago" data-estado={avisoPago.estado} role="status">
            <p>
              {avisoPago.estado === "esperando" && <span className="ojo" aria-hidden="true" />}
              {avisoPago.texto}
            </p>
            {avisoPago.estado !== "esperando" && (
              <button className="enlace" type="button" onClick={() => setAvisoPago(null)}>
                Cerrar
              </button>
            )}
          </div>
        )}
        {vista.tipo === "cargando" && <Cargando texto="Abriendo tu ficha" />}
        {vista.tipo === "conectar" && <Conectar {...(vista.aviso ? { aviso: vista.aviso } : {})} />}
        {vista.tipo === "pendiente" && <Pendiente onReintentar={() => void cargar()} />}
        {vista.tipo === "sin-fichas" && <SinFichas email={vista.email} />}
        {vista.tipo === "elegir" && <Elegir me={vista.me} onElegida={(me) => setVista(vistaDe(me, false))} />}
        {vista.tipo === "panel" && (
          <Panel
            // Al cambiar de negocio el panel se monta de nuevo: nada de la ficha anterior se queda pintado.
            key={vista.ficha.id}
            ficha={vista.ficha}
            email={vista.me.email}
            onRecargar={() => void cargar()}
            onRefrescar={refrescar}
            onDespedida={(borrado) => setVista({ tipo: "despedida", borrado })}
            me={vista.me}
            variasFichas={vista.me.locations.length > 1}
            negocios={esMultinegocio(vista.me) ? vista.me.locations.filter((l) => l.active) : []}
            // Con multinegocio, se cambia desde el selector de la barra.
            onCambiar={esMultinegocio(vista.me) ? undefined : () => setVista({ tipo: "elegir", me: vista.me })}
            onDesactivarFicha={
              esMultinegocio(vista.me)
                ? () => setVista({ tipo: "negocios", me: vista.me, pregunta: { n: "desactivar", id: vista.ficha.id } })
                : undefined
            }
            onSinSesion={() => setVista({ tipo: "conectar" })}
          />
        )}
        {vista.tipo === "negocios" && (
          <TusNegocios
            me={vista.me}
            preguntaInicial={vista.pregunta}
            onCambio={(me) => setVista({ tipo: "negocios", me })}
            onVer={verNegocio}
            onVolver={() => void cargar()}
            onSinSesion={() => setVista({ tipo: "conectar" })}
          />
        )}
        {vista.tipo === "bloqueada" && (
          <CuentaEnPausa
            me={vista.me}
            onComprobar={refrescar}
            onTusNegocios={() => setVista({ tipo: "negocios", me: vista.me })}
            onSalir={() => void salir()}
            onSinSesion={() => setVista({ tipo: "conectar" })}
            onDespedida={(borrado) => setVista({ tipo: "despedida", borrado })}
          />
        )}
        {vista.tipo === "sin-acceso" && (
          <section className="pantalla">
            <h1 className="cartel">
              Ya no tenemos acceso a esta ficha en Google<Punto />
            </h1>
            <p className="lead">
              Suele pasar cuando alguien te quita como gestor de la ficha. Vuelve a conectar con una cuenta de Google que la gestione.
            </p>
            <div className="acciones">
              <a className="btn" href={OAUTH_START}>
                Conectar con Google <Flecha />
              </a>
              {vista.me.locations.length > 0 && (
                <button className="btn sec" type="button" onClick={() => setVista({ tipo: "elegir", me: vista.me })}>
                  Elegir otra ficha
                </button>
              )}
            </div>
            <p className="sec">Si no vuelves a conectarla, borraremos lo guardado de esta ficha a los 30 días.</p>
            <Ajustes
              email={vista.me.email}
              soloCuenta
              onSinSesion={() => setVista({ tipo: "conectar" })}
              onCambio={() => void cargar()}
              onDespedida={(borrado) => setVista({ tipo: "despedida", borrado })}
            />
          </section>
        )}
        {vista.tipo === "desconectada" && (
          <section className="pantalla">
            <h1 className="cartel">
              Tu ficha está desconectada<Punto />
            </h1>
            <p className="lead">
              Hemos revocado el acceso a tu cuenta de Google y lo obtenido de ella se borra en un máximo de 30 días. Si quieres volver a usar
              Fichaitor, conecta de nuevo con Google.
            </p>
            <div className="acciones">
              <a className="btn" href={OAUTH_START}>
                Conectar con Google <Flecha />
              </a>
            </div>
            <Ajustes
              email={vista.me.email}
              soloCuenta
              onSinSesion={() => setVista({ tipo: "conectar" })}
              onCambio={() => void cargar()}
              onDespedida={(borrado) => setVista({ tipo: "despedida", borrado })}
            />
          </section>
        )}
        {vista.tipo === "baja" && (
          <section className="pantalla">
            <AvisoBaja baja={vista.baja} onSinSesion={() => setVista({ tipo: "conectar" })} onCambio={() => void cargar()} />
            <p className="sec">Mientras la baja está programada, Fichaitor no lee tu ficha ni te envía avisos.</p>
          </section>
        )}
        {vista.tipo === "despedida" && (
          <section className="pantalla">
            <h1 className="cartel">
              Hasta aquí<Punto />
            </h1>
            <p className="lead">Tu cuenta se borrará el {cuando(vista.borrado)}.</p>
            <p>Te hemos enviado un correo para restaurarla si cambias de opinión. Pasado ese momento, no se puede deshacer.</p>
            <div className="acciones">
              <a className="btn sec" href="/">
                Volver al inicio
              </a>
            </div>
          </section>
        )}
        {vista.tipo === "restaurar" && vista.estado === "enviando" && <Cargando texto="Restaurando" />}
        {vista.tipo === "restaurar" && vista.estado === "ok" && (
          <section className="pantalla">
            <h1 className="cartel">
              Restaurada<Punto />
            </h1>
            <p className="lead">La baja está cancelada. Todo sigue como estaba y no se ha borrado nada.</p>
            <div className="acciones">
              <button className="btn" type="button" onClick={() => void cargar()}>
                Entrar en mi panel <Flecha />
              </button>
            </div>
          </section>
        )}
        {vista.tipo === "restaurar" && vista.estado === "fallo" && (
          <section className="pantalla">
            <h1 className="cartel">
              No hemos podido restaurarla<Punto />
            </h1>
            <p className="lead">{vista.mensaje ?? "Algo ha fallado por el camino."} El enlace sigue valiendo: prueba de nuevo.</p>
            <div className="acciones">
              <button className="btn" type="button" onClick={restaurar}>
                Reintentar <Flecha />
              </button>
            </div>
          </section>
        )}
        {vista.tipo === "restaurar" && vista.estado === "caducado" && (
          <section className="pantalla">
            <h1 className="cartel">
              Ese enlace ya no sirve<Punto />
            </h1>
            <p className="lead">O ya se usó, o la baja ya no se puede deshacer.</p>
            <p>Si la baja sigue programada, puedes restaurarla desde tu panel. Si tienes dudas, escribe a soporte@fichaitor.es.</p>
            <div className="acciones">
              <button className="btn" type="button" onClick={() => void cargar()}>
                Ir a mi panel <Flecha />
              </button>
            </div>
          </section>
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
  const [paywall, setPaywall] = useState<string | null>(null);
  const multi = esMultinegocio(me);
  const fichaPaywall = paywall ? me.locations.find((l) => l.id === paywall) : undefined;

  const elegir = async (id: string) => {
    setEnviando(id);
    setError(null);
    try {
      if (multi) await api.activarFicha(id, planPedido(me));
      onElegida(await api.elegirFicha(id));
    } catch (e) {
      // 402: no queda hueco en ningún plan. El paywall ofrece una plaza de pago para esa ficha.
      if (e instanceof ApiError && e.status === 402 && e.codigo !== "account_blocked" && me.plans) setPaywall(id);
      else setError(e instanceof Error ? e.message : "No hemos podido elegir esa ficha.");
      setEnviando(null);
    }
  };

  return (
    <section className="pantalla">
      <h1 className="cartel">
        {multi ? (
          <>
            Activa tu primera ficha<span className="pt">.</span>
          </>
        ) : (
          <>
            ¿Cuál es tu ficha<span className="pt">?</span>
          </>
        )}
      </h1>
      <p className="lead">
        {me.locations.length === 1 ? "Esta es la ficha que administra" : "Estas son las fichas que administra"} <strong>{me.email}</strong>.{" "}
        {multi ? "Elige con cuál empiezas. Después podrás activar las demás." : "Elige con cuál quieres trabajar."}
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
      {fichaPaywall && (
        <Paywall
          me={me}
          ficha={fichaPaywall}
          motivo="no_seat"
          onCerrar={() => setPaywall(null)}
          onSinSesion={() => {
            setPaywall(null);
            setError("Tu sesión ha caducado. Vuelve a entrar.");
          }}
          onActivada={(nuevo) => {
            setPaywall(null);
            // Beta: ya está activa en Pro. Se guarda como la preferida y se abre su panel.
            void api
              .elegirFicha(fichaPaywall.id)
              .then(onElegida)
              .catch(() => onElegida(nuevo));
          }}
        />
      )}
    </section>
  );
}

/**
 * Muchas fichas llevan el nombre seguido de una descripción: «Publiexpe | Regalos de empresa y
 * merchandising personalizado». El titular muestra solo el nombre; el resto va debajo, en pequeño.
 * Si no hay separador, el titular es el nombre entero y su tamaño se ajusta a la longitud.
 */
function partirNombre(titulo: string): { principal: string; resto: string } {
  const limpio = titulo.trim().replace(/\s+/g, " ");
  const m = /^(.{2,40}?)\s*(?:\s[|·•–—-]\s|:\s)\s*(.+)$/.exec(limpio);
  if (m?.[1] && m[2]) return { principal: m[1].trim(), resto: m[2].trim() };
  return { principal: limpio, resto: "" };
}

function largoDe(texto: string): "corto" | "medio" | "largo" {
  if (texto.length <= 16) return "corto";
  if (texto.length <= 32) return "medio";
  return "largo";
}

const SECCIONES = [
  // «Resumen» sube hasta el nombre del negocio: la cabecera forma parte del resumen.
  { id: "resumen", nombre: "Resumen", arriba: true },
  { id: "opinion", nombre: "Opinión" },
  { id: "resenas", nombre: "Reseñas" },
  { id: "festivos", nombre: "Festivos" },
  { id: "mejoras", nombre: "Mejoras" },
  { id: "avisos", nombre: "Avisos" },
  { id: "facturacion", nombre: "Facturación" },
  { id: "ajustes", nombre: "Ajustes" },
];

const IMPACTO: Record<string, string> = { alto: "Impacto alto", medio: "Impacto medio", bajo: "Impacto bajo" };


function Panel({
  ficha,
  email,
  onRecargar,
  onRefrescar,
  onDespedida,
  me,
  variasFichas,
  negocios,
  onCambiar,
  onDesactivarFicha,
  onSinSesion,
}: {
  ficha: Location;
  email: string;
  /** Vuelve a pedir la cuenta tras una baja o una restauración. */
  onRecargar: () => void;
  /** Vuelve a leer la cuenta sin quitar el panel. */
  onRefrescar: () => Promise<boolean>;
  /** Tras confirmar el borrado de la cuenta, con el momento del borrado. */
  onDespedida: (borrado: string) => void;
  /** La cuenta: precios, planes y cobro, para el paywall. */
  me: Me;
  variasFichas: boolean;
  /** Multinegocio: fichas activas, para elegir qué negocios ve cada chat de «Avisos». Vacía sin multinegocio. */
  negocios: Location[];
  onCambiar?: (() => void) | undefined;
  /** Multinegocio: desactivar la que se ve (también la última: se vuelve a «activa tu primera ficha»). */
  onDesactivarFicha?: (() => void) | undefined;
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
  // Paywall abierto, y por qué. Solo en multinegocio (sin `plans`, el backend no vende planes).
  const [paywall, setPaywall] = useState<MotivoPaywall | null>(null);
  const mejorar = (motivo: MotivoPaywall) => (me.plans ? () => setPaywall(motivo) : undefined);
  const cargar = useCallback(() => setIntento((n) => n + 1), []);

  // Mientras el servidor lee la ficha, la API responde 404 con `status: "pendiente"`:
  // se sondea cada 3 s, 10 intentos como máximo, igual que el resumen.
  useEffect(() => {
    let vivo = true;
    let temporizador: ReturnType<typeof setTimeout> | undefined;
    let sondeos = 0;

    const pedir = async () => {
      try {
        const d = await api.diagnostico(ficha.id);
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

  const nombre = partirNombre(ficha.title);
  // Tras publicar una respuesta, se vuelven a pedir el resumen y el sentimiento.
  const [version, setVersion] = useState(0);
  const alCambiar = useCallback(() => setVersion((n) => n + 1), []);
  // Valoración real de la ficha (media sobre 5), que llega con el resumen.
  const [media, setMedia] = useState<{ valor: number; total: number | null } | null>(null);
  const alLeerResumen = useCallback(
    (s: Summary) => setMedia(s.average_rating !== null ? { valor: s.average_rating, total: s.review_count } : null),
    [],
  );

  return (
    <section className="pantalla panel">
      <div className="panel-cab">
        <div>
          <h1 className="cartel nombre" data-largo={largoDe(nombre.principal)}>
            {nombre.principal}
            <Punto />
          </h1>
          {nombre.resto && <p className="rotulo nombre-resto">{nombre.resto}</p>}
          {media && <SelloEstrellas media={media.valor} total={media.total} />}
          {estado.tipo === "ok" && (
            <>
              <p className="sec">{[estado.d.category, estado.d.address].filter(Boolean).join(" · ")}</p>
              <p className="sec fuente">{!estado.d.attribution || estado.d.attribution === "Google Maps" ? "Datos de Google Maps" : estado.d.attribution}</p>
            </>
          )}
          {variasFichas && onCambiar && (
            <button className="enlace" type="button" onClick={onCambiar}>
              Cambiar de ficha
            </button>
          )}
        </div>
        {estado.tipo === "ok" && (
          <div className="nota-grande" data-nivel={estado.d.score < 60 ? "mal" : "bien"}>
            <p className="cartel">
              <Cuenta valor={estado.d.score} />
              <small>/100</small>
            </p>
            <p className="rotulo">{estado.d.label}</p>
          </div>
        )}
      </div>

      {/* Orden decidido por el propietario (2026-11-12): la nota de la ficha en la cabecera,
          después las tres tarjetas (reseñas sin responder, festivos, cambios) y debajo el sentimiento. */}
      <Indice secciones={SECCIONES} />

      <Resumen key={`resumen-${version}`} fichaId={ficha.id} onSinSesion={onSinSesion} onDatos={alLeerResumen} />

      <Sentimiento fichaId={ficha.id} version={version} onSinSesion={onSinSesion} />

      <Resenas fichaId={ficha.id} usoIA={ficha.usage?.ai_replies ?? null} onSinSesion={onSinSesion} onCambio={alCambiar} onMejorar={mejorar("ai_replies")} />

      <Fechas fichaId={ficha.id} incluidos={ficha.features?.key_dates !== false} onSinSesion={onSinSesion} onMejorar={mejorar("key_dates")} />

      {paywall && (
        <Paywall
          me={me}
          ficha={ficha}
          motivo={paywall}
          onCerrar={() => setPaywall(null)}
          onSinSesion={onSinSesion}
          onActivada={() => {
            setPaywall(null);
            onRecargar();
          }}
        />
      )}

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
        <Seccion id="mejoras">
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
        </Seccion>
      )}

      <Canales
        negocios={negocios}
        onSinSesion={onSinSesion}
        // Sin WhatsApp en el plan de esta ficha (Gratis o Pro), «Conectar WhatsApp» lleva al paywall de Pro+.
        onWhatsappSinPlan={ficha.features?.whatsapp === false ? mejorar("whatsapp") : undefined}
      />

      <Instalar />

      <Facturacion me={me} onRecargar={onRefrescar} onSinSesion={onSinSesion} />

      <Ajustes
        email={email}
        variasFichas={variasFichas}
        onDesactivarFicha={onDesactivarFicha}
        onSinSesion={onSinSesion}
        onCambio={onRecargar}
        onDespedida={onDespedida}
      />

      <Tour />
    </section>
  );
}
