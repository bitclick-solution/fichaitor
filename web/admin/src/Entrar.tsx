import { useEffect, useId, useState } from "react";
import { api, ErrorAdmin, type AltaTotp, type Rol, type Sesion } from "./api";
import { CampoCodigo, CodigosRecuperacion, Error, mensajeDe, QRTotp } from "./comun";

// Entrar (docs/contrato-backoffice.md, apartado 2): usuario y contraseña, después el código TOTP o, en su
// lugar, un código de recuperación (que obliga a dar de alta un TOTP nuevo en ese momento).

function Marca() {
  return (
    <p className="marca">
      <span className="logo" aria-hidden="true">
        F<b>.</b>
      </span>
      <span>
        Fichaitor<span className="pt">.</span> <small>Backoffice</small>
      </span>
    </p>
  );
}

const ROL: Record<Rol, string> = { soporte: "Soporte", finanzas: "Finanzas", propietario: "Propietario" };
export const nombreRol = (r: Rol) => ROL[r] ?? r;

type Paso = "contrasena" | "codigo" | "recuperacion";

export function Entrar({ onDentro, onReenroll, aviso }: { onDentro: (s: Sesion) => void; onReenroll: () => void; aviso?: string | undefined }) {
  const id = useId();
  const [paso, setPaso] = useState<Paso>("contrasena");
  const [usuario, setUsuario] = useState("");
  const [contrasena, setContrasena] = useState("");
  const [codigo, setCodigo] = useState("");
  const [recuperacion, setRecuperacion] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fallo = (e: unknown) => {
    if (e instanceof ErrorAdmin && e.codigo === "invalid_credentials") return "Usuario o contraseña incorrectos.";
    if (e instanceof ErrorAdmin && e.codigo === "totp_invalid") return "El código no vale o ya se usó. Espera al siguiente.";
    if (e instanceof ErrorAdmin && e.codigo === "recovery_invalid") return "Ese código de recuperación no vale o ya se usó.";
    if (e instanceof ErrorAdmin && e.codigo === "session_invalid") {
      // La espera del código dura 5 minutos: se vuelve a empezar.
      setPaso("contrasena");
      return "Ha pasado demasiado tiempo. Vuelve a escribir tu contraseña.";
    }
    if (e instanceof ErrorAdmin && e.codigo === "host_not_allowed") return "Entra por http://localhost:8090, a través del túnel.";
    return mensajeDe(e);
  };

  const enviar = async (accion: () => Promise<void>) => {
    setEnviando(true);
    setError(null);
    try {
      await accion();
    } catch (e) {
      setError(fallo(e));
    } finally {
      setEnviando(false);
    }
  };

  return (
    <main className="puerta">
      <div className="caja">
        <Marca />
        <h1>Entrar</h1>
        {aviso && <p className="aviso">{aviso}</p>}

        {paso === "contrasena" && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void enviar(async () => {
                await api.entrar(usuario.trim(), contrasena);
                setContrasena("");
                setPaso("codigo");
              });
            }}
          >
            <label className="campo" htmlFor={`${id}-u`}>
              <span>Usuario</span>
              <input id={`${id}-u`} type="text" autoComplete="username" value={usuario} onChange={(e) => setUsuario(e.target.value)} autoFocus required />
            </label>
            <label className="campo" htmlFor={`${id}-p`}>
              <span>Contraseña</span>
              <input id={`${id}-p`} type="password" autoComplete="current-password" value={contrasena} onChange={(e) => setContrasena(e.target.value)} required />
            </label>
            <Error texto={error} />
            <div className="acciones">
              <button className="btn" type="submit" disabled={enviando || !usuario.trim() || !contrasena}>
                {enviando ? "Comprobando…" : "Continuar"}
              </button>
            </div>
          </form>
        )}

        {paso === "codigo" && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void enviar(async () => onDentro(await api.codigo(codigo)));
            }}
          >
            <CampoCodigo valor={codigo} onCambio={setCodigo} autoFocus />
            <Error texto={error} />
            <div className="acciones">
              <button className="btn" type="submit" disabled={enviando || codigo.length !== 6}>
                {enviando ? "Comprobando…" : "Entrar"}
              </button>
            </div>
            <p className="sec">
              ¿Sin tu app de autenticación?{" "}
              <button
                className="enlace"
                type="button"
                onClick={() => {
                  setError(null);
                  setPaso("recuperacion");
                }}
              >
                Usar un código de recuperación
              </button>
            </p>
          </form>
        )}

        {paso === "recuperacion" && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void enviar(async () => {
                await api.recuperacion(recuperacion.trim());
                onReenroll();
              });
            }}
          >
            <label className="campo" htmlFor={`${id}-r`}>
              <span>Código de recuperación</span>
              <input id={`${id}-r`} type="text" autoComplete="off" value={recuperacion} onChange={(e) => setRecuperacion(e.target.value)} placeholder="k7m2-q9xa-4tpz" autoFocus required />
              <small>Con o sin guiones. Después tendrás que dar de alta tu app de autenticación de nuevo.</small>
            </label>
            <Error texto={error} />
            <div className="acciones">
              <button className="btn" type="submit" disabled={enviando || recuperacion.trim().length < 6}>
                {enviando ? "Comprobando…" : "Usar el código"}
              </button>
              <button
                className="btn sec"
                type="button"
                onClick={() => {
                  setError(null);
                  setPaso("codigo");
                }}
              >
                Volver
              </button>
            </div>
          </form>
        )}
      </div>
    </main>
  );
}

/**
 * Dar de alta un TOTP nuevo (`POST /totp/start` y `/totp/finish`): obligatorio tras un código de
 * recuperación y voluntario desde «Mi cuenta». El `state` vive solo en memoria.
 */
export function NuevoTotp({
  usuario,
  onHecho,
  onCancelar,
  obligatorio = false,
}: {
  usuario: string;
  onHecho: () => void;
  onCancelar?: (() => void) | undefined;
  /** Tras un código de recuperación: sin diálogo de código (la persona no tiene su app). */
  obligatorio?: boolean;
}) {
  const [alta, setAlta] = useState<AltaTotp | null>(null);
  const [codigo, setCodigo] = useState("");
  const [codigos, setCodigos] = useState<string[] | null>(null);
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const explicar = (e: unknown): string => {
    if (obligatorio && e instanceof ErrorAdmin && e.codigo === "totp_required")
      return "Han pasado más de 5 minutos desde el código de recuperación. Pide al propietario que restablezca tu acceso.";
    return mensajeDe(e);
  };

  const empezar = async (aviso: string | null = null) => {
    setError(aviso);
    setCodigo("");
    setAlta(null);
    try {
      setAlta(await (obligatorio ? api.totpEmpezarObligatorio() : api.totpEmpezar()));
    } catch (e) {
      setError(explicar(e));
    }
  };

  useEffect(() => {
    void empezar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (codigos) return <CodigosRecuperacion codigos={codigos} usuario={usuario} onListo={onHecho} etiquetaListo="Continuar" />;

  return (
    <div className="nuevo-totp">
      {alta ? <QRTotp uri={alta.otpauth_uri} secreto={alta.secret} /> : !error && <p className="cargando">Preparando el código QR…</p>}
      {alta && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setEnviando(true);
            setError(null);
            (obligatorio ? api.totpTerminarObligatorio(alta.state, codigo) : api.totpTerminar(alta.state, codigo))
              .then((r) => setCodigos(r.recovery_codes))
              .catch((e2: unknown) => {
                if (e2 instanceof ErrorAdmin && e2.codigo === "enrollment_expired") void empezar("Han pasado más de 15 minutos. Escanea este código QR nuevo.");
                else if (e2 instanceof ErrorAdmin && e2.codigo === "totp_invalid") setError("El código no vale. Escribe el que enseña ahora tu app.");
                else setError(explicar(e2));
              })
              .finally(() => setEnviando(false));
          }}
        >
          <CampoCodigo valor={codigo} onCambio={setCodigo} />
          <div className="acciones">
            <button className="btn" type="submit" disabled={enviando || codigo.length !== 6}>
              {enviando ? "Comprobando…" : "Activar"}
            </button>
            {onCancelar && (
              <button className="btn sec" type="button" onClick={onCancelar} disabled={enviando}>
                {obligatorio ? "Salir" : "Cancelar"}
              </button>
            )}
          </div>
        </form>
      )}
      <Error texto={error} />
      {!alta && error && (
        <div className="acciones">
          {!obligatorio && (
            <button className="btn" type="button" onClick={() => void empezar()}>
              Reintentar
            </button>
          )}
          {onCancelar && (
            <button className="btn sec" type="button" onClick={onCancelar}>
              {obligatorio ? "Salir" : "Cancelar"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Tras un código de recuperación: hay que dar de alta un TOTP nuevo antes de usar nada más. */
export function Reenroll({ onHecho, onSalir }: { onHecho: () => void; onSalir: () => void }) {
  return (
    <main className="puerta">
      <div className="caja ancha">
        <Marca />
        <h1>Da de alta tu app de autenticación</h1>
        <p>Has entrado con un código de recuperación. Antes de seguir, configura de nuevo tu app de autenticación. Tienes 5 minutos.</p>
        <NuevoTotp usuario="" onHecho={onHecho} onCancelar={onSalir} obligatorio />
      </div>
    </main>
  );
}

/** Alta por enlace: http://localhost:8090/#alta=<token> (el token ya se ha quitado de la barra). */
export function Alta({ token, onTerminar }: { token: string; onTerminar: () => void }) {
  const id = useId();
  const [datos, setDatos] = useState<(AltaTotp & { username: string; role: Rol }) | null>(null);
  const [contrasena, setContrasena] = useState("");
  const [repetida, setRepetida] = useState("");
  const [codigo, setCodigo] = useState("");
  const [codigos, setCodigos] = useState<string[] | null>(null);
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [invalido, setInvalido] = useState(false);

  useEffect(() => {
    let vivo = true;
    api
      .altaEmpezar(token)
      .then((d) => vivo && setDatos(d))
      .catch((e: unknown) => {
        if (!vivo) return;
        if (e instanceof ErrorAdmin && (e.codigo === "enrollment_invalid" || e.status === 404)) setInvalido(true);
        else setError(mensajeDe(e));
      });
    return () => {
      vivo = false;
    };
  }, [token]);

  const corta = contrasena.length > 0 && contrasena.length < 12;
  const distintas = repetida.length > 0 && repetida !== contrasena;

  return (
    <main className="puerta">
      <div className="caja ancha">
        <Marca />
        <h1>Alta en el backoffice</h1>
        {invalido ? (
          <>
            <p className="error">Este enlace de alta no vale: ha caducado (dura 24 horas) o ya se usó. Pide otro al propietario.</p>
            <div className="acciones">
              <button className="btn sec" type="button" onClick={onTerminar}>
                Ir a Entrar
              </button>
            </div>
          </>
        ) : codigos && datos ? (
          <>
            <p className="ok">Listo, {datos.username}. Ya puedes entrar con tu usuario, tu contraseña y el código de tu app.</p>
            <CodigosRecuperacion codigos={codigos} usuario={datos.username} onListo={onTerminar} etiquetaListo="Ir a Entrar" />
          </>
        ) : datos ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setEnviando(true);
              setError(null);
              api
                .altaTerminar(token, contrasena, datos.state, codigo)
                .then((r) => {
                  setContrasena("");
                  setRepetida("");
                  setCodigos(r.recovery_codes);
                })
                .catch((e2: unknown) => {
                  if (e2 instanceof ErrorAdmin && e2.codigo === "totp_invalid") setError("El código no vale. Escribe el que enseña ahora tu app.");
                  else if (e2 instanceof ErrorAdmin && e2.codigo === "enrollment_expired") {
                    // El QR caduca a los 15 minutos, pero el enlace vale 24 horas: se pide un QR nuevo y se
                    // conserva la contraseña escrita.
                    setCodigo("");
                    api
                      .altaEmpezar(token)
                      .then((d) => {
                        setDatos(d);
                        setError("Han pasado más de 15 minutos. Escanea este código QR nuevo (el anterior ya no vale) y escribe su código.");
                      })
                      .catch((e3: unknown) => {
                        if (e3 instanceof ErrorAdmin && (e3.codigo === "enrollment_invalid" || e3.status === 404)) setInvalido(true);
                        else setError(mensajeDe(e3));
                      });
                  } else if (e2 instanceof ErrorAdmin && e2.codigo === "enrollment_invalid") setInvalido(true);
                  else setError(mensajeDe(e2));
                })
                .finally(() => setEnviando(false));
            }}
          >
            <p>
              Usuario <strong>{datos.username}</strong>, con el rol <strong>{nombreRol(datos.role)}</strong>.
            </p>
            <h2>1. Tu contraseña</h2>
            <label className="campo" htmlFor={`${id}-p`}>
              <span>Contraseña nueva</span>
              <input id={`${id}-p`} type="password" autoComplete="new-password" value={contrasena} onChange={(e) => setContrasena(e.target.value)} maxLength={128} required />
              <small>De 12 a 128 caracteres. Que no sea común, ni contenga tu usuario, ni sea una secuencia o una repetición.</small>
            </label>
            <label className="campo" htmlFor={`${id}-r`}>
              <span>Repite la contraseña</span>
              <input id={`${id}-r`} type="password" autoComplete="new-password" value={repetida} onChange={(e) => setRepetida(e.target.value)} maxLength={128} required />
            </label>
            {corta && <p className="sec">Le faltan {12 - contrasena.length} caracteres.</p>}
            {distintas && <p className="sec">Las dos contraseñas no coinciden.</p>}
            <h2>2. Tu app de autenticación</h2>
            <QRTotp uri={datos.otpauth_uri} secreto={datos.secret} />
            <CampoCodigo valor={codigo} onCambio={setCodigo} />
            <Error texto={error} />
            <div className="acciones">
              <button className="btn" type="submit" disabled={enviando || contrasena.length < 12 || contrasena !== repetida || codigo.length !== 6}>
                {enviando ? "Dando de alta…" : "Terminar el alta"}
              </button>
            </div>
          </form>
        ) : (
          <>
            {!error && <p className="cargando">Comprobando el enlace…</p>}
            <Error texto={error} />
          </>
        )}
      </div>
    </main>
  );
}
