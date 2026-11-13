import { useEffect, useId, useState } from "react";
import { api, type CeldaEspera, type Metricas as DatosMetricas, type Salud as DatosSalud } from "./api";
import { Cargando, Cifra, cuando, dia, Error, euros, mensajeDe, numero, plan } from "./comun";

// Salud, métricas y lista de espera (docs/contrato-backoffice.md, apartado 7). Solo lectura.

export function Salud() {
  const [s, setS] = useState<DatosSalud | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    setS(null);
    setError(null);
    api
      .salud()
      .then(setS)
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, [version]);

  return (
    <>
      <h1>Salud</h1>
      <p>
        <button className="enlace" type="button" onClick={() => setVersion((v) => v + 1)}>
          Actualizar
        </button>
      </p>
      <Error texto={error} />
      {!s && !error && <Cargando />}
      {s && (
        <>
          <div className="cifras">
            <Cifra valor={numero(s.summaries["ok"] ?? 0)} etiqueta="Fichas leídas bien" />
            <Cifra valor={numero(s.summaries["needs_reconnect"] ?? 0)} etiqueta="Piden volver a conectar" alerta={(s.summaries["needs_reconnect"] ?? 0) > 0} />
            <Cifra valor={numero(s.summaries["error"] ?? 0)} etiqueta="Con error" alerta={(s.summaries["error"] ?? 0) > 0} />
            <Cifra valor={numero(s.summaries_stale)} etiqueta="Sin leer en 24 h" alerta={s.summaries_stale > 0} />
            <Cifra valor={numero(s.jobs_stuck)} etiqueta="Trabajos atascados" alerta={s.jobs_stuck > 0} />
            <Cifra valor={numero(s.jobs_failed_24h)} etiqueta="Trabajos fallidos en 24 h" alerta={s.jobs_failed_24h > 0} />
          </div>

          <section className="bloque">
            <h2>Canales</h2>
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Canal</th>
                    <th className="num">Vinculados</th>
                    <th className="num">Desvinculados</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(s.channels).map(([canal, c]) => (
                    <tr key={canal}>
                      <td>{canal === "whatsapp" ? "WhatsApp" : canal === "telegram" ? "Telegram" : canal}</td>
                      <td className="num">{numero(c.linked)}</td>
                      <td className="num">{numero(c.unlinked)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="bloque">
            <h2>Cola de trabajos</h2>
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Tipo</th>
                    <th>Estado</th>
                    <th className="num">Trabajos</th>
                    <th>El más antiguo</th>
                    <th className="num">Atascados</th>
                  </tr>
                </thead>
                <tbody>
                  {s.jobs.map((j) => (
                    <tr key={`${j.kind}-${j.state}`}>
                      <td>{j.kind}</td>
                      <td>{j.state}</td>
                      <td className="num">{numero(j.jobs)}</td>
                      <td>{cuando(j.oldest_run_at)}</td>
                      <td className="num">{j.stuck > 0 ? <span className="etiqueta mal">{j.stuck}</span> : 0}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="bloque">
            <h2>Trabajos fallidos (los 50 últimos)</h2>
            {s.failed_jobs.length === 0 ? (
              <p className="sec">Ninguno.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Tipo</th>
                      <th className="num">Intentos</th>
                      <th>Terminó</th>
                      <th>Último error</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.failed_jobs.map((f) => (
                      <tr key={f.job_id}>
                        <td>{f.kind}</td>
                        <td className="num">{f.attempts}</td>
                        <td>{cuando(f.finished_at)}</td>
                        <td>
                          <code>{f.last_error ?? "—"}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Fichas que no se leen bien</h2>
            {s.sync_errors.length === 0 ? (
              <p className="sec">Ninguna.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuenta</th>
                      <th>Ficha</th>
                      <th>Estado</th>
                      <th>Desde</th>
                      <th>Última lectura correcta</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.sync_errors.map((x) => (
                      <tr key={x.location_id}>
                        <td>
                          <code>{x.account_id}</code>
                        </td>
                        <td>
                          <code>{x.location_id}</code>
                        </td>
                        <td>
                          <span className="etiqueta mal">{x.status}</span>
                        </td>
                        <td>{cuando(x.status_at)}</td>
                        <td>{cuando(x.synced_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="bloque">
            <h2>Retención (las 10 últimas pasadas)</h2>
            {s.retention_runs.length === 0 ? (
              <p className="sec">Ninguna.</p>
            ) : (
              <div className="tabla">
                <table>
                  <thead>
                    <tr>
                      <th>Cuándo</th>
                      <th>Borrado</th>
                    </tr>
                  </thead>
                  <tbody>
                    {s.retention_runs.map((r) => (
                      <tr key={r.at}>
                        <td>{cuando(r.at)}</td>
                        <td>
                          {Object.entries(r.deleted)
                            .map(([k, n]) => `${k}: ${numero(n)}`)
                            .join(" · ") || "Nada"}
                        </td>
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

/** «AAAA-MM-DD» de hoy (UTC) más `dias`. */
function diaUTC(dias = 0): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + dias);
  return d.toISOString().slice(0, 10);
}

const EMBUDO: [string, string][] = [
  ["visit", "Visitas"],
  ["diagnosis", "Diagnósticos"],
  ["signup", "Altas"],
  ["connected", "Conectan Google"],
  ["checkout_started", "Empiezan el pago"],
  ["paid", "Pagan"],
];

export function Metricas() {
  const id = useId();
  // Por defecto, los 30 últimos días hasta hoy incluido: [hoy − 29, mañana).
  const [desde, setDesde] = useState(diaUTC(-29));
  const [hasta, setHasta] = useState(diaUTC(1));
  const [m, setM] = useState<DatosMetricas | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cargando, setCargando] = useState(false);

  const cargar = () => {
    setCargando(true);
    setError(null);
    api
      .metricas(desde, hasta)
      .then(setM)
      .catch((e: unknown) => setError(mensajeDe(e)))
      .finally(() => setCargando(false));
  };
  useEffect(() => {
    cargar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rango = (() => {
    const d = (new Date(`${hasta}T00:00:00Z`).getTime() - new Date(`${desde}T00:00:00Z`).getTime()) / 86_400_000;
    return d > 0 && d <= 400;
  })();

  return (
    <>
      <h1>Métricas</h1>
      <form
        className="bloque"
        onSubmit={(e) => {
          e.preventDefault();
          if (rango) cargar();
        }}
      >
        <div className="fila">
          <label className="campo" htmlFor={`${id}-d`}>
            <span>Desde (incluido)</span>
            <input id={`${id}-d`} type="date" value={desde} onChange={(e) => setDesde(e.target.value)} />
          </label>
          <label className="campo" htmlFor={`${id}-h`}>
            <span>Hasta (sin incluir)</span>
            <input id={`${id}-h`} type="date" value={hasta} onChange={(e) => setHasta(e.target.value)} />
          </label>
        </div>
        {!rango && <p className="sec">El periodo tiene que ir hacia delante y durar 400 días como mucho.</p>}
        <div className="acciones">
          <button className="btn" type="submit" disabled={!rango || cargando}>
            {cargando ? "Calculando…" : "Ver el periodo"}
          </button>
        </div>
      </form>
      <Error texto={error} />
      {m && (
        <>
          <p className="sec">
            Periodo: del {dia(m.period.from)} al {dia(m.period.to)} (sin incluir). Días en UTC. Importes sin IVA.
          </p>
          <div className="cifras">
            <Cifra valor={euros(m.revenue.arr_cents)} etiqueta="Ingresos anuales (ARR)" />
            <Cifra valor={euros(m.revenue.mrr_cents)} etiqueta="Ingresos mensuales (MRR)" />
            <Cifra valor={numero(m.signups.accounts)} etiqueta="Cuentas nuevas" />
            <Cifra valor={numero(m.signups.first_seats)} etiqueta="Primeras plazas" />
            <Cifra valor={numero(m.signups.new_seats)} etiqueta="Plazas nuevas" />
            <Cifra valor={numero(m.churn.seats_canceled)} etiqueta="Plazas canceladas" />
            <Cifra valor={numero(m.churn.seats_lost_unpaid)} etiqueta="Perdidas por impago" alerta={m.churn.seats_lost_unpaid > 0} />
            <Cifra valor={numero(m.churn.seats_to_cancel)} etiqueta="Se cancelarán al renovar" />
            <Cifra valor={numero(m.churn.account_leaves)} etiqueta="Bajas de cuenta" />
            <Cifra valor={numero(m.blocks.open_now)} etiqueta="Bloqueos abiertos ahora" alerta={m.blocks.open_now > 0} />
          </div>
          {m.revenue.estimated_seats > 0 && (
            <p className="sec">{numero(m.revenue.estimated_seats)} plazas sin precio siguiente fijado cuentan con el precio de la primera plaza de su plan.</p>
          )}

          <section className="bloque">
            <h2>Plazas en vigor</h2>
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Plan</th>
                    <th>Precio</th>
                    <th className="num">Plazas</th>
                  </tr>
                </thead>
                <tbody>
                  {m.seats_in_force.map((s) => (
                    <tr key={`${s.plan}-${s.next_price_key}`}>
                      <td>{plan(s.plan)}</td>
                      <td>
                        <code>{s.next_price_key ?? "—"}</code>
                      </td>
                      <td className="num">{numero(s.seats)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section className="bloque">
            <h2>Conversión</h2>
            <dl className="dl">
              <dt>Cuentas de la beta</dt>
              <dd>
                {numero(m.conversion.beta_paid)} de {numero(m.conversion.beta_accounts)} pagan
              </dd>
              <dt>Cuentas sin beta (del periodo)</dt>
              <dd>
                {numero(m.conversion.free_paid)} de {numero(m.conversion.free_accounts)} pagan
              </dd>
              <dt>Bloqueos del periodo</dt>
              <dd>
                {numero(m.blocks.opened)} abiertos, {numero(m.blocks.closed)} cerrados
              </dd>
            </dl>
          </section>

          <section className="bloque">
            <h2>Embudo</h2>
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Paso</th>
                    <th className="num">Eventos</th>
                    <th className="num">Respecto al anterior</th>
                  </tr>
                </thead>
                <tbody>
                  {EMBUDO.map(([k, nombre], i) => {
                    const n = m.funnel[k] ?? 0;
                    const previo = i > 0 ? (m.funnel[EMBUDO[i - 1]![0]] ?? 0) : 0;
                    return (
                      <tr key={k}>
                        <td>{nombre}</td>
                        <td className="num">{numero(n)}</td>
                        <td className="num">{i > 0 && previo > 0 ? `${Math.round((n / previo) * 100)} %` : "—"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </>
  );
}

const TRAMO: Record<string, string> = { "0-49": "0–49", "50-69": "50–69", "70-89": "70–89", "90-100": "90–100", sin_nota: "Sin nota" };

export function Espera() {
  const [d, setD] = useState<{ min_cell: number; cells: CeldaEspera[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api
      .espera()
      .then(setD)
      .catch((e: unknown) => setError(mensajeDe(e)));
  }, []);
  return (
    <>
      <h1>Lista de espera</h1>
      <p className="sec">
        Solo totales anónimos, por semana, canal, tramo de nota y origen. Las celdas de menos de {d?.min_cell ?? 5} personas no salen y no hay totales por
        fila ni por columna, para que nadie se pueda identificar.
      </p>
      <Error texto={error} />
      {!d && !error && <Cargando />}
      {d &&
        (d.cells.length === 0 ? (
          <p className="sec">Ninguna celda llega al mínimo.</p>
        ) : (
          <section className="bloque">
            <div className="tabla">
              <table>
                <thead>
                  <tr>
                    <th>Semana</th>
                    <th>Canal</th>
                    <th>Nota</th>
                    <th>Origen</th>
                    <th className="num">Personas</th>
                  </tr>
                </thead>
                <tbody>
                  {d.cells.map((c) => (
                    <tr key={`${c.week}-${c.channel}-${c.score_band}-${c.source}`}>
                      <td>Desde el {dia(c.week)}</td>
                      <td>{c.channel === "email" ? "Correo" : c.channel === "whatsapp" ? "WhatsApp" : c.channel}</td>
                      <td>{TRAMO[c.score_band] ?? c.score_band}</td>
                      <td>{c.source}</td>
                      <td className="num">{numero(c.people)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
    </>
  );
}
