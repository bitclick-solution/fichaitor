import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, OAUTH_START, type Action, type Review, type ReviewsResponse } from "./api";
import { Seccion } from "./movimiento";

// Reseñas y respuestas. Contrato: docs/contrato-web-api.md, «Reseñas y respuestas».
//
// Reglas que esta pantalla cumple siempre:
//  - Nada se publica sin que el dueño vea el texto exacto y pulse «Publicar en Google».
//  - Se envía lo que hay en el cuadro en ese momento, tal cual, sin recortar ni normalizar.
//  - La web no reintenta sola una publicación: ante un fallo, se muestra y decide el dueño.
//  - Un 202 no es «publicada»: se sigue la acción hasta que la API diga `hecha`.
//  - No se guarda nada en el navegador y todo se pinta como texto.

const MAX_BYTES = 4096;
const bytesDe = (t: string) => new TextEncoder().encode(t).length;
const numero = new Intl.NumberFormat("es-ES");
const fechaCorta = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "long", year: "numeric" });

function fecha(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : fechaCorta.format(d);
}

type Filtro = "unanswered" | "all";
type Carga = "cargando" | "leyendo" | "listo" | "agotado" | "error";

export function Resenas({ fichaId, onSinSesion, onCambio }: { fichaId: string; onSinSesion: () => void; onCambio: () => void }) {
  const [filtro, setFiltro] = useState<Filtro>("unanswered");
  const [datos, setDatos] = useState<ReviewsResponse | null>(null);
  const [carga, setCarga] = useState<Carga>("cargando");
  const [mensaje, setMensaje] = useState<string | null>(null);
  const [abierta, setAbierta] = useState<string | null>(null);
  const [recarga, setRecarga] = useState(0);

  useEffect(() => {
    let vivo = true;
    let temporizador: ReturnType<typeof setTimeout> | undefined;
    let intentos = 0;

    const pedir = async () => {
      try {
        const r = await api.resenas(filtro);
        if (!vivo) return;
        if (r.status === "pendiente") {
          intentos += 1;
          if (intentos >= 10) setCarga("agotado");
          else {
            setCarga("leyendo");
            temporizador = setTimeout(() => void pedir(), 3000);
          }
          return;
        }
        setDatos(r);
        setCarga("listo");
      } catch (e) {
        if (!vivo) return;
        if (e instanceof ApiError && e.status === 401) onSinSesion();
        else {
          setMensaje(e instanceof Error ? e.message : "No hemos podido leer tus reseñas.");
          setCarga("error");
        }
      }
    };

    setCarga((c) => (c === "listo" ? "listo" : "cargando"));
    void pedir();
    return () => {
      vivo = false;
      if (temporizador) clearTimeout(temporizador);
    };
  }, [fichaId, filtro, recarga, onSinSesion]);

  const alPublicar = useCallback(() => {
    setAbierta(null);
    setRecarga((n) => n + 1);
    onCambio();
  }, [onCambio]);

  return (
    <Seccion id="resenas" className="resenas">
      <div className="res-titulo">
        <h2 className="rotulo">Reseñas</h2>
        <div className="filtro" role="group" aria-label="Qué reseñas ver">
          <button type="button" aria-pressed={filtro === "unanswered"} onClick={() => setFiltro("unanswered")}>
            Sin responder
          </button>
          <button type="button" aria-pressed={filtro === "all"} onClick={() => setFiltro("all")}>
            Todas
          </button>
        </div>
      </div>

      {(carga === "cargando" || carga === "leyendo") && (
        <p className="cargando" role="status" aria-live="polite">
          <span className="ojo" aria-hidden="true" />
          Leyendo tus reseñas…
        </p>
      )}
      {carga === "agotado" && <p className="lead">Todavía estamos leyendo tus reseñas; vuelve en un minuto.</p>}
      {carga === "error" && (
        <p className="aviso" role="alert">
          {mensaje}
        </p>
      )}

      {carga === "listo" && datos && (
        <>
          {datos.status === "needs_reconnect" && (
            <div className="reconectar" role="alert">
              <p>
                <strong>El acceso a Google ha caducado.</strong> Estas son las últimas reseñas que leímos. Para responder, vuelve a conectar tu
                ficha.
              </p>
              <a className="btn" href={OAUTH_START}>
                Volver a conectar con Google
              </a>
            </div>
          )}
          {datos.reviews.length === 0 ? (
            <p className="lead">{filtro === "unanswered" ? "No tienes reseñas sin responder. Bien hecho." : "Tu ficha todavía no tiene reseñas."}</p>
          ) : (
            <>
              <p className="sec">
                {filtro === "unanswered"
                  ? `${numero.format(datos.total)} sin responder. Primero, las de menos estrellas.`
                  : `${numero.format(datos.total)} ${datos.total === 1 ? "reseña" : "reseñas"}.`}
                {datos.reviews.length < datos.total && ` Se muestran ${numero.format(datos.reviews.length)}.`}
              </p>
              <ul className="lista-resenas">
                {datos.reviews.map((r) => (
                  <Resena
                    key={r.id}
                    resena={r}
                    abierta={abierta === r.id}
                    bloqueada={abierta !== null && abierta !== r.id}
                    onAbrir={() => setAbierta(r.id)}
                    onCerrar={() => setAbierta(null)}
                    onPublicada={alPublicar}
                    onSinSesion={onSinSesion}
                  />
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </Seccion>
  );
}

function Estrellas({ n }: { n: number }) {
  const v = Math.min(5, Math.max(0, Math.round(n)));
  return (
    <span className="estrellas">
      <span aria-hidden="true">
        {"★".repeat(v)}
        {"☆".repeat(5 - v)}
      </span>
      <span className="sr">
        {v} {v === 1 ? "estrella" : "estrellas"}
      </span>
    </span>
  );
}

type Editor =
  | { fase: "cerrado" }
  | { fase: "redactando" }
  | { fase: "editando"; aviso: string | null }
  | { fase: "enviando" }
  | { fase: "siguiendo"; accion: Action }
  | { fase: "hecha"; accion: Action }
  | { fase: "fallida"; accion: Action };

function Resena({
  resena,
  abierta,
  bloqueada,
  onAbrir,
  onCerrar,
  onPublicada,
  onSinSesion,
}: {
  resena: Review;
  abierta: boolean;
  bloqueada: boolean;
  onAbrir: () => void;
  onCerrar: () => void;
  onPublicada: () => void;
  onSinSesion: () => void;
}) {
  const [editor, setEditor] = useState<Editor>({ fase: "cerrado" });
  const [texto, setTexto] = useState("");
  const [error, setError] = useState<string | null>(null);
  const cuadro = useRef<HTMLTextAreaElement>(null);
  const vivo = useRef(true);
  const temporizador = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    vivo.current = true;
    return () => {
      vivo.current = false;
      if (temporizador.current) clearTimeout(temporizador.current);
    };
  }, []);

  // Si otra reseña abre su editor, este se cierra (salvo que haya un envío en marcha).
  useEffect(() => {
    if (!abierta && (editor.fase === "editando" || editor.fase === "redactando")) setEditor({ fase: "cerrado" });
  }, [abierta, editor.fase]);

  const fallo = (e: unknown, porDefecto: string): string => {
    if (e instanceof ApiError && e.status === 401) {
      onSinSesion();
      return "";
    }
    return e instanceof Error ? e.message : porDefecto;
  };

  const escribir = (inicial: string, aviso: string | null = null) => {
    setTexto(inicial);
    setError(null);
    setEditor({ fase: "editando", aviso });
    onAbrir();
    setTimeout(() => cuadro.current?.focus(), 0);
  };

  const redactar = async () => {
    setError(null);
    setEditor({ fase: "redactando" });
    onAbrir();
    try {
      const b = await api.borrador(resena.id);
      if (vivo.current) escribir(b.text);
    } catch (e) {
      if (!vivo.current) return;
      // Sin borrador válido (422), límite diario (429) o fallo: el dueño escribe la suya.
      escribir("", fallo(e, "No hemos podido redactar un borrador. Escribe tú la respuesta."));
    }
  };

  // Sigue la acción: cada 2 s, 15 veces; después, una vez por minuto, 5 veces. Sin reintentar la publicación.
  const seguir = (accion: Action, vuelta: number) => {
    if (accion.status === "hecha") {
      setEditor({ fase: "hecha", accion });
      return;
    }
    if (accion.status === "fallida") {
      setEditor({ fase: "fallida", accion });
      return;
    }
    setEditor({ fase: "siguiendo", accion });
    if (vuelta >= 20) return; // se deja de preguntar; el mensaje indica que puede tardar
    temporizador.current = setTimeout(
      async () => {
        try {
          const a = await api.accion(accion.id);
          if (vivo.current) seguir(a, vuelta + 1);
        } catch (e) {
          if (!vivo.current) return;
          const m = fallo(e, "No hemos podido comprobar el envío.");
          if (m) setError(`${m} La respuesta puede haberse enviado igualmente: compruébalo en tu ficha antes de volver a publicarla.`);
        }
      },
      vuelta < 15 ? 2000 : 60000,
    );
  };

  const publicar = async () => {
    // Se envía exactamente lo que hay en el cuadro, sin recortar.
    const enviado = texto;
    setError(null);
    setEditor({ fase: "enviando" });
    try {
      const a = await api.responder(resena.id, enviado);
      if (vivo.current) seguir(a, 0);
    } catch (e) {
      if (!vivo.current) return;
      const m = fallo(e, "No hemos podido enviar la respuesta.");
      setError(m);
      setEditor({ fase: "editando", aviso: null });
    }
  };

  const bytes = bytesDe(texto);
  const vacio = texto.trim() === "";
  const excedido = bytes > MAX_BYTES;
  const enMarcha = editor.fase === "enviando" || editor.fase === "siguiendo";

  return (
    <li className="resena" data-baja={resena.stars > 0 && resena.stars <= 2}>
      <div className="resena-cab">
        <Estrellas n={resena.stars} />
        <span className="sec">{fecha(resena.created_at)}</span>
      </div>
      <p className={resena.text ? "resena-texto" : "resena-texto sec"}>{resena.text || "Reseña sin texto, solo con estrellas."}</p>

      {resena.reply && editor.fase === "cerrado" && (
        <div className="tu-respuesta">
          <p className="rotulo">Tu respuesta</p>
          <p>{resena.reply.text}</p>
        </div>
      )}

      {editor.fase === "cerrado" && (
        <div className="acciones">
          {resena.replied ? (
            <button className="btn sec" type="button" disabled={bloqueada} onClick={() => escribir(resena.reply?.text ?? "", null)}>
              Editar respuesta
            </button>
          ) : (
            <>
              <button className="btn" type="button" disabled={bloqueada} onClick={() => void redactar()}>
                Redactar respuesta
              </button>
              <button className="btn sec" type="button" disabled={bloqueada} onClick={() => escribir("")}>
                Escribirla yo
              </button>
            </>
          )}
        </div>
      )}

      {editor.fase === "redactando" && (
        <p className="cargando" role="status" aria-live="polite">
          <span className="ojo" aria-hidden="true" />
          Redactando una propuesta…
        </p>
      )}

      {(editor.fase === "editando" || editor.fase === "enviando") && (
        <div className="editor">
          {editor.fase === "editando" && editor.aviso && <p className="aviso">{editor.aviso}</p>}
          {resena.replied && <p className="sec">Esta reseña ya tiene respuesta. Lo que publiques sustituye a la actual.</p>}
          <label htmlFor={`respuesta-${resena.id}`} className="rotulo">
            Tu respuesta
          </label>
          <textarea
            id={`respuesta-${resena.id}`}
            ref={cuadro}
            value={texto}
            onChange={(e) => setTexto(e.target.value)}
            rows={6}
            disabled={editor.fase === "enviando"}
            aria-describedby={`ayuda-${resena.id}`}
            aria-invalid={excedido}
          />
          <p className={excedido ? "contador mal" : "contador sec"} id={`ayuda-${resena.id}`}>
            {numero.format(bytes)} de {numero.format(MAX_BYTES)} bytes.{excedido ? " Acórtala para poder publicarla." : " Se publica exactamente este texto, en tu nombre."}
          </p>
          {error && (
            <p className="aviso" role="alert">
              {error}
            </p>
          )}
          <div className="acciones">
            <button className="btn" type="button" disabled={vacio || excedido || editor.fase === "enviando"} onClick={() => void publicar()}>
              {editor.fase === "enviando" ? "Enviando…" : "Publicar en Google"}
            </button>
            <button
              className="btn sec"
              type="button"
              disabled={editor.fase === "enviando"}
              onClick={() => {
                setEditor({ fase: "cerrado" });
                setError(null);
                onCerrar();
              }}
            >
              Cancelar
            </button>
          </div>
        </div>
      )}

      {(editor.fase === "siguiendo" || editor.fase === "hecha" || editor.fase === "fallida") && (
        <div className="estado-envio" data-estado={editor.accion.status} role="status" aria-live="polite">
          <p className="rotulo">
            {editor.fase === "siguiendo" && <span className="ojo" aria-hidden="true" />}
            {editor.accion.message || (enMarcha ? "Enviando tu respuesta a Google…" : "")}
          </p>
          <p className="texto-enviado">{texto}</p>
          {error && (
            <p className="aviso" role="alert">
              {error}
            </p>
          )}
          {editor.fase === "fallida" && (
            <div className="acciones">
              {editor.accion.error === "reconnect" ? (
                <a className="btn" href={OAUTH_START}>
                  Volver a conectar con Google
                </a>
              ) : (
                editor.accion.error !== "review_gone" && (
                  <button className="btn sec" type="button" onClick={() => escribir(texto)}>
                    Revisar y volver a publicar
                  </button>
                )
              )}
            </div>
          )}
          {editor.fase === "hecha" && (
            <div className="acciones">
              <button className="btn sec" type="button" onClick={onPublicada}>
                Seguir con las demás
              </button>
            </div>
          )}
        </div>
      )}
    </li>
  );
}
