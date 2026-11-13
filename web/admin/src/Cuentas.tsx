import { useEffect, useId, useState } from "react";
import {
  api,
  type CobroCuenta,
  type ContactoFiscal,
  type CuentaEncontrada,
  type DetalleCuenta,
  type Derecho,
  type NombreFicha,
  type Rol,
} from "./api";
import { CampoTicket, Cargando, cuando, dia, Error, euros, Externo, mensajeDe, plan, problemaTicket, Ventana } from "./comun";

// «Cuentas» (docs/contrato-backoffice.md, apartados 4, 5 y 6). Se busca por id, correo exacto, NIF o
// razón social, nunca por el nombre de una ficha. Los nombres de las fichas, la dirección fiscal y el
// correo de facturas solo se leen con una referencia de ticket, que queda en la auditoría.

const PRECIO: Record<string, number> = {
  pro_first_yearly: 3500,
  pro_additional_yearly: 3325,
  whatsapp_first_yearly: 5900,
  whatsapp_additional_yearly: 5605,
};

const ORIGEN: Record<string, string> = { beta: "Beta", manual: "Manual", pago: "Pago", cortesia: "Cortesía", recomendacion: "Recomendación" };
const MOTIVO_REEMBOLSO: [string, string][] = [
  ["error_cobro", "Error en el cobro"],
  ["decision_comercial", "Decisión comercial"],
  ["cambio_tercero", "Cambio de un tercero (cláusula 11)"],
  ["disputa", "Disputa"],
];

export function Cuentas({ rol }: { rol: Rol }) {
  const id = useId();
  const [q, setQ] = useState("");
  const [resultados, setResultados] = useState<CuentaEncontrada[] | null>(null);
  const [buscando, setBuscando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [abierta, setAbierta] = useState<string | null>(null);

  if (abierta) return <Cuenta id={abierta} rol={rol} onVolver={() => setAbierta(null)} />;

  const valida = q.trim().length >= 3 && q.trim().length <= 254;
  return (
    <>
      <h1>Cuentas</h1>
      <form
        className="bloque"
        onSubmit={(e) => {
          e.preventDefault();
          if (!valida) return;
          setBuscando(true);
          setError(null);
          api
            .buscar(q.trim())
            .then((r) => setResultados(r.accounts))
            .catch((e2: unknown) => setError(mensajeDe(e2)))
            .finally(() => setBuscando(false));
        }}
      >
        <label className="campo" htmlFor={id}>
          <span>Buscar</span>
          <input id={id} type="search" value={q} onChange={(e) => setQ(e.target.value)} maxLength={254} autoComplete="off" autoFocus />
          <small>Id de cuenta, id de ficha (loc_…), correo exacto, NIF o razón social. No por el nombre de la ficha.</small>
        </label>
        <div className="acciones">
          <button className="btn" type="submit" disabled={!valida || buscando}>
            {buscando ? "Buscando…" : "Buscar"}
          </button>
        </div>
      </form>
      <Error texto={error} />
      {resultados !== null &&
        (resultados.length === 0 ? (
          <p className="sec">Sin resultados.</p>
        ) : (
          <ul className="resultados">
            {resultados.map((c) => (
              <li key={c.account_id}>
                <button type="button" onClick={() => setAbierta(c.account_id)}>
                  <span>
                    <strong>{c.email_masked}</strong>
                    <br />
                    <code className="sec">{c.account_id}</code>
                  </span>
                  <span className="sec">
                    {c.active_locations} de {c.locations} fichas activas · alta {dia(c.created_at)}
                    {c.deleted_at && (
                      <>
                        {" "}
                        · <span className="etiqueta mal">Borrada</span>
                      </>
                    )}
                    {c.disconnected_at && !c.deleted_at && (
                      <>
                        {" "}
                        · <span className="etiqueta neutra">Desconectada</span>
                      </>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ))}
    </>
  );
}

type Pestana = "resumen" | "fichas" | "sync" | "cobro" | "baja" | "derechos";

function Cuenta({ id, rol, onVolver }: { id: string; rol: Rol; onVolver: () => void }) {
  const [d, setD] = useState<DetalleCuenta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pestana, setPestana] = useState<Pestana>("resumen");
  const [version, setVersion] = useState(0);
  const [hecho, setHecho] = useState<string | null>(null);
  // Nombres de las fichas, si ya se han leído con un ticket (solo en memoria).
  const [nombres, setNombres] = useState<NombreFicha[] | null>(null);

  useEffect(() => {
    let vivo = true;
    api
      .cuenta(id)
      .then((r) => vivo && setD(r))
      .catch((e: unknown) => vivo && setError(mensajeDe(e)));
    return () => {
      vivo = false;
    };
  }, [id, version]);

  const recargar = (mensaje: string) => {
    setHecho(mensaje);
    setVersion((v) => v + 1);
  };

  const PESTANAS: { id: Pestana; nombre: string; roles: Rol[] }[] = [
    { id: "resumen", nombre: "Resumen", roles: ["soporte", "finanzas", "propietario"] },
    { id: "fichas", nombre: "Fichas y chats", roles: ["soporte", "finanzas", "propietario"] },
    { id: "sync", nombre: "Sincronización", roles: ["soporte", "propietario"] },
    { id: "cobro", nombre: "Cobro", roles: ["finanzas", "propietario"] },
    { id: "baja", nombre: "Baja por correo", roles: ["soporte", "propietario"] },
    { id: "derechos", nombre: "Conceder un derecho", roles: ["propietario"] },
  ];
  const visibles = PESTANAS.filter((p) => p.roles.includes(rol));

  return (
    <>
      <p>
        <button className="enlace" type="button" onClick={onVolver}>
          ← Volver a la búsqueda
        </button>
      </p>
      {error && <Error texto={error} />}
      {!d && !error && <Cargando />}
      {d && (
        <>
          <h1>
            {d.account.email_masked} {d.blocked && <span className="etiqueta mal">En pausa por impago</span>}
          </h1>
          <p className="sec">
            <code>{d.account.id}</code> · alta {cuando(d.account.created_at)}
            {d.account.deleted_at && ` · borrada ${cuando(d.account.deleted_at)}`}
            {d.account.disconnected_at && ` · desconectada ${cuando(d.account.disconnected_at)}`}
          </p>
          {hecho && (
            <p className="ok" role="status">
              {hecho}
            </p>
          )}
          <nav className="pestanas" aria-label="Partes de la cuenta">
            {visibles.map((p) => (
              <button
                key={p.id}
                type="button"
                aria-current={pestana === p.id ? "true" : undefined}
                onClick={() => {
                  setPestana(p.id);
                  setHecho(null);
                }}
              >
                {p.nombre}
              </button>
            ))}
          </nav>
          {pestana === "resumen" && <Resumen d={d} rol={rol} nombres={nombres} onCambio={recargar} />}
          {pestana === "fichas" && <FichasYChats id={id} nombres={nombres} onNombres={setNombres} />}
          {pestana === "sync" && <Sincronizacion id={id} />}
          {pestana === "cobro" && <Cobro id={id} onCambio={recargar} />}
          {pestana === "baja" && <BajaPorCorreo d={d} nombres={nombres} onHecha={recargar} />}
          {pestana === "derechos" && <Conceder id={id} onHecho={recargar} />}
        </>
      )}
    </>
  );
}

const nombreDe = (nombres: NombreFicha[] | null, loc: string | null) => (loc ? (nombres?.find((n) => n.location_id === loc)?.title ?? null) : null);

function Ficha({ id, nombres }: { id: string | null; nombres: NombreFicha[] | null }) {
  if (!id) return <span className="sec">Sin ficha</span>;
  const n = nombreDe(nombres, id);
  return (
    <>
      {n && <strong>{n} </strong>}
      <code>{id}</code>
    </>
  );
}

function Resumen({ d, rol, nombres, onCambio }: { d: DetalleCuenta; rol: Rol; nombres: NombreFicha[] | null; onCambio: (m: string) => void }) {
  const [recortando, setRecortando] = useState<Derecho | null>(null);
  const [cerrando, setCerrando] = useState<number | null>(null);
  const propietario = rol === "propietario";

  return (
    <>
      <section className="bloque">
        <h2>Fichas</h2>
        {d.locations.length === 0 ? (
          <p className="sec">Sin fichas.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Ficha</th>
                  <th>Plan</th>
                  <th>Activa desde</th>
                  <th>Estado</th>
                </tr>
              </thead>
              <tbody>
                {d.locations.map((l) => (
                  <tr key={l.id}>
                    <td>
                      <Ficha id={l.id} nombres={nombres} />
                    </td>
                    <td>{plan(l.plan)}</td>
                    <td>{cuando(l.active_since)}</td>
                    <td>
                      {l.removed_at ? `Google la retiró ${dia(l.removed_at)}` : l.deactivated_at ? `Desactivada ${dia(l.deactivated_at)}` : l.plan ? "Activa" : "Sin activar"}
                      {!l.verified && " · sin verificar"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Derechos</h2>
        {d.entitlements.length === 0 ? (
          <p className="sec">Sin derechos.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Plan</th>
                  <th className="num">Fichas</th>
                  <th>Origen</th>
                  <th>Desde</th>
                  <th>Hasta</th>
                  <th>Concedido por</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {d.entitlements.map((e) => (
                  <tr key={e.id}>
                    <td>
                      {plan(e.plan)} {e.current ? <span className="etiqueta bien">Vigente</span> : <span className="etiqueta neutra">Pasado</span>}
                      {e.provisional && <span className="etiqueta neutra">Provisional</span>}
                    </td>
                    <td className="num">{e.quantity}</td>
                    <td>
                      {ORIGEN[e.source] ?? e.source}
                      {e.note_code && <span className="sec"> · {e.note_code}</span>}
                    </td>
                    <td>{dia(e.starts_at)}</td>
                    <td>{dia(e.ends_at)}</td>
                    <td>{e.granted_by ?? "—"}</td>
                    <td>
                      {/* Solo los que no son de pago se recortan (los pagados se cierran con un reembolso). */}
                      {propietario && e.current && e.source !== "pago" && (
                        <button className="enlace" type="button" onClick={() => setRecortando(e)}>
                          Recortar
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Plazas</h2>
        {d.seats.length === 0 ? (
          <p className="sec">Sin plazas de pago.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Plaza</th>
                  <th>Plan</th>
                  <th>Ficha</th>
                  <th>Estado</th>
                  <th>Periodo</th>
                  <th className="num">Precio siguiente</th>
                </tr>
              </thead>
              <tbody>
                {d.seats.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <code>{s.id}</code>
                    </td>
                    <td>{plan(s.plan)}</td>
                    <td>
                      <Ficha id={s.location_id} nombres={nombres} />
                    </td>
                    <td>
                      {s.status}
                      {s.cancel_at_period_end && " · no se renueva"}
                      {s.trial_ends_at && ` · primer cobro ${dia(s.trial_ends_at)}`}
                      {s.review_reason && (
                        <>
                          {" "}
                          <span className="etiqueta mal">{s.review_reason}</span>
                        </>
                      )}
                    </td>
                    <td>
                      {dia(s.current_period_start)} – {dia(s.current_period_end)}
                    </td>
                    <td className="num">{!s.next_price_key ? "—" : PRECIO[s.next_price_key] !== undefined ? euros(PRECIO[s.next_price_key]) : <code>{s.next_price_key}</code>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Bloqueos por impago</h2>
        {d.blocks.length === 0 ? (
          <p className="sec">Ninguno.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Desde</th>
                  <th>Motivo</th>
                  <th>Plaza</th>
                  <th>Cerrado</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {d.blocks.map((b) => (
                  <tr key={b.id}>
                    <td>{cuando(b.blocked_at)}</td>
                    <td>{b.reason}</td>
                    <td>{b.seat_id ? <code>{b.seat_id}</code> : "—"}</td>
                    <td>{b.unblocked_at ? `${cuando(b.unblocked_at)} (${b.unblocked_by ?? "—"})` : <span className="etiqueta mal">Abierto</span>}</td>
                    <td>
                      {propietario && !b.unblocked_at && (
                        <button className="enlace" type="button" onClick={() => setCerrando(b.id)}>
                          Cerrar a mano
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Bajas</h2>
        {d.offboardings.length === 0 ? (
          <p className="sec">Ninguna.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Alcance</th>
                  <th>Confirmada</th>
                  <th>Efectiva el</th>
                  <th>Estado</th>
                  <th>Motivo</th>
                </tr>
              </thead>
              <tbody>
                {d.offboardings.map((o) => (
                  <tr key={o.id}>
                    <td>{o.scope}</td>
                    <td>{cuando(o.confirmed_at)}</td>
                    <td>{cuando(o.delete_at)}</td>
                    <td>{o.restored_at ? `Restaurada ${cuando(o.restored_at)}` : o.effective_at ? `Efectiva ${cuando(o.effective_at)}` : "Programada"}</td>
                    <td>{o.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Activaciones (las 100 últimas)</h2>
        {d.activations.length === 0 ? (
          <p className="sec">Ninguna.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Cuándo</th>
                  <th>Ficha</th>
                  <th>Evento</th>
                  <th>Plan</th>
                  <th>Motivo</th>
                </tr>
              </thead>
              <tbody>
                {d.activations.map((a, i) => (
                  <tr key={`${a.at}-${i}`}>
                    <td>{cuando(a.at)}</td>
                    <td>
                      <Ficha id={a.location_id} nombres={nombres} />
                    </td>
                    <td>{a.event}</td>
                    <td>{plan(a.plan)}</td>
                    <td>{a.reason ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {recortando && (
        <Recortar
          derecho={recortando}
          onCerrar={() => setRecortando(null)}
          onHecho={() => {
            setRecortando(null);
            onCambio("Derecho recortado.");
          }}
        />
      )}
      {cerrando !== null && (
        <CerrarBloqueo
          id={cerrando}
          onCerrar={() => setCerrando(null)}
          onHecho={() => {
            setCerrando(null);
            onCambio("Bloqueo cerrado. La cuenta vuelve a funcionar.");
          }}
        />
      )}
    </>
  );
}

/** Formulario de una acción con ticket: valida el ticket, envía y enseña el error tal cual. */
function useAccion() {
  const [ticket, setTicket] = useState("");
  const [enviando, setEnviando] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ejecutar = async (f: () => Promise<void>, max = 500) => {
    const p = problemaTicket(ticket, max);
    if (p) {
      setError(p);
      return;
    }
    setEnviando(true);
    setError(null);
    try {
      await f();
    } catch (e) {
      setError(mensajeDe(e));
    } finally {
      setEnviando(false);
    }
  };
  return { ticket: ticket.trim(), campo: { valor: ticket, onCambio: setTicket }, enviando, error, ejecutar };
}

function Recortar({ derecho, onCerrar, onHecho }: { derecho: Derecho; onCerrar: () => void; onHecho: () => void }) {
  const id = useId();
  const a = useAccion();
  const [fin, setFin] = useState("");
  return (
    <Ventana titulo="Recortar un derecho" onCerrar={onCerrar} bloqueada={a.enviando}>
      <p>
        {plan(derecho.plan)} × {derecho.quantity} ({ORIGEN[derecho.source] ?? derecho.source}), hasta {dia(derecho.ends_at)}. Recortar adelanta su fin; nunca lo
        alarga.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void a.ejecutar(async () => {
            await api.recortar(derecho.id, fin ? finDelDiaMadrid(fin).toISOString() : null, a.ticket);
            onHecho();
          });
        }}
      >
        <label className="campo" htmlFor={id}>
          <span>Último día, incluido (vacío: termina ahora)</span>
          <input id={id} type="date" value={fin} min={diaMadrid(new Date())} onChange={(e) => setFin(e.target.value)} />
        </label>
        <CampoTicket {...a.campo} />
        <Error texto={a.error} />
        <div className="acciones">
          <button className="btn peligro" type="submit" disabled={a.enviando}>
            {a.enviando ? "Recortando…" : fin ? "Recortar hasta esa fecha" : "Terminarlo ahora"}
          </button>
          <button className="btn sec" type="button" onClick={onCerrar} disabled={a.enviando}>
            Cancelar
          </button>
        </div>
      </form>
    </Ventana>
  );
}

function CerrarBloqueo({ id, onCerrar, onHecho }: { id: number; onCerrar: () => void; onHecho: () => void }) {
  const a = useAccion();
  return (
    <Ventana titulo="Cerrar un bloqueo a mano" onCerrar={onCerrar} bloqueada={a.enviando}>
      <p className="aviso">Solo en casos excepcionales: lo normal es que el cliente pague. La cuenta vuelve a funcionar al momento.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void a.ejecutar(async () => {
            await api.cerrarBloqueo(id, a.ticket);
            onHecho();
          });
        }}
      >
        <CampoTicket {...a.campo} />
        <Error texto={a.error} />
        <div className="acciones">
          <button className="btn peligro" type="submit" disabled={a.enviando}>
            {a.enviando ? "Cerrando…" : "Cerrar el bloqueo"}
          </button>
          <button className="btn sec" type="button" onClick={onCerrar} disabled={a.enviando}>
            Cancelar
          </button>
        </div>
      </form>
    </Ventana>
  );
}

function FichasYChats({ id, nombres, onNombres }: { id: string; nombres: NombreFicha[] | null; onNombres: (n: NombreFicha[]) => void }) {
  const a = useAccion();
  const [chats, setChats] = useState<Awaited<ReturnType<typeof api.chats>>["channels"] | null>(null);
  const [errorChats, setErrorChats] = useState<string | null>(null);

  useEffect(() => {
    api
      .chats(id)
      .then((r) => setChats(r.channels))
      .catch((e: unknown) => setErrorChats(mensajeDe(e)));
  }, [id]);

  return (
    <>
      <section className="bloque">
        <h2>Nombres de las fichas</h2>
        {nombres ? (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Ficha</th>
                  <th>Plan</th>
                  <th>Activa desde</th>
                  <th>Estado</th>
                </tr>
              </thead>
              <tbody>
                {nombres.map((n) => (
                  <tr key={n.location_id}>
                    <td>
                      <strong>{n.title}</strong> <code className="sec">{n.location_id}</code>
                    </td>
                    <td>{plan(n.plan)}</td>
                    <td>{cuando(n.active_since)}</td>
                    <td>{n.removed_at ? "Retirada por Google" : n.deactivated_at ? "Desactivada" : n.plan ? "Activa" : "Sin activar"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void a.ejecutar(async () => onNombres((await api.nombresFichas(id, a.ticket)).locations));
            }}
          >
            <p className="sec">El nombre de una ficha es un dato de Google: solo se lee con la referencia del ticket que lo justifica.</p>
            <CampoTicket {...a.campo} />
            <Error texto={a.error} />
            <div className="acciones">
              <button className="btn" type="submit" disabled={a.enviando}>
                {a.enviando ? "Leyendo…" : "Ver los nombres"}
              </button>
            </div>
          </form>
        )}
      </section>
      <section className="bloque">
        <h2>Chats vinculados</h2>
        <Error texto={errorChats} />
        {chats === null && !errorChats && <Cargando />}
        {chats &&
          (chats.length === 0 ? (
            <p className="sec">Ninguno.</p>
          ) : (
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Canal</th>
                    <th>Vinculado</th>
                    <th>Último mensaje recibido</th>
                    <th>Desvinculado</th>
                  </tr>
                </thead>
                <tbody>
                  {chats.map((c) => (
                    <tr key={c.identity_id}>
                      <td>{c.channel === "whatsapp" ? "WhatsApp" : c.channel === "telegram" ? "Telegram" : c.channel}</td>
                      <td>{cuando(c.verified_at)}</td>
                      <td>{cuando(c.last_inbound_at)}</td>
                      <td>{cuando(c.unlinked_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
      </section>
    </>
  );
}

function Sincronizacion({ id }: { id: string }) {
  const [filas, setFilas] = useState<Awaited<ReturnType<typeof api.sincronizacion>>["sync"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .sincronizacion(id)
      .then((r) => setFilas(r.sync))
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, [id]);
  return (
    <section className="bloque">
      <h2>Sincronización con Google</h2>
      <Error texto={error} />
      {!filas && !error && <Cargando />}
      {filas &&
        (filas.length === 0 ? (
          <p className="sec">Sin fichas que sincronizar.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Ficha</th>
                  <th>Estado</th>
                  <th>Desde</th>
                  <th>Última lectura correcta</th>
                </tr>
              </thead>
              <tbody>
                {filas.map((f) => (
                  <tr key={f.location_id}>
                    <td>
                      <code>{f.location_id}</code>
                    </td>
                    <td>
                      <span className={`etiqueta ${f.status === "ok" ? "bien" : "mal"}`}>{f.status}</span>
                    </td>
                    <td>{cuando(f.status_at)}</td>
                    <td>{cuando(f.synced_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))}
    </section>
  );
}

function Cobro({ id, onCambio }: { id: string; onCambio: (m: string) => void }) {
  const [c, setC] = useState<CobroCuenta | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [contacto, setContacto] = useState<ContactoFiscal | null>(null);
  const [reembolsando, setReembolsando] = useState<CobroCuenta["charges"][number] | null>(null);
  const [version, setVersion] = useState(0);
  const a = useAccion();

  useEffect(() => {
    api
      .cobro(id)
      .then(setC)
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, [id, version]);

  if (error) return <Error texto={error} />;
  if (!c) return <Cargando />;

  return (
    <>
      {!c.livemode && <p className="aviso">Modo de prueba de Stripe: los enlaces van al panel de pruebas.</p>}
      <section className="bloque">
        <h2>Cliente</h2>
        {!c.customer ? (
          <p className="sec">La cuenta no ha rellenado sus datos fiscales.</p>
        ) : (
          <>
            <dl className="dl">
              <dt>Razón social</dt>
              <dd>{c.customer.legal_name}</dd>
              <dt>NIF</dt>
              <dd>
                {c.customer.tax_id} <span className="sec">({c.customer.tax_id_type})</span>
              </dd>
              <dt>País y territorio</dt>
              <dd>
                {c.customer.country} · {c.customer.territory}
              </dd>
              <dt>Tratamiento fiscal</dt>
              <dd>{c.customer.tax_treatment}</dd>
              <dt>Stripe</dt>
              <dd>
                <Externo href={c.customer.stripe_url}>{c.customer.stripe_customer_id}</Externo>
              </dd>
            </dl>
            {contacto ? (
              <dl className="dl">
                <dt>Dirección</dt>
                <dd>
                  {contacto.address_line1}
                  {contacto.address_line2 && `, ${contacto.address_line2}`} · {contacto.postal_code} {contacto.city}
                  {contacto.province && ` (${contacto.province})`} · {contacto.country}
                </dd>
                <dt>Correo de facturas</dt>
                <dd>{contacto.invoice_email}</dd>
              </dl>
            ) : (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void a.ejecutar(async () => setContacto(await api.contactoFiscal(id, a.ticket)));
                }}
              >
                <h3>Dirección fiscal y correo de facturas</h3>
                <CampoTicket {...a.campo} />
                <Error texto={a.error} />
                <div className="acciones">
                  <button className="btn sec" type="submit" disabled={a.enviando}>
                    {a.enviando ? "Leyendo…" : "Ver dirección y correo"}
                  </button>
                </div>
              </form>
            )}
          </>
        )}
      </section>

      <section className="bloque">
        <h2>Cobros</h2>
        {c.charges.length === 0 ? (
          <p className="sec">Ninguno.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Factura de Stripe</th>
                  <th>Estado</th>
                  <th>Periodo</th>
                  <th className="num">Total</th>
                  <th className="num">Devuelto</th>
                  <th>Pagado</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {c.charges.map((g) => {
                  const pendiente = g.amount_paid - g.amount_refunded;
                  return (
                    <tr key={g.stripe_invoice_id}>
                      <td>
                        <Externo href={g.stripe_url}>
                          <code>{g.stripe_invoice_id}</code>
                        </Externo>
                        {g.disputed && <span className="etiqueta mal">Disputa</span>}
                      </td>
                      <td>{g.status}</td>
                      <td>
                        {dia(g.period_start)} – {dia(g.period_end)}
                      </td>
                      <td className="num">{euros(g.total)}</td>
                      <td className="num">{euros(g.amount_refunded)}</td>
                      <td>{cuando(g.paid_at)}</td>
                      <td>
                        {g.status === "paid" && pendiente > 0 && (
                          <button className="enlace" type="button" onClick={() => setReembolsando(g)}>
                            Reembolsar
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Facturas legales (Odoo)</h2>
        {c.documents.length === 0 ? (
          <p className="sec">Ninguna todavía.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Número</th>
                  <th>Tipo</th>
                  <th>Estado</th>
                  <th>VERI*FACTU</th>
                  <th className="num">Total</th>
                  <th>Emitida</th>
                </tr>
              </thead>
              <tbody>
                {c.documents.map((doc) => (
                  <tr key={doc.id}>
                    <td>
                      <Externo href={doc.odoo_url}>{doc.number ?? "—"}</Externo>
                    </td>
                    <td>{doc.kind}</td>
                    <td>
                      {doc.state}
                      {doc.review_reason && <span className="etiqueta mal">{doc.review_reason}</span>}
                    </td>
                    <td>{doc.verifactu_state ?? "—"}</td>
                    <td className="num">{euros(doc.amount_total)}</td>
                    <td>{cuando(doc.issued_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="bloque">
        <h2>Reembolsos</h2>
        {c.refunds.length === 0 ? (
          <p className="sec">Ninguno.</p>
        ) : (
          <div className="tabla">
            <table>
              <thead>
                <tr>
                  <th>Factura</th>
                  <th className="num">Importe</th>
                  <th>Motivo</th>
                  <th>Ticket</th>
                  <th>Pedido por</th>
                  <th>Estado</th>
                </tr>
              </thead>
              <tbody>
                {c.refunds.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <code>{r.stripe_invoice_id}</code>
                    </td>
                    <td className="num">{r.amount === null ? "Todo" : euros(r.amount)}</td>
                    <td>{r.reason}</td>
                    <td>{r.ticket_ref ?? "—"}</td>
                    <td>{r.requested_by}</td>
                    <td>{r.state}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {reembolsando && (
        <Reembolsar
          cargo={reembolsando}
          onCerrar={() => setReembolsando(null)}
          onHecho={() => {
            setReembolsando(null);
            setVersion((v) => v + 1);
            onCambio("Reembolso pedido. El proceso de cobro lo ejecuta en segundo plano: su estado sale en «Reembolsos».");
          }}
        />
      )}
    </>
  );
}

function Reembolsar({ cargo, onCerrar, onHecho }: { cargo: CobroCuenta["charges"][number]; onCerrar: () => void; onHecho: () => void }) {
  const id = useId();
  const a = useAccion();
  const pendiente = cargo.amount_paid - cargo.amount_refunded;
  const [todo, setTodo] = useState(true);
  const [importe, setImporte] = useState("");
  const [motivo, setMotivo] = useState("error_cobro");
  const centimos = Math.round(Number(importe.replace(",", ".")) * 100);
  const importeMal = !todo && (!Number.isFinite(centimos) || centimos <= 0 || centimos > pendiente);
  return (
    <Ventana titulo="Reembolso excepcional" onCerrar={onCerrar} bloqueada={a.enviando}>
      <p>
        Factura <code>{cargo.stripe_invoice_id}</code>: pagado {euros(cargo.amount_paid)}, devuelto {euros(cargo.amount_refunded)}. Queda por devolver{" "}
        <strong>{euros(pendiente)}</strong> (IVA incluido).
      </p>
      <p className="aviso">Si es total, la plaza deja de renovarse y se emite la factura rectificativa.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (importeMal) return;
          void a.ejecutar(async () => {
            await api.reembolsar(cargo.stripe_invoice_id, todo ? null : centimos, motivo, a.ticket);
            onHecho();
          }, 80);
        }}
      >
        <label className="casilla">
          <input type="checkbox" checked={todo} onChange={(e) => setTodo(e.target.checked)} />
          <span>Devolver todo lo que queda ({euros(pendiente)})</span>
        </label>
        {!todo && (
          <label className="campo" htmlFor={`${id}-i`}>
            <span>Importe a devolver, en euros con IVA</span>
            <input id={`${id}-i`} type="text" inputMode="decimal" value={importe} onChange={(e) => setImporte(e.target.value)} placeholder="10,00" />
            {importeMal && importe && <small>Entre 0,01 € y {euros(pendiente)}.</small>}
          </label>
        )}
        <label className="campo" htmlFor={`${id}-m`}>
          <span>Motivo</span>
          <select id={`${id}-m`} value={motivo} onChange={(e) => setMotivo(e.target.value)}>
            {MOTIVO_REEMBOLSO.map(([v, n]) => (
              <option key={v} value={v}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <CampoTicket {...a.campo} max={80} />
        <Error texto={a.error} />
        <div className="acciones">
          <button className="btn peligro" type="submit" disabled={a.enviando || importeMal}>
            {a.enviando ? "Pidiendo…" : "Pedir el reembolso"}
          </button>
          <button className="btn sec" type="button" onClick={onCerrar} disabled={a.enviando}>
            Cancelar
          </button>
        </div>
      </form>
    </Ventana>
  );
}

/** Baja por correo (apartado 4): comprobar DMARC, elegir alcance y programar a 48 horas. */
function BajaPorCorreo({ d, nombres, onHecha }: { d: DetalleCuenta; nombres: NombreFicha[] | null; onHecha: (m: string) => void }) {
  const id = useId();
  const a = useAccion();
  const [mensaje, setMensaje] = useState("");
  const [comprobacion, setComprobacion] = useState<{ dmarc: string; from_domain: string; matches_account: boolean } | null>(null);
  const [comprobando, setComprobando] = useState(false);
  const [errorCheck, setErrorCheck] = useState<string | null>(null);
  const [alcance, setAlcance] = useState<"ficha" | "cuenta">("ficha");
  const activas = d.locations.filter((l) => l.plan && !l.deactivated_at && !l.removed_at);
  const [ficha, setFicha] = useState(activas.length === 1 ? activas[0]!.id : "");
  const tamano = new Blob([mensaje]).size;

  return (
    <section className="bloque">
      <h2>Baja por correo</h2>
      <p className="sec">
        Pega el original completo del correo (en Gmail, «Mostrar original»), con sus cabeceras. Solo se tramita si nuestro servidor de correo confirma
        el remitente (DMARC) y es el correo de la cuenta. La baja queda programada a 48 horas, con el correo de aviso y su enlace para restaurarla.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setComprobando(true);
          setErrorCheck(null);
          setComprobacion(null);
          api
            .comprobarCorreo(d.account.id, mensaje)
            .then(setComprobacion)
            .catch((e2: unknown) => setErrorCheck(mensajeDe(e2)))
            .finally(() => setComprobando(false));
        }}
      >
        <label className="campo" htmlFor={`${id}-m`}>
          <span>1. El mensaje original</span>
          <textarea id={`${id}-m`} value={mensaje} onChange={(e) => setMensaje(e.target.value)} spellCheck={false} />
          <small>{tamano > 256 * 1024 ? "Demasiado largo: como mucho 256 KiB." : "Hasta 256 KiB."}</small>
        </label>
        <Error texto={errorCheck} />
        {errorCheck && <p className="sec">Contesta al cliente que haga la baja desde Ajustes, en su panel.</p>}
        <div className="acciones">
          <button className="btn sec" type="submit" disabled={comprobando || !mensaje.trim() || tamano > 256 * 1024}>
            {comprobando ? "Comprobando…" : "Comprobar el remitente"}
          </button>
        </div>
      </form>

      {comprobacion &&
        (!comprobacion.matches_account ? (
          <p className="error">
            DMARC correcto ({comprobacion.from_domain}), pero el remitente no es el correo de esta cuenta. No sigas: pide que escriban desde la dirección
            de la cuenta.
          </p>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (alcance === "ficha" && !ficha) return;
              void a.ejecutar(async () => {
                const r = await api.bajaPorCorreo(d.account.id, alcance, alcance === "ficha" ? ficha : null, a.ticket);
                // Hecha: el formulario desaparece para que no se pueda programar dos veces.
                setComprobacion(null);
                setMensaje("");
                onHecha(`Baja programada: será efectiva el ${cuando(r.delete_at)}. El cliente ha recibido el correo con el enlace para restaurarla.`);
              });
            }}
          >
            <p className="ok">El remitente es el correo de la cuenta (DMARC correcto, {comprobacion.from_domain}).</p>
            <fieldset className="campo">
              <legend>2. Qué pide dar de baja</legend>
              <label className="casilla">
                <input type="radio" name={`${id}-a`} checked={alcance === "ficha"} onChange={() => setAlcance("ficha")} />
                <span>Desconectar una ficha</span>
              </label>
              <label className="casilla">
                <input type="radio" name={`${id}-a`} checked={alcance === "cuenta"} onChange={() => setAlcance("cuenta")} />
                <span>Borrar la cuenta entera</span>
              </label>
            </fieldset>
            {alcance === "ficha" && (
              <label className="campo" htmlFor={`${id}-f`}>
                <span>Ficha que nombra el correo</span>
                <select id={`${id}-f`} value={ficha} onChange={(e) => setFicha(e.target.value)}>
                  <option value="">Elige una ficha</option>
                  {d.locations
                    .filter((l) => !l.removed_at)
                    .map((l) => (
                      <option key={l.id} value={l.id}>
                        {nombreDe(nombres, l.id) ?? l.id}
                      </option>
                    ))}
                </select>
                {!nombres && <small>Para ver los nombres, léelos en «Fichas y chats» con el ticket.</small>}
              </label>
            )}
            <CampoTicket {...a.campo} />
            <Error texto={a.error} />
            <div className="acciones">
              <button className="btn peligro" type="submit" disabled={a.enviando || (alcance === "ficha" && !ficha)}>
                {a.enviando ? "Programando…" : "Programar la baja a 48 horas"}
              </button>
            </div>
          </form>
        ))}
    </section>
  );
}

function Conceder({ id, onHecho }: { id: string; onHecho: (m: string) => void }) {
  const fid = useId();
  const a = useAccion();
  const [planElegido, setPlan] = useState("pro");
  const [cantidad, setCantidad] = useState(1);
  const [origen, setOrigen] = useState("cortesia");
  const [fin, setFin] = useState("");
  const [nota, setNota] = useState("");
  const notaMal = nota !== "" && !/^[a-z_]{1,40}$/.test(nota);
  // «Hasta» incluye ese día: el derecho termina al empezar el día siguiente en Madrid. La base exige que
  // termine entre una hora y dos años (interval '2 years') desde ahora.
  const termina = fin ? finDelDiaMadrid(fin) : null;
  const tope = new Date();
  tope.setUTCFullYear(tope.getUTCFullYear() + 2);
  const finMal = !termina || termina.getTime() < Date.now() + 3600_000 || termina.getTime() > tope.getTime();
  const hoy = diaMadrid(new Date());
  const ultimo = diaMadrid(new Date(tope.getTime() - 86_400_000));

  return (
    <section className="bloque">
      <h2>Conceder un derecho</h2>
      <p className="sec">Empieza al concederlo. Ampliar la beta es conceder un derecho «Beta» con un fin posterior.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (finMal || notaMal || !termina) return;
          void a.ejecutar(async () => {
            await api.conceder(id, {
              plan: planElegido,
              quantity: cantidad,
              source: origen,
              ends_at: termina.toISOString(),
              ...(nota ? { note_code: nota } : {}),
              ticket_ref: a.ticket,
            });
            // Concedido: se vacía la fecha para que un segundo clic no cree otro derecho igual.
            setFin("");
            setNota("");
            onHecho(`Derecho concedido hasta el ${dia(fin)} incluido.`);
          });
        }}
      >
        <div className="fila">
          <label className="campo" htmlFor={`${fid}-p`}>
            <span>Plan</span>
            <select id={`${fid}-p`} value={planElegido} onChange={(e) => setPlan(e.target.value)}>
              <option value="pro">Pro</option>
              <option value="pro_whatsapp">Pro+</option>
            </select>
          </label>
          <label className="campo" htmlFor={`${fid}-c`}>
            <span>Fichas</span>
            <input id={`${fid}-c`} type="number" min={1} max={10} value={cantidad} onChange={(e) => setCantidad(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} />
          </label>
          <label className="campo" htmlFor={`${fid}-o`}>
            <span>Origen</span>
            <select id={`${fid}-o`} value={origen} onChange={(e) => setOrigen(e.target.value)}>
              <option value="cortesia">Cortesía</option>
              <option value="recomendacion">Recomendación</option>
              <option value="manual">Manual</option>
              <option value="beta">Beta</option>
            </select>
          </label>
          <label className="campo" htmlFor={`${fid}-f`}>
            <span>Hasta (incluido)</span>
            <input id={`${fid}-f`} type="date" value={fin} min={hoy} max={ultimo} onChange={(e) => setFin(e.target.value)} />
            <small>Termina al acabar ese día (hora de Madrid). Como mucho, dos años.</small>
          </label>
        </div>
        <label className="campo" htmlFor={`${fid}-n`}>
          <span>Nota (opcional)</span>
          <input id={`${fid}-n`} type="text" value={nota} onChange={(e) => setNota(e.target.value)} maxLength={40} placeholder="cliente_fundador" />
          <small>{notaMal ? "Solo minúsculas y guiones bajos." : "Minúsculas y guiones bajos, hasta 40."}</small>
        </label>
        <CampoTicket {...a.campo} />
        <Error texto={a.error} />
        <div className="acciones">
          <button className="btn" type="submit" disabled={a.enviando || finMal || notaMal}>
            {a.enviando ? "Concediendo…" : "Conceder"}
          </button>
        </div>
      </form>
    </section>
  );
}

// ── Fechas en hora de Madrid ──

const PARTES_MADRID = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Madrid", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

/** «AAAA-MM-DD» del día que es en Madrid en ese instante (para los `min`/`max` de un `<input type=date>`). */
function diaMadrid(d: Date): string {
  const p = Object.fromEntries(PARTES_MADRID.formatToParts(d).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

/** Diferencia de Madrid con UTC en ese instante, en milisegundos (+1 h o +2 h). */
function desfaseMadrid(d: Date): number {
  const p = Object.fromEntries(PARTES_MADRID.formatToParts(d).map((x) => [x.type, Number(x.value)]));
  return Date.UTC(p.year!, p.month! - 1, p.day!, p.hour!, p.minute!) - Math.floor(d.getTime() / 60_000) * 60_000;
}

/** El instante en que termina el día «AAAA-MM-DD» en Madrid (las 00:00 del día siguiente, hora de Madrid). */
function finDelDiaMadrid(diaIso: string): Date {
  const [a, m, d] = diaIso.split("-").map(Number) as [number, number, number];
  const medianocheUTC = Date.UTC(a, m - 1, d + 1);
  let t = medianocheUTC - desfaseMadrid(new Date(medianocheUTC));
  t = medianocheUTC - desfaseMadrid(new Date(t)); // segunda pasada por si el cambio de hora cae en medio
  return new Date(t);
}
