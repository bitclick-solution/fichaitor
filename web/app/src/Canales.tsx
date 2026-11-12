import { useEffect, useRef, useState } from "react";
import qrcode from "qrcode-generator";
import { api, ApiError, type Canal, type CodigoVinculacion } from "./api";
import { Modal } from "./Ajustes";
import { Seccion } from "./movimiento";

// «Avisos»: los canales por los que Fichaitor escribe al dueño (Telegram y, más adelante, WhatsApp).
// Contrato: el previsto en docs/canales.md, «Paso 3. Rutas de la API». La vinculación es la misma en
// los dos canales: un código de 6 cifras de un solo uso, válido 10 minutos, que el dueño envía desde
// su propio chat. Enviarlo desde su teléfono demuestra que es suyo.

const NOMBRE: Record<Canal["channel"], string> = { telegram: "Telegram", whatsapp: "WhatsApp", email: "Correo" };
const fecha = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", year: "numeric" });

function hace(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : fecha.format(d);
}

export function Canales({ onSinSesion }: { onSinSesion: () => void }) {
  const [canales, setCanales] = useState<Canal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [vinculando, setVinculando] = useState<"telegram" | "whatsapp" | null>(null);
  const [quitando, setQuitando] = useState<string | null>(null);

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
      await cargar();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 404: ya no estaba conectado (o se quitó desde otro sitio). Basta con refrescar la lista.
      else if (e instanceof ApiError && e.status === 404) await cargar();
      else setError(e instanceof Error ? e.message : "No hemos podido desconectarlo.");
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
              </div>
              <button className="enlace" type="button" disabled={quitando === c.id} onClick={() => void quitar(c)}>
                {quitando === c.id ? "Desconectando…" : "Desconectar"}
              </button>
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
          <p className="sec">Con el plan Pro WhatsApp. Te escribe al WhatsApp de tu negocio.</p>
          <button className="btn sec" type="button" onClick={() => setVinculando("whatsapp")}>
            {hay("whatsapp") ? "Conectar otro WhatsApp" : "Conectar WhatsApp"}
          </button>
        </div>
      </div>

      {vinculando && (
        <ModalVincular
          canal={vinculando}
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

type Estado = { n: "pidiendo" } | { n: "esperando"; codigo: CodigoVinculacion } | { n: "caducado" } | { n: "hecho" } | { n: "no-disponible" } | { n: "error"; mensaje: string };

function ModalVincular({
  canal,
  antes,
  onCerrar,
  onVinculado,
  onSinSesion,
}: {
  canal: "telegram" | "whatsapp";
  antes: string[];
  onCerrar: () => void;
  onVinculado: () => void;
  onSinSesion: () => void;
}) {
  const [estado, setEstado] = useState<Estado>({ n: "pidiendo" });
  const [quedan, setQuedan] = useState(0);
  const vivo = useRef(true);

  const pedirCodigo = async () => {
    setEstado({ n: "pidiendo" });
    try {
      const c = await api.codigoVinculacion();
      if (!vivo.current) return;
      const url = canal === "telegram" ? c.telegram_url : c.whatsapp_url;
      setEstado(url ? { n: "esperando", codigo: c } : { n: "no-disponible" });
    } catch (e) {
      if (!vivo.current) return;
      if (e instanceof ApiError && e.status === 401) onSinSesion();
      // 429: 5 códigos por cuenta y hora. Se informa y no se reintenta solo.
      else if (e instanceof ApiError && e.status === 429)
        setEstado({ n: "error", mensaje: e.message || "Has pedido muchos códigos. Prueba dentro de un rato." });
      else setEstado({ n: "error", mensaje: e instanceof Error ? e.message : "No hemos podido generar el código." });
    }
  };

  useEffect(() => {
    vivo.current = true;
    void pedirCodigo();
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
        if (!parado && ahora.some((c) => c.channel === canal && !antes.includes(c.id))) setEstado({ n: "hecho" });
      } catch {
        /* se reintenta en la siguiente vuelta */
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
  }, [estado, canal, antes]);

  const nombre = NOMBRE[canal];
  const titulo = estado.n === "hecho" ? `${nombre} conectado` : `Conectar ${nombre}`;

  return (
    <Modal titulo={titulo} onCerrar={estado.n === "hecho" ? onVinculado : onCerrar} bloqueado={false}>
      {estado.n === "pidiendo" && (
        <p className="cargando" role="status">
          <span className="ojo" aria-hidden="true" />
          Preparando tu código…
        </p>
      )}

      {estado.n === "esperando" && (
        <Esperando canal={canal} codigo={estado.codigo} quedan={quedan} onCancelar={onCerrar} />
      )}

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
          <p>{canal === "whatsapp" ? "Llegará con el plan Pro WhatsApp. Mientras tanto, puedes recibir los avisos por Telegram." : "Vuelve a intentarlo más tarde."}</p>
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
            <button className="btn" type="button" onClick={() => void pedirCodigo()}>
              Reintentar
            </button>
            <button className="btn sec" type="button" onClick={onCerrar}>
              Cancelar
            </button>
          </div>
        </>
      )}
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
