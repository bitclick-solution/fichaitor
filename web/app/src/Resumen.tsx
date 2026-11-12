import { useEffect, useState } from "react";
import { api, ApiError, OAUTH_START, type Summary } from "./api";
import { Cuenta, Seccion } from "./movimiento";

// Reglas de sondeo del contrato: `pendiente` → cada 3 s, 10 intentos; `stale` → una sola repetición a los 10 s.
const INTERVALO_PENDIENTE = 3000;
const MAX_INTENTOS = 10;
const ESPERA_STALE = 10000;

// Al volver de Google (?google=ok) la sincronización tarda unos segundos: si llega `needs_reconnect`
// o `error`, se repite la consulta una vez a los 10 s. Se lee al cargar el módulo, antes de que
// App limpie la barra de direcciones.
const RECIEN_CONECTADO = new URLSearchParams(location.search).get("google") === "ok";

type Fase = "cargando" | "leyendo" | "listo" | "agotado" | "error";

const numero = new Intl.NumberFormat("es-ES");
const media = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const fechaLarga = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", year: "numeric" });
const fechaHora = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });

function fecha(iso: string | null, formato: Intl.DateTimeFormat): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : formato.format(d);
}


export function Resumen({
  fichaId,
  onSinSesion,
  onDatos,
}: {
  fichaId: string;
  onSinSesion: () => void;
  /** Avisa al panel de cada lectura buena (para la valoración sobre 5 de la cabecera). */
  onDatos?: (s: Summary) => void;
}) {
  const [datos, setDatos] = useState<Summary | null>(null);
  const [fase, setFase] = useState<Fase>("cargando");
  const [mensaje, setMensaje] = useState<string | null>(null);

  useEffect(() => {
    let vivo = true;
    let temporizador: ReturnType<typeof setTimeout> | undefined;
    let intentos = 0;
    let repetidoPorStale = false;

    const pedir = async () => {
      try {
        const s = await api.resumen();
        if (!vivo) return;
        if (s.status === "pendiente") {
          intentos += 1;
          if (intentos >= MAX_INTENTOS) {
            setFase("agotado");
            return;
          }
          setFase("leyendo");
          temporizador = setTimeout(() => void pedir(), INTERVALO_PENDIENTE);
          return;
        }
        setDatos(s);
        setFase("listo");
        onDatos?.(s);
        const falloTrasConectar = RECIEN_CONECTADO && (s.status === "needs_reconnect" || s.status === "error");
        if ((s.stale || falloTrasConectar) && !repetidoPorStale) {
          repetidoPorStale = true;
          temporizador = setTimeout(() => void pedir(), ESPERA_STALE);
        }
      } catch (e) {
        if (!vivo) return;
        if (e instanceof ApiError && e.status === 401) onSinSesion();
        else {
          setMensaje(e instanceof Error ? e.message : "No hemos podido leer tu ficha.");
          setFase("error");
        }
      }
    };

    setDatos(null);
    setFase("cargando");
    setMensaje(null);
    void pedir();
    return () => {
      vivo = false;
      if (temporizador) clearTimeout(temporizador);
    };
  }, [fichaId, onSinSesion, onDatos]);

  if (fase === "cargando" || fase === "leyendo") {
    return (
      <div className="bloque" role="status" aria-live="polite">
        <p className="cargando">
          <span className="ojo" aria-hidden="true" />
          {fase === "leyendo" ? "Leyendo tus reseñas y tus horarios…" : "Abriendo tu ficha…"}
        </p>
      </div>
    );
  }

  if (fase === "agotado") {
    return (
      <div className="bloque">
        <p className="lead">Estamos leyendo tu ficha; vuelve en un minuto.</p>
      </div>
    );
  }

  if (fase === "error" || !datos) {
    return (
      <div className="bloque">
        <p className="aviso" role="alert">
          {mensaje ?? "No hemos podido leer tu ficha."}
        </p>
      </div>
    );
  }

  const s = datos;
  const sinCifras = s.review_count === null && s.upcoming_special_hours === null && s.google_changed === null;
  const bajas = s.unanswered_low_count ?? 0;
  const ultima = fecha(s.last_review_at, fechaLarga);
  const actualizado = fecha(s.synced_at, fechaHora);
  const cambios = s.google_changed_labels ?? [];

  return (
    <Seccion id="resumen">
      {s.status === "needs_reconnect" && (
        <div className="reconectar" role="alert">
          <p>
            <strong>El acceso a Google ha caducado.</strong> Fichaitor no puede ver tu ficha hasta que vuelvas a conectarla.
            {!sinCifras && " Estas son las últimas cifras que leyó."}
          </p>
          <a className="btn" href={OAUTH_START}>
            Volver a conectar con Google{" "}
            <span className="fl" aria-hidden="true">
              →
            </span>
          </a>
        </div>
      )}

      {!sinCifras && (
        <div className="tres resumen">
          <div>
            <h2 className="rotulo">Reseñas sin responder</h2>
            <p className="cifra" data-alerta={(s.unanswered_count ?? 0) > 0}>
              {s.unanswered_count === null ? "—" : <Cuenta valor={s.unanswered_count} />}
            </p>
            {s.review_count !== null && (
              <p className="sec">
                de {numero.format(s.review_count)} {s.review_count === 1 ? "reseña" : "reseñas"}
                {s.average_rating !== null && `, con una media de ${media.format(s.average_rating)}`}
              </p>
            )}
            {bajas > 0 && <p className="ojo-texto">{bajas === 1 ? "1 es de 1 o 2 estrellas" : `${numero.format(bajas)} son de 1 o 2 estrellas`}</p>}
            {ultima && <p className="sec pie-tarjeta">Última reseña: {ultima}</p>}
          </div>

          <div>
            <h2 className="rotulo">Horarios de festivos</h2>
            <p className="cifra" data-alerta={s.upcoming_special_hours === 0}>
              {s.upcoming_special_hours === null ? "—" : <Cuenta valor={s.upcoming_special_hours} />}
            </p>
            {s.upcoming_special_hours !== null && (
              <p className="sec">
                {s.upcoming_special_hours === 0
                  ? "No tienes horarios de festivos puestos"
                  : s.upcoming_special_hours === 1
                    ? "día próximo con horario especial puesto"
                    : "días próximos con horario especial puesto"}
              </p>
            )}
            {s.has_regular_hours === false && <p className="ojo-texto">Tu ficha no tiene horario habitual</p>}
          </div>

          <div>
            <h2 className="rotulo">Cambios de Google</h2>
            <p className="cifra" data-alerta={s.google_changed === true}>
              {s.google_changed === null ? "—" : s.google_changed ? numero.format(Math.max(1, cambios.length)) : "0"}
            </p>
            {s.google_changed === true && (
              <p className="ojo-texto">Google ha cambiado: {cambios.length > 0 ? cambios.join(", ") : "datos de tu ficha"}</p>
            )}
            {s.google_changed === false && <p className="sec">Google no ha cambiado nada de tu ficha</p>}
          </div>
        </div>
      )}

      {s.status === "error" && (
        <p className="sec">
          No hemos podido actualizar los datos ahora mismo.{!sinCifras && " Estas son las últimas cifras."}
        </p>
      )}
      {actualizado && (
        <p className="sec">
          Actualizado el {actualizado}
          {s.stale && ". Actualizando…"}
        </p>
      )}
    </Seccion>
  );
}
