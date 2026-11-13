import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api, ApiError, type AlcanceBaja, type BajaPendiente } from "./api";
import { pausarScroll, Seccion } from "./movimiento";
import { EVENTO_RECORRIDO } from "./Tour";

// Ajustes del panel: desconectar Google y borrar la cuenta.
//
// Contrato: docs/contrato-web-api.md (baja con código). Textos de «Lo que la web debe mostrar».
// Reglas pedidas por el propietario:
//  - La baja exige un código de un solo uso enviado al correo de la cuenta. Una sesión abierta
//    en un ordenador ajeno no basta para borrar nada.
//  - Al confirmar, se avisa por correo y la baja queda programada: durante 48 horas se puede
//    restaurar. Pasado ese plazo, se elimina.

const fechaHora = new Intl.DateTimeFormat("es-ES", { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });

export function cuando(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : fechaHora.format(d);
}

type Textos = { titulo: string; tarjeta: string; que: string[]; confirmar: string; hecho: string };

const BORRADO_30 =
  "En un máximo de 30 días borraremos todo lo obtenido de Google: los datos de la ficha, las reseñas, su clasificación, los resúmenes, el historial de notas y los borradores de respuesta.";
const REGISTRO_12 = "El registro de las respuestas que publicaste se conserva 12 meses.";
const CAMBIAR_DE_IDEA = "Tienes 48 horas para cambiar de idea. Te enviaremos un correo con un enlace para restaurarla.";

/** Textos de la baja. La desconexión afecta a todas las fichas de la cuenta de Google, no solo a la que se ve. */
function textos(alcance: AlcanceBaja, variasFichas: boolean): Textos {
  if (alcance === "ficha") {
    const fichas = variasFichas ? "tus fichas de esta cuenta de Google" : "tu ficha";
    return {
      titulo: variasFichas ? "Desconectar tus fichas de Google" : "Desconectar tu ficha de Google",
      tarjeta: `Dejamos de leer ${fichas}. Tienes 48 horas para cambiar de idea.`,
      que: [
        `Dejaremos de leer ${fichas} y de avisarte desde este momento.`,
        "A las 48 horas revocaremos el acceso a tu cuenta de Google.",
        BORRADO_30,
        REGISTRO_12,
        "Tu cuenta sigue existiendo y podrás volver a conectar.",
        CAMBIAR_DE_IDEA,
      ],
      confirmar: "Desconectar",
      hecho: "Desconexión en marcha",
    };
  }
  return {
    titulo: "Borrar tu cuenta de Fichaitor",
    tarjeta: "Tu cuenta queda borrada a las 48 horas. Hasta entonces puedes restaurarla.",
    que: [
      "Dejaremos de leer tus fichas y de avisarte desde este momento.",
      "Cerraremos tu sesión en todos tus dispositivos.",
      "A las 48 horas revocaremos el acceso a tu cuenta de Google y tu cuenta quedará borrada.",
      BORRADO_30,
      REGISTRO_12,
      CAMBIAR_DE_IDEA,
    ],
    confirmar: "Borrar mi cuenta",
    hecho: "Baja en marcha",
  };
}

export function Ajustes({
  email,
  onSinSesion,
  onCambio,
  onDespedida,
  soloCuenta = false,
  variasFichas = false,
  onDesactivarFicha,
}: {
  email: string;
  variasFichas?: boolean;
  /** Multinegocio: desactivar la ficha que se ve (lleva a «Tus negocios» con la confirmación abierta). */
  onDesactivarFicha?: (() => void) | undefined;
  /** Sin ficha conectada solo queda borrar la cuenta. */
  soloCuenta?: boolean;
  onSinSesion: () => void;
  onCambio: () => void;
  /** Al borrar la cuenta se cierran todas las sesiones: se va a la despedida, no al panel. */
  onDespedida: (borrado: string) => void;
}) {
  const [abierto, setAbierto] = useState<AlcanceBaja | null>(null);

  return (
    <Seccion id="ajustes" className="ajustes">
      <h2 className="rotulo">Ajustes</h2>
      <p className="sec">
        Has entrado como <strong>{email}</strong>.
        {/* El recorrido solo existe dentro del panel; sin ficha conectada no hay nada que recorrer. */}
        {!soloCuenta && (
          <>
            {" "}
            <button className="enlace" type="button" onClick={() => dispatchEvent(new Event(EVENTO_RECORRIDO))}>
              Ver el recorrido del panel
            </button>
          </>
        )}
      </p>
      <div className="ajustes-lista">
        {onDesactivarFicha && (
          <div>
            <h3 className="rotulo">Desactivar esta ficha</h3>
            <p className="sec">Fichaitor deja de leerla y de avisarte de ella. Tus otros negocios siguen igual.</p>
            <button className="btn sec" type="button" onClick={onDesactivarFicha}>
              Desactivar esta ficha
            </button>
          </div>
        )}
        {!soloCuenta && (
          <div>
            <h3 className="rotulo">Desconectar Google</h3>
            <p className="sec">{textos("ficha", variasFichas).tarjeta}</p>
            <button className="btn sec" type="button" onClick={() => setAbierto("ficha")}>
              Desconectar Google
            </button>
          </div>
        )}
        <div>
          <h3 className="rotulo">Borrar mi cuenta</h3>
          <p className="sec">{textos("cuenta", variasFichas).tarjeta}</p>
          <button className="btn sec peligro" type="button" onClick={() => setAbierto("cuenta")}>
            Borrar mi cuenta
          </button>
        </div>
      </div>

      {abierto && (
        <ModalBaja
          alcance={abierto}
          variasFichas={variasFichas}
          onCerrar={() => setAbierto(null)}
          onSinSesion={onSinSesion}
          onDespedida={onDespedida}
          onHecho={() => {
            setAbierto(null);
            onCambio();
          }}
        />
      )}
    </Seccion>
  );
}

/** Aviso fijo mientras hay una baja programada, con la opción de restaurar. */
export function AvisoBaja({ baja, onSinSesion, onCambio }: { baja: BajaPendiente; onSinSesion: () => void; onCambio: () => void }) {
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const restaurar = async () => {
    setEnviando(true);
    setError(null);
    try {
      await api.restaurarBaja();
      onCambio();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 409: no había ninguna baja programada (por ejemplo, se restauró desde el correo).
      else if (e instanceof ApiError && e.status === 409) onCambio();
      else setError(e instanceof Error ? e.message : "No hemos podido restaurarla.");
      setEnviando(false);
    }
  };

  return (
    <div className="aviso-baja" role="alert">
      <div>
        <p className="rotulo">{baja.scope === "cuenta" ? "Tu cuenta está en proceso de baja" : "Desconexión de Google en marcha"}</p>
        <p>
          {baja.scope === "cuenta" ? "Tu cuenta se borrará el " : "Revocaremos el acceso a tu cuenta de Google el "}
          {cuando(baja.delete_at)}. Hasta entonces puedes restaurarla y todo sigue como estaba.
        </p>
        {error && <p className="error-linea">{error}</p>}
      </div>
      <button className="btn" type="button" disabled={enviando} onClick={() => void restaurar()}>
        {enviando ? "Restaurando…" : "Restaurar"}
      </button>
    </div>
  );
}

type Paso = { n: "explicar" } | { n: "codigo"; destino: string } | { n: "hecho"; borrado: string };
type Envio = "pendiente" | "enviado" | "fallido" | "ninguno" | "tarda";

function ModalBaja({
  alcance,
  variasFichas,
  onCerrar,
  onHecho,
  onDespedida,
  onSinSesion,
}: {
  alcance: AlcanceBaja;
  variasFichas: boolean;
  onCerrar: () => void;
  onHecho: () => void;
  onDespedida: (borrado: string) => void;
  onSinSesion: () => void;
}) {
  const t = textos(alcance, variasFichas);
  const [paso, setPaso] = useState<Paso>({ n: "explicar" });
  const [codigo, setCodigo] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const campo = useRef<HTMLInputElement>(null);
  // El POST responde antes de que salga el correo: se consulta su estado (cada 3 s, 30 s como máximo).
  const [envio, setEnvio] = useState<Envio>("pendiente");
  const [vuelta, setVuelta] = useState(0);

  const fallo = (e: unknown, porDefecto: string) => {
    if (e instanceof ApiError && e.status === 401) onSinSesion();
    // 409: ya hay una baja programada. Se cierra y el panel muestra el aviso para restaurarla.
    else if (e instanceof ApiError && e.status === 409) onHecho();
    else setError(e instanceof Error ? e.message : porDefecto);
  };

  useEffect(() => {
    if (paso.n !== "codigo") return;
    let vivo = true;
    let intentos = 0;
    let reloj: ReturnType<typeof setTimeout> | undefined;
    const mirar = async () => {
      try {
        const { status } = await api.estadoCodigoBaja();
        if (!vivo) return;
        if (status !== "pendiente") setEnvio(status);
        else if (++intentos < 10) reloj = setTimeout(() => void mirar(), 3000);
        // 30 s sin salir: no se sabe si llegará. Se deja de esperar y se ofrece pedir otro.
        else setEnvio("tarda");
      } catch (e) {
        if (!vivo) return;
        if (e instanceof ApiError && e.status === 401) onSinSesion();
        // Sin respuesta no se sabe si ha salido: no se afirma que se haya enviado.
        else setEnvio("tarda");
      }
    };
    setEnvio("pendiente");
    void mirar();
    return () => {
      vivo = false;
      if (reloj) clearTimeout(reloj);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paso.n, vuelta]);

  const pedirCodigo = async () => {
    setEnviando(true);
    setError(null);
    try {
      const r = await api.pedirCodigoBaja(alcance);
      setCodigo("");
      setPaso({ n: "codigo", destino: r.sent_to });
      setVuelta((n) => n + 1);
      setTimeout(() => campo.current?.focus(), 0);
    } catch (e) {
      fallo(e, "No hemos podido enviarte el código.");
    } finally {
      setEnviando(false);
    }
  };

  const confirmar = async () => {
    setEnviando(true);
    setError(null);
    try {
      const r = await api.confirmarBaja(alcance, codigo);
      // Con la cuenta, la sesión ya está cerrada: se sale del panel hacia la despedida.
      if (alcance === "cuenta") onDespedida(r.delete_at);
      else setPaso({ n: "hecho", borrado: r.delete_at });
    } catch (e) {
      // 410 o 429: ese código ya no vale. Se borra el campo para pedir otro.
      if (e instanceof ApiError && (e.status === 410 || e.status === 429)) setCodigo("");
      fallo(e, "No hemos podido confirmar la baja.");
    } finally {
      setEnviando(false);
    }
  };

  const completo = /^\d{6}$/.test(codigo);

  return (
    <Modal titulo={paso.n === "hecho" ? t.hecho : t.titulo} onCerrar={paso.n === "hecho" ? onHecho : onCerrar} bloqueado={enviando}>
      {paso.n === "explicar" && (
        <>
          <ul className="lista-puntos compacta">
            {t.que.map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
          <p>Para confirmarlo, te enviamos un código de un solo uso al correo de tu cuenta.</p>
          {error && (
            <p className="aviso" role="alert">
              {error}
            </p>
          )}
          <div className="acciones">
            <button className="btn" type="button" disabled={enviando} onClick={() => void pedirCodigo()}>
              {enviando ? "Enviando…" : "Enviarme el código"}
            </button>
            <button className="btn sec" type="button" disabled={enviando} onClick={onCerrar}>
              Cancelar
            </button>
          </div>
        </>
      )}

      {paso.n === "codigo" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (completo && !enviando) void confirmar();
          }}
        >
          {envio === "pendiente" && (
            <p className="cargando" role="status" aria-live="polite">
              <span className="ojo" aria-hidden="true" />
              {/* Un solo elemento: dentro del flex de .cargando, el <strong> suelto iría en otra columna */}
              <span>
                Enviando el código a <strong>{paso.destino}</strong>…
              </span>
            </p>
          )}
          {envio === "enviado" && (
            <p>
              Hemos enviado un código de 6 cifras a <strong>{paso.destino}</strong>. Caduca en 10 minutos y solo sirve para esta acción.
            </p>
          )}
          {envio === "tarda" && (
            <p role="status">
              Está tardando más de lo normal. Mira tu correo dentro de un momento, también en correo no deseado, o pide otro código.
            </p>
          )}
          {(envio === "fallido" || envio === "ninguno") && (
            <p className="aviso" role="alert">
              {envio === "fallido" ? "No hemos podido enviarte el código." : "Ese código ya no está vigente."} Pide otro o escríbenos a soporte@fichaitor.es.
            </p>
          )}
          <label className="rotulo" htmlFor="codigo-baja">
            Código
          </label>
          <input
            id="codigo-baja"
            ref={campo}
            className="codigo"
            value={codigo}
            onChange={(e) => setCodigo(e.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric"
            autoComplete="one-time-code"
            pattern="\d{6}"
            maxLength={6}
            aria-describedby="codigo-ayuda"
            aria-invalid={error !== null}
            disabled={enviando}
          />
          <p className="sec" id="codigo-ayuda">
            ¿No te llega? Mira en correo no deseado o{" "}
            <button className="enlace" type="button" disabled={enviando} onClick={() => void pedirCodigo()}>
              pide otro código
            </button>
            .
          </p>
          {error && (
            <p className="aviso" role="alert">
              {error}
            </p>
          )}
          <div className="acciones">
            <button className={alcance === "cuenta" ? "btn peligro-lleno" : "btn"} type="submit" disabled={!completo || enviando}>
              {enviando ? "Confirmando…" : t.confirmar}
            </button>
            <button className="btn sec" type="button" disabled={enviando} onClick={onCerrar}>
              Cancelar
            </button>
          </div>
        </form>
      )}

      {paso.n === "hecho" && (
        <>
          <p className="lead">Revocaremos el acceso a tu cuenta de Google el {cuando(paso.borrado)}.</p>
          <p>Te hemos enviado un correo con el aviso y un enlace para restaurarla. Si no has sido tú, úsalo: basta un clic.</p>
          <div className="acciones">
            <button className="btn" type="button" onClick={onHecho}>
              Entendido
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

/** Ventana modal de la marca: esquinas rectas, borde de 2 px y sombra dura. Foco atrapado y cierre con Escape. */
export function Modal({ titulo, children, onCerrar, bloqueado }: { titulo: string; children: ReactNode; onCerrar: () => void; bloqueado: boolean }) {
  const caja = useRef<HTMLDivElement>(null);
  const idTitulo = useId();

  useEffect(() => {
    const anterior = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const reanudar = pausarScroll();
    caja.current?.focus();
    return () => {
      reanudar();
      anterior?.focus();
    };
  }, []);

  useEffect(() => {
    const tecla = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !bloqueado) {
        onCerrar();
        return;
      }
      if (e.key !== "Tab" || !caja.current) return;
      const enfocables = Array.from(caja.current.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]"));
      const primero = enfocables[0];
      const ultimo = enfocables[enfocables.length - 1];
      if (!primero || !ultimo) return;
      if (e.shiftKey && (document.activeElement === primero || document.activeElement === caja.current)) {
        e.preventDefault();
        ultimo.focus();
      } else if (!e.shiftKey && document.activeElement === ultimo) {
        e.preventDefault();
        primero.focus();
      }
    };
    addEventListener("keydown", tecla);
    return () => removeEventListener("keydown", tecla);
  }, [bloqueado, onCerrar]);

  return createPortal(
    <div className="velo" onMouseDown={(e) => e.target === e.currentTarget && !bloqueado && onCerrar()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={idTitulo} tabIndex={-1} ref={caja} data-lenis-prevent>
        <h2 className="cartel" id={idTitulo}>
          {titulo}
          <span className="pt">.</span>
        </h2>
        {children}
      </div>
    </div>,
    document.body,
  );
}
