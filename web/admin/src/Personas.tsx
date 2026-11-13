import { useEffect, useId, useState } from "react";
import { api, type FilaAuditoria, type Persona, type Sesion } from "./api";
import { Cargando, cuando, Error, Mas, mensajeDe, useLista } from "./comun";
import { nombreRol, NuevoTotp } from "./Entrar";

// Personas y auditoría (apartado 8, solo propietario) y «Mi cuenta» (cambiar la contraseña y la app de
// autenticación, para todos los roles).

export function Personas() {
  const lPersonas = useLista<Persona>((cursor) => api.personas(cursor).then((r) => ({ items: r.users, next_cursor: r.next_cursor })));
  const lAuditoria = useLista<FilaAuditoria>((cursor) => api.auditoria(cursor).then((r) => ({ items: r.rows, next_cursor: r.next_cursor })));
  const personas = lPersonas.filas;
  const filas = lAuditoria.filas ?? [];
  const cargando = lAuditoria.filas === null;
  const [error, setError] = useState<string | null>(null);
  const empezarPersonas = lPersonas.empezar;
  const empezarAuditoria = lAuditoria.empezar;

  useEffect(() => {
    api
      .personas()
      .then((r) => empezarPersonas(r.users, r.next_cursor))
      .catch((e: unknown) => setError(mensajeDe(e)));
    api
      .auditoria()
      .then((r) => empezarAuditoria(r.rows, r.next_cursor))
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, [empezarPersonas, empezarAuditoria]);

  return (
    <>
      <h1>Personas y auditoría</h1>
      <Error texto={error} />
      <section className="bloque">
        <h2>Personas</h2>
        <p className="sec">Solo lectura. Las altas, los restablecimientos y las bajas se hacen en el servidor con scripts/admin_usuario.py.</p>
        {!personas ? (
          <Cargando />
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Usuario</th>
                  <th>Rol</th>
                  <th>Estado</th>
                  <th>Alta</th>
                  <th>Contraseña cambiada</th>
                </tr>
              </thead>
              <tbody>
                {personas.map((p) => (
                  <tr key={p.admin_user_id}>
                    <td>
                      <strong>{p.username}</strong>
                    </td>
                    <td>{nombreRol(p.role)}</td>
                    <td>
                      {!p.active ? (
                        <span className="etiqueta neutra">Desactivada</span>
                      ) : p.must_enroll ? (
                        <span className="etiqueta mal">Alta pendiente</span>
                      ) : p.locked_until && new Date(p.locked_until).getTime() > Date.now() ? (
                        <span className="etiqueta mal">Bloqueada hasta {cuando(p.locked_until)}</span>
                      ) : (
                        <span className="etiqueta bien">Activa</span>
                      )}
                    </td>
                    <td>{cuando(p.created_at)}</td>
                    <td>{cuando(p.password_changed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <Mas lista={lPersonas} />
      </section>

      <section className="bloque">
        <h2>Auditoría</h2>
        {filas.length === 0 && cargando && <Cargando />}
        {filas.length > 0 && (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Cuándo</th>
                  <th>Quién</th>
                  <th>Acción</th>
                  <th>Cuenta</th>
                  <th>Objeto</th>
                  <th>Ticket</th>
                  <th>Detalle</th>
                </tr>
              </thead>
              <tbody>
                {filas.map((f) => (
                  <tr key={f.id}>
                    <td>{cuando(f.at)}</td>
                    <td>
                      {f.username ?? f.written_by}
                      {f.role && <span className="sec"> · {f.role}</span>}
                    </td>
                    <td>
                      <code>{f.action}</code>
                    </td>
                    <td>{f.account_id ? <code>{f.account_id}</code> : "—"}</td>
                    <td>{f.target ? <code>{f.target}</code> : "—"}</td>
                    <td>{f.ticket_ref ?? "—"}</td>
                    <td>{Object.keys(f.detail).length > 0 ? <code>{JSON.stringify(f.detail)}</code> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!cargando && filas.length === 0 && <p className="sec">Sin registros.</p>}
        <Mas lista={lAuditoria} texto="Más antiguos" />
      </section>
    </>
  );
}

export function MiCuenta({ sesion }: { sesion: Sesion }) {
  const id = useId();
  const [actual, setActual] = useState("");
  const [nueva, setNueva] = useState("");
  const [repetida, setRepetida] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hecho, setHecho] = useState<string | null>(null);
  const [cambiandoTotp, setCambiandoTotp] = useState(false);

  return (
    <>
      <h1>Mi cuenta</h1>
      <p>
        <strong>{sesion.username}</strong>, rol {nombreRol(sesion.role)}.
      </p>
      {hecho && (
        <p className="ok" role="status">
          {hecho}
        </p>
      )}
      <section className="bloque">
        <h2>Cambiar la contraseña</h2>
        <p className="sec">Te pediremos el código de tu app si el último tiene más de 5 minutos. Se cierran tus demás sesiones.</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setEnviando(true);
            setError(null);
            setHecho(null);
            api
              .cambiarContrasena(actual, nueva)
              .then(() => {
                setActual("");
                setNueva("");
                setRepetida("");
                setHecho("Contraseña cambiada. Se han cerrado tus demás sesiones.");
              })
              .catch((e2: unknown) => setError(mensajeDe(e2)))
              .finally(() => setEnviando(false));
          }}
        >
          <label className="campo" htmlFor={`${id}-a`}>
            <span>Contraseña actual</span>
            <input id={`${id}-a`} type="password" autoComplete="current-password" value={actual} onChange={(e) => setActual(e.target.value)} required />
          </label>
          <label className="campo" htmlFor={`${id}-n`}>
            <span>Contraseña nueva</span>
            <input id={`${id}-n`} type="password" autoComplete="new-password" value={nueva} onChange={(e) => setNueva(e.target.value)} maxLength={128} required />
            <small>De 12 a 128 caracteres. Que no sea común, ni contenga tu usuario, ni sea una secuencia o una repetición.</small>
          </label>
          <label className="campo" htmlFor={`${id}-r`}>
            <span>Repite la contraseña nueva</span>
            <input id={`${id}-r`} type="password" autoComplete="new-password" value={repetida} onChange={(e) => setRepetida(e.target.value)} maxLength={128} required />
          </label>
          {repetida && repetida !== nueva && <p className="sec">Las dos contraseñas nuevas no coinciden.</p>}
          <Error texto={error} />
          <div className="acciones">
            <button className="btn" type="submit" disabled={enviando || !actual || nueva.length < 12 || nueva !== repetida}>
              {enviando ? "Cambiando…" : "Cambiar la contraseña"}
            </button>
          </div>
        </form>
      </section>

      <section className="bloque">
        <h2>App de autenticación</h2>
        {cambiandoTotp ? (
          <NuevoTotp
            usuario={sesion.username}
            onCancelar={() => setCambiandoTotp(false)}
            onHecho={() => {
              setCambiandoTotp(false);
              setHecho("App de autenticación cambiada. Tus códigos de recuperación anteriores ya no valen.");
            }}
          />
        ) : (
          <>
            <p className="sec">Cambia el código QR (por ejemplo, si cambias de móvil). Te daremos 10 códigos de recuperación nuevos.</p>
            <div className="acciones">
              <button className="btn sec" type="button" onClick={() => setCambiandoTotp(true)}>
                Configurar otra app
              </button>
            </div>
          </>
        )}
      </section>
    </>
  );
}
