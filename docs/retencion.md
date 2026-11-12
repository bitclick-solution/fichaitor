# Baja y conservación de datos

Qué dato guarda Fichaitor, cuánto tiempo, quién lo borra y cuándo. Sirve para contrastar la Política de Privacidad (`docs/legal/privacidad.md`) con lo que hace el sistema, línea a línea.

Los plazos los aprobó el propietario el 2026-11-12. Todos están en configuración (`FICHAITOR_RETENTION_*` y `FICHAITOR_OFFBOARDING_*`) con esos valores por defecto.

## Cómo es una baja

Hay dos alcances: `ficha` (desconectar Google; la cuenta sigue existiendo, sin fichas) y `cuenta` (borrar la cuenta).

1. **Código.** El dueño pide la baja desde su sesión. La API genera un código de 6 cifras, guarda solo su huella y encola el correo que lo lleva.
2. **Confirmación.** Con el código correcto, la baja queda **programada** a 48 horas (`FICHAITOR_OFFBOARDING_GRACE_HOURS`). Desde ese instante:
   - Ninguna ruta devuelve datos de la ficha: responden 409.
   - No se encolan sincronizaciones y el worker no lee la ficha, no la analiza y no envía respuestas de esa cuenta.
   - Con `cuenta`, se cierran todas las sesiones.
   - No se revoca ni se borra nada: se puede restaurar sin volver a conectar con Google.
3. **Restauración.** Hasta la fecha, desde la sesión o desde el enlace del correo. Todo vuelve a como estaba.
4. **Baja efectiva.** A las 48 horas, el worker (job `apply_offboarding`):
   - Deja la cuenta sin fichas. Con `cuenta`, la da de baja: vacía su correo y libera su identidad de Google.
   - **Revoca en Google todas las conexiones guardadas** de ese usuario y **borra cada token cifrado en ese momento**.
   - Si Google no responde tras 5 intentos, borra los tokens igualmente, anota `revoke_state = 'failed'` en `offboarding` y escribe un aviso en el log (`offboarding_effective`, nivel `warning`).
5. **Borrado.** La tarea diaria de retención borra físicamente lo que vence, según la tabla siguiente.

> **Nota:** La fecha efectiva de la baja es `offboarding.effective_at`, que coincide con `account.disconnected_at`. Los plazos de 30 días y de 12 meses se cuentan desde ella.

## Tabla de conservación

| Dato | Tabla | Plazo | Quién lo borra | Cuándo |
|---|---|---|---|---|
| Tokens de Google (cifrados) y el correo de la conexión | `google_connection` | Hasta la baja efectiva, o hasta que una conexión más reciente del mismo usuario funciona | Worker, job `apply_offboarding`; y `sync_profile` | En el momento de la baja efectiva, tras revocar cada token en Google o agotar los intentos. Tras una reconexión, en la primera sincronización correcta con la conexión nueva: las anteriores se borran sin revocarlas (Google revocaría la concesión entera y cortaría la nueva) |
| Datos de la ficha y hechos (`facts`) | `profile_summary` | 30 días desde la baja efectiva | Worker, job `retention` | En la primera pasada diaria tras cumplirse el plazo, o al volver a conectar |
| Reseñas | `review` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que la fila anterior |
| Clasificación de cada reseña | `review_sentiment` | Con su reseña | La base (`ON DELETE CASCADE`) | Al borrarse la reseña |
| Borradores de respuesta | `review_draft` | Con su reseña | La base (`ON DELETE CASCADE`) | Al borrarse la reseña |
| Análisis del sentimiento (resumen, temas) | `sentiment_analysis` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Historia de notas | `sentiment_history` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Uso del modelo por ficha (contadores) | `sentiment_usage` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Fichas guardadas | `location` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Jobs que nombran al usuario de Google y la ficha | `job` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas. Solo los anteriores a la baja y que no estén en curso |
| Reseñas que Google deja de devolver | `review` | 30 días desde que se marcan (`removed_at`) | Worker, job `retention` | Pasada diaria |
| Fichas que Google deja de listar, o cuya lectura rechaza (403 o 404) dos sincronizaciones seguidas, con sus datos | `location` y las de arriba | 30 días desde que se retiran (`removed_at`) | Worker, job `retention` | Pasada diaria. Desde que se retiran, la API no sirve nada de ellas |
| Topes de llamadas de pago: contadores por ámbito y ventana (el id de la cuenta en los borradores) | `usage_counter` | Hasta que acaba su ventana (un día) | Worker, job `retention` | Pasada diaria |
| Registro de respuestas publicadas | `audit_log` y `action` (con su texto) | 12 meses desde la baja efectiva | Worker, job `retention` | Pasada diaria. En `audit_log` la base impone los 12 meses con una política |
| Sesiones | `session` | 30 días desde que caducan o se cierran | Worker, job `retention` | Pasada diaria |
| Correo de la cuenta | `account.email` | Hasta la baja efectiva de la cuenta | La base (disparador `account_baja_guard`) | En la misma escritura que da de baja la cuenta |
| Lista de espera | `waitlist_entry` | 12 meses desde el alta, o hasta el lanzamiento | Worker, job `retention` | Pasada diaria. El lanzamiento se indica con `FICHAITOR_RETENTION_WAITLIST_LAUNCHED_AT` |
| Código de baja (en claro) | Carga del job `send_mail` | Hasta enviar el correo | Worker, job `send_mail` | Al enviarlo, al fallar para siempre o al agotar los intentos |
| Token de restauración (en claro) | Carga del job `send_mail` | Hasta enviar el correo | Worker, job `send_mail` | Igual que la fila anterior |
| Huella del código de baja | `offboarding_code` | 30 días desde que caduca | Worker, job `retention` | Pasada diaria |
| Huella del token de restauración | `offboarding.restore_token_hash` | Hasta restaurar o hasta la baja efectiva | API (al restaurar) o worker | En esa misma escritura |
| Mensajes del canal (lo que escribe el dueño y el extracto de la reseña) | `channel_message` | 90 días desde que se escriben; o hasta la baja efectiva | Worker, job `retention`; y `apply_offboarding` | Pasada diaria. Al hacerse efectiva la baja (de la ficha o de la cuenta), en esa misma transacción |
| Propuestas (con el texto de la reseña y la respuesta propuesta) y el hilo de la conversación | `proposal` y `conversation` | 90 días desde que se crean, una vez terminadas; o hasta la baja efectiva | Worker, job `retention`; y `apply_offboarding` | Pasada diaria, si ningún mensaje ni hilo las nombra. Al hacerse efectiva la baja, en esa misma transacción |
| Chats vinculados (identificador del chat de Telegram) | `channel_identity` | Hasta que se desvinculan, más 90 días; o hasta la baja efectiva de la cuenta | Worker, job `retention`; y `apply_offboarding` | Al confirmar la baja de la cuenta se desvinculan (la API, en la misma escritura). Al hacerse efectiva, se borran. Los desvinculados, en la pasada diaria a los 90 días |
| Huellas de los códigos de vinculación | `link_code` | Un día desde que caducan; o hasta la baja efectiva de la cuenta | Worker, job `retention`; y `apply_offboarding` | Pasada diaria |
| Eventos de canal ya vistos (canal e identificador de la actualización) | `channel_inbound_event` | 90 días | Worker, job `retention` | Pasada diaria |
| Chats sin vincular (huella, intentos y bloqueo) | `channel_sender` | 90 días sin actividad y sin bloqueo vigente | Worker, job `retention` | Pasada diaria. Sin datos personales: solo la huella del chat |
| Códigos incorrectos de todos los chats, por hora | `link_failure_window` | Un día | Worker, job `retention` | Pasada diaria. Sin datos personales |
| Actualización de Telegram (puede llevar un código y lo que escribe el dueño) | Carga del job `channel_inbound` | Hasta procesarla | Conversación, job `channel_inbound` | Al terminar el job, acabe como acabe; `on_lost` vacía lo que deje un proceso caído |
| Eventos de embudo | `funnel_event` | Sin plazo | Nadie | No llevan datos personales |

Lo que queda para siempre, sin datos personales:

- La fila de `account` de una cuenta dada de baja: su identificador, `created_at`, `disconnected_at` y `deleted_at`. El correo está vacío y `google_sub` vale `deleted:<id>`.
- La fila de `offboarding`: alcance y fechas. `google_sub` se vacía al borrar los datos.
- Las filas de `retention_run`: recuentos por tabla.
- Los jobs terminados que no nombran a ningún usuario.

## Qué significa "al momento"

El acceso a los datos se cierra en la misma petición que confirma la baja, aunque el borrado físico llegue después:

- Las rutas responden 409 mientras la baja está programada.
- Las consultas de la API llevan la condición en el propio SQL: no devuelven nada de un usuario de Google con una baja programada, ni con una baja efectiva cuyos datos todavía no se han borrado.
- La condición va por usuario de Google, no por cuenta. Una cuenta nueva creada después con la misma identidad de Google no ve nada de la anterior.

## Volver a conectar

Volver a conectar antes de los 30 días no recupera nada. La primera sincronización de la conexión nueva borra antes todo lo anterior (`leave_purged_on_reconnect` en el log) y la ficha se lee desde cero. Hasta entonces el panel responde `pendiente`.

Si la baja se hace efectiva cuando el dueño ya ha vuelto a conectar, el worker no revoca nada (`revoke_state = 'superseded'`). Google revoca la concesión entera, no un token, y cortaría la conexión nueva. Los tokens antiguos se borran igualmente.

## Dos cuentas con la misma ficha

Los datos se guardan por usuario de Google y ficha. La baja de una cuenta no toca los datos de otra que administre la misma ficha de Google: son filas distintas.

## La tarea de retención

- **Job `retention`**, una pasada al día a las `FICHAITOR_RETENTION_HOUR_UTC` (03:00 UTC).
- **Sin planificador aparte.** Cada worker intenta encolar el job del día con la clave `retention:<fecha>`. La clave es única, así que con varias réplicas hay un solo job.
- **Idempotente.** Decide con el reloj y con lo que hay en la base. Repetirla no borra nada más. Dos pasadas a la vez no fallan.
- **Registro.** Cada pasada inserta una fila en `retention_run` con los recuentos por tabla y escribe `retention_done` en el log. Sin identificadores.
- **Red de seguridad.** También hace efectivas las bajas vencidas cuyo job se perdió.

Para consultar las pasadas, como propietario:

```sql
SELECT at, deleted FROM retention_run ORDER BY id DESC LIMIT 10;
```

## Privilegios

El borrado lo hace el worker y nadie más. La API sigue sin poder borrar nada.

- **Sin rol aparte para la retención.** Lo usaría el mismo proceso, con la otra cadena de conexión al lado. No separaría nada y añadiría un secreto.
- **`audit_log`.** El worker tiene `DELETE` y una política de fila que solo deja borrar las filas de cuentas con más de 12 meses de baja. No puede leerlas ni cambiarlas. Su `DELETE` no lleva `WHERE`, porque no tiene `SELECT` sobre ninguna columna: qué filas caen lo decide la política. El suelo de 12 meses está fijo en la base; la configuración puede alargarlo, no acortarlo.
- **`account`.** El worker fecha la desconexión y la baja. No lee el correo ni cambia `google_sub`: lo hace el disparador. La API solo puede quitar `disconnected_at`, al volver a conectar.
- **`session` y `waitlist_entry`.** El worker lee fechas y borra. No lee la huella del token ni el contacto.
- **Tablas de los canales.** El worker solo borra: lee las columnas de fecha y de cuenta que usan sus `WHERE`, nunca la dirección del chat ni el texto de un mensaje. La conversación (`fichaitor_conversation`) no borra nada; la API solo desvincula.
- Nadie tiene `TRUNCATE`.

## Variables

| Variable | Por defecto | Uso |
|---|---|---|
| `FICHAITOR_OFFBOARDING_GRACE_HOURS` | `48` | Horas que una baja confirmada queda programada |
| `FICHAITOR_OFFBOARDING_CODE_TTL_SECONDS` | `600` | Validez del código |
| `FICHAITOR_OFFBOARDING_CODE_MAX_ATTEMPTS` | `5` | Intentos antes de invalidar el código |
| `FICHAITOR_OFFBOARDING_CODE_RATE_LIMIT` y `..._RATE_WINDOW_SECONDS` | `5` y `3600` | Códigos por cuenta y ventana |
| `FICHAITOR_OFFBOARDING_RESTORE_RATE_LIMIT` | `20` | Restauraciones por enlace, por IP y hora |
| `FICHAITOR_OFFBOARDING_SECRET` | El de `FICHAITOR_OAUTH_STATE_SECRET` | Secreto de la huella de los códigos |
| `FICHAITOR_RETENTION_GOOGLE_DATA_DAYS` | `30` | Lo obtenido de Google, tras la baja efectiva. Máximo 30 |
| `FICHAITOR_RETENTION_REMOVED_DAYS` | `30` | Reseñas y fichas retiradas por Google. Máximo 30 |
| `FICHAITOR_RETENTION_SESSION_DAYS` | `30` | Sesiones caducadas o cerradas. Máximo 30 |
| `FICHAITOR_RETENTION_AUDIT_MONTHS` | `12` | Registro de respuestas. Mínimo 12 |
| `FICHAITOR_RETENTION_WAITLIST_MONTHS` | `12` | Lista de espera. Máximo 12 |
| `FICHAITOR_RETENTION_WAITLIST_LAUNCHED_AT` | — | Fecha del aviso de lanzamiento. Las altas anteriores se borran en la pasada siguiente |
| `FICHAITOR_RETENTION_CODE_DAYS` | `30` | Huellas de códigos caducados |
| `FICHAITOR_RETENTION_CHANNEL_DAYS` | `90` | Mensajes del canal, eventos vistos, propuestas terminadas y chats desvinculados. Máximo 90 |
| `FICHAITOR_RETENTION_HOUR_UTC` | `3` | Hora de la pasada diaria |

Los límites impiden configurar un plazo más largo que el que promete la política.

## Lo que no cubre

- **La baja de la lista de espera a petición** ("responde «baja»"). Hoy es manual.
- **Los registros del servidor** (6 meses, según la política). Dependen de la rotación de logs del despliegue, no de este código.
- **Las copias de seguridad.** Un dato borrado sigue en las copias hasta que caducan.
- **El modo sin base de datos** (`MemoryStore`). La baja se programa y se restaura, pero nunca se hace efectiva: no hay worker.

## Pruebas

Las pruebas de integración crean y borran filas y prueban sentencias que deben denegarse. **Solo se ejecutan contra una base de datos de pruebas**, cuyo nombre termina en `_test`. `tests/conftest.py` detiene la suite si alguna de las tres URL apunta a otra base.

Crea la base de pruebas, con el servicio `db` en marcha:

```bash
scripts/preparar_bd_pruebas.sh
```

> **Precaución:** El 2026-11-12 una prueba de privilegios ejecutada contra la base de desarrollo borró las conexiones de Google y los resúmenes de una cuenta real. La prueba hacía `DELETE` sin `WHERE` confiando en que se denegaba, y dejó de denegarse al conceder al worker el privilegio de borrado. Desde entonces las pruebas tienen su propia base y toda sentencia que una prueba de privilegios intenta lleva `WHERE false`.
