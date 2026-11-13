import { useEffect, useId, useRef, useState } from "react";
import qrcode from "qrcode-generator";
import { api, ApiError, GENERICO, type Canal, type CodigoVinculacion, type Location } from "./api";
import { Modal } from "./Ajustes";
import { Seccion } from "./movimiento";

// «Avisos»: los canales por los que Fichaitor escribe al dueño (Telegram y, más adelante, WhatsApp).
// Contrato: docs/contrato-web-api.md, «Canales de mensajería». La vinculación es la misma en los dos
// canales: un código de un solo uso, válido 10 minutos, que el dueño canjea desde su propio chat.
// El enlace (`telegram_url`) lleva un testigo largo, no las 6 cifras: se usa tal cual para el botón y el
// QR, y las cifras solo se muestran para teclearlas. Nada de esto se guarda en el navegador.

const NOMBRE: Record<Canal["channel"], string> = {
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  email: "Correo",
};
const fecha = new Intl.DateTimeFormat("es-ES", {
  day: "numeric",
  month: "long",
  year: "numeric",
});

/** Tope del contrato: de 1 a 50 fichas por chat. */
const MAX_NEGOCIOS = 50;

/**
 * Error de un filtro de negocios. Un 404 o un 422 con texto suelen querer decir que la lista de fichas ha
 * cambiado (una desactivada o que Google ya no muestra): se pide de nuevo la cuenta. Un 422 sin texto es
 * una lista fuera de límites.
 */
function errorDeFiltro(e: unknown, onRecargarCuenta: () => void): string {
  if (e instanceof ApiError && (e.status === 404 || e.status === 422)) {
    if (e.status === 422 && e.message === GENERICO) return `Elige entre 1 y ${MAX_NEGOCIOS} negocios activos.`;
    onRecargarCuenta();
    return `${e.message} Hemos puesto al día la lista de tus negocios: revísala y vuelve a intentarlo.`;
  }
  return e instanceof Error ? e.message : "No hemos podido guardarlo. Prueba otra vez.";
}

function hace(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : fecha.format(d);
}

export function Canales({
  negocios: negociosPanel,
  onSinSesion,
}: {
  /** Multinegocio: las fichas activas de la cuenta, entre las que se elige qué ve cada chat. Vacía sin multinegocio. */
  negocios: Location[];
  onSinSesion: () => void;
}) {
  // Tras un filtro rechazado (una ficha desactivada o que Google ya no muestra), la lista de fichas activas se
  // pide aquí mismo: recargar la cuenta entera desmontaría el panel y cerraría el modal.
  const [alDia, setAlDia] = useState<Location[] | null>(null);
  const negocios = alDia ?? negociosPanel;
  const onRecargarCuenta = () => {
    if (negociosPanel.length === 0) return;
    api
      .me()
      .then((me) => setAlDia(me.locations.filter((l) => l.active)))
      .catch(() => {
        /* si falla, se queda la lista que había */
      });
  };
  const [canales, setCanales] = useState<Canal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vinculando, setVinculando] = useState<"telegram" | "whatsapp" | null>(null);
  const [quitando, setQuitando] = useState<string | null>(null);
  // Desconectar pide confirmación: desde ese momento el bot deja de escribir a ese chat.
  const [confirmando, setConfirmando] = useState<Canal | null>(null);
  // Multinegocio: el chat cuyos negocios se están cambiando.
  const [filtrando, setFiltrando] = useState<Canal | null>(null);
  // Hay algo que elegir con dos o más fichas activas, o si el chat tiene filtro (para volver a «todos»).
  const puedeElegir = (c: Canal) => negocios.length > 1 || (negocios.length > 0 && (c.locations ?? null) !== null);

  // Si el chat que se está editando ya no está en la lista (desconectado desde otro sitio), se cierra.
  useEffect(() => {
    if (filtrando && canales && !canales.some((c) => c.id === filtrando.id)) setFiltrando(null);
  }, [canales, filtrando]);

  const cargar = async () => {
    try {
      setCanales(await api.canales());
      setError(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // Si la ruta aún no existe, la sección se oculta en lugar de mostrar un error.
      else if (e instanceof ApiError && e.status === 404) setCanales(null);
      else setError(e instanceof Error ? e.message : "No hemos podido leer tus avisos.");
    }
  };

  useEffect(() => {
    void cargar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const quitar = async (c: Canal) => {
    setQuitando(c.id);
    try {
      await api.desvincularCanal(c.id);
      setConfirmando(null);
      await cargar();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 404: ya no estaba conectado (o se quitó desde otro sitio). Basta con refrescar la lista.
      else if (e instanceof ApiError && e.status === 404) {
        setConfirmando(null);
        await cargar();
      } else {
        setConfirmando(null);
        setError(e instanceof Error ? e.message : "No hemos podido desconectarlo.");
      }
    } finally {
      setQuitando(null);
    }
  };

  if (canales === null && !error) return null;
  const lista = canales ?? [];
  const hay = (canal: Canal["channel"]) => lista.some((c) => c.channel === canal);

  return (
    <Seccion id="avisos" className="avisos">
      <h2 className="rotulo">Avisos</h2>
      <p className="sec">Por dónde te escribe Fichaitor cuando hay algo que decidir. Puedes conectar más de uno: el tuyo y el de tu encargado.</p>

      {error && (
        <p className="aviso" role="alert">
          {error}
        </p>
      )}

      {lista.length > 0 && (
        <ul className="canales-lista">
          {lista.map((c) => (
            <li key={c.id}>
              <div>
                <p className="rotulo">{NOMBRE[c.channel]}</p>
                <p className="sec">
                  {c.address} · conectado el {hace(c.linked_at)}
                </p>
                {c.locations_label && (negocios.length > 1 || (c.locations ?? null) !== null) && <p className="canal-recibe">Recibe: {c.locations_label}</p>}
              </div>
              <div className="canal-acciones">
                {puedeElegir(c) && (
                  <button className="enlace" type="button" onClick={() => setFiltrando(c)}>
                    Cambiar negocios
                  </button>
                )}
                <button className="enlace" type="button" onClick={() => setConfirmando(c)}>
                  Desconectar
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="ajustes-lista">
        <div>
          <h3 className="rotulo">Telegram</h3>
          <p className="sec">Gratis, en todos los planes. Te escribe con botones para responder en un toque.</p>
          <button className={hay("telegram") ? "btn sec" : "btn"} type="button" onClick={() => setVinculando("telegram")}>
            {hay("telegram") ? "Conectar otro Telegram" : "Conectar Telegram"}
          </button>
        </div>
        <div>
          <h3 className="rotulo">WhatsApp</h3>
          <p className="sec">Con el plan Pro+. Te escribe al WhatsApp de tu negocio.</p>
          <button className="btn sec" type="button" onClick={() => setVinculando("whatsapp")}>
            {hay("whatsapp") ? "Conectar otro WhatsApp" : "Conectar WhatsApp"}
          </button>
        </div>
      </div>

      {confirmando && (
        <Modal titulo={`Desconectar ${NOMBRE[confirmando.channel]}`} onCerrar={() => setConfirmando(null)} bloqueado={quitando !== null}>
          <p>
            Dejarás de recibir avisos en este chat (<strong>{confirmando.address}</strong>). Puedes volver a conectarlo cuando quieras.
          </p>
          <div className="acciones">
            <button className="btn" type="button" disabled={quitando !== null} onClick={() => void quitar(confirmando)}>
              {quitando ? "Desconectando…" : "Desconectar"}
            </button>
            <button className="btn sec" type="button" disabled={quitando !== null} onClick={() => setConfirmando(null)}>
              Cancelar
            </button>
          </div>
        </Modal>
      )}

      {filtrando && (
        <ModalFiltro
          canal={filtrando}
          negocios={negocios}
          onCerrar={() => setFiltrando(null)}
          onSinSesion={onSinSesion}
          onGuardado={(c) => {
            setFiltrando(null);
            setCanales((antes) => (antes ?? []).map((x) => (x.id === c.id ? c : x)));
          }}
          onRecargar={() => {
            onRecargarCuenta();
            void cargar();
          }}
        />
      )}

      {vinculando && (
        <ModalVincular
          canal={vinculando}
          negocios={negocios}
          onRecargarCuenta={onRecargarCuenta}
          antes={lista.map((c) => c.id)}
          onCerrar={() => setVinculando(null)}
          onSinSesion={onSinSesion}
          onVinculado={() => {
            setVinculando(null);
            void cargar();
          }}
        />
      )}
    </Seccion>
  );
}

/**
 * Qué negocios ve un chat: todos (también los que se activen después) o solo algunos, entre los activos.
 * La lista que se envía va en el orden de `negocios`.
 */
function ElegirNegocios({
  negocios,
  todas,
  elegidas,
  onTodas,
  onElegidas,
}: {
  negocios: Location[];
  todas: boolean;
  elegidas: string[];
  onTodas: (todas: boolean) => void;
  onElegidas: (ids: string[]) => void;
}) {
  const id = useId();
  return (
    <fieldset className="fecha-opciones">
      <legend className="lead">¿De qué negocios te avisa este chat?</legend>
      <div className="opciones opciones-dos">
        <label className="opcion">
          <input type="radio" name={`${id}-alcance`} checked={todas} onChange={() => onTodas(true)} />
          <span>
            Todos tus negocios
            <small>También los que actives después</small>
          </span>
        </label>
        <label className="opcion">
          <input type="radio" name={`${id}-alcance`} checked={!todas} onChange={() => onTodas(false)} />
          <span>
            Solo algunos
            <small>Por ejemplo, el chat de un encargado</small>
          </span>
        </label>
      </div>
      {!todas && (
        <div className="opciones opciones-lista" role="group" aria-label="Negocios de este chat">
          {negocios.map((n) => (
            <label className="opcion" key={n.id}>
              <input
                type="checkbox"
                checked={elegidas.includes(n.id)}
                disabled={!elegidas.includes(n.id) && elegidas.length >= MAX_NEGOCIOS}
                onChange={(ev) => onElegidas(ev.target.checked ? [...elegidas, n.id] : elegidas.filter((x) => x !== n.id))}
              />
              <span>{n.title}</span>
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}

const ordenadas = (negocios: Location[], ids: string[]) => negocios.filter((n) => ids.includes(n.id)).map((n) => n.id);

/** Pista del botón desactivado con «Solo algunos» y ninguno marcado: se anuncia y queda asociada al botón. */
function PistaVacia({ id, visible }: { id: string; visible: boolean }) {
  return (
    <p className="sec" id={id} role="status">
      {visible ? "Elige al menos un negocio." : ""}
    </p>
  );
}

/** Cambiar los negocios de un chat ya conectado (PUT …/locations). Las fichas desactivadas salen del filtro. */
function ModalFiltro({
  canal,
  negocios,
  onCerrar,
  onGuardado,
  onRecargar,
  onSinSesion,
}: {
  canal: Canal;
  negocios: Location[];
  onCerrar: () => void;
  onGuardado: (c: Canal) => void;
  /** Tras un 404 o un 422: pedir de nuevo la cuenta y los chats (si el chat ya no existe, el modal se cierra). */
  onRecargar: () => void;
  onSinSesion: () => void;
}) {
  const pista = useId();
  const activos = negocios.map((n) => n.id);
  const filtro = canal.locations ?? null;
  const [todas, setTodas] = useState(filtro === null);
  // Se parte del filtro del chat sin las fichas desactivadas: con ellas, el servidor responde 422.
  const [elegidas, setElegidas] = useState(() => (filtro ?? []).filter((id) => activos.includes(id)));
  const quitadas = (filtro ?? []).some((id) => !activos.includes(id));
  const [guardando, setGuardando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Con la lista al día, solo cuentan las marcadas que siguen activas.
  const vacio = !todas && ordenadas(negocios, elegidas).length === 0;

  const guardar = async () => {
    setGuardando(true);
    setError(null);
    try {
      onGuardado(await api.filtrarCanal(canal.id, todas ? null : ordenadas(negocios, elegidas)));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      else setError(errorDeFiltro(e, onRecargar));
    } finally {
      setGuardando(false);
    }
  };

  return (
    <Modal titulo="Negocios de este chat" onCerrar={onCerrar} bloqueado={guardando}>
      <p>
        Elige de qué negocios te avisa <strong>{canal.address}</strong>. Los demás chats no cambian.
      </p>
      {quitadas && <p className="sec">Hemos quitado de la lista los negocios desactivados: solo se pueden elegir los activos.</p>}
      <ElegirNegocios negocios={negocios} todas={todas} elegidas={elegidas} onTodas={setTodas} onElegidas={setElegidas} />
      {error && (
        <p className="aviso" role="alert">
          {error}
        </p>
      )}
      <div className="acciones">
        <button className="btn" type="button" disabled={guardando || vacio} aria-describedby={pista} onClick={() => void guardar()}>
          {guardando ? "Guardando…" : "Guardar"}
        </button>
        <button className="btn sec" type="button" disabled={guardando} onClick={onCerrar}>
          Cancelar
        </button>
      </div>
      <PistaVacia id={pista} visible={vacio} />
    </Modal>
  );
}

/** QR en SVG, dibujado con rectángulos: sin HTML inyectado ni imágenes externas. */
function QR({ texto }: { texto: string }) {
  const q = qrcode(0, "M");
  q.addData(texto);
  q.make();
  const n = q.getModuleCount();
  const celdas: JSX.Element[] = [];
  for (let f = 0; f < n; f++) for (let c = 0; c < n; c++) if (q.isDark(f, c)) celdas.push(<rect key={`${f}-${c}`} x={c} y={f} width={1} height={1} />);
  return (
    <svg className="qr" viewBox={`-2 -2 ${n + 4} ${n + 4}`} role="img" aria-label="Código QR para abrir el chat en el móvil" shapeRendering="crispEdges">
      <rect x={-2} y={-2} width={n + 4} height={n + 4} className="qr-fondo" />
      {celdas}
    </svg>
  );
}

type Estado =
  | { n: "elegir" }
  | { n: "pidiendo" }
  | { n: "esperando"; codigo: CodigoVinculacion }
  | { n: "caducado" }
  | { n: "hecho" }
  | { n: "no-disponible" }
  | { n: "error"; mensaje: string };

function ModalVincular({
  canal,
  negocios,
  onRecargarCuenta,
  antes,
  onCerrar,
  onVinculado,
  onSinSesion,
}: {
  canal: "telegram" | "whatsapp";
  negocios: Location[];
  onRecargarCuenta: () => void;
  antes: string[];
  onCerrar: () => void;
  onVinculado: () => void;
  onSinSesion: () => void;
}) {
  // Con dos o más fichas activas, antes del código se elige qué negocios verá el chat.
  const elegir = negocios.length > 1;
  const [estado, setEstado] = useState<Estado>(elegir ? { n: "elegir" } : { n: "pidiendo" });
  const [todas, setTodas] = useState(true);
  const [elegidas, setElegidas] = useState<string[]>([]);
  const [quedan, setQuedan] = useState(0);
  const vivo = useRef(true);
  // Los canales que ya había al abrir: fijos, para que un repintado del panel no reinicie el sondeo.
  const previos = useRef(antes);
  const pista = useId();
  const sinElegir = !todas && ordenadas(negocios, elegidas).length === 0;

  // Al cambiar de paso, el botón pulsado desaparece: el foco pasa al contenido nuevo para no salirse del modal.
  const paso = useRef<HTMLDivElement>(null);
  const primerPaso = useRef(true);
  useEffect(() => {
    if (primerPaso.current) {
      primerPaso.current = false;
      return;
    }
    paso.current?.focus();
  }, [estado.n]);

  const pedirCodigo = async () => {
    setEstado({ n: "pidiendo" });
    try {
      // El filtro viaja con el código: el chat lo recibe al vincularse. Pedir otro código lo repite.
      const c = await api.codigoVinculacion(elegir && !todas ? ordenadas(negocios, elegidas) : null);
      if (!vivo.current) return;
      const url = canal === "telegram" ? c.telegram_url : c.whatsapp_url;
      setEstado(url ? { n: "esperando", codigo: c } : { n: "no-disponible" });
    } catch (e) {
      if (!vivo.current) return;
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 429: 5 códigos por cuenta y hora. Se informa y no se reintenta solo.
      else if (e instanceof ApiError && e.status === 429)
        setEstado({
          n: "error",
          mensaje: e.message || "Has pedido muchos códigos. Prueba dentro de un rato.",
        });
      // 404 o 422: el filtro no vale (una ficha desactivada o que Google ya no muestra). No gasta código.
      else if (elegir && e instanceof ApiError && (e.status === 404 || e.status === 422)) setEstado({ n: "error", mensaje: errorDeFiltro(e, onRecargarCuenta) });
      else
        setEstado({
          n: "error",
          mensaje: e instanceof Error ? e.message : "No hemos podido generar el código.",
        });
    }
  };

  useEffect(() => {
    vivo.current = true;
    if (!elegir) void pedirCodigo();
    return () => {
      vivo.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Mientras se espera: cuenta atrás y comprobación cada 3 s de si ya está conectado.
  useEffect(() => {
    if (estado.n !== "esperando") return;
    const caduca = new Date(estado.codigo.expires_at).getTime();
    let parado = false;
    const reloj = setInterval(() => {
      const s = Math.max(0, Math.round((caduca - Date.now()) / 1000));
      setQuedan(s);
      if (s === 0) setEstado({ n: "caducado" });
    }, 1000);
    // Con la pestaña oculta no se consulta; al volver, se comprueba enseguida.
    const mirar = async () => {
      if (parado || document.hidden || Date.now() >= caduca) return;
      try {
        const ahora = await api.canales();
        if (!parado && ahora.some((c) => c.channel === canal && !previos.current.includes(c.id))) setEstado({ n: "hecho" });
      } catch (e) {
        if (!parado && e instanceof ApiError && e.status === 401) onSinSesion();
        /* cualquier otro fallo: se reintenta en la siguiente vuelta */
      }
    };
    const comprobar = setInterval(() => void mirar(), 3000);
    const alVolver = () => {
      if (!document.hidden) void mirar();
    };
    document.addEventListener("visibilitychange", alVolver);
    setQuedan(Math.max(0, Math.round((caduca - Date.now()) / 1000)));
    return () => {
      parado = true;
      clearInterval(reloj);
      clearInterval(comprobar);
      document.removeEventListener("visibilitychange", alVolver);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [estado, canal]);

  const nombre = NOMBRE[canal];
  const titulo = estado.n === "hecho" ? `${nombre} conectado` : `Conectar ${nombre}`;

  return (
    <Modal titulo={titulo} onCerrar={estado.n === "hecho" ? onVinculado : onCerrar} bloqueado={false}>
      <div className="paso-modal" ref={paso} tabIndex={-1}>
        {estado.n === "elegir" && (
          <>
            <ElegirNegocios negocios={negocios} todas={todas} elegidas={elegidas} onTodas={setTodas} onElegidas={setElegidas} />
            <div className="acciones">
              <button className="btn" type="button" disabled={sinElegir} aria-describedby={pista} onClick={() => void pedirCodigo()}>
                Pedir el código
              </button>
              <button className="btn sec" type="button" onClick={onCerrar}>
                Cancelar
              </button>
            </div>
            <PistaVacia id={pista} visible={sinElegir} />
          </>
        )}

        {estado.n === "pidiendo" && (
          <p className="cargando" role="status">
            <span className="ojo" aria-hidden="true" />
            Preparando tu código…
          </p>
        )}

        {estado.n === "esperando" && <Esperando canal={canal} codigo={estado.codigo} quedan={quedan} onCancelar={onCerrar} />}

        {estado.n === "caducado" && (
          <>
            <p className="lead">El código ha caducado sin usarse.</p>
            <div className="acciones">
              <button className="btn" type="button" onClick={() => void pedirCodigo()}>
                Pedir otro código
              </button>
              <button className="btn sec" type="button" onClick={onCerrar}>
                Cancelar
              </button>
            </div>
          </>
        )}

        {estado.n === "no-disponible" && (
          <>
            <p className="lead">{canal === "whatsapp" ? "WhatsApp todavía no está disponible." : "Telegram todavía no está disponible."}</p>
            <p>{canal === "whatsapp" ? "Llegará con el plan Pro+. Mientras tanto, puedes recibir los avisos por Telegram." : "Vuelve a intentarlo más tarde."}</p>
            <div className="acciones">
              <button className="btn sec" type="button" onClick={onCerrar}>
                Entendido
              </button>
            </div>
          </>
        )}

        {estado.n === "hecho" && (
          <>
            <p className="lead">A partir de ahora, Fichaitor te escribe por {nombre} cuando haya algo que decidir.</p>
            <p>Nunca cambia nada en tu ficha sin que lo confirmes desde el chat o desde aquí.</p>
            <div className="acciones">
              <button className="btn" type="button" onClick={onVinculado}>
                Hecho
              </button>
            </div>
          </>
        )}

        {estado.n === "error" && (
          <>
            <p className="aviso" role="alert">
              {estado.mensaje}
            </p>
            <div className="acciones">
              {/* Si con la lista al día ya no queda ninguna marcada, solo cabe volver a elegir. */}
              {!sinElegir && (
                <button className="btn" type="button" onClick={() => void pedirCodigo()}>
                  Reintentar
                </button>
              )}
              {elegir && (
                <button className={sinElegir ? "btn" : "btn sec"} type="button" onClick={() => setEstado({ n: "elegir" })}>
                  Cambiar los negocios
                </button>
              )}
              <button className="btn sec" type="button" onClick={onCerrar}>
                Cancelar
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

function Esperando({ canal, codigo, quedan, onCancelar }: { canal: "telegram" | "whatsapp"; codigo: CodigoVinculacion; quedan: number; onCancelar: () => void }) {
  const url = (canal === "telegram" ? codigo.telegram_url : codigo.whatsapp_url) ?? "";
  const min = Math.floor(quedan / 60);
  const seg = String(quedan % 60).padStart(2, "0");
  const cifras = `${codigo.code.slice(0, 3)} ${codigo.code.slice(3)}`;

  return (
    <div className="vincular">
      <ol className="pasos-vincular">
        <li>
          <span>Abre el chat de Fichaitor desde el móvil donde quieres recibir los avisos.</span>
        </li>
        <li>
          <span>{canal === "telegram" ? "Pulsa «Iniciar». El código viaja solo." : "Envía el mensaje con el código que aparece escrito."}</span>
        </li>
      </ol>
      <div className="vincular-medios">
        <QR texto={url} />
        <div>
          <a className="btn" href={url} target="_blank" rel="noopener noreferrer">
            Abrir {canal === "telegram" ? "Telegram" : "WhatsApp"}
          </a>
          <p className="sec">En el ordenador, escanea el código con la cámara del móvil.</p>
          <p className="sec">
            O escribe este código en el chat: <strong className="codigo-chico">{cifras}</strong>
          </p>
        </div>
      </div>
      <p className="cargando" role="status" aria-live="polite">
        <span className="ojo" aria-hidden="true" />
        Esperando a que lo envíes… caduca en {min}:{seg}
      </p>
      <div className="acciones">
        <button className="btn sec" type="button" onClick={onCancelar}>
          Cancelar
        </button>
      </div>
    </div>
  );
}
