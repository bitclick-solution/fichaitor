import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import qrcode from "qrcode-generator";
import { ErrorAdmin } from "./api";

// Piezas comunes del backoffice. Sin estilos en línea: la CSP solo permite hojas de estilo propias.

// ── Formatos ──

const fechaHora = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
const soloFecha = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "short", year: "numeric" });
const soloFechaUTC = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

/** Fecha y hora local, o «—». */
export function cuando(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : fechaHora.format(d);
}

/** Solo la fecha (para «AAAA-MM-DD» se lee en UTC). */
export function dia(iso: string | null | undefined): string {
  if (!iso) return "—";
  const sinHora = /^\d{4}-\d{2}-\d{2}$/.test(iso);
  const d = new Date(sinHora ? `${iso}T00:00:00Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : (sinHora ? soloFechaUTC : soloFecha).format(d);
}

/** Céntimos → «35,00 €». null → «—». */
export function euros(centimos: number | null | undefined): string {
  if (centimos === null || centimos === undefined) return "—";
  return new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR" }).format(centimos / 100);
}

export const numero = (n: number) => new Intl.NumberFormat("es-ES").format(n);

export const PLAN: Record<string, string> = { gratis: "Gratis", pro: "Pro", pro_whatsapp: "Pro+" };
export const plan = (p: string | null | undefined) => (p ? (PLAN[p] ?? p) : "—");

/** Mensaje de un error para enseñarlo tal cual (el `message` del servidor nunca lleva datos personales). */
export function mensajeDe(e: unknown, porDefecto = "Algo ha fallado. Prueba de nuevo."): string {
  if (e instanceof ErrorAdmin) {
    if (e.codigo === "validation" && e.campos.length > 0) return `${e.message} Campos: ${e.campos.join(", ")}.`;
    return e.message;
  }
  // `Error` en este módulo es el componente; el de JavaScript es globalThis.Error.
  return e instanceof globalThis.Error && e.message ? e.message : porDefecto;
}

// ── Referencia del ticket ──

/** De 3 a `max` caracteres, en una línea y sin direcciones de correo. null si vale; si no, por qué. */
export function problemaTicket(t: string, max = 500): string | null {
  const v = t.trim();
  if (v.length < 3) return "Escribe la referencia del ticket (3 caracteres como mínimo).";
  if (v.length > max) return `La referencia es demasiado larga (${max} caracteres como máximo).`;
  // Las mismas reglas que admin_ticket en la base: sin caracteres de control y sin «algo@algo».
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(t)) return "La referencia va en una sola línea, sin tabuladores.";
  if (/\S+@\S+/.test(v)) return "La referencia no puede llevar direcciones de correo (nada con «@»).";
  return null;
}

export function CampoTicket({ valor, onCambio, max = 500, etiqueta = "Referencia del ticket" }: { valor: string; onCambio: (v: string) => void; max?: number; etiqueta?: string }) {
  const id = useId();
  return (
    <label className="campo" htmlFor={id}>
      <span>{etiqueta}</span>
      <input id={id} type="text" value={valor} maxLength={max} onChange={(e) => onCambio(e.target.value.replace(/[\u0000-\u001f\u007f]/g, " "))} placeholder="Ticket 4321" autoComplete="off" />
      <small>Se guarda en la auditoría. Sin direcciones de correo.</small>
    </label>
  );
}

// ── Ventana ──

export function Ventana({ titulo, children, onCerrar, bloqueada = false }: { titulo: string; children: ReactNode; onCerrar: () => void; bloqueada?: boolean }) {
  const caja = useRef<HTMLDivElement>(null);
  const idTitulo = useId();
  useEffect(() => {
    const anterior = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // El foco va al primer campo de texto (casi siempre la referencia del ticket); si no hay, a la ventana.
    const campo = caja.current?.querySelector<HTMLElement>("input:not([type=checkbox]):not([type=radio]):not([disabled]), textarea");
    (campo ?? caja.current)?.focus();
    return () => anterior?.focus();
  }, []);
  useEffect(() => {
    const tecla = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !bloqueada) onCerrar();
      // Tab no sale de la ventana: lo de detrás sigue en la página, pero no se alcanza con el teclado.
      if (e.key === "Tab" && caja.current) {
        const enfocables = [...caja.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])")];
        const primero = enfocables[0];
        const ultimo = enfocables[enfocables.length - 1];
        if (!primero || !ultimo) return;
        const dentro = caja.current.contains(document.activeElement);
        if (e.shiftKey && (document.activeElement === primero || !dentro)) {
          e.preventDefault();
          ultimo.focus();
        } else if (!e.shiftKey && (document.activeElement === ultimo || !dentro)) {
          e.preventDefault();
          primero.focus();
        }
      }
    };
    addEventListener("keydown", tecla);
    return () => removeEventListener("keydown", tecla);
  }, [onCerrar, bloqueada]);
  return (
    <div className="velo" onMouseDown={(e) => e.target === e.currentTarget && !bloqueada && onCerrar()}>
      <div className="ventana" role="dialog" aria-modal="true" aria-labelledby={idTitulo} tabIndex={-1} ref={caja}>
        <h2 id={idTitulo}>{titulo}</h2>
        {children}
      </div>
    </div>
  );
}

// ── Código TOTP ──

export function CampoCodigo({ valor, onCambio, autoFocus = false }: { valor: string; onCambio: (v: string) => void; autoFocus?: boolean }) {
  const id = useId();
  return (
    <label className="campo" htmlFor={id}>
      <span>Código de 6 dígitos de tu app de autenticación</span>
      <input
        id={id}
        className="codigo"
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]{6}"
        maxLength={6}
        value={valor}
        autoFocus={autoFocus}
        onChange={(e) => onCambio(e.target.value.replace(/\D/g, "").slice(0, 6))}
      />
    </label>
  );
}

/** El diálogo que pide el código para una acción sensible (403 `totp_required`). */
export function DialogoCodigo({ onCodigo, onCancelar, fallo = false }: { onCodigo: (c: string) => void; onCancelar: () => void; fallo?: boolean }) {
  const [codigo, setCodigo] = useState("");
  return (
    <Ventana titulo="Confirma con tu código" onCerrar={onCancelar}>
      <p>Esta acción es sensible y tu último código tiene más de 5 minutos.</p>
      <Error texto={fallo ? "Ese código no vale o ya se ha usado. Espera al siguiente de tu app." : null} />
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (codigo.length === 6) onCodigo(codigo);
        }}
      >
        <CampoCodigo valor={codigo} onCambio={setCodigo} autoFocus />
        <div className="acciones">
          <button className="btn" type="submit" disabled={codigo.length !== 6}>
            Confirmar
          </button>
          <button className="btn sec" type="button" onClick={onCancelar}>
            Cancelar
          </button>
        </div>
      </form>
    </Ventana>
  );
}

/** QR en SVG, dibujado con rectángulos: sin HTML inyectado, sin imágenes externas y sin estilos en línea. */
export function QR({ texto, etiqueta }: { texto: string; etiqueta: string }) {
  const q = qrcode(0, "M");
  q.addData(texto);
  q.make();
  const n = q.getModuleCount();
  const celdas: JSX.Element[] = [];
  for (let f = 0; f < n; f++) for (let c = 0; c < n; c++) if (q.isDark(f, c)) celdas.push(<rect key={`${f}-${c}`} x={c} y={f} width={1} height={1} />);
  return (
    <svg className="qr" viewBox={`-4 -4 ${n + 8} ${n + 8}`} role="img" aria-label={etiqueta} shapeRendering="crispEdges">
      <rect x={-4} y={-4} width={n + 8} height={n + 8} className="qr-fondo" />
      {celdas}
    </svg>
  );
}

/**
 * Los 10 códigos de recuperación: se enseñan una sola vez. Se pueden copiar (para el gestor de
 * contraseñas) o descargar en JSON; la descarga avisa de sacar el archivo de este ordenador.
 */
export function CodigosRecuperacion({ codigos, usuario, onListo, etiquetaListo = "Ya los he guardado" }: { codigos: string[]; usuario: string; onListo: () => void; etiquetaListo?: string }) {
  const [guardados, setGuardados] = useState(false);
  const [copiados, setCopiados] = useState<boolean | null>(null);
  const [descargado, setDescargado] = useState(false);
  const descargar = () => {
    const datos = {
      servicio: "Fichaitor backoffice",
      ...(usuario ? { usuario } : {}),
      creados: new Date().toISOString(),
      nota: "Códigos de recuperación. Cada uno sirve una sola vez. Guárdalos fuera de este ordenador.",
      codigos,
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(datos, null, 2) + "\n"], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `fichaitor-backoffice-recuperacion${usuario ? `-${usuario}` : ""}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setDescargado(true);
  };
  const copiar = () => {
    const texto = `Fichaitor · Backoffice: códigos de recuperación${usuario ? ` de ${usuario}` : ""} (cada uno sirve una sola vez)\n${codigos.join("\n")}`;
    navigator.clipboard.writeText(texto).then(
      () => setCopiados(true),
      () => setCopiados(false),
    );
  };
  return (
    <div className="recuperacion">
      <p className="aviso">
        Estos códigos solo se enseñan ahora. Guárdalos fuera de este ordenador (en tu gestor de contraseñas o en papel). Cada uno sirve una sola vez si
        pierdes tu app de autenticación.
      </p>
      <ol className="codigos">
        {codigos.map((c) => (
          <li key={c}>
            <code>{c}</code>
          </li>
        ))}
      </ol>
      <div className="acciones">
        <button className="btn sec" type="button" onClick={descargar}>
          Descargar en JSON
        </button>
        <button className="btn sec" type="button" onClick={copiar}>
          Copiar para el gestor de contraseñas
        </button>
      </div>
      {descargado && (
        <p className="sec" role="status">
          Descargado. Mueve el archivo a tu gestor de contraseñas o a un USB y bórralo de Descargas.
        </p>
      )}
      {copiados === true && (
        <p className="sec" role="status">
          Copiados. Pégalos en tu gestor de contraseñas y no los dejes en ningún archivo de este ordenador.
        </p>
      )}
      {copiados === false && <p className="sec">No se han podido copiar: apúntalos a mano.</p>}
      <label className="casilla">
        <input type="checkbox" checked={guardados} onChange={(e) => setGuardados(e.target.checked)} />
        <span>Los he guardado fuera de este ordenador.</span>
      </label>
      <div className="acciones">
        <button className="btn" type="button" disabled={!guardados} onClick={onListo}>
          {etiquetaListo}
        </button>
      </div>
    </div>
  );
}

/** QR del TOTP con el secreto debajo, para teclearlo a mano. */
export function QRTotp({ uri, secreto }: { uri: string; secreto: string }) {
  return (
    <div className="totp">
      <QR texto={uri} etiqueta="Código QR para tu app de autenticación" />
      <div>
        <p>Escanéalo con tu app de autenticación (Google Authenticator, 1Password, Aegis…).</p>
        <p className="sec">Si no puedes escanearlo, escribe este secreto:</p>
        <p>
          <code className="secreto">{secreto.replace(/(.{4})/g, "$1 ").trim()}</code>
        </p>
      </div>
    </div>
  );
}

// ── Pequeñas piezas de pantalla ──

export function Cargando({ texto = "Cargando…" }: { texto?: string }) {
  return (
    <p className="cargando" role="status">
      {texto}
    </p>
  );
}

export function Error({ texto }: { texto: string | null }) {
  if (!texto) return null;
  return (
    <p className="error" role="alert">
      {texto}
    </p>
  );
}

/** Un enlace a Stripe u Odoo: solo https, en otra pestaña y sin Referer. */
export function Externo({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  if (!href || !href.startsWith("https://")) return <>{children}</>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

export function Cifra({ valor, etiqueta, alerta = false }: { valor: ReactNode; etiqueta: string; alerta?: boolean }) {
  return (
    <div className="cifra" data-alerta={alerta}>
      <strong>{valor}</strong>
      <span>{etiqueta}</span>
    </div>
  );
}
