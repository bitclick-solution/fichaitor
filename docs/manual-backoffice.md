# Manual del backoffice

Guía de uso del backoffice de Fichaitor para el propietario y su equipo de soporte y de finanzas. Explica cómo entrar, qué ve cada rol, para qué sirve cada pantalla y cómo hacer las tareas de cada día. Fecha: 2026-11-13.

Detalles técnicos, si los necesitas: el diseño y los motivos están en la sección 6 de [admin-facturacion.md](admin-facturacion.md); las rutas y los datos exactos, en [contrato-backoffice.md](contrato-backoffice.md); la instalación del túnel y el alta de personas, en [despliegue.md](despliegue.md#cobro-stripe-odoo-y-el-túnel).

## Cómo entrar

### El túnel

El backoffice no está en internet. Solo se abre desde tu equipo, a través de un túnel SSH al servidor.

1. Pide al propietario que añada tu clave SSH pública al túnel. Sin ella no puedes conectarte.
2. Abre el túnel en una terminal y déjala abierta mientras trabajas:

   ```bash
   ssh -N -L 8090:127.0.0.1:8090 fichaitor-tunel@<servidor>
   ```

   La terminal no muestra nada: es normal.

3. Abre `http://localhost:8090` en el navegador. También vale `http://127.0.0.1:8090`. Cualquier otra dirección da error, aunque apunte a tu equipo.
4. Al terminar, pulsa **Salir** y cierra el túnel con `Ctrl+C`.

> **Nota:** la sesión caduca a las 8 horas, o tras 30 minutos sin actividad. Entonces vuelves a la pantalla de entrada.

### El alta

Nadie se da de alta desde el backoffice. El propietario te crea en el servidor y te envía un enlace de alta por un canal privado.

1. Con el túnel abierto, abre el enlace (empieza por `http://localhost:8090/#alta=`). Vale **24 horas** y **una sola vez**.
2. Elige una contraseña de **al menos 12 caracteres**. Se rechazan las contraseñas comunes, las secuencias y las que contienen tu usuario.
3. Escanea el código QR con una aplicación de códigos (Google Authenticator u otra que use TOTP).
4. Escribe el código de 6 cifras que muestra la aplicación.
5. Guarda los **10 códigos de recuperación** que aparecen fuera del ordenador: en papel o en un gestor de contraseñas. Solo se enseñan esta vez.

Si el enlace caducó, pide otro al propietario.

### Entrar cada día

1. Escribe tu usuario y tu contraseña.
2. Escribe el código de 6 cifras de tu aplicación. Cada código vale una sola vez: si te dice que no vale, espera al siguiente.

**Bloqueos por intentos:**

- 5 fallos seguidos (de contraseña, de código o de código de recuperación) te bloquean 15 minutos. Cada bloqueo seguido dura el doble.
- 20 fallos en una hora, de quien sea, detienen la entrada de todo el equipo hasta que pasa la hora.
- El mensaje de error es el mismo si el usuario no existe, si la contraseña está mal o si estás bloqueado.

### Códigos de recuperación

Te sirven para entrar si no tienes el móvil a mano. Cada uno vale **una vez**.

1. En la pantalla del código, elige entrar con un código de recuperación y escríbelo.
2. El backoffice te pide en ese momento dar de alta otra vez la aplicación de códigos: tienes 5 minutos.
3. Recibes **10 códigos nuevos**. Los anteriores dejan de valer. Guárdalos como los primeros.

### Si pierdes el móvil

- **Tienes tus códigos de recuperación:** entra con uno y da de alta la aplicación en el móvil nuevo, como en el apartado anterior.
- **No tienes los códigos, o has olvidado la contraseña:** pide al propietario que te restablezca. No hay recuperación por correo.

Para cambiar de móvil sin perder el acceso, entra y ve a **Mi cuenta > App de autenticación**.

### Restablecer a otra persona (propietario)

Solo el propietario, en el servidor. Las órdenes exactas están en [despliegue.md](despliegue.md#alta-del-primer-usuario-del-backoffice).

1. Comprueba la identidad de la persona **en persona o por teléfono**. Nunca solo por correo o por chat.
2. Ejecuta `restablecer --usuario <usuario>`. Se cierran todas sus sesiones y el script escribe un enlace de alta nuevo.
3. Dale el enlace por un canal privado. Hará el alta otra vez: contraseña, aplicación de códigos y códigos de recuperación.

**Si alguien deja el equipo:** ejecuta `desactivar --usuario <usuario>` y borra su línea del túnel. Las dos cosas.

**Si eres tú quien pierde el acceso:** usa tus códigos de recuperación. Como último recurso, `restablecer` sobre tu propio usuario con tu acceso SSH al servidor.

## Roles

Cada persona tiene un rol. El backoffice solo te enseña las pantallas de tu rol, y la base de datos vuelve a comprobarlo en cada petición.

- **Soporte:** atiende a los clientes. Busca cuentas, ve su estado, tramita bajas pedidas por correo y vigila la salud del servicio. No ve pagos ni métricas.
- **Finanzas:** lleva el cobro. Ve pagos, facturas y datos fiscales, revisa las facturas con problemas, pide reembolsos excepcionales y ve las métricas. No tramita bajas ni ve la salud técnica.
- **Propietario:** todo lo anterior y, además, concede y recorta derechos, cierra bloqueos a mano, ve la lista de espera en totales, las personas y el registro de auditoría.

| Pantalla o acción | Soporte | Finanzas | Propietario |
|---|---|---|---|
| Buscar cuentas | Sí | Sí | Sí |
| Cuenta: resumen, nombres de las fichas (con ticket) y chats | Sí | Sí | Sí |
| Baja por correo | Sí | No | Sí |
| Salud y sincronización | Sí | No | Sí |
| Cobro de una cuenta, datos fiscales y revisión del cobro | No | Sí | Sí |
| Reembolso excepcional | No | Sí | Sí |
| Métricas | No | Sí | Sí |
| Conceder y recortar derechos | No | No | Sí |
| Cerrar un bloqueo a mano | No | No | Sí |
| Lista de espera (totales) | No | No | Sí |
| Personas y auditoría | No | No | Sí |

**Acciones sensibles.** Reembolsar, conceder o recortar un derecho, cerrar un bloqueo, tramitar una baja y cambiar tu contraseña piden un código de tu aplicación si el último que escribiste tiene más de 5 minutos.

**La referencia del ticket.** Ver los nombres de las fichas o la dirección fiscal, y todas las acciones, piden la referencia del caso (por ejemplo, `Ticket 4321`). Escribe la del caso real, de 3 a 500 caracteres (80 en los reembolsos), en una línea y **nunca una dirección de correo**: se guarda en el registro de auditoría.

## Pantallas

### Cuentas

Busca la cuenta de un cliente. Escribe uno de estos datos, de 3 a 254 caracteres:

- el id de la cuenta;
- el id de una ficha (`loc_…`);
- el correo de la cuenta, **completo y exacto** (no importan mayúsculas);
- el NIF;
- parte de la razón social.

**No se puede buscar por el nombre de una ficha.** Es un dato de Google, y sus normas solo permiten que una persona lo lea para soporte, seguridad u obligación legal.

Cada resultado enseña el correo **enmascarado** (`d***o@example.com`), la fecha de alta, si la cuenta está dada de baja o desconectada, y cuántas fichas tiene y cuántas activas. El registro guarda qué tipo de búsqueda hiciste, no lo que buscaste.

Al abrir una cuenta ves sus pestañas:

| Pestaña | Para qué sirve | Quién la ve |
|---|---|---|
| **Resumen** | Fichas (sin nombre), derechos, plazas, bloqueos por impago, bajas y activaciones | Todos |
| **Fichas y chats** | Los nombres de las fichas, solo con la referencia del ticket, y los chats vinculados (Telegram, WhatsApp) sin teléfonos ni direcciones | Todos |
| **Sincronización** | Si cada ficha se lee bien de Google | Soporte y propietario |
| **Cobro** | Cliente, cobros, facturas legales y reembolsos, con enlaces a Stripe y a Odoo | Finanzas y propietario |
| **Baja por correo** | Tramitar una baja pedida por correo | Soporte y propietario |
| **Conceder un derecho** | Dar fichas de cortesía, de recomendación, manuales o ampliar la beta | Propietario |

Qué significa cada dato del **Resumen**:

- **Fichas:** su plan (Gratis, Pro o Pro+), desde cuándo está activa, si se desactivó o se retiró, y si Google la tiene verificada.
- **Derechos:** cuántas fichas de pago puede tener activas la cuenta, de qué plan, desde y hasta cuándo, y su origen: `pago` (una plaza pagada en Stripe), `beta`, `cortesia`, `recomendacion` o `manual`. «Vigente» indica si cuenta ahora.
- **Plazas:** cada plaza es un año pagado de un plan, con su propia fecha de renovación. Verás su estado (`active`, `pending`, `payment_failed`, `action_required`, `disputed` o `canceled`), el fin del periodo, si se cancelará al renovar, el precio de la renovación y, si está en la ventana de la beta, cuándo acaba su periodo de prueba.
- **Bloqueos por impago:** cuándo empezó, por qué (`payment_failed`: pago rechazado; `action_required`: el banco pidió confirmar el pago y no se hizo; `dispute`: el cliente disputó el cobro) y cuándo y cómo se cerró (`payment`: pagó; `admin`: se cerró a mano).
- **Bajas:** cada baja pedida, cuándo se confirmó, cuándo se hace efectiva, si se restauró y si ya es efectiva.
- **Activaciones:** el historial de fichas activadas, desactivadas y cambiadas de plan.

### Cobro: revisión

Para finanzas y propietario. Lo que en el cobro necesita una persona. Arriba, los totales; debajo, una lista por tipo de problema:

| Lista | Qué es |
|---|---|
| **Facturas legales en revisión o atrasadas** | Facturas que Odoo no ha emitido bien (`revision`), o que siguen `pendiente` desde hace más de una hora |
| **Cobrado sin factura legal** | Plazas pagadas en Stripe que todavía no tienen factura en Odoo |
| **Plazas en revisión** | Plazas pagadas que no se pudieron asignar a su ficha: `duplicate_payment` (la ficha ya tenía otra plaza pagada; esta queda libre) o `activation_refused` (la base no dejó activar la ficha, por ejemplo por una baja pendiente) |
| **Reembolsos pendientes** | Reembolsos pedidos que aún no ha hecho Stripe, o que Stripe rechazó (`fallido`) |
| **Bloqueos abiertos** | Cuentas bloqueadas por impago ahora mismo |
| **Eventos de Stripe huérfanos o atrasados** | Avisos de Stripe que no corresponden a ninguna plaza, o sin procesar desde hace más de una hora |

### Salud

Para soporte y propietario. El estado técnico del servicio:

- **Fichas leídas bien**, **Piden volver a conectar** y **Con error:** cuántas fichas están en cada estado de lectura de Google.
- **Sin leer en 24 h:** fichas sin una lectura correcta en el último día.
- **Trabajos atascados:** trabajos en marcha desde hace más de 15 minutos.
- **Trabajos fallidos en 24 h:** trabajos que agotaron sus intentos en el último día.
- **Canales:** chats vinculados y desvinculados de cada canal.
- **Cola de trabajos:** por tipo y estado, cuántos hay, el más antiguo y cuántos están atascados.
- **Trabajos fallidos:** los más recientes, con su último error recortado.
- **Fichas que no se leen bien:** las que no están en estado correcto, con su cuenta.
- **Retención:** las últimas pasadas de la limpieza diaria y cuántas filas borró de cada tabla.

### Métricas

Para finanzas y propietario. Ver [Métricas](#métricas-1).

### Lista de espera

Solo el propietario. Cuántas personas se apuntaron, por semana, canal (correo o WhatsApp), tramo de nota del diagnóstico y origen. **Solo totales:** las casillas con menos de 5 personas no salen, y no hay totales por fila ni por columna, para que no se pueda deducir a nadie.

### Personas y auditoría

Solo el propietario.

- **Personas:** quién tiene acceso, con qué rol, si está activa, si tiene el alta pendiente, si está bloqueada por intentos y cuándo cambió su contraseña. Es solo lectura: las altas, los restablecimientos y las bajas se hacen en el servidor.
- **Auditoría:** el registro de todo lo que pasa en el backoffice, de lo más nuevo a lo más viejo: entradas y fallos, lecturas de cuentas, acciones y rechazos por rol. No se puede borrar ni cambiar.

### Mi cuenta

Para todos. Cambia tu contraseña o la aplicación de códigos (por ejemplo, al cambiar de móvil).

## Procedimientos

### Atender una baja pedida por correo

Para soporte y propietario. Un cliente escribe a soporte pidiendo desconectar Google o borrar su cuenta.

**Plazos:** tienes **un mes como máximo** desde que llega el correo para tramitarla (artículo 12.3 del RGPD). Desde que la tramitas, el cliente tiene **48 horas** para deshacerla con el enlace que le llega por correo.

1. Busca la cuenta por el correo **exacto** del remitente. Si no aparece, contesta que esa dirección no tiene cuenta.
2. En el cliente de correo, abre el **original** del mensaje, con sus cabeceras. En Gmail: los tres puntos > **Mostrar original**. Cópialo entero.
3. Abre la pestaña **Baja por correo** y pega el original.
4. Pulsa **Comprobar el remitente**. El backoffice lee la primera cabecera `Authentication-Results`, la que pone **nuestro** servidor de correo, y exige `dmarc=pass` del dominio del remitente. Luego compara el remitente con el correo de la cuenta **sin enseñártelo**.
5. Mira el resultado:
   - **Error de DMARC** (falta la cabecera, es de otro servidor, el resultado no es `pass` o el dominio no coincide): **no la tramites por correo.** Contesta al cliente que haga la baja desde **Ajustes**, en su panel, con el código que le llega a su correo.
   - **DMARC correcto, pero el remitente no es el de la cuenta:** no sigas. Pide que escriban desde la dirección de la cuenta.
   - **El remitente es el correo de la cuenta:** sigue.
6. Elige qué pide:
   - **Desconectar una ficha:** elige la ficha que nombra el correo. Google concede el acceso por usuario, así que se desconectan **todas** las fichas de la cuenta. Si el cliente solo quiere dejar de usar una, dile que la desactive desde su panel.
   - **Borrar la cuenta entera.**
7. Escribe la referencia del ticket y pulsa **Programar la baja a 48 horas**. Si hace falta, escribe un código de tu aplicación.
8. Contesta al cliente: la baja está programada; le llega un correo con un enlace para deshacerla durante 48 horas; pasado ese plazo se hace efectiva y lo obtenido de Google se borra en un máximo de 30 días.

Desde el primer momento, la cuenta deja de leer la ficha y de enviar avisos. Si el cliente se arrepiente, la restaura él con el enlace del correo: **el backoffice no restaura bajas.**

> **Precaución:** la **baja inmediata**, sin las 48 horas, no está en el backoffice. Es excepcional (una orden de una autoridad, o un cliente que ya no recibe correo en esa dirección y ha acreditado su identidad por otra vía), necesita el visto bueno del responsable del tratamiento y la hace el propietario en el servidor con `scripts/baja_manual.py --inmediata` ([despliegue.md](despliegue.md#tareas-habituales)). No tiene vuelta atrás.

### Conceder o recortar fichas

Solo el propietario. Sirve para dar fichas de pago sin cobrarlas, o para quitarlas antes de tiempo.

**Conceder:**

1. Abre la cuenta y la pestaña **Conceder un derecho**.
2. Elige el plan (Pro o Pro+) y cuántas fichas (de 1 a 10).
3. Elige el origen:
   - **Cortesía:** un regalo, por ejemplo para compensar una incidencia.
   - **Recomendación:** el premio de un cliente que trae a otros.
   - **Manual:** cualquier otro acuerdo.
   - **Beta:** ampliar la beta de una cuenta. Ampliarla es conceder otro derecho «Beta» con un fin posterior.
4. Elige hasta qué día dura (incluido). Tiene que terminar entre una hora y dos años desde ahora. Empieza en el momento de concederlo.
5. Si quieres, añade una nota corta en minúsculas y guiones bajos (por ejemplo, `incidencia_noviembre`).
6. Escribe la referencia del ticket y confirma.

**Recortar:**

1. En el **Resumen**, en **Derechos**, elige el derecho y **Recortar**.
2. Elige la nueva fecha de fin, o déjala vacía para que termine ahora.
3. Escribe la referencia del ticket y confirma.

Un derecho solo se puede recortar, nunca alargar: para dar más tiempo, concede otro. Los derechos de origen `pago` no se recortan aquí: se cierran con un reembolso.

### Cerrar un bloqueo a mano

Solo el propietario. Cuando falla un pago, la cuenta entera queda bloqueada al momento, **sin límite de tiempo**, hasta que el cliente paga. La plaza no se pierde por el impago. Lo normal es que el bloqueo se cierre solo cuando llega el pago: no hagas nada.

Ciérralo a mano solo cuando sepas que la deuda está resuelta y Stripe no lo va a reflejar, por ejemplo:

- el cliente pagó por otra vía y lo has comprobado en el banco;
- Bitclick decide perdonar esa factura;
- el bloqueo se abrió por un error.

Cerrar el bloqueo **no cobra ni anula la factura en Stripe**: solo devuelve el servicio a la cuenta. Si hace falta, ajusta la factura en Stripe.

1. Abre la cuenta. En el **Resumen**, en **Bloqueos por impago**, elige el bloqueo abierto y **Cerrar a mano**.
2. Escribe la referencia del ticket, con el motivo, y confirma.

El bloqueo queda cerrado por `admin` y la acción, en la auditoría.

### Reembolso excepcional

Para finanzas y propietario. **No se devuelve nada cobrado**: activar una ficha cobra un año entero, sin prorrateo. Solo hay reembolsos en casos excepcionales.

1. Abre la cuenta, la pestaña **Cobro** y, en **Cobros**, pulsa **Reembolsar** en el cobro que vas a devolver. Solo sale en los cobros pagados que tienen algo por devolver.
2. Marca **Devolver todo lo que queda**, o escribe el importe a devolver, en euros con IVA.
3. Elige el motivo:
   - **Error en el cobro:** se cobró algo que no tocaba.
   - **Decisión comercial.**
   - **Cambio de un tercero (cláusula 11):** un servicio de terceros del que depende Fichaitor, como Google, cambió (cláusula 11 de las condiciones de uso).
   - **Disputa.**
4. Escribe la referencia del caso (hasta 80 caracteres) y confirma.

Qué pasa después, sin que hagas nada:

1. El proceso de cobro pide el reembolso a Stripe. Solo puede haber un reembolso pendiente por cobro.
2. Si el reembolso es **total**, la plaza deja de renovarse y su derecho termina en ese momento.
3. Se emite la **factura rectificativa** en Odoo, en la serie de rectificativas, y se envía al cliente por correo con su PDF.

Sigue el estado en **Cobro > Reembolsos** (`pendiente`, `hecho` o `fallido`) y la rectificativa en **Facturas legales**. Si Stripe lo rechaza, queda `fallido` y aparece en **Cobro: revisión**: mira el motivo en el panel de Stripe.

### Revisar facturas en «revisión»

Para finanzas y propietario. Cada cobro de Stripe genera su factura legal en el Odoo de Bitclick. Si algo no cuadra, la factura queda en `revision`, **sin correo al cliente**, y aparece en **Cobro: revisión**. El motivo está en la columna del motivo:

| Motivo | Qué ha pasado | Qué hacer |
|---|---|---|
| `total_mismatch` | El total de Odoo no es igual, al céntimo, a lo que cobró Stripe | Abre la factura en Odoo con su enlace. Revisa los impuestos y el redondeo (debe ser **por línea**). Una factura validada no se borra: corrige con una rectificativa en Odoo |
| `verifactu_refused` | VERI\*FACTU la aceptó con errores o la rechazó | Revisa el registro en Odoo y corrige según indique la AEAT, con una rectificativa si hace falta |
| `not_posted` | Odoo no la validó | Revisa la factura en Odoo y valídala |
| `missing_data` | Faltan datos para una factura completa: los datos fiscales del cliente, o el precio o el periodo del cobro | Mira en la pestaña **Cobro** de la cuenta si tiene datos fiscales y comprueba el cobro en Stripe. Avisa al propietario: la factura se emite a mano en Odoo con la referencia `stripe:<in_…>` |
| Otro código (por ejemplo, `odoo_rejected`) | Odoo rechazó la petición | Revisa el error en Odoo |

**Facturas pendientes** (sin motivo, `pendiente` desde hace más de una hora): normalmente Odoo no responde y el proceso reintenta durante unos tres días. El cliente no nota nada: el servicio depende del pago, no de la factura. Si sigue pendiente pasado el día 10 del mes siguiente al cobro, emítela a mano en Odoo con la referencia `stripe:<in_…>`, como explica [despliegue.md](despliegue.md#facturas-pendientes). El plazo legal es el día 16.

> **Nota:** el backoffice no cambia el estado de una factura: solo te la enseña con sus enlaces. La corrección se hace en Odoo.

### Buscar una cuenta para un ticket de soporte

Para todos. Ve solo lo necesario para resolver el caso.

1. En **Cuentas**, busca por el dato que tengas: el correo completo del cliente, el NIF, la razón social o un id que te haya pasado.
2. Si hay varios resultados, distínguelos por la fecha de alta y el número de fichas. El correo sale enmascarado.
3. Abre la cuenta y mira el **Resumen**: casi todas las consultas se resuelven con el plan, las plazas, los bloqueos y las bajas.
4. Solo si el caso lo necesita, mira **Sincronización** (no se lee una ficha) o **Fichas y chats** (no le llegan los mensajes).
5. Lee los **nombres de las fichas** solo si hace falta para el caso, con la referencia del ticket. Queda registrado.

Evita abrir pantallas que el caso no necesita: cada lectura de una cuenta queda en la auditoría.

## Métricas

En la pantalla **Métricas**, elige el periodo (días en UTC, de 400 días como mucho). Los importes van sin IVA. Las definiciones exactas y el código que registra cada cifra están en [contrato-backoffice.md](contrato-backoffice.md#get-adminapimetricsfromaaaa-mm-ddtoaaaa-mm-dd).

| Métrica | Qué significa | De dónde sale |
|---|---|---|
| **Ingresos anuales (ARR)** | Lo que suman en un año las plazas activas que no se van a cancelar, al precio de su próxima renovación | Las plazas que copia el proceso de cobro desde Stripe |
| **Ingresos mensuales (MRR)** | El ARR entre 12 | El ARR |
| **Cuentas nuevas** | Cuentas creadas en el periodo (primera conexión con Google) | La base de datos |
| **Primeras plazas** | Plazas contratadas en el periodo que son la primera de su cuenta | Plazas con su primera factura pagada en Stripe |
| **Plazas nuevas** | Plazas contratadas en el periodo | Ídem |
| **Plazas canceladas** | Plazas contratadas que se cancelaron en el periodo | Stripe, a través del proceso de cobro |
| **Se cancelarán al renovar** | Plazas activas que el cliente ha pedido no renovar (hoy) | Ídem |
| **Bajas de cuenta** | Cuentas cuya baja se hizo efectiva en el periodo | Las bajas efectivas |
| **Bloqueos abiertos ahora** | Cuentas bloqueadas por impago en este momento | Los bloqueos |
| **Plazas en vigor** | Plazas activas, por plan y precio de renovación | Ídem que el ARR |
| **Conversión: cuentas de la beta** | De las cuentas creadas antes del lanzamiento, cuántas han pagado algo con dinero | Cobros pagados con importe |
| **Conversión: cuentas sin beta** | De las cuentas creadas en el periodo después del lanzamiento, cuántas han pagado algo con dinero | Ídem |
| **Bloqueos del periodo** | Bloqueos abiertos y cerrados en el periodo | Los bloqueos |

Ten en cuenta:

- Una plaza contratada en la ventana de la beta (30 días desde el lanzamiento) cuenta como plaza nueva, pero su cuenta no «paga» hasta su primer cobro, al acabar la ventana.
- No hay «plazas perdidas por impago»: un bloqueo no caduca y la plaza no se pierde mientras dura.
- Hasta el lanzamiento no se cobra nada y todas las cuentas nuevas son de la beta.

**El embudo** cuenta eventos del periodo:

| Paso | Qué cuenta | Cuándo no cuenta |
|---|---|---|
| **Visitas** | Cada carga de la portada | En el modo «solo web», sin API. Tampoco robots ni más de 10 por dirección y hora |
| **Diagnósticos por nombre** | Diagnósticos con el buscador por nombre | En el modo «solo web». La portada solo enseña el buscador con `?v=buscador` |
| **Diagnósticos por captura** | Pruebas con una captura de las reseñas que se leyeron bien | En el modo «solo web» o sin la clave de Gemini |
| **Lista de espera** | Envíos del formulario de la lista de espera | En el modo «solo web». El mismo contacto dos veces cuenta dos |
| **Conectan Google** | Primeras conexiones con Google (cuentas nuevas) | Volver a entrar no cuenta |
| **Abren el pago** | Cada vez que se abre el pago de una plaza en Stripe | Antes del lanzamiento |
| **Pagan** | Primeras facturas pagadas de cada plaza | Las renovaciones. Cuenta plazas, no cuentas |

Los diagnósticos y la lista de espera son entradas distintas, no pasos seguidos. El porcentaje entre pasos tiene sentido de **Conectan Google** a **Abren el pago** y de **Abren el pago** a **Pagan**.

## Salud: qué mirar cada día

Para soporte y propietario. Abre **Salud** una vez al día, a primera hora. Finanzas mira **Cobro: revisión**.

| Qué mirar | Lo normal | Si sale en rojo |
|---|---|---|
| **Trabajos atascados** | 0 | Un trabajo lleva más de 15 minutos en marcha. Avisa al propietario: puede que el worker se haya parado (`docker logs fichaitor-worker`) |
| **Trabajos fallidos en 24 h** | 0 o muy pocos | Mira la lista de **Trabajos fallidos**: el tipo y el último error dicen qué falló. Un `send_mail` fallido es un correo que no salió; un `sync_profile`, una ficha que no se leyó. Si se repite, avisa al propietario |
| **Piden volver a conectar** | Pocas | El cliente quitó el acceso a Google o caducó. Al contactar con él, pídele que vuelva a conectar desde su panel |
| **Con error** y **Fichas que no se leen bien** | 0 | Abre la cuenta y su pestaña **Sincronización**. Si muchas fichas fallan a la vez, es un problema general: avisa al propietario |
| **Sin leer en 24 h** | 0 o pocas | Si crece, el worker o Google tienen problemas |
| **Retención** | Una pasada por día, la última de hoy o de ayer | Si la última tiene más de un día y medio, la limpieza diaria no corre: avisa al propietario (el worker escribe `retention_overdue`) |
| **Cobro: revisión** (finanzas) | Listas vacías | Facturas en revisión o pendientes: [Revisar facturas en «revisión»](#revisar-facturas-en-revisión). Cobrado sin factura: normalmente se resuelve solo con la conciliación diaria; si dura más de un día, revisa Odoo. Eventos atrasados: avisa al propietario |

## Lo que el backoffice no puede hacer

Por diseño, para proteger a los clientes y cumplir las normas de Google y la protección de datos:

- **No enseña tokens** de Google, de sesión ni de ningún tipo, ni contraseñas.
- **No enseña correos completos.** El de la cuenta sale enmascarado; solo se puede comparar con el remitente de un correo, sin verlo. El correo de facturas y la dirección fiscal los ve finanzas, con la referencia del ticket.
- **No enseña la lista de espera persona a persona**: solo totales anónimos, y solo al propietario.
- **No enseña la auditoría** salvo al propietario.
- **No enseña reseñas, respuestas, borradores, propuestas, conversaciones ni direcciones o teléfonos de los chats.**
- **No busca por el nombre de una ficha.** Los nombres solo se leen dentro de una cuenta, con ticket.
- **No crea, restablece ni da de baja personas**: eso lo hace el propietario en el servidor.
- **No hace bajas inmediatas ni restaura bajas**: la inmediata es un script del servidor y la restauración la hace el cliente con su enlace.
- **No edita facturas**: se corrigen en Odoo.
- **No cobra**: el cobro lo hace Stripe con el proceso de cobro.
