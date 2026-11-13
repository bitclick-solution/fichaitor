# Baja y conservación de datos

Qué dato guarda Fichaitor, cuánto tiempo, quién lo borra y cuándo. Sirve para contrastar la Política de Privacidad (`docs/legal/privacidad.md`) con lo que hace el sistema, línea a línea.

Los plazos los aprobó el propietario el 2026-11-12. Todos están en configuración (`FICHAITOR_RETENTION_*` y `FICHAITOR_OFFBOARDING_*`) con esos valores por defecto.

## Cómo es una baja

Hay dos alcances: `ficha` (desconectar Google; la cuenta sigue existiendo, sin fichas) y `cuenta` (borrar la cuenta).

Hay tres formas de llegar al borrado: la baja desde el panel (este apartado), la baja por correo que tramita soporte ([Baja por correo](#baja-por-correo)) y retirar el acceso desde la cuenta de Google ([Retirar el acceso desde Google](#retirar-el-acceso-desde-google)).

1. **Código.** El dueño pide la baja desde su sesión. La API genera un código de 6 cifras, guarda solo su huella y encola el correo que lo lleva.
2. **Confirmación.** Con el código correcto, la baja queda **programada** a 48 horas (`FICHAITOR_OFFBOARDING_GRACE_HOURS`, nunca menos de 48). La base de datos no admite una baja programada a menos de 47 horas ni una cuyo usuario de Google no sea el de esa cuenta (disparador `offboarding_guard`), y el worker no hace efectiva una baja que no cumpla eso. Desde ese instante:
   - Ninguna ruta devuelve datos de la ficha: responden 409.
   - No se encolan sincronizaciones y el worker no lee la ficha, no la analiza y no envía respuestas de esa cuenta.
   - Por el canal (Telegram) no sale nada por iniciativa del agente: las propuestas sin confirmar de la cuenta pasan a `cancelada` (motivo `offboarding`) en la misma escritura, no se crean propuestas nuevas, no se envían recordatorios y el resultado de una acción ya en marcha se anota sin avisar. Los jobs que ya estaban en cola terminan sin enviar nada.
   - Con `cuenta`, se cierran todas las sesiones.
   - No se revoca ni se borra nada: se puede restaurar sin volver a conectar con Google.
3. **Restauración.** Hasta la fecha, desde la sesión o desde el enlace del correo. Todo vuelve a como estaba, salvo las propuestas canceladas por la baja: una fecha clave se vuelve a preguntar en la siguiente planificación diaria; una reseña sigue en el panel.
4. **Baja efectiva.** A las 48 horas, el worker (job `apply_offboarding`):
   - Deja la cuenta sin fichas. Con `cuenta`, la da de baja: vacía su correo y libera su identidad de Google.
   - **Revoca en Google todas las conexiones guardadas** de ese usuario y **borra cada token cifrado en ese momento**.
   - Si Google no responde tras 5 intentos, borra los tokens igualmente, anota `revoke_state = 'failed'` en `offboarding` y escribe un aviso en el log (`offboarding_effective`, nivel `warning`).
5. **Borrado.** La tarea diaria de retención borra físicamente lo que vence, según la tabla siguiente.

> **Nota:** La fecha efectiva de la baja es `offboarding.effective_at`, que coincide con `account.disconnected_at`. Los plazos de 30 días y de 12 meses se cuentan desde ella.

## Tabla de conservación

| Dato | Tabla | Plazo | Quién lo borra | Cuándo |
|---|---|---|---|---|
| Tokens de Google (cifrados) y el correo de la conexión | `google_connection` | Hasta la baja efectiva, o hasta que una conexión más reciente del mismo usuario funciona, o hasta que Google responde `invalid_grant` | Worker, job `apply_offboarding`; y `sync_profile`, `reply_review` y `set_special_hours` | En el momento de la baja efectiva, tras revocar cada token en Google o agotar los intentos. Tras una reconexión, en la primera sincronización correcta con la conexión nueva: las anteriores se borran sin revocarlas (Google revocaría la concesión entera y cortaría la nueva). Con `invalid_grant`, el token se borra en ese momento y la fila queda, sin token y con `revoked_at`, hasta el borrado de los 30 días |
| Lo obtenido de Google de **todas** las fichas de un usuario que retiró el acceso desde Google: las mismas tablas que en la baja efectiva de la ficha (resumen y hechos, reseñas con su clasificación y su borrador, análisis, historia y uso del modelo, fichas guardadas, jobs que lo nombran, conversación y propuestas) y la conexión revocada | `profile_summary`, `review`, `review_sentiment`, `review_draft`, `sentiment_analysis`, `sentiment_history`, `sentiment_usage`, `location`, `job`, `proposal`, `conversation`, `channel_message`, `google_connection` | 30 días desde el último `revoked_at` de sus conexiones (`FICHAITOR_RETENTION_GOOGLE_DATA_DAYS`), si en ese tiempo no vuelve a conectar | Worker, job `retention` | Pasada diaria. Volver a conectar lo cancela. Consulta [Retirar el acceso desde Google](#retirar-el-acceso-desde-google) |
| Datos de la ficha y hechos (`facts`) | `profile_summary` | 30 días desde la baja efectiva | Worker, job `retention` | En la primera pasada diaria tras cumplirse el plazo, o al volver a conectar |
| Reseñas | `review` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que la fila anterior |
| Clasificación de cada reseña | `review_sentiment` | Con su reseña | La base (`ON DELETE CASCADE`) | Al borrarse la reseña |
| Borradores de respuesta | `review_draft` | Con su reseña | La base (`ON DELETE CASCADE`) | Al borrarse la reseña |
| Análisis del sentimiento (resumen, temas) | `sentiment_analysis` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Historia de notas | `sentiment_history` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Uso del modelo por ficha (contadores) | `sentiment_usage` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Fichas guardadas | `location` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas |
| Jobs que nombran al usuario de Google y la ficha | `job` | 30 días desde la baja efectiva | Worker, job `retention` | Igual que las reseñas. Solo los anteriores a la baja y que no estén en curso |
| Jobs terminados (`done` y `failed`), de cualquier tipo | `job` | 14 días desde que terminan (`FICHAITOR_RETENTION_JOB_DAYS`) | Worker, job `retention` | Pasada diaria. Se conserva el job que ejecutó una acción (`action.job_id`) mientras se conserva la acción: es el rastro de quién la ejecutó. Las claves diarias del sistema (`retention:<fecha>`, `plan_key_dates:<fecha>`) son siempre más recientes que el plazo (mínimo 2 días), así que no se vuelven a encolar |
| Reseñas que Google deja de devolver | `review` | 30 días desde que se marcan (`removed_at`) | Worker, job `retention` | Pasada diaria |
| Fichas que Google deja de listar, o cuya lectura rechaza (403 o 404) dos sincronizaciones seguidas, con sus datos | `location` y las de arriba | 30 días desde que se retiran (`removed_at`) | Worker, job `retention` | Pasada diaria. Desde que se retiran, la API no sirve nada de ellas |
| Lo obtenido de Google de una ficha que el dueño **desactiva** (multinegocio): resumen, hechos, reseñas con su clasificación y su borrador, análisis, historia y uso del modelo | `profile_summary`, `review`, `review_sentiment`, `review_draft`, `sentiment_analysis`, `sentiment_history`, `sentiment_usage` | 30 días desde que se desactiva (`location.deactivated_at`, `FICHAITOR_RETENTION_INACTIVE_DAYS`), si no se vuelve a activar | Worker, job `retention` | Pasada diaria, con las mismas sentencias que las fichas retiradas. La fila de `location` se queda mientras Google la liste: se puede activar otra vez y se lee desde cero. Activarla antes del plazo vacía `deactivated_at` y conserva los datos. Hasta el paso 9 de `docs/multinegocio.md`, no se toca la ficha elegida en el panel |
| Topes de llamadas de pago: contadores por ámbito y ventana (el id de la cuenta en los borradores) | `usage_counter` | Hasta que acaba su ventana (un día) | Worker, job `retention` | Pasada diaria |
| Respuestas con IA gastadas del plan Gratis, por ficha de Google: solo una huella (HMAC-SHA256 del id de la ficha con `FICHAITOR_ALLOWANCE_PEPPER`), cuántas y la primera y la última fecha. Ni el id en claro, ni la cuenta, ni un correo | `free_allowance` | **3 años desde el último uso**. No la borran la baja, la retención de 30 días ni la purga por revocación: si se borrase, darse de baja y volver a conectar devolvería las 25 gratuitas. Base legal: interés legítimo (art. 6.1.f RGPD) en prevenir el abuso del plan gratuito | Worker, job `retention` | Pasada diaria. La base impide borrar antes de los 3 años (`free_allowance_guard`) |
| Peticiones de la web al proceso de cobro (comprar una plaza, pagar, portal…), con su URL de Stripe | `billing_request` | 30 días | Worker, job `retention` | Pasada diaria |
| Identificadores de los eventos de Stripe ya procesados (nunca su carga) | `stripe_event` | 90 días desde que llegan | Worker, job `retention` | Pasada diaria. Los sin procesar se quedan |
| Datos fiscales, plazas, cobros, facturas y bloqueos por impago | `billing_customer`, `billing_customer_history`, `billing_seat`, `billing_charge`, `billing_document`, `account_billing_block` | 6 años desde la última factura (art. 30 del Código de Comercio), también tras la baja | Proceso de cobro, job `billing_purge`, con la función `billing_purge()` (el plazo está en la función; el proceso no tiene `DELETE`) | Pasada diaria. Nunca un bloqueo abierto; los datos fiscales, solo con la cuenta dada de baja y sin facturas, cobros ni plazas |
| Registro de respuestas publicadas | `audit_log` y `action` (con su texto) | 12 meses desde la baja efectiva | Worker, job `retention` | Pasada diaria. En `audit_log` la base impone los 12 meses con una política |
| Registro de cambios de horario de un día (fechas clave: el día y el horario confirmado) | `audit_log` y `action` (`type = 'set_special_hours'`) | 12 meses desde la baja efectiva, como las respuestas | Worker, job `retention` | Pasada diaria, en las mismas sentencias que las respuestas. Es la prueba de qué cambió Fichaitor en la ficha y quién lo confirmó |
| Registro de activaciones de fichas (cuándo se activó, desactivó o cambió de plan cada ficha, y quién lo escribió) | `location_activation_log` | 12 meses desde la baja efectiva, como el registro de respuestas. El cobro lo revisará (obligaciones fiscales) | Worker, job `retention` | Pasada diaria. Sin datos personales: identificadores, fechas y planes |
| Derechos de plan (cuántas fichas de cada plan puede tener la cuenta y hasta cuándo) | `plan_entitlement` | 12 meses desde la baja efectiva | Worker, job `retention` | Pasada diaria. Sin datos personales |
| Comunidad autónoma y horario normal de la ficha (`facts.region`, `facts.regular_hours`) | `profile_summary` | 30 días desde la baja efectiva | Worker, job `retention` | Con el resto de los hechos de la ficha |
| Sesiones | `session` | 30 días desde que caducan o se cierran | Worker, job `retention` | Pasada diaria |
| Correo de la cuenta | `account.email` | Hasta la baja efectiva de la cuenta | La base (disparador `account_baja_guard`) | En la misma escritura que da de baja la cuenta |
| Lista de espera | `waitlist_entry` | 12 meses desde el alta, o hasta el lanzamiento | Worker, job `retention` | Pasada diaria. El lanzamiento se indica con `FICHAITOR_RETENTION_WAITLIST_LAUNCHED_AT` |
| Código de baja (en claro) | Carga del job `send_mail` | Hasta enviar el correo | Worker, job `send_mail` | Al enviarlo, al fallar para siempre o al agotar los intentos |
| Token de restauración (en claro) | Carga del job `send_mail` | Hasta enviar el correo | Worker, job `send_mail` | Igual que la fila anterior |
| Huella del código de baja | `offboarding_code` | 30 días desde que caduca | Worker, job `retention` | Pasada diaria |
| Huella del token de restauración | `offboarding.restore_token_hash` | Hasta restaurar o hasta la baja efectiva | API (al restaurar) o worker | En esa misma escritura |
| Mensajes del canal (lo que escribe el dueño y el extracto de la reseña) | `channel_message` | 90 días desde que se escriben; o hasta la baja efectiva | Worker, job `retention`; y `apply_offboarding` | Pasada diaria. Al hacerse efectiva la baja (de la ficha o de la cuenta), en esa misma transacción |
| Propuestas (con el texto de la reseña y la respuesta propuesta, o la fecha clave con el nombre del festivo y el horario elegido) y el hilo de la conversación | `proposal` y `conversation` | 90 días desde que se crean, una vez terminadas; o hasta la baja efectiva | Worker, job `retention`; y `apply_offboarding` | Pasada diaria, si ningún mensaje, hilo ni carril las nombra. Al hacerse efectiva la baja, en esa misma transacción |
| Carriles de la conversación (qué propuesta está activa en cada ficha) | `conversation_lane` | Con su ficha, o hasta la baja efectiva | La base (`ON DELETE CASCADE` al borrar la ficha) y `apply_offboarding` | Al borrarse la ficha. Al hacerse efectiva la baja, en esa misma transacción, antes que las propuestas. Sin datos personales |
| Chats vinculados (identificador del chat de Telegram) | `channel_identity` | Hasta que se desvinculan, más 90 días; o hasta la baja efectiva de la cuenta | Worker, job `retention`; y `apply_offboarding` | Al confirmar la baja de la cuenta se desvinculan (la API, en la misma escritura). Al hacerse efectiva, se borran. Los desvinculados, en la pasada diaria a los 90 días |
| Huellas de los códigos de vinculación | `link_code` | Un día desde que caducan; o hasta la baja efectiva de la cuenta | Worker, job `retention`; y `apply_offboarding` | Pasada diaria |
| Eventos de canal ya vistos (canal e identificador de la actualización) | `channel_inbound_event` | 90 días | Worker, job `retention` | Pasada diaria |
| Chats sin vincular (huella, intentos y bloqueo) | `channel_sender` | 90 días sin actividad y sin bloqueo vigente | Worker, job `retention` | Pasada diaria. Sin datos personales: solo la huella del chat |
| Códigos incorrectos de todos los chats, por hora | `link_failure_window` | Un día | Worker, job `retention` | Pasada diaria. Sin datos personales |
| Actualización de Telegram (puede llevar un código y lo que escribe el dueño) | Carga del job `channel_inbound` | Hasta procesarla | Conversación, job `channel_inbound` | Al terminar el job, acabe como acabe; `on_lost` vacía lo que deje un proceso caído |
| Eventos de embudo | `funnel_event` | Sin plazo | Nadie | No llevan datos personales |

Lo que queda para siempre, sin datos personales:

- La fila de `account` de una cuenta dada de baja: su identificador, `created_at`, `disconnected_at` y `deleted_at`. El correo está vacío y `google_sub` vale `deleted:<id>`.
- La fila de `offboarding`: alcance, fechas y `reason` (`email_request` en una baja por correo). `google_sub` se vacía al borrar los datos de una baja efectiva y, en una baja restaurada o sustituida por otra, en la misma escritura que la restaura (disparador `offboarding_guard`).
- Las filas de `retention_run`: recuentos por tabla.
- Los jobs terminados que ejecutaron una acción, mientras se conserva la acción (12 meses desde la baja). El resto de jobs terminados, 14 días.

## Qué significa "al momento"

El acceso a los datos se cierra en la misma petición que confirma la baja, aunque el borrado físico llegue después:

- Las rutas responden 409 mientras la baja está programada.
- Las consultas de la API llevan la condición en el propio SQL: no devuelven nada de un usuario de Google con una baja programada, ni con una baja efectiva cuyos datos todavía no se han borrado.
- La condición va por usuario de Google, no por cuenta. Una cuenta nueva creada después con la misma identidad de Google no ve nada de la anterior.

## Cuenta en pausa por impago

Un impago bloquea toda la cuenta, sin límite de tiempo, hasta que se paga (decisiones 1.1 y 9.0 de `docs/admin-facturacion.md`). **La pausa no es una baja y no borra nada:**

- Se conservan las fichas, los resúmenes, las reseñas, el análisis, las propuestas abiertas, los chats vinculados y la conexión con Google. No empieza ningún plazo de borrado por el bloqueo.
- Lo que sí deja de pasar: la API responde 402 `account_blocked` en las rutas de datos, el worker no lee Google ni llama al modelo y la conversación no propone ni entrega nada (`docs/worker.md` y `docs/canales.md`, "Cuenta en pausa por impago").
- Los plazos de siempre siguen corriendo para lo que ya tenía su plazo: por ejemplo, los 90 días de los mensajes de los canales o los 30 días de una ficha que el dueño había desactivado antes. La pasada diaria de retención no mira el bloqueo.
- Darse de baja y desconectar Google siguen disponibles durante el bloqueo. Si el dueño se da de baja, se aplican los plazos de este documento.
- La fila de `account_billing_block` se guarda con los datos de cobro (tabla de conservación).

## Volver a conectar

Volver a conectar antes de los 30 días no recupera nada. La primera sincronización de la conexión nueva borra antes todo lo anterior (`leave_purged_on_reconnect` en el log) y la ficha se lee desde cero. Hasta entonces el panel responde `pendiente`.

Si la baja se hace efectiva cuando el dueño ya ha vuelto a conectar, el worker no revoca nada (`revoke_state = 'superseded'`). Google revoca la concesión entera, no un token, y cortaría la conexión nueva. Los tokens antiguos se borran igualmente.

## Retirar el acceso desde Google

El dueño puede quitar el acceso a Fichaitor desde `myaccount.google.com/connections`, sin pasar por el panel. Fichaitor se entera la siguiente vez que renueva el token: Google responde `invalid_grant`.

1. **Revocada por el usuario.** Ante un `invalid_grant` al renovar el token (en `sync_profile`, `reply_review` o `set_special_hours`), el worker marca esa conexión con `revoked_at` y **borra su token cifrado** en la misma escritura: ya no vale. Solo esa: las anteriores no se tocan. Si también están muertas (lo normal cuando el dueño retira el acceso, porque Google revoca la concesión entera), cada una cae por su propio `invalid_grant` en la sincronización siguiente, porque `latest_connection` pasa a la siguiente válida. Así una conexión basura que la API haya guardado para otro usuario no arrastra a la buena (hallazgo B14 de `docs/seguridad/revision-2026-11-12-c.md`). Las anteriores solo se borran cuando una más nueva ha funcionado (la primera sincronización correcta). No se revoca nada en Google: ya está revocado. En el log queda `google_grant_revoked`, nivel `warning`, sin identificadores de Google.
2. **Lo demás sigue igual.** Las sesiones siguen abiertas, los datos se siguen sirviendo y el panel enseña `needs_reconnect`, como con cualquier acceso perdido.
3. **Cuenta atrás.** Si ese usuario de Google se queda sin ninguna conexión válida (todas revocadas), empieza la cuenta atrás. No hay tabla aparte: la fecha es el último `revoked_at` de sus conexiones. Con varias conexiones guardadas, empieza cuando cae la última, una sincronización después de la primera por cada conexión que quedaba.
4. **Borrado a los 30 días** (`FICHAITOR_RETENTION_GOOGLE_DATA_DAYS`). La pasada diaria de retención borra lo obtenido de Google de **todas** las fichas de esa identidad, con las mismas tablas y reglas que la baja efectiva de una ficha: la conversación y las propuestas (citan reseñas), las fichas guardadas, los resúmenes y hechos, las reseñas con su clasificación y su borrador, el análisis del sentimiento con su historia y su uso, los jobs que nombran al usuario y la conexión revocada. La cuenta queda sin fichas y con `disconnected_at`, así que `GET /api/v1/me` responde `connected: false`; sus sesiones y sus chats vinculados siguen. El registro de respuestas publicadas se conserva 12 meses desde ese momento, como en una baja. En los recuentos de `retention_run` aparece `revoked_identity`.
5. **Volver a conectar lo cancela.** Una conexión nueva con Google, antes de los 30 días, deja al usuario con una conexión válida: la pasada ya no lo borra, y la primera sincronización correcta borra la conexión revocada. Si después vuelve a retirar el acceso, la cuenta atrás empieza de nuevo desde esa fecha. La pasada del día 30 bloquea antes la fila de la cuenta y vuelve a leer las conexiones después: una reconexión que llega a la vez (la conexión se guarda antes de escribir la cuenta) la cancela (I17).

Qué no cuenta como acceso retirado: un 401 sin `invalid_grant`, un 5xx, un 429 (cuota) o un fallo de red. El primero pide volver a conectar, como antes; los demás se reintentan. Las credenciales de Fichaitor rechazadas (`invalid_client`) son un error nuestro y no tocan la conexión.

La base lo protege (disparador `google_connection_guard`): una conexión nace válida, y lo único que el worker puede hacerle después es marcarla como revocada borrando su token. Una conexión revocada no vuelve a tener token. Que haya `revoked_at` y no haya token van siempre juntos (`google_connection_revoked_check`).

## Baja por correo

Soporte recibe una petición de baja por correo, comprueba que viene de la dirección de la cuenta con `dmarc=pass` alineado con el dominio del `From:` (sin eso no se tramita por correo: se pide hacerla desde el panel) y la tramita con `scripts/baja_manual.py` (procedimiento en [despliegue.md](despliegue.md#tareas-habituales)). Hace lo mismo que la baja del panel, sin código.

- **Programada 48 horas, como en el panel** (hallazgo B15 de `docs/seguridad/revision-2026-11-12-c.md`). El script escribe la baja con `reason = 'email_request'`, `delete_at` a 48 horas y la huella de un token de restauración, en la misma transacción que cancela las propuestas, cierra las sesiones y desvincula los chats (con `cuenta`), encola el correo de «baja programada» a la dirección de la cuenta (con su enlace `#restaurar=`) y encola `apply_offboarding` para esa fecha. Hasta entonces se puede restaurar desde el enlace, como una del panel; desde el primer momento no se lee la ficha ni se envían avisos. Pasado el plazo, el worker la hace efectiva: revoca en Google, borra los tokens y empiezan los plazos de la tabla de arriba.
- **Inmediata, solo en casos excepcionales** (`--inmediata`, con el visto bueno del responsable): `delete_at = confirmed_at`, sin token ni correo, y `apply_offboarding` para ya. El worker la hace efectiva en segundos. No tiene vuelta atrás.
- **Una baja programada desde el panel queda sustituida** (`restored_at`), como cuando el dueño confirma otra.
- **Con `ficha`**, Google concede el acceso por usuario: se desconectan todas las fichas de la cuenta. Si la cuenta tiene más de una, el script exige `--location` con la ficha que nombra el correo, para comprobar que es de esa cuenta.
- **La vía del propietario.** El disparador `offboarding_guard` exige 47 horas a cualquier rol que no sea el propietario (y desde M6, ninguna tabla temporal lo puede engañar: ver «Privilegios»). `reason = 'email_request'` solo lo puede escribir el propietario: la API no tiene privilegio sobre esa columna y el disparador lo rechaza a cualquier otro rol. Nadie la cambia después. El worker hace efectiva sin las 47 horas una baja solo si lleva esa razón; una escrita por el propietario sin ella se queda sin hacer.
- **Auditoría.** Una fila en `audit_log` con el evento `offboarding_email_request`: el id de la baja, el alcance, la ficha nombrada, el motivo (una referencia, sin direcciones de correo: el script las rechaza) y `written_by`, el usuario de la base que lo ejecutó. Se conserva 12 meses desde la baja efectiva, como el resto del registro. El script imprime además una línea JSON `baja_manual` con lo mismo, sin el motivo.

## Dos cuentas con la misma ficha

Los datos se guardan por usuario de Google y ficha. La baja de una cuenta no toca los datos de otra que administre la misma ficha de Google: son filas distintas.

## La tarea de retención

- **Job `retention`**, una pasada al día a las `FICHAITOR_RETENTION_HOUR_UTC` (03:00 UTC).
- **Sin planificador aparte.** Cada worker intenta encolar el job del día con la clave `retention:<fecha>`. La clave es única, así que con varias réplicas hay un solo job.
- **Idempotente.** Decide con el reloj y con lo que hay en la base. Repetirla no borra nada más. Dos pasadas a la vez no fallan.
- **Registro.** Cada pasada inserta una fila en `retention_run` con los recuentos por tabla y escribe `retention_done` en el log. Sin identificadores.
- **Red de seguridad.** También hace efectivas las bajas vencidas cuyo job se perdió.
- **Aviso si no corre.** Si pasan 36 horas sin una fila nueva en `retention_run` (o sin ninguna desde que arrancó el worker), el worker escribe `retention_overdue` en el log, nivel `warning`, cada pocos minutos hasta que corra. Nadie más que el worker puede encolar un job con la clave `retention:<fecha>` (políticas de la cola en `db/schema_channels.sql`).

Para consultar las pasadas, como propietario:

```sql
SELECT at, deleted FROM retention_run ORDER BY id DESC LIMIT 10;
```

## Privilegios

El borrado lo hace el worker y nadie más. La API sigue sin poder borrar nada.

- **Sin rol aparte para la retención.** Lo usaría el mismo proceso, con la otra cadena de conexión al lado. No separaría nada y añadiría un secreto.
- **`audit_log`.** El worker tiene `DELETE` y una política de fila que solo deja borrar las filas de cuentas con más de 12 meses de baja. No puede leerlas ni cambiarlas. Su `DELETE` no lleva `WHERE`, porque no tiene `SELECT` sobre ninguna columna: qué filas caen lo decide la política. El suelo de 12 meses está fijo en la base; la configuración puede alargarlo, no acortarlo.
- **`google_connection`.** El worker lee la conexión sin el correo. Solo puede escribir `revoked_at` y `refresh_token_enc`, y el disparador `google_connection_guard` solo le deja marcarla como revocada a la vez que borra el token. La API solo inserta, y sin elegir `connected_at` (toma `now()`) ni `revoked_at` (B14).
- **Tablas temporales (M6).** Ningún rol de la aplicación tiene `TEMPORARY` sobre la base (se retira a `PUBLIC` al aplicar el esquema), y cada función de la base fija `search_path = pg_catalog, public, pg_temp`. Así ninguna tabla temporal puede tapar a una de verdad dentro de los disparadores que protegen las bajas, los topes, las fichas activas o las conexiones.
- **`offboarding.reason`.** Solo el propietario lo escribe (baja por correo). Ni la API ni el worker tienen privilegio sobre la columna.
- **`account`.** El worker fecha la desconexión y la baja. No lee el correo ni cambia `google_sub`: lo hace el disparador. La API solo puede quitar `disconnected_at`, al volver a conectar.
- **`session` y `waitlist_entry`.** El worker lee fechas y borra. No lee la huella del token ni el contacto.
- **Tablas de los canales.** El worker solo borra: lee las columnas de fecha y de cuenta que usan sus `WHERE`, nunca la dirección del chat ni el texto de un mensaje. La conversación (`fichaitor_conversation`) no borra nada; la API solo desvincula.
- **`job`.** Seguridad a nivel de fila: el worker ve y cambia todo; la API solo encola sus tipos (`reply_review`, `set_special_hours`, `send_mail`, `apply_offboarding`, `sync_profile`, `channel_inbound`) y no lee la cola (solo la vista `profile_sync_pending`: usuario de Google, ficha de Google y hora de los `sync_profile` vencidos o en curso, sin la carga; para enseñar «pendiente» al volver a conectar); la conversación solo ve, toma y cierra los suyos (`channel_inbound`, `conversation_tick`, `propose_review`, `channel_action_result`, `plan_key_dates`), nunca un `send_mail`. La clave de idempotencia de un job tiene que empezar por su tipo (salvo `<canal>:<id>` de `channel_inbound`), así que nadie más que el worker ocupa `retention:<fecha>`. La forma `sync:<usuario>:<ficha>:<franja>` de la sincronización periódica solo la pueden usar la conversación (su planificador) y el worker: la API sincroniza con claves `sync_profile:...` y no puede ocupar una franja del planificador (I16).
- **`retention_run`.** El worker inserta y lee solo `at` (para el aviso de 36 horas).
- **Fichas, carriles, registro de activaciones y derechos de plan** (multinegocio). El worker lee si una ficha está activa, con qué plan y desde cuándo está inactiva, pero no activa ni desactiva nada. Lee y borra los carriles. Del registro y de los derechos solo lee `account_id` y borra.
- Nadie tiene `TRUNCATE`.

## Variables

| Variable | Por defecto | Uso |
|---|---|---|
| `FICHAITOR_OFFBOARDING_GRACE_HOURS` | `48` | Horas que una baja confirmada queda programada. Mínimo 48: la base no admite menos de 47 |
| `FICHAITOR_OFFBOARDING_CODE_TTL_SECONDS` | `600` | Validez del código |
| `FICHAITOR_OFFBOARDING_CODE_MAX_ATTEMPTS` | `5` | Intentos antes de invalidar el código |
| `FICHAITOR_OFFBOARDING_CODE_RATE_LIMIT` y `..._RATE_WINDOW_SECONDS` | `5` y `3600` | Códigos por cuenta y ventana |
| `FICHAITOR_OFFBOARDING_RESTORE_RATE_LIMIT` | `20` | Restauraciones por enlace, por IP y hora |
| `FICHAITOR_OFFBOARDING_SECRET` | El de `FICHAITOR_OAUTH_STATE_SECRET` | Secreto de la huella de los códigos |
| `FICHAITOR_RETENTION_GOOGLE_DATA_DAYS` | `30` | Lo obtenido de Google, tras la baja efectiva o tras retirar el acceso desde Google. Máximo 30 |
| `FICHAITOR_RETENTION_REMOVED_DAYS` | `30` | Reseñas y fichas retiradas por Google. Máximo 30 |
| `FICHAITOR_RETENTION_INACTIVE_DAYS` | `30` | Lo obtenido de Google de una ficha desactivada, desde que se desactiva. Máximo 30 |
| `FICHAITOR_RETENTION_SESSION_DAYS` | `30` | Sesiones caducadas o cerradas. Máximo 30 |
| `FICHAITOR_RETENTION_AUDIT_MONTHS` | `12` | Registro de respuestas, registro de activaciones y derechos de plan. Mínimo 12 |
| `FICHAITOR_RETENTION_WAITLIST_MONTHS` | `12` | Lista de espera. Máximo 12 |
| `FICHAITOR_RETENTION_WAITLIST_LAUNCHED_AT` | — | Fecha del aviso de lanzamiento. Las altas anteriores se borran en la pasada siguiente |
| `FICHAITOR_RETENTION_CODE_DAYS` | `30` | Huellas de códigos caducados |
| `FICHAITOR_RETENTION_CHANNEL_DAYS` | `90` | Mensajes del canal, eventos vistos, propuestas terminadas y chats desvinculados. Máximo 90 |
| `FICHAITOR_RETENTION_JOB_DAYS` | `14` | Jobs terminados (`done` y `failed`). Entre 2 y 90 |
| `FICHAITOR_RETENTION_HOUR_UTC` | `3` | Hora de la pasada diaria |

Los límites impiden configurar un plazo más largo que el que promete la política.

## Lo que no cubre

- **La baja de la lista de espera a petición** ("responde «baja»"). Hoy es manual.
- **Los registros del servidor** (6 meses, según la política). Dependen de la rotación de logs del despliegue, no de este código.
- **Las copias de seguridad.** Un dato borrado sigue en las copias hasta que caducan.
- **El modo sin base de datos** (`MemoryStore`). La baja se programa y se restaura, pero nunca se hace efectiva: no hay worker.
- **Retirar el acceso sin que Fichaitor llame a Google.** Fichaitor se entera de que el acceso se retiró al renovar el token: en la siguiente sincronización (al abrir el panel) o al publicar algo. Si nadie abre el panel y no hay nada que publicar, la cuenta atrás no empieza hasta entonces.
- **Una baja por correo sin worker en marcha.** Queda registrada y los datos dejan de servirse al momento; el correo de baja programada, la revocación y el borrado de los tokens esperan a que el worker tome sus jobs. Sin el correo, el dueño no tiene el enlace para restaurarla: si el worker estuvo parado, compruébalo antes de que venza el plazo.

## Pruebas

Las pruebas de integración crean y borran filas y prueban sentencias que deben denegarse. **Solo se ejecutan contra una base de datos de pruebas**, cuyo nombre termina en `_test`. `tests/conftest.py` detiene la suite si alguna de las tres URL apunta a otra base.

Crea la base de pruebas, con el servicio `db` en marcha:

```bash
scripts/preparar_bd_pruebas.sh
```

> **Precaución:** El 2026-11-12 una prueba de privilegios ejecutada contra la base de desarrollo borró las conexiones de Google y los resúmenes de una cuenta real. La prueba hacía `DELETE` sin `WHERE` confiando en que se denegaba, y dejó de denegarse al conceder al worker el privilegio de borrado. Desde entonces las pruebas tienen su propia base y toda sentencia que una prueba de privilegios intenta lleva `WHERE false`.
