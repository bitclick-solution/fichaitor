import { useEffect, useState } from "react";
import { api, type Revision as DatosRevision, type Rol } from "./api";
import { Cargando, Cifra, cuando, Error, euros, Externo, mensajeDe, numero, plan } from "./comun";

// «Cobro: revisión» (docs/contrato-backoffice.md, apartado 5): la cola de lo que necesita una persona.
// Los enlaces a Stripe y Odoo salen de las plantillas de la respuesta, sustituyendo {id}.

const CONTADOR: [string, string][] = [
  ["events_unprocessed", "Eventos de Stripe sin procesar"],
  ["documents_pending", "Facturas pendientes"],
  ["documents_overdue", "Facturas pendientes de más de una hora"],
  ["documents_review", "Facturas en revisión"],
  ["blocks_open", "Bloqueos abiertos"],
  ["refunds_pending", "Reembolsos pendientes"],
  ["paid_without_document", "Cobrado sin factura legal"],
];

const enlace = (plantilla: string | undefined, id: string | number | null | undefined) => (plantilla && id !== null && id !== undefined ? plantilla.replace("{id}", encodeURIComponent(String(id))) : null);

export function Revision({ rol: _rol }: { rol: Rol }) {
  const [r, setR] = useState<DatosRevision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    setR(null);
    setError(null);
    api
      .revision()
      .then(setR)
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, [version]);

  return (
    <>
      <h1>Cobro: revisión</h1>
      <p>
        <button className="enlace" type="button" onClick={() => setVersion((v) => v + 1)}>
          Actualizar
        </button>
      </p>
      <Error texto={error} />
      {!r && !error && <Cargando />}
      {r && (
        <>
          {!r.livemode && <p className="aviso">Modo de prueba de Stripe: los enlaces van al panel de pruebas.</p>}
          <div className="cifras">
            {CONTADOR.map(([k, nombre]) => (
              <Cifra key={k} valor={numero(r.counts[k] ?? 0)} etiqueta={nombre} alerta={(r.counts[k] ?? 0) > 0} />
            ))}
          </div>

          <section className="bloque">
            <h2>Facturas legales en revisión o atrasadas</h2>
            {r.documents.length === 0 ? (
              <p className="sec">Ninguna.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuenta</th>
                      <th>Tipo</th>
                      <th>Estado</th>
                      <th>Número</th>
                      <th>Stripe</th>
                      <th className="num">Total</th>
                      <th>Desde</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.documents.map((d) => (
                      <tr key={d.id}>
                        <td>
                          <code>{d.account_id}</code>
                        </td>
                        <td>{d.kind}</td>
                        <td>
                          {d.state}
                          {d.review_reason && <span className="etiqueta mal">{d.review_reason}</span>}
                        </td>
                        <td>
                          <Externo href={enlace(r.odoo_link, d.odoo_move_id)}>{d.number ?? "—"}</Externo>
                        </td>
                        <td>
                          <Externo href={enlace(r.links.invoice, d.stripe_invoice_id)}>
                            <code>{d.stripe_invoice_id ?? "—"}</code>
                          </Externo>
                        </td>
                        <td className="num">{euros(d.amount_total)}</td>
                        <td>{cuando(d.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Cobrado sin factura legal</h2>
            {r.paid_without_document.length === 0 ? (
              <p className="sec">Nada.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuenta</th>
                      <th>Factura de Stripe</th>
                      <th>Plaza</th>
                      <th>Desde</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.paid_without_document.map((p) => (
                      <tr key={p.entitlement_id}>
                        <td>
                          <code>{p.account_id}</code>
                        </td>
                        <td>
                          <Externo href={enlace(r.links.invoice, p.stripe_invoice_id)}>
                            <code>{p.stripe_invoice_id}</code>
                          </Externo>
                        </td>
                        <td>{p.seat_id ? <code>{p.seat_id}</code> : "—"}</td>
                        <td>{cuando(p.starts_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Plazas en revisión</h2>
            {r.seats.length === 0 ? (
              <p className="sec">Ninguna.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuenta</th>
                      <th>Plaza</th>
                      <th>Plan</th>
                      <th>Estado</th>
                      <th>Motivo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.seats.map((s) => (
                      <tr key={s.id}>
                        <td>
                          <code>{s.account_id}</code>
                        </td>
                        <td>
                          <Externo href={enlace(r.links.subscription, s.stripe_subscription_id)}>
                            <code>{s.id}</code>
                          </Externo>
                        </td>
                        <td>{plan(s.plan)}</td>
                        <td>{s.status}</td>
                        <td>{s.review_reason ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Reembolsos pendientes</h2>
            {r.refunds.length === 0 ? (
              <p className="sec">Ninguno.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuenta</th>
                      <th>Factura</th>
                      <th className="num">Importe</th>
                      <th>Motivo</th>
                      <th>Pedido por</th>
                      <th>Estado</th>
                      <th>Cuándo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.refunds.map((f) => (
                      <tr key={f.id}>
                        <td>
                          <code>{f.account_id}</code>
                        </td>
                        <td>
                          <code>{f.stripe_invoice_id}</code>
                        </td>
                        <td className="num">{f.amount === null ? "Todo" : euros(f.amount)}</td>
                        <td>{f.reason}</td>
                        <td>{f.requested_by}</td>
                        <td>{f.state}</td>
                        <td>{cuando(f.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Bloqueos abiertos</h2>
            {r.blocks.length === 0 ? (
              <p className="sec">Ninguno.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuenta</th>
                      <th>Desde</th>
                      <th>Motivo</th>
                      <th>Factura</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.blocks.map((b) => (
                      <tr key={b.id}>
                        <td>
                          <code>{b.account_id}</code>
                        </td>
                        <td>{cuando(b.blocked_at)}</td>
                        <td>{b.reason}</td>
                        <td>
                          <Externo href={enlace(r.links.invoice, b.stripe_invoice_id)}>
                            <code>{b.stripe_invoice_id ?? "—"}</code>
                          </Externo>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Eventos de Stripe huérfanos o atrasados</h2>
            {r.events.length === 0 ? (
              <p className="sec">Ninguno.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Evento</th>
                      <th>Tipo</th>
                      <th>Objeto</th>
                      <th>Recibido</th>
                      <th>Procesado</th>
                      <th>Resultado</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.events.map((ev) => (
                      <tr key={ev.id}>
                        <td>
                          <code>{ev.id}</code>
                        </td>
                        <td>{ev.type}</td>
                        <td>{ev.object_id ? <code>{ev.object_id}</code> : "—"}</td>
                        <td>{cuando(ev.received_at)}</td>
                        <td>{cuando(ev.processed_at)}</td>
                        <td>{ev.outcome ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}
