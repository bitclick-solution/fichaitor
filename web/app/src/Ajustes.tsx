import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api, ApiError, type AlcanceBaja, type BajaPendiente } from "./api";
import { pausarScroll, Seccion } from "./movimiento";
import { EVENTO_RECORRIDO } from "./Tour";

// Ajustes del panel: desconectar Google y borrar la cuenta.
//
// PROPUESTA de interfaz y de contrato (2026-11-12), pendiente de que el backend la construya.
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

const TEXTOS: Record<AlcanceBaja, { boton: string; titulo: string; que: string[]; confirmar: string; hecho: string }> = {
  ficha: {
    boton: "Desconectar Google",
    titulo: "Desconectar tu ficha de Google",
    que: [
      "Fichaitor deja de leer tu ficha y de avisarte desde este momento.",
      "Durante 48 horas puedes restaurarla, desde aquí o desde el correo que te enviamos.",
      "Pasado ese plazo, se retira el acceso en Google y se borran tus reseñas guardadas, los resúmenes y el historial.",
      "Lo que ya está publicado en tu ficha no cambia: sigue en Google.",
    ],
    confirmar: "Desconectar",
    hecho: "Ficha desconectada",
  },
  cuenta: {
    boton: "Borrar mi cuenta",
    titulo: "Borrar tu cuenta de Fichaitor",
    que: [
      "Se desconectan todas tus fichas y se cierra tu sesión en todos los dispositivos.",
      "Durante 48 horas puedes restaurar la cuenta desde el correo que te enviamos.",
      "Pasado ese plazo, se borran la cuenta y todos sus datos. No se puede deshacer.",
      "Lo que ya está publicado en tus fichas no cambia: sigue en Google.",
    ],
    confirmar: "Borrar mi cuenta",
    hecho: "Cuenta en proceso de baja",
  },
};

export function Ajustes({
  email,
  onSinSesion,
  onCambio,
  onDespedida,
}: {
  email: string;
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
        Has entrado como <strong>{email}</strong>.{" "}
        <button className="enlace" type="button" onClick={() => dispatchEvent(new Event(EVENTO_RECORRIDO))}>
          Ver el recorrido del panel
        </button>
      </p>
      <div className="ajustes-lista">
        <div>
          <h3 className="rotulo">Desconectar Google</h3>
          <p className="sec">Fichaitor deja de leer esta ficha. Tienes 48 horas para arrepentirte.</p>
          <button className="btn sec" type="button" onClick={() => setAbierto("ficha")}>
            Desconectar Google
          </button>
        </div>
        <div>
          <h3 className="rotulo">Borrar mi cuenta</h3>
          <p className="sec">Se borran la cuenta y todos sus datos, pasadas 48 horas.</p>
          <button className="btn sec peligro" type="button" onClick={() => setAbierto("cuenta")}>
            Borrar mi cuenta
          </button>
        </div>
      </div>

      {abierto && (
        <ModalBaja
          alcance={abierto}
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
      else setError(e instanceof Error ? e.message : "No hemos podido restaurarla.");
      setEnviando(false);
    }
  };

  return (
    <div className="aviso-baja" role="alert">
      <div>
        <p className="rotulo">{baja.scope === "cuenta" ? "Tu cuenta está en proceso de baja" : "Esta ficha está desconectada"}</p>
        <p>
          Se borrará el {cuando(baja.delete_at)}. Hasta entonces puedes restaurarla y todo sigue como estaba.
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

function ModalBaja({
  alcance,
  onCerrar,
  onHecho,
  onDespedida,
  onSinSesion,
}: {
  alcance: AlcanceBaja;
  onCerrar: () => void;
  onHecho: () => void;
  onDespedida: (borrado: string) => void;
  onSinSesion: () => void;
}) {
  const t = TEXTOS[alcance];
  const [paso, setPaso] = useState<Paso>({ n: "explicar" });
  const [codigo, setCodigo] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const campo = useRef<HTMLInputElement>(null);

  const fallo = (e: unknown, porDefecto: string) => {
    if (e instanceof ApiError && e.status === 401) onSinSesion();
    else setError(e instanceof Error ? e.message : porDefecto);
  };

  const pedirCodigo = async () => {
    setEnviando(true);
    setError(null);
    try {
      const r = await api.pedirCodigoBaja(alcance);
      setCodigo("");
      setPaso({ n: "codigo", destino: r.sent_to });
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
          <p>
            Hemos enviado un código de 6 cifras a <strong>{paso.destino}</strong>. Caduca en 10 minutos y solo sirve para esta acción.
          </p>
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
          <p className="lead">Se borrará el {cuando(paso.borrado)}.</p>
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
      const enfocables = Array.from(caja.current.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), a[href]"));
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
