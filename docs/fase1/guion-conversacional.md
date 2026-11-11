# Fase 1.5 — Guion conversacional del MVP

Entregable 1.5 del roadmap. Cada mensaje del agente, sus botones, lo que pasa con cada respuesta y las plantillas de WhatsApp que hay que enviar a aprobación.

Base: estados y flujos de `docs/01-flujos.md`, tono y reglas de `docs/04-ux-conversacional.md`, límites y precios de `docs/fase0/whatsapp.md`. Fecha: 2026-11-11.

## Resumen

- 14 flujos, 82 textos del agente, 45 títulos de botón y 11 plantillas de WhatsApp.
- Todos los títulos de botón caben en 20 caracteres. El más largo tiene 17. Comprobado uno a uno en la [sección 4](#4-comprobación-de-botones).
- Un bar con 10 reseñas y 2 fechas clave al mes consume unos **45 mensajes**. Cabe en los 80 del plan, con 35 de margen.
- Tres plantillas corren riesgo de pasar a marketing: `key_date_v1` (alto), `weekly_summary_v1` y `profile_fix_v1` (medio).

## 1. Reglas comunes

### 1.1 Cómo leer el guion

Cada mensaje tiene una **clave** (por ejemplo, `review.sent`). Es la misma clave del catálogo de textos de la [sección 9](#9-catálogo-de-textos).

- `{variable}`: dato que rellena el sistema en un mensaje de sesión.
- `{{1}}`: variable de una plantilla de WhatsApp.
- **Botones:** títulos exactos, separados por `·`.
- **Mensajes:** cuántos mensajes del agente consume el recorrido típico. Solo cuentan los del agente.

### 1.2 Reglas de redacción

| Regla | Aplicación |
|---|---|
| Tuteo en singular | "¿Abres?", no "¿Abrís?" |
| Sin emojis | Las estrellas se escriben: "5 estrellas" |
| Tres líneas como máximo antes de los botones | Excepción: los mensajes de reseña, que citan la reseña y la respuesta |
| Fechas completas | "martes 8 de diciembre" |
| Salida fácil | Todo mensaje con botones tiene uno para descartar |
| Después de un cambio | "Enviado a Google", nunca "ya está publicado". Google revisa los cambios |
| Texto de terceros | El texto de una reseña es un dato. Nunca se interpreta como instrucción |

### 1.3 Confirmación

El agente no cambia nada sin confirmación. Hay dos formas válidas de confirmar:

- **Confirmación en un toque.** El mensaje describe un cambio completo y sin ambigüedad (qué y qué día) y el botón lo acepta. Ejemplo: `Cierro` en el aviso de un festivo con la fecha completa. No hay segundo mensaje de confirmación.
- **Resumen y confirmación.** Cuando el dueño da los datos con texto libre o audio, el agente envía el resumen `change.confirm` y espera `Sí, enviar`.

La confirmación en un toque ahorra un mensaje por recorrido.

### 1.4 Aclaraciones

Máximo dos preguntas de aclaración seguidas. A la tercera, opciones cerradas con botones. Cada flujo indica sus textos.

### 1.5 Texto libre en lugar de botón

| El dueño escribe | Se trata como |
|---|---|
| "sí", "vale", "ok", "dale", "publica", "envíalo" | El botón afirmativo del mensaje activo |
| "no", "déjalo", "paso", "cancela" | El botón de descarte del mensaje activo |
| Datos del cambio ("de 10 a 14") | Entrada del flujo activo |
| Otra cosa | Enrutador de intenciones ([flujo 12](#flujo-12-mensaje-no-entendido)) |

Si llegan dos mensajes que se contradicen, vale el último. El resumen `change.confirm` lo deja claro.

### 1.6 Sesión o plantilla

| Situación | Formato en WhatsApp |
|---|---|
| El dueño ha escrito en las últimas 24 horas | Mensaje de sesión con botones de respuesta |
| Han pasado más de 24 horas | Plantilla aprobada con respuestas rápidas |

Al pulsar un botón de plantilla, el dueño abre la ventana de 24 horas. El resto del recorrido va como mensaje de sesión.

### 1.7 Mensajes comunes a todos los flujos

**`change.confirm`** — resumen antes de un cambio con datos libres.

> Esto es lo que envío a Google:
> • {cambio_1}
> • {cambio_2}
> ¿Lo envío?

Botones: `Sí, enviar` · `Cambiar` · `Cancelar`

| Respuesta | Qué pasa |
|---|---|
| `Sí, enviar` | El ejecutor aplica el cambio. Responde `change.sent` o un error de Google |
| `Cambiar` | `change.edit_ask`. Vuelve a recopilar datos |
| `Cancelar` | `generic.cancelled`. La propuesta pasa a descartada |

**`change.edit_ask`**

> Dime qué cambio y te lo vuelvo a enseñar.

**`change.sent`**

> Enviado a Google: {cambio}. Suele verse en unos minutos; a veces Google tarda más en revisarlo. {enlace_ficha}

**`generic.cancelled`**

> Vale, no cambio nada.

### 1.8 Errores de Google

Valen para cualquier flujo que termine en un cambio.

| Caso | Qué hace el agente | Mensaje |
|---|---|---|
| Fallo temporal (límite de peticiones, error del servidor) | Reintenta con espera, sin avisar, durante 10 minutos | Ninguno si se resuelve |
| Sigue fallando a los 10 minutos | Avisa una vez y sigue reintentando una hora | `google.retrying` |
| Fallo definitivo tras los reintentos | La propuesta pasa a fallida | `google.failed` |
| Google rechaza el cambio (dato no válido, política) | La propuesta vuelve a recopilar datos | `google.rejected` |
| El acceso ha caducado o se ha revocado | Pausa las propuestas | [Flujo 10](#flujo-10-reconectar-google) |
| La ficha está suspendida o sin verificar | Pausa los cambios | `google.unverified` |
| Google no muestra el cambio en la comprobación posterior | Avisa solo si el cambio tiene fecha cercana | `google.not_applied` |

**`google.retrying`**

> Google no responde ahora mismo. Lo sigo intentando yo durante la próxima hora y te aviso. No tienes que hacer nada.

**`google.failed`**

> No he podido enviar el cambio a Google. No se ha cambiado nada en tu ficha. Puedes hacerlo desde el panel: {enlace_panel}

Botones: `Reintentar` · `Dejarlo`

**`google.rejected`**

> Google no ha aceptado el cambio: {motivo}. Tu ficha sigue como estaba.

Botones: `Cambiar` · `Dejarlo`

**`google.unverified`**

> Tu ficha aparece como {estado} en Google. Hasta que se resuelva no puedo enviar cambios. Aquí tienes los pasos: {enlace_ayuda}

**`google.not_applied`**

> Google todavía no muestra este cambio: {cambio}. A veces lo revisa a mano. Lo sigo vigilando y te aviso si lo rechaza.

| Respuesta | Qué pasa |
|---|---|
| `Reintentar` | El ejecutor lo intenta de nuevo. Si falla otra vez, `google.failed` sin el botón `Reintentar` |
| `Cambiar` | `change.edit_ask` |
| `Dejarlo` | `generic.cancelled` |

### 1.9 Cola y prioridad

Solo hay una propuesta activa por conversación. Las demás esperan. Una reseña negativa se adelanta. Al terminar, el agente retoma la anterior con:

**`queue.resume`**

> Seguimos con lo de antes: {resumen_propuesta}.

Lleva los mismos botones que el mensaje original.

---

## 2. Flujos

### Flujo 1. Bienvenida tras vincular el canal

Disparador: el dueño envía el código de 6 dígitos desde un número sin vincular. La ventana de 24 horas está abierta: no hace falta plantilla.

**`welcome.linked`**

> Listo, {nombre}. Ya llevo la ficha de {negocio} desde aquí.
> Te aviso de las reseñas nuevas y de los festivos, y puedes pedirme cambios cuando quieras.
> No cambio nada sin que me lo confirmes.

Sin botones. A continuación, el agente envía la primera propuesta de mejora ([flujo 7](#flujo-7-propuesta-de-mejora-de-la-ficha)), la de más impacto del diagnóstico.

Si no hay ninguna mejora que proponer:

**`welcome.all_good`**

> Tu ficha está bien montada. No hay nada urgente que cambiar. Te escribo cuando llegue una reseña o se acerque un festivo.

Errores de vinculación:

| Caso | Mensaje |
|---|---|
| Código incorrecto o caducado | `link.code_invalid` |
| El número ya está vinculado a otra cuenta | `link.already_linked` |
| Tercer código incorrecto seguido | `link.blocked`. No se responde más en 1 hora |

**`link.code_invalid`**

> Ese código no me consta o ha caducado. Genera uno nuevo en el panel y envíamelo: {enlace_panel}

**`link.already_linked`**

> Este número ya está vinculado a otra cuenta. Si quieres cambiarlo, desvincúlalo primero en el panel: {enlace_panel}

**`link.blocked`**

> Llevas varios códigos incorrectos. Espera una hora y genera uno nuevo en el panel: {enlace_panel}

Mensajes: **2** (bienvenida y primera propuesta). Si acepta la propuesta, 3.

### Flujo 2. Reseña nueva

Disparador: Google notifica una reseña. Se envía al momento, dentro del horario permitido.

Límites de texto: el extracto de la reseña se corta a 280 caracteres y la respuesta propuesta a 500. El mensaje completo no pasa de 1.024.

#### 2a. Positiva (4 o 5 estrellas, sin queja)

**`review.new.positive`**

> Nueva reseña de {autor}, {estrellas} estrellas: «{extracto}»
> Respuesta que propongo: «{respuesta}»
> ¿La envío?

Botones: `Publicar` · `Editar` · `No responder`

En modo automático no se envía este mensaje: la respuesta sale sola y aparece en el resumen semanal.

#### 2b. Negativa (1 a 3 estrellas, o cualquier reseña con queja)

**`review.new.negative`**

> Reseña de {autor}, {estrellas} estrellas: «{extracto}»
> Conviene contestar con calma y sin entrar al detalle. Propongo: «{respuesta}»
> ¿La envío?

Botones: `Publicar` · `Editar` · `No responder`

Nunca se responde en automático. Se adelanta a cualquier propuesta activa.

#### 2c. Sin texto (solo estrellas)

**`review.new.no_text`**

> Nueva reseña de {autor}, {estrellas} estrellas, sin texto.
> Respuesta que propongo: «{respuesta}»
> ¿La envío?

Botones: `Publicar` · `Editar` · `No responder`

#### Respuestas (comunes a 2a, 2b y 2c)

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Publicar` | El ejecutor envía la respuesta | `review.sent` |
| `Editar` | El agente pide la versión del dueño | `review.edit_ask` |
| `No responder` | La propuesta pasa a descartada | `review.skipped` |
| Texto afirmativo | Igual que `Publicar` | `review.sent` |
| Texto que parece una respuesta al cliente | Primera aclaración | `review.clarify_text` |
| Sin respuesta en 24 horas | Recordatorio ([flujo 8](#flujo-8-recordatorio)) | — |
| Sin respuesta en 7 días | Caduca en silencio ([flujo 9](#flujo-9-propuesta-caducada)) | — |

**`review.sent`**

> Respuesta enviada a Google. Suele verse en unos minutos. {enlace_reseña}

**`review.skipped`**

> Hecho. La dejo sin respuesta.

**`review.edit_ask`**

> Escríbeme o díctame tu respuesta y la envío tal cual.

Después de `review.edit_ask`:

| El dueño envía | Qué pasa | Mensaje |
|---|---|---|
| Texto | Se envía sin más preguntas. La edición se guarda como aprendizaje de voz | `review.sent` |
| Audio | El agente transcribe y pide confirmación | `review.edit_confirm` |
| Texto con un problema (datos personales del cliente, insultos, una oferta) | Aviso y decisión del dueño | `review.edit_warn` |
| Audio que no se entiende | Petición de repetir | `audio.unclear` |

**`review.edit_confirm`**

> He entendido esto: «{texto}»
> ¿Lo envío como respuesta?

Botones: `Sí, enviar` · `Cambiar` · `Cancelar`

**`review.edit_warn`**

> Tu respuesta incluye {problema}. Google puede retirarla. ¿La envío igual o la cambias?

Botones: `Enviar igual` · `Cambiar` · `Cancelar`

**`review.clarify_text`** — primera aclaración.

> ¿Quieres que envíe esto como respuesta a la reseña? «{texto}»

Botones: `Sí, enviar` · `No`

Si responde `No` y lo siguiente tampoco se entiende, segunda aclaración:

**`review.clarify_options`**

> Dime qué hago con la reseña de {autor}.

Botones: `Publicar` · `Editar` · `No responder`

Errores propios de este flujo:

| Caso | Mensaje |
|---|---|
| El autor ha borrado la reseña antes de responder | `review.gone` |
| Google rechaza la respuesta | `google.rejected` |

**`review.gone`**

> Esa reseña ya no está en Google. No hay nada que responder.

#### 2d. Varias reseñas el mismo día

Más de 3 reseñas en un día se envían juntas.

**`review.digest`**

> Hoy han llegado {n} reseñas a {negocio}: {reparto_estrellas}. Tengo una respuesta preparada para cada una.
> ¿Las vemos ahora?

Botones: `Ver ahora` · `Más tarde`

| Respuesta | Qué pasa |
|---|---|
| `Ver ahora` | Envía la primera reseña (2a, 2b o 2c). Al resolverla, envía la siguiente |
| `Más tarde` | `review.digest_later`. Vuelve a ofrecerlas en el recordatorio de las 24 horas |

**`review.digest_later`**

> Vale. Te las recuerdo mañana. También las tienes en el panel: {enlace_panel}

Mensajes por reseña: **2** (propuesta y resultado). Con edición por texto, 3. Con edición por audio, 4. En modo automático, 0.

### Flujo 3. Festivo

Disparador: el planificador, 5 días antes. Recordatorio 1 día antes. Caduca el mismo día.

**`holiday.ask`**

> El {fecha_larga} es festivo ({nombre_festivo}). ¿Abres?
> Si no me dices nada, tu ficha mostrará el horario normal.

Botones: `Abro normal` · `Cierro` · `Otro horario`

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Abro normal` | Confirmación en un toque. Fija para ese día el horario habitual, para que Google no muestre "el horario puede variar" | `holiday.sent_open` |
| `Cierro` | Confirmación en un toque. Marca el día como cerrado | `holiday.sent_closed` |
| `Otro horario` | Pide el horario | `hours.ask` |
| Texto con horario completo ("abro de 10 a 14") | Resumen y confirmación | `change.confirm` |
| Texto ambiguo ("abrimos por la mañana") | Aclaración | `hours.clarify_1` |

**`holiday.sent_open`**

> Enviado a Google: el {fecha_larga} abres con tu horario normal, {horario}. {enlace_ficha}

**`holiday.sent_closed`**

> Enviado a Google: {fecha_larga}, cerrado. Suele verse en unos minutos. {enlace_ficha}

**`hours.ask`**

> ¿Qué horario haces el {fecha_larga}? Por ejemplo: de 10 a 14.

**`hours.clarify_1`** — primera aclaración.

> ¿A qué hora abres y a qué hora cierras ese día? Dímelo con horas, por ejemplo: de 10:00 a 14:00.

**`hours.clarify_2`** — segunda aclaración.

> Sigo sin tenerlo claro. Dime la hora de apertura y la de cierre. Si partes el día: de 10 a 14 y de 17 a 20.

**`hours.options`** — opciones cerradas.

> Elige una opción y lo dejamos resuelto.

Botones: `Horario normal` · `Cerrado` · `Lo pongo en panel`

| Respuesta | Qué pasa |
|---|---|
| `Horario normal` | Como `Abro normal` |
| `Cerrado` | Como `Cierro` |
| `Lo pongo en panel` | `generic.panel_link`. La propuesta sigue abierta en la bandeja del panel |

**`generic.panel_link`**

> Lo tienes aquí: {enlace_panel}

Dos festivos seguidos, o un festivo y una fecha comercial en la misma semana, van en un solo mensaje:

**`holiday.ask_multi`**

> Vienen dos festivos: {fecha_1} y {fecha_2}. ¿Abres?

Botones: `Abro los dos` · `Cierro los dos` · `Depende del día`

`Depende del día` lleva a `hours.ask_multi`:

> Dime qué haces cada día. Por ejemplo: el lunes cierro y el martes abro de 10 a 14.

La respuesta termina en `change.confirm`.

Mensajes: **2** con `Abro normal` o `Cierro`. **4** con `Otro horario` (pregunta, petición de horario, resumen y resultado).

### Flujo 4. Fecha comercial (ejemplo: Black Friday)

Disparador: el planificador, 14 días antes. Recordatorio 7 días antes. Caduca 2 días antes. Solo se propone a los sectores para los que es relevante.

> **Nota:** La publicación de una oferta usa la API de posts, que el README sitúa en la versión Pro. Si los posts no entran en el MVP, la rama `Tengo oferta` se retira y el botón desaparece. La rama de horario funciona en cualquier caso.

**`keydate.ask`**

> Se acerca {evento} ({fecha_larga}). ¿Tienes una oferta o un horario distinto?
> Una oferta publicada con tiempo se ve más días.

Botones: `Tengo oferta` · `Cambio horario` · `Nada este año`

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Tengo oferta` | Pide los datos | `offer.ask` |
| `Cambio horario` | Pide el horario | `hours.ask` |
| `Nada este año` | Descartada. Se anota la preferencia; al segundo año deja de proponerse | `keydate.declined` |
| Texto con los datos ("20 % en todo el finde y el viernes hasta las 22") | Resumen y confirmación | `change.confirm` |

**`offer.ask`**

> Cuéntame la oferta: qué ofreces, cuánto y hasta qué día.

Aclaraciones de la oferta. Se pregunta solo lo que falta, una cosa cada vez:

| Falta | Clave | Texto |
|---|---|---|
| Qué se ofrece | `offer.clarify_what` | ¿Sobre qué es la oferta? Por ejemplo: 20 % en toda la carta. |
| Las fechas | `offer.clarify_dates` | ¿Qué días vale la oferta? Dime el primero y el último. |
| El servicio no consta en la ficha | `offer.clarify_service` | En tu ficha no consta {servicio}. ¿Lo ofreces? Si es así, sigo. |

Botones de `offer.clarify_service`: `Sí, lo ofrezco` · `No, quítalo`

Tras dos aclaraciones sin datos completos:

**`offer.options`**

> Elige cuánto dura la oferta y lo dejamos resuelto.

Botones: `Solo ese día` · `Todo el finde` · `Lo dejo`

`Lo dejo` responde `generic.cancelled`.

El resumen usa `change.confirm` con estas líneas:

> Esto es lo que envío a Google:
> • Publicación de oferta: {texto_oferta}, del {fecha_inicio} al {fecha_fin}.
> • Horario del {fecha_larga}: {horario}.
> ¿Lo envío?

**`keydate.sent`**

> Enviado a Google. La publicación pasa una revisión y suele verse en unos minutos. {enlace_ficha}

**`keydate.declined`**

> Anotado. No te lo vuelvo a proponer este año.

Mensajes: **3** en el recorrido típico (pregunta, resumen y resultado). **1 o 2** si descarta. **5** con dos aclaraciones.

### Flujo 5. "Hoy cierro"

Disparador: el dueño escribe. La ventana está abierta.

**`close_today.confirm`**

> Marco hoy, {fecha_larga}, como cerrado en tu ficha. ¿Confirmas?

Botones: `Sí, cerrar hoy` · `No`

Si el dueño da un motivo personal ("estoy malo"), el mensaje empieza con una frase breve: "Que te mejores." La frase la redacta el modelo y no lleva clave.

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Sí, cerrar hoy` | El ejecutor marca el día como cerrado | `close_today.sent` |
| `No` | Descartada | `generic.cancelled` |

**`close_today.sent`**

> Enviado a Google: hoy, {fecha_larga}, cerrado. Mañana vuelve tu horario normal. {enlace_ficha}

Variantes:

| El dueño escribe | Mensaje | Botones |
|---|---|---|
| "mañana cierro", "el viernes cierro" | `close_day.confirm`: Marco el {fecha_larga} como cerrado en tu ficha. ¿Confirmas? | `Sí, cerrar` · `No` |
| "cierro del 1 al 15 por vacaciones" | `close_range.confirm`: Marco como cerrado del {fecha_inicio} al {fecha_fin}, los dos incluidos. ¿Confirmas? | `Sí, cerrar` · `No` |
| "hoy cierro a las 5" | `change.confirm` con el horario de hoy | `Sí, enviar` · `Cambiar` · `Cancelar` |
| "cambia el teléfono" (sin el dato) | `order.ask_value`: ¿Cuál es el dato nuevo? | — |

El resultado de las variantes usa `change.sent`.

Si "el viernes" puede ser esta semana o la siguiente, el agente elige el más cercano y lo deja a la vista en la fecha completa del mensaje de confirmación. No hace una pregunta aparte.

Mensajes: **2** (confirmación y resultado).

### Flujo 6. Consulta al experto

Disparador: el dueño pregunta. La ventana está abierta.

La respuesta la redacta el modelo con los datos de la ficha. Estructura fija:

1. El dato de su ficha que explica la respuesta.
2. El motivo, en una línea.
3. Si hay un cambio que lo mejora, la propuesta con botones.

**`expert.answer`** (estructura, no texto fijo)

> {dato_de_la_ficha}. {motivo}. {propuesta}

Botones, solo si hay propuesta: `Sí, cambiar` · `Ahora no`

Ejemplo:

> Tu categoría principal es «Restaurante» y la palabra pizzería no aparece ni en la categoría ni en los servicios. Es lo primero que mira Google. ¿Cambio la categoría principal a «Pizzería»?

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Sí, cambiar` | Confirmación en un toque: el mensaje ya describe el cambio completo | `change.sent` |
| `Ahora no` | Descartada | `generic.cancelled` |

Textos fijos:

| Caso | Clave | Texto |
|---|---|---|
| No hay datos para responder | `expert.no_data` | Eso no lo puedo saber con los datos de tu ficha. Lo que sí veo: {dato}. |
| Pregunta ajena a la ficha | `expert.out_of_scope` | De eso no me ocupo. Yo llevo tu ficha de Google: reseñas, horarios y datos del negocio. |
| Pide algo de otro plan (foto, post) | `expert.other_plan` | Publicar fotos y posts desde aquí no entra en tu plan. Los detalles están en el panel: {enlace_panel} |
| Baja, factura, cambio de plan | `account.link` | Eso se gestiona en el panel: {enlace_panel} |

Mensajes: **1**. Con propuesta aceptada, 2.

### Flujo 7. Propuesta de mejora de la ficha

Disparador: semanal, solo si no hay nada más pendiente. Sin recordatorio. Caduca a los 7 días. También es el segundo mensaje de la bienvenida.

**`improve.proposal`** (estructura)

> {hallazgo}. {motivo}. {pregunta}

Un texto por tipo de mejora:

| Mejora | Clave | Texto | Botones |
|---|---|---|---|
| Categoría | `improve.category` | Tu categoría principal es «{actual}» y la gente te busca como «{sugerida}». Es lo primero que mira Google. ¿La cambio a «{sugerida}»? | `Sí, cambiar` · `Ahora no` |
| Sin teléfono | `improve.phone` | Tu ficha no tiene teléfono. Quien quiere llamar no puede. ¿Qué número pongo? | `Ahora no` |
| Sin web | `improve.website` | Tu ficha no enlaza a ninguna web. Pierdes a quien quiere ver la carta o los precios. ¿Qué dirección pongo? | `Ahora no` |
| Sin horario | `improve.hours` | Tu ficha no tiene horario. Sin él, Google no puede decir que estás abierto. ¿Qué horario haces? | `Ahora no` |
| Sin servicios | `improve.attributes` | Tu ficha no dice si tienes {servicios_sugeridos}. Son filtros que la gente usa al buscar. ¿Cuáles tienes? | `Ahora no` |
| Cambio hecho por Google | `improve.google_edit` | Google ha cambiado tu {campo}: antes «{antes}», ahora «{ahora}». ¿Es correcto? | `Sí, es correcto` · `No, deshacer` |

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Sí, cambiar` | Confirmación en un toque | `change.sent` |
| El dueño escribe el dato | Resumen y confirmación | `change.confirm` |
| `Ahora no` | Descartada. No se repite la misma mejora en 90 días | `improve.declined` |
| `Sí, es correcto` | Se acepta el cambio de Google | `improve.google_ok` |
| `No, deshacer` | Confirmación en un toque: vuelve al valor anterior | `change.sent` |

**`improve.declined`**

> Vale. No te lo vuelvo a proponer en una temporada.

**`improve.google_ok`**

> Perfecto, lo dejo así.

Los horarios ambiguos usan las aclaraciones `hours.clarify_1`, `hours.clarify_2` y `hours.options` del flujo 3.

Mensajes: **2** con un toque. **3** si hace falta un dato (propuesta, resumen y resultado).

### Flujo 8. Recordatorio

Disparador: propuesta enviada y sin respuesta en el plazo de la tabla de `04-ux-conversacional.md`. Un solo recordatorio por propuesta.

Dentro de la ventana de 24 horas:

**`reminder.pending`**

> Sigue pendiente: {resumen_propuesta}. Caduca el {fecha_larga}.

Lleva los botones del mensaje original.

Fuera de la ventana, la plantilla `reminder_v1`, con dos botones fijos: `Verla ahora` · `Descartar`.

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Verla ahora` | Reenvía el mensaje original con sus botones | El original |
| `Descartar` | Descartada | `generic.cancelled` |

Excepción: el recordatorio de un festivo reutiliza la plantilla `holiday_v1`. El texto sirve igual el día antes y ahorra un mensaje.

Mensajes: **1**. Con `Verla ahora`, 2.

### Flujo 9. Propuesta caducada

Al caducar, el agente **no envía nada**. La propuesta aparece como caducada en el panel y en el resumen semanal.

Si el dueño responde a una propuesta caducada:

| Caso | Qué pasa | Mensaje |
|---|---|---|
| La fecha sigue vigente | Se reabre y sigue el flujo donde estaba | El del flujo |
| La fecha ya pasó | Se explica | `expired.past` |
| Es una reseña de más de 7 días | Se reabre: una reseña se puede responder siempre | El del flujo 2 |

**`expired.past`**

> Esa propuesta era para el {fecha_larga} y ya ha pasado. No he cambiado nada en tu ficha.

Mensajes: **0**. Si responde tarde, 1.

### Flujo 10. Reconectar Google

Disparador: el acceso a Google caduca o se revoca. Se pausan todas las propuestas.

**`reconnect.ask`**

> He perdido el acceso a tu ficha de Google. Hasta que lo renueves no puedo avisarte de reseñas ni enviar cambios.
> Tarda un minuto: {enlace_reconectar}

En la plantilla, el enlace va en un botón de URL: `Reconectar Google`.

| Qué pasa después | Mensaje |
|---|---|
| El dueño reconecta | `reconnect.done` |
| Sin respuesta en 72 horas | Un segundo aviso, igual que el primero |
| Sin respuesta tras el segundo aviso | No más mensajes por WhatsApp. Aviso por correo y en el panel |
| El dueño pide un cambio con el acceso caducado | `reconnect.needed` |

**`reconnect.done`**

> Conectado de nuevo. Retomo lo que estaba pendiente.

**`reconnect.needed`**

> Antes tengo que recuperar el acceso a tu ficha. Tarda un minuto: {enlace_reconectar}

Mensajes: **2** (aviso y confirmación).

### Flujo 11. Resumen semanal

Disparador: una vez por semana, el día y la hora que elija el dueño. Por defecto, el lunes a las 10:00. **No se envía si no ha pasado nada** en la semana.

**`summary.weekly`**

> Tu semana en {negocio}: {n_reseñas} reseñas nuevas, {n_respondidas} respondidas y {n_pendientes} pendientes. Cambios enviados a Google: {n_cambios}.
> {linea_mensajes}

Botones: `Ver pendientes` · `No enviar más`

`{linea_mensajes}` solo aparece cuando el dueño ha gastado 60 mensajes o más del mes: "Llevas {usados} de 80 mensajes de WhatsApp este mes." Avisa sin gastar un mensaje aparte.

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Ver pendientes` | Envía la propuesta pendiente más antigua. Si no hay, `summary.nothing_pending` | El de su flujo |
| `No enviar más` | Desactiva el resumen | `summary.off` |
| Sin respuesta | Nada. El resumen no tiene recordatorio | — |

**`summary.nothing_pending`**

> No tienes nada pendiente. Todo al día.

**`summary.off`**

> Hecho. No te envío más resúmenes. Puedes volver a activarlos en Ajustes: {enlace_panel}

Mensajes: **1** por semana.

### Flujo 12. Mensaje no entendido

Disparador: el enrutador no reconoce la intención.

**`fallback.first`** — primera aclaración.

> No te he entendido. ¿Me lo dices de otra forma? Puedo responder reseñas, cambiar horarios y resolver dudas de tu ficha.

**`fallback.options`** — a la segunda, opciones cerradas.

> Elige por dónde seguimos.

Botones: `Reseñas` · `Horario` · `Otra cosa`

| Respuesta | Qué pasa | Mensaje |
|---|---|---|
| `Reseñas` | Envía la reseña pendiente más antigua. Si no hay, `fallback.no_reviews` | — |
| `Horario` | `fallback.hours` | — |
| `Otra cosa` | `fallback.other` | — |

**`fallback.no_reviews`**

> No tienes reseñas pendientes de respuesta.

**`fallback.hours`**

> Dime qué día y qué horario. Por ejemplo: el sábado cierro, o mañana abro de 10 a 14.

**`fallback.other`**

> Escríbeme qué necesitas con una frase. Si prefieres hacerlo tú, el panel está aquí: {enlace_panel}

Otros casos:

| Caso | Clave | Texto |
|---|---|---|
| Audio que no se entiende | `audio.unclear` | No he entendido el audio. ¿Me lo repites o me lo escribes? |
| Insultos o mensaje sin relación | `generic.neutral` | Yo me ocupo de tu ficha de Google. Si necesitas algo de eso, dímelo. |
| Saludo o agradecimiento | Sin respuesta | No gasta mensaje |

Un mensaje con insultos no crea ninguna acción. Si se repite, el agente no vuelve a responder en esa ventana.

Mensajes: **1**. Con opciones, 2 o 3.

### Flujo 13. Número no vinculado

Disparador: mensaje desde un número que no está vinculado y que no contiene un código válido.

**`unlinked.reply`**

> Hola. Soy Fichaitor: llevo la ficha de Google de tu negocio desde WhatsApp.
> Este número no está vinculado a ninguna cuenta. Si ya tienes una, envíame el código de 6 dígitos del panel. Si no, empieza aquí: {enlace_alta}

Reglas:

- Una sola respuesta por número cada 24 horas. Los mensajes siguientes no se contestan.
- No se da ningún dato de ninguna ficha ni se confirma si un negocio es cliente.
- No cuenta para el plan de ningún cliente. El coste lo asume Fichaitor.

Mensajes: **1**.

### Flujo 14. Mensajes agotados del plan de WhatsApp

Disparador: el agente va a enviar el mensaje número 80 del mes. Ese mensaje es el aviso.

**`quota.reached`**

> Este mes ya he usado los 80 mensajes de WhatsApp de tu plan. Hasta el día 1 te aviso por {canal_alternativo}.
> No se pierde nada: las reseñas y los avisos siguen en tu panel. {enlace_panel}

Sin botones: una respuesta obligaría a contestar y ya no quedan mensajes.

Reglas:

- El mensaje no ofrece subir de plan. Una oferta lo convertiría en marketing.
- Desde ese momento, lo proactivo sale por el canal alternativo (correo, Telegram o panel).
- Si hay un cambio a medias (el dueño ya confirmó), el resultado se envía igual aunque supere los 80. Un cambio no se queda sin respuesta.
- El día 1 el contador vuelve a cero. No se envía ningún mensaje para avisarlo.

Si el dueño escribe con los mensajes agotados:

**`quota.reply`**

> Este mes ya no puedo contestarte por aquí. Hazlo desde el panel y sale igual: {enlace_panel}

Como máximo una vez cada 24 horas.

> **Importante:** Qué pasa cuando el dueño escribe con el plan agotado es una decisión abierta. Propuesta: `quota.reply` una vez al día, y "hoy cierro" se atiende siempre, porque es urgente y son 2 mensajes. Coste máximo de la excepción: unos céntimos al mes por negocio.

Mensajes: **1**.

---

## 3. Plantillas de WhatsApp para aprobación

Hacen falta 11 plantillas. `04-ux-conversacional.md` preveía 5; las otras 6 salen de separar casos que no pueden compartir texto fijo.

### 3.1 Criterios de Meta

Fuente: [Meta, categorización de plantillas](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-categorization), consultada el 2026-11-11.

Una plantilla es de **utilidad** si cumple dos condiciones:

1. "Must be non-promotional, not containing any promotional or persuasive intent."
2. Es específica del usuario o pedida por él ("clearly related to their order, account, services, or transactions"), o es esencial o crítica.

Caso de uso que aplica a Fichaitor: **avisos o actualizaciones de cuenta**, "important or time-sensitive updates or alerts or other information specific to purchased or subscribed products/services". Estos mensajes no pueden "promote, recommend, upsell, or cross-sell".

Es **marketing**:

- Cualquier contenido mixto, por ejemplo un aviso con una promoción.
- El contenido poco claro, por ejemplo una plantilla que solo contiene `{{1}}`.
- Los mensajes para "crear relación con el cliente", de notoriedad o de venta.
- Una encuesta o petición de opinión genérica.

Cómo decide Meta:

- Al crear la plantilla, valida la categoría solicitada. Si no está de acuerdo, la rechaza con el motivo `INCORRECT_CATEGORY`.
- Después, revisa de forma periódica las plantillas aprobadas. Si una de utilidad debería ser de marketing, la cambia con un día de aviso. Tras una advertencia por mal uso, el cambio es inmediato.
- Hay 60 días para recurrir.
- La reincidencia puede limitar el volumen de utilidad de la cuenta y pasar todas las plantillas de utilidad a marketing.

Coste en España: utilidad, 0,0166 €; marketing, 0,0585 € (`docs/fase0/whatsapp.md`). Una plantilla reclasificada cuesta 3,5 veces más y descuenta 3 o 4 mensajes del plan.

### 3.2 Reglas de redacción para que sean utilidad

1. **Nombra siempre la ficha del dueño.** Cada plantilla lleva el nombre del negocio: deja claro que es un aviso de un servicio contratado.
2. **Habla de un hecho, no de una oportunidad.** "Ha llegado una reseña", "el martes es festivo", "se ha perdido el acceso".
3. **Fuera palabras de venta en el texto fijo:** oferta, descuento, promoción, aprovecha, gratis, no te lo pierdas, mejora tu plan.
4. **Sin consejos de negocio.** "Publicar con tiempo se ve más" es una recomendación: va en el mensaje de sesión, no en la plantilla.
5. **Variables con contenido concreto.** Ninguna plantilla es solo variables, y ninguna empieza ni termina con una variable.
6. **Las variables no llevan texto de venta.** En `key_date_v1`, la variable del evento puede decir "Black Friday": es el punto débil de esa plantilla.

### 3.3 Datos comunes

| Campo | Valor |
|---|---|
| Idioma | Español de España (`es_ES`) |
| Cuerpo | Hasta 1.024 caracteres, con las variables rellenas |
| Pie | "Fichaitor" (9 caracteres; el máximo es 60) |
| Botones | Respuesta rápida. La plantilla admite títulos de 25 caracteres; se limitan a 20 para compartirlos con los mensajes de sesión |
| Texto de terceros en variables | Se corta: 280 caracteres el extracto de la reseña, 500 la respuesta. Se eliminan los saltos de línea |

### 3.4 Las plantillas

Longitud del cuerpo: caracteres del texto fijo, sin las variables.

#### `new_review_v1`

| Campo | Valor |
|---|---|
| Uso | Reseña nueva con texto, positiva o negativa (flujos 2a y 2b) |
| Categoría solicitada | Utilidad |
| Por qué | Aviso de un hecho concreto en la ficha del dueño, dentro del servicio contratado. Sin contenido de venta |
| Riesgo de marketing | Bajo |
| Botones | `Publicar` · `Editar` · `No responder` |

Cuerpo:

```text
Nueva reseña en la ficha de {{1}}. {{2}} ha puesto {{3}} estrellas y ha escrito: «{{4}}». Respuesta preparada: «{{5}}». ¿La envío a Google?
```

Ejemplo:

```text
Nueva reseña en la ficha de Bar La Esquina. Marta ha puesto 5 estrellas y ha escrito: «El arroz, espectacular. Volveremos.». Respuesta preparada: «Gracias, Marta. Nos alegra que disfrutaras el arroz. Te esperamos pronto.». ¿La envío a Google?
```

#### `new_review_no_text_v1`

| Campo | Valor |
|---|---|
| Uso | Reseña nueva sin texto (flujo 2c) |
| Categoría solicitada | Utilidad |
| Por qué | El mismo que `new_review_v1` |
| Riesgo de marketing | Bajo |
| Botones | `Publicar` · `Editar` · `No responder` |

Cuerpo:

```text
Nueva reseña en la ficha de {{1}}. {{2}} ha puesto {{3}} estrellas, sin texto. Respuesta preparada: «{{4}}». ¿La envío a Google?
```

Ejemplo:

```text
Nueva reseña en la ficha de Bar La Esquina. Luis ha puesto 4 estrellas, sin texto. Respuesta preparada: «Gracias por tu valoración, Luis. Te esperamos pronto.». ¿La envío a Google?
```

#### `review_digest_v1`

| Campo | Valor |
|---|---|
| Uso | Más de 3 reseñas en un día (flujo 2d) |
| Categoría solicitada | Utilidad |
| Por qué | Aviso agrupado de hechos de la ficha |
| Riesgo de marketing | Bajo |
| Botones | `Ver ahora` · `Más tarde` |

Cuerpo:

```text
Hoy han llegado {{1}} reseñas nuevas a la ficha de {{2}}: {{3}}. Hay una respuesta preparada para cada una. ¿Las revisas ahora?
```

Ejemplo:

```text
Hoy han llegado 4 reseñas nuevas a la ficha de Bar La Esquina: tres de 5 estrellas y una de 2. Hay una respuesta preparada para cada una. ¿Las revisas ahora?
```

#### `holiday_v1`

| Campo | Valor |
|---|---|
| Uso | Festivo y su recordatorio (flujo 3) |
| Categoría solicitada | Utilidad |
| Por qué | Aviso con fecha límite sobre un dato de la ficha: el horario que verá el público ese día |
| Riesgo de marketing | Bajo |
| Botones | `Abro normal` · `Cierro` · `Otro horario` |

Cuerpo:

```text
El {{1}} es festivo ({{2}}). La ficha de {{3}} mostrará tu horario normal si no se cambia. ¿Abres ese día?
```

Ejemplo:

```text
El martes 8 de diciembre es festivo (Inmaculada Concepción). La ficha de Bar La Esquina mostrará tu horario normal si no se cambia. ¿Abres ese día?
```

#### `key_date_v1`

| Campo | Valor |
|---|---|
| Uso | Fecha comercial (flujo 4) |
| Categoría solicitada | Utilidad |
| Por qué | Pregunta por el horario y los datos de la ficha en una fecha concreta |
| Riesgo de marketing | **Alto** |
| Botones | `Cambio horario` · `Tengo novedades` · `Nada este año` |

Cuerpo:

```text
Se acerca el {{1}} ({{2}}). ¿Cambia ese día el horario de {{3}} o hay algo que deba constar en la ficha?
```

Ejemplo:

```text
Se acerca el viernes 27 de noviembre (Black Friday). ¿Cambia ese día el horario de Bar La Esquina o hay algo que deba constar en la ficha?
```

Por qué el riesgo es alto: el evento es comercial y el flujo termina en una oferta. El texto fijo evita "oferta" y el botón dice `Tengo novedades` en lugar de `Tengo oferta`, pero la variable "Black Friday" lo delata.

Cómo reducirlo:

- Solicítala como utilidad con este texto. Si Meta la rechaza o la reclasifica, no insistas: la reincidencia pone en riesgo las demás plantillas.
- Envía la pregunta **dentro de la ventana de 24 horas** siempre que se pueda, como mensaje de sesión (`keydate.ask`): justo después de que el dueño resuelva una reseña. El mensaje de sesión cuesta 0,0166 € y no pasa por categorías.
- Si no hay ventana abierta, inclúyela como una línea del resumen semanal en lugar de enviar una plantilla aparte.
- Plan B: aceptar marketing para esta plantilla. Con una fecha comercial al mes son 12 envíos al año por negocio; el sobrecoste es de 12 × (0,0585 − 0,0166) = 0,50 € al año.

#### `profile_fix_v1`

| Campo | Valor |
|---|---|
| Uso | Propuesta de mejora de la ficha (flujo 7) |
| Categoría solicitada | Utilidad |
| Por qué | Aviso sobre el estado de un dato de la ficha |
| Riesgo de marketing | **Medio**: una mejora puede leerse como recomendación |
| Botones | `Verlo ahora` · `Ahora no` |

Cuerpo:

```text
Aviso sobre la ficha de {{1}}: {{2}}. Tengo el cambio preparado. ¿Lo revisas?
```

Ejemplo:

```text
Aviso sobre la ficha de Bar La Esquina: no tiene teléfono publicado. Tengo el cambio preparado. ¿Lo revisas?
```

Cómo reducir el riesgo: la variable describe el dato que falta, sin beneficios ("no tiene teléfono publicado", no "consigue más llamadas"). El motivo y la pregunta van en el mensaje de sesión que sigue a `Verlo ahora`.

#### `reminder_v1`

| Campo | Valor |
|---|---|
| Uso | Recordatorio de una propuesta pendiente (flujo 8) |
| Categoría solicitada | Utilidad |
| Por qué | Seguimiento de un aviso concreto, con fecha límite |
| Riesgo de marketing | Bajo. Sube si la variable describe una fecha comercial |
| Botones | `Verla ahora` · `Descartar` |

Cuerpo:

```text
Sigue pendiente de tu respuesta en la ficha de {{1}}: {{2}}. Caduca el {{3}}.
```

Ejemplo:

```text
Sigue pendiente de tu respuesta en la ficha de Bar La Esquina: la reseña de Marta, de 5 estrellas. Caduca el lunes 16 de noviembre.
```

#### `reconnect_google_v1`

| Campo | Valor |
|---|---|
| Uso | El acceso a Google ha caducado (flujo 10) |
| Categoría solicitada | Utilidad |
| Por qué | Aviso de interrupción del servicio contratado |
| Riesgo de marketing | Bajo |
| Botones | Botón de URL: `Reconectar Google`, con el enlace firmado como variable |

Cuerpo:

```text
Se ha perdido el acceso a la ficha de Google de {{1}}. Hasta que se renueve, no llegan los avisos de reseñas ni se pueden enviar cambios. Renovarlo tarda un minuto.
```

Ejemplo:

```text
Se ha perdido el acceso a la ficha de Google de Bar La Esquina. Hasta que se renueve, no llegan los avisos de reseñas ni se pueden enviar cambios. Renovarlo tarda un minuto.
```

#### `weekly_summary_v1`

| Campo | Valor |
|---|---|
| Uso | Resumen semanal (flujo 11) |
| Categoría solicitada | Utilidad |
| Por qué | Informe de actividad de la cuenta, con cifras propias del dueño |
| Riesgo de marketing | **Medio**: un mensaje periódico puede leerse como "crear relación con el cliente" |
| Botones | `Ver pendientes` · `No enviar más` |

Cuerpo:

```text
Resumen de la semana en la ficha de {{1}}: {{2}} reseñas nuevas, {{3}} respondidas y {{4}} pendientes de tu respuesta. Cambios enviados a Google: {{5}}.
```

Ejemplo:

```text
Resumen de la semana en la ficha de Bar La Esquina: 3 reseñas nuevas, 2 respondidas y 1 pendientes de tu respuesta. Cambios enviados a Google: 1.
```

Cómo reducir el riesgo: solo cifras, sin valoraciones ("buena semana") ni consejos. No se envía si todas las cifras son cero. La línea del contador de mensajes no cabe en esta plantilla: solo aparece en la versión de sesión.

#### `quota_reached_v1`

| Campo | Valor |
|---|---|
| Uso | Mensajes del mes agotados (flujo 14) |
| Categoría solicitada | Utilidad |
| Por qué | Aviso sobre el estado del servicio contratado |
| Riesgo de marketing | Bajo, mientras no ofrezca subir de plan |
| Botones | Ninguno |

Cuerpo:

```text
Este mes ya se han usado los {{1}} mensajes de WhatsApp del plan de {{2}}. Hasta el día 1, los avisos llegan por {{3}}. Las reseñas y los avisos siguen disponibles en el panel.
```

Ejemplo:

```text
Este mes ya se han usado los 80 mensajes de WhatsApp del plan de Bar La Esquina. Hasta el día 1, los avisos llegan por correo. Las reseñas y los avisos siguen disponibles en el panel.
```

#### `change_result_v1`

| Campo | Valor |
|---|---|
| Uso | Resultado de un cambio cuando la ventana ya se ha cerrado: fallo tras los reintentos o cambio que Google no muestra (`google.failed`, `google.not_applied`) |
| Categoría solicitada | Utilidad |
| Por qué | Resultado de una acción que pidió el dueño |
| Riesgo de marketing | Bajo |
| Botones | `Reintentar` · `Dejarlo` |

Cuerpo:

```text
Sobre el cambio que confirmaste en la ficha de {{1}} ({{2}}): {{3}}. La ficha sigue como estaba.
```

Ejemplo:

```text
Sobre el cambio que confirmaste en la ficha de Bar La Esquina (cerrado el martes 8 de diciembre): Google no lo ha aceptado tras varios intentos. La ficha sigue como estaba.
```

### 3.5 Resumen de riesgo

| Plantilla | Riesgo | Si pasa a marketing |
|---|---|---|
| `key_date_v1` | Alto | Se envía en ventana abierta o dentro del resumen. Plan B: se acepta |
| `weekly_summary_v1` | Medio | 4 envíos al mes a 0,0585 €: 0,17 € más al mes por negocio. Se pasa a quincenal o al correo |
| `profile_fix_v1` | Medio | Se deja de enviar como plantilla: la mejora solo se propone con la ventana abierta o en el panel |
| Las otras 8 | Bajo | — |

### 3.6 Antes de enviarlas

1. El alta con código cuenta como consentimiento del dueño para recibir mensajes. Guarda la fecha y el número.
2. Envía primero las 8 de riesgo bajo. Las 3 de riesgo medio y alto, después y de una en una, para no acumular rechazos.
3. Rellena los ejemplos de las variables con los de esta sección. Meta los exige al crear la plantilla.
4. Anota la categoría que asigna Meta a cada una en `docs/fase0/whatsapp.md`.

---

## 4. Comprobación de botones

Límites: 3 botones por mensaje, 20 caracteres por título y títulos distintos dentro del mismo mensaje.

| Título | Caracteres | Cabe |
|---|---|---|
| `Lo pongo en panel` | 17 | Sí |
| `Reconectar Google` | 17 | Sí |
| `Depende del día` | 15 | Sí |
| `Tengo novedades` | 15 | Sí |
| `Cambio horario` | 14 | Sí |
| `Sí, cerrar hoy` | 14 | Sí |
| `Sí, lo ofrezco` | 14 | Sí |
| `Sí, es correcto` | 15 | Sí |
| `Horario normal` | 14 | Sí |
| `Cierro los dos` | 14 | Sí |
| `Ver pendientes` | 14 | Sí |
| `Nada este año` | 13 | Sí |
| `No enviar más` | 13 | Sí |
| `Todo el finde` | 13 | Sí |
| `No, quítalo` | 11 | Sí |
| `No, deshacer` | 12 | Sí |
| `No responder` | 12 | Sí |
| `Otro horario` | 12 | Sí |
| `Tengo oferta` | 12 | Sí |
| `Abro los dos` | 12 | Sí |
| `Enviar igual` | 12 | Sí |
| `Solo ese día` | 12 | Sí |
| `Abro normal` | 11 | Sí |
| `Sí, cambiar` | 11 | Sí |
| `Verla ahora` | 11 | Sí |
| `Verlo ahora` | 11 | Sí |
| `Sí, cerrar` | 10 | Sí |
| `Sí, enviar` | 10 | Sí |
| `Reintentar` | 10 | Sí |
| `Otra cosa` | 9 | Sí |
| `Ver ahora` | 9 | Sí |
| `Más tarde` | 9 | Sí |
| `Descartar` | 9 | Sí |
| `Ahora no` | 8 | Sí |
| `Cancelar` | 8 | Sí |
| `Publicar` | 8 | Sí |
| `Lo dejo` | 7 | Sí |
| `Dejarlo` | 7 | Sí |
| `Cambiar` | 7 | Sí |
| `Reseñas` | 7 | Sí |
| `Horario` | 7 | Sí |
| `Cerrado` | 7 | Sí |
| `Cierro` | 6 | Sí |
| `Editar` | 6 | Sí |
| `No` | 2 | Sí |

Resultado: **ningún título supera los 20 caracteres**, ningún mensaje tiene más de 3 botones y no hay títulos repetidos dentro de un mismo mensaje.

Otros límites:

| Límite | Estado |
|---|---|
| Cuerpo de 1.024 caracteres | Todos los textos fijos tienen menos de 300. Los mensajes de reseña llegan como máximo a unos 950 con el extracto (280) y la respuesta (500) al límite |
| Pie de 60 caracteres | Los mensajes de sesión no llevan pie. Las plantillas llevan "Fichaitor" |

---

## 5. Consumo de mensajes

### 5.1 Por flujo

| Flujo | Recorrido típico | Mensajes del agente | Peor caso |
|---|---|---|---|
| 1. Bienvenida | Bienvenida y primera propuesta | 2 | 4 |
| 2. Reseña | Propuesta y resultado | 2 | 4 (edición por audio) |
| 2d. Varias reseñas | Aviso agrupado y 2 por reseña | 1 + 2 por reseña | — |
| 3. Festivo | Pregunta y resultado | 2 | 6 (dos aclaraciones) |
| 4. Fecha comercial | Pregunta, resumen y resultado | 3 | 5 |
| 5. "Hoy cierro" | Confirmación y resultado | 2 | 3 |
| 6. Consulta al experto | Respuesta | 1 | 2 |
| 7. Mejora de la ficha | Propuesta y resultado | 2 | 3 |
| 8. Recordatorio | Aviso | 1 | 2 |
| 9. Caducada | Nada | 0 | 1 |
| 10. Reconectar Google | Aviso y confirmación | 2 | 3 |
| 11. Resumen semanal | Resumen | 1 | 2 |
| 12. No entendido | Aclaración | 1 | 3 |
| 13. Número no vinculado | Respuesta | 1 (no cuenta en el plan) | 1 |
| 14. Mensajes agotados | Aviso | 1 | 1 |

### 5.2 Un bar con 10 reseñas y 2 fechas clave al mes

Supuestos, sin datos de la beta: modo proponer (no automático); 2 de las 10 reseñas se editan por texto; 3 necesitan recordatorio; las 2 fechas clave son un festivo y una fecha comercial.

| Concepto | Cálculo | Mensajes |
|---|---|---|
| Reseñas | 10 × 2 | 20 |
| Ediciones de reseña | 2 × 1 | 2 |
| Recordatorios de reseña | 3 × 1 | 3 |
| Festivo | 1 × 2 | 2 |
| Fecha comercial | 1 × 3 | 3 |
| Recordatorio de fecha clave | 1 × 1 | 1 |
| Resumen semanal | 4 × 1 | 4 |
| Mejoras de la ficha | 2 × 2 | 4 |
| "Hoy cierro" | 1 × 2 | 2 |
| Consultas al experto | 2 × 1, una con cambio | 3 |
| Mensaje no entendido | 1 × 1 | 1 |
| **Total** | | **45** |

**Cabe en el plan de 80**, con 35 mensajes de margen.

Coste para Fichaitor: 45 × 0,0166 € = **0,75 € al mes**, 8,96 € al año, si todo se factura como utilidad o servicio. Está por debajo de los 16 € al año que reserva `docs/fase0/precios.md`.

Escenarios:

| Escenario | Mensajes | ¿Cabe en 80? |
|---|---|---|
| Típico | 45 | Sí |
| Modo automático para las reseñas positivas (8 de 10 salen solas) | 45 − 16 = 29 | Sí |
| La plantilla de fecha comercial pasa a marketing (descuenta 4 en lugar de 1) | 48 | Sí |
| Todas las reseñas se editan por audio y todas necesitan recordatorio | 45 + 18 + 7 = 70 | Sí, justo |
| 20 reseñas al mes, resto igual | 45 + 25 = 70 | Sí, justo |
| 30 reseñas al mes, resto igual | 95 | **No.** Se agota hacia el día 25 |

Conclusión: el plan de 80 aguanta hasta unas 20 reseñas al mes en modo proponer. Por encima, el modo automático para las positivas es lo que lo hace caber. La beta mide el dato real.

### 5.3 Dónde se ahorra

| Decisión del guion | Ahorro al mes en el bar del ejemplo |
|---|---|
| Confirmación en un toque (festivo, 2 mejoras, experto) | 4 mensajes |
| Edición por texto sin segunda confirmación | 2 mensajes |
| Caducar en silencio | 1 o 2 mensajes |
| No enviar el resumen si no ha pasado nada | Hasta 4 mensajes |
| Aviso del contador dentro del resumen, no aparte | 1 mensaje |
| No contestar a saludos y agradecimientos | Variable |

---

## 6. Equivalentes en Telegram

El texto es el mismo. Cambia el formato.

| Elemento | WhatsApp | Telegram |
|---|---|---|
| Botones | Hasta 3 botones de respuesta | Botones en línea, uno por fila. Mismos títulos |
| Más de 3 opciones | Lista | Botones en línea en varias filas |
| Fuera de las 24 horas | Plantilla | Mensaje normal. No hay ventana ni plantillas |
| Tras pulsar un botón | Mensaje nuevo | El agente **edita el mensaje original**: quita los botones y añade el resultado. La conversación queda más limpia |
| Enlaces | En el texto | Botón de URL en línea (`Ver ficha`, `Abrir panel`, `Reconectar Google`) |
| Vinculación | Código de 6 dígitos al número | Enlace `t.me/<bot>?start=<código>` |
| Límite de mensajes | 80 al mes | Sin límite ni coste por mensaje |
| Flujo 14 | Aplica | No aplica |
| Número no vinculado | `unlinked.reply` | La misma respuesta al comando `/start` sin código |

Diferencias de comportamiento:

- En Telegram no hace falta ahorrar mensajes. Las confirmaciones en un toque se mantienen por coherencia entre canales.
- Las preguntas de aclaración admiten más botones: `hours.options` puede ofrecer cuatro o cinco horarios habituales.
- El resumen semanal puede incluir la línea de la fecha comercial y las mejoras pendientes sin riesgo de categoría.

Los límites técnicos de Telegram (datos por botón, longitud del mensaje) están sin verificar en esta fase.

## 7. Equivalentes en correo

Cada mensaje proactivo es un correo. Los botones son **enlaces firmados** de un solo uso que abren una página de confirmación del panel. La página repite el cambio y pide un clic: un enlace abierto por un filtro antispam no cambia nada.

Remitente: `notificaciones@` (solo envío). Las respuestas van a `info@` con `Reply-To`.

| Flujo | Asunto | Enlaces |
|---|---|---|
| 2. Reseña positiva o sin texto | Nueva reseña de {autor} ({estrellas} estrellas) en {negocio} | Publicar la respuesta · Editarla · No responder |
| 2. Reseña negativa | Reseña de {estrellas} estrellas en {negocio}: conviene responder | Publicar la respuesta · Editarla · No responder |
| 2d. Varias reseñas | {n} reseñas nuevas en {negocio} | Verlas en el panel |
| 3. Festivo | El {fecha_larga} es festivo: ¿abre {negocio}? | Abro normal · Cierro · Otro horario |
| 4. Fecha comercial | Se acerca {evento}: ¿algún cambio en {negocio}? | Tengo oferta · Cambio horario · Nada este año |
| 7. Mejora | Un dato por completar en la ficha de {negocio} | Verlo y decidir · Ahora no |
| 8. Recordatorio | Pendiente de tu respuesta: {resumen_propuesta} | Los del correo original |
| 10. Reconectar | Hay que renovar el acceso a la ficha de {negocio} | Reconectar Google |
| 11. Resumen semanal | Tu semana en {negocio} | Ver pendientes · No enviar más |
| 14. Mensajes agotados | Este mes los avisos de {negocio} te llegan por correo | Abrir el panel |
| Resultado de un cambio | Enviado a Google: {cambio} | Ver la ficha |
| Error de Google | No se ha podido enviar el cambio a Google | Reintentar · Abrir el panel |

Diferencias de comportamiento:

- **El correo no conversa.** `Editar`, `Otro horario` y `Tengo oferta` abren un formulario del panel. No hay aclaraciones por correo.
- El cuerpo del correo lleva el mismo texto que el mensaje, con la reseña completa en lugar del extracto.
- Los flujos que inicia el dueño (5, 6 y 12) no existen por correo. Si el dueño responde a un correo, el mensaje llega a `info@` y lo atiende una persona.
- El flujo 13 no aplica.
- El resumen semanal por correo incluye la lista de pendientes, no solo las cifras.

## 8. Equivalentes en el panel web

| Elemento del guion | En el panel |
|---|---|
| Propuesta con botones | Tarjeta en **Bandeja** con el mismo texto y los mismos botones |
| Más de 3 opciones | Selector |
| Entrada libre (horario, oferta, respuesta) | Formulario con campos de fecha y hora. Sin aclaraciones: el formulario valida |
| `change.confirm` | Vista previa en tarjeta con `Sí, enviar` y `Cancelar` |
| `change.sent` | Aviso en pantalla y estado "Enviado a Google" en la tarjeta, que pasa a "Visible en Google" tras la comprobación |
| Errores de Google | Estado de error en la tarjeta, con el motivo y `Reintentar` |
| Recordatorio | Contador de pendientes en **Inicio** y notificación de la aplicación instalable |
| Propuesta caducada | La tarjeta pasa a "Caducada" y se puede reabrir si la fecha sigue vigente |
| Reconectar Google | Estado "sin conexión con Google" en todas las pantallas, con el botón `Reconectar Google` |
| Resumen semanal | Pantalla **Inicio** |
| Consulta al experto | No hay chat en el panel del MVP. Enlace para abrir WhatsApp o Telegram |
| Mensaje no entendido, número no vinculado | No aplican |
| Mensajes agotados | Aviso en **Inicio**: "Este mes has usado los 80 mensajes de WhatsApp. Los avisos te llegan por {canal}." |

El panel usa los mismos textos del catálogo. Los botones pueden superar los 20 caracteres, pero se mantienen los mismos títulos para que el dueño los reconozca.

---

## 9. Catálogo de textos

Una clave por texto. El texto en español es el de las secciones 1 a 3. Las plantillas de WhatsApp se traducen aparte: cada idioma es una plantilla nueva que Meta aprueba por separado.

### 9.1 Mensajes

| Clave | Variables | Flujo |
|---|---|---|
| `change.confirm` | `cambio_1…n` | Común |
| `change.edit_ask` | — | Común |
| `change.sent` | `cambio`, `enlace_ficha` | Común |
| `generic.cancelled` | — | Común |
| `generic.panel_link` | `enlace_panel` | Común |
| `generic.neutral` | — | 12 |
| `queue.resume` | `resumen_propuesta` | Común |
| `google.retrying` | — | Errores |
| `google.failed` | `enlace_panel` | Errores |
| `google.rejected` | `motivo` | Errores |
| `google.unverified` | `estado`, `enlace_ayuda` | Errores |
| `google.not_applied` | `cambio` | Errores |
| `welcome.linked` | `nombre`, `negocio` | 1 |
| `welcome.all_good` | — | 1 |
| `link.code_invalid` | `enlace_panel` | 1 |
| `link.already_linked` | `enlace_panel` | 1 |
| `link.blocked` | `enlace_panel` | 1 |
| `review.new.positive` | `autor`, `estrellas`, `extracto`, `respuesta` | 2 |
| `review.new.negative` | `autor`, `estrellas`, `extracto`, `respuesta` | 2 |
| `review.new.no_text` | `autor`, `estrellas`, `respuesta` | 2 |
| `review.sent` | `enlace_reseña` | 2 |
| `review.skipped` | — | 2 |
| `review.edit_ask` | — | 2 |
| `review.edit_confirm` | `texto` | 2 |
| `review.edit_warn` | `problema` | 2 |
| `review.clarify_text` | `texto` | 2 |
| `review.clarify_options` | `autor` | 2 |
| `review.gone` | — | 2 |
| `review.digest` | `n`, `negocio`, `reparto_estrellas` | 2 |
| `review.digest_later` | `enlace_panel` | 2 |
| `holiday.ask` | `fecha_larga`, `nombre_festivo` | 3 |
| `holiday.ask_multi` | `fecha_1`, `fecha_2` | 3 |
| `holiday.sent_open` | `fecha_larga`, `horario`, `enlace_ficha` | 3 |
| `holiday.sent_closed` | `fecha_larga`, `enlace_ficha` | 3 |
| `hours.ask` | `fecha_larga` | 3, 4, 7 |
| `hours.ask_multi` | — | 3 |
| `hours.clarify_1` | — | 3, 4, 7 |
| `hours.clarify_2` | — | 3, 4, 7 |
| `hours.options` | — | 3, 4, 7 |
| `keydate.ask` | `evento`, `fecha_larga` | 4 |
| `keydate.sent` | `enlace_ficha` | 4 |
| `keydate.declined` | — | 4 |
| `offer.ask` | — | 4 |
| `offer.clarify_what` | — | 4 |
| `offer.clarify_dates` | — | 4 |
| `offer.clarify_service` | `servicio` | 4 |
| `offer.options` | — | 4 |
| `close_today.confirm` | `fecha_larga` | 5 |
| `close_today.sent` | `fecha_larga`, `enlace_ficha` | 5 |
| `close_day.confirm` | `fecha_larga` | 5 |
| `close_range.confirm` | `fecha_inicio`, `fecha_fin` | 5 |
| `order.ask_value` | — | 5 |
| `expert.no_data` | `dato` | 6 |
| `expert.out_of_scope` | — | 6 |
| `expert.other_plan` | `enlace_panel` | 6 |
| `account.link` | `enlace_panel` | 6 |
| `improve.category` | `actual`, `sugerida` | 7 |
| `improve.phone` | — | 7 |
| `improve.website` | — | 7 |
| `improve.hours` | — | 7 |
| `improve.attributes` | `servicios_sugeridos` | 7 |
| `improve.google_edit` | `campo`, `antes`, `ahora` | 7 |
| `improve.declined` | — | 7 |
| `improve.google_ok` | — | 7 |
| `reminder.pending` | `resumen_propuesta`, `fecha_larga` | 8 |
| `expired.past` | `fecha_larga` | 9 |
| `reconnect.ask` | `enlace_reconectar` | 10 |
| `reconnect.done` | — | 10 |
| `reconnect.needed` | `enlace_reconectar` | 10 |
| `summary.weekly` | `negocio`, `n_reseñas`, `n_respondidas`, `n_pendientes`, `n_cambios`, `linea_mensajes` | 11 |
| `summary.messages_line` | `usados` | 11 |
| `summary.nothing_pending` | — | 11 |
| `summary.off` | `enlace_panel` | 11 |
| `fallback.first` | — | 12 |
| `fallback.options` | — | 12 |
| `fallback.no_reviews` | — | 12 |
| `fallback.hours` | — | 12 |
| `fallback.other` | `enlace_panel` | 12 |
| `audio.unclear` | — | 2, 12 |
| `unlinked.reply` | `enlace_alta` | 13 |
| `quota.reached` | `canal_alternativo`, `enlace_panel` | 14 |
| `quota.reply` | `enlace_panel` | 14 |

Los textos que redacta el modelo (`expert.answer`, la respuesta propuesta a una reseña, la frase de cortesía de "hoy cierro") no van en el catálogo: el modelo responde en el idioma del dueño. Las respuestas a reseñas salen en el idioma de la ficha.

### 9.2 Botones

Cada título se traduce respetando los 20 caracteres. Si una traducción no cabe, se acorta la traducción, no se cambia el límite.

| Clave | Título |
|---|---|
| `btn.publish` | Publicar |
| `btn.edit` | Editar |
| `btn.no_reply` | No responder |
| `btn.yes_send` | Sí, enviar |
| `btn.change` | Cambiar |
| `btn.cancel` | Cancelar |
| `btn.send_anyway` | Enviar igual |
| `btn.no` | No |
| `btn.retry` | Reintentar |
| `btn.leave_it` | Dejarlo |
| `btn.see_now` | Ver ahora |
| `btn.later` | Más tarde |
| `btn.open_normal` | Abro normal |
| `btn.closed_day` | Cierro |
| `btn.other_hours` | Otro horario |
| `btn.normal_hours` | Horario normal |
| `btn.closed` | Cerrado |
| `btn.in_panel` | Lo pongo en panel |
| `btn.open_both` | Abro los dos |
| `btn.close_both` | Cierro los dos |
| `btn.depends` | Depende del día |
| `btn.have_offer` | Tengo oferta |
| `btn.have_news` | Tengo novedades |
| `btn.change_hours` | Cambio horario |
| `btn.nothing_this_year` | Nada este año |
| `btn.yes_offered` | Sí, lo ofrezco |
| `btn.no_remove` | No, quítalo |
| `btn.only_that_day` | Solo ese día |
| `btn.whole_weekend` | Todo el finde |
| `btn.drop_it` | Lo dejo |
| `btn.yes_close_today` | Sí, cerrar hoy |
| `btn.yes_close` | Sí, cerrar |
| `btn.yes_change` | Sí, cambiar |
| `btn.not_now` | Ahora no |
| `btn.yes_correct` | Sí, es correcto |
| `btn.no_undo` | No, deshacer |
| `btn.see_it_f` | Verla ahora |
| `btn.see_it_m` | Verlo ahora |
| `btn.dismiss` | Descartar |
| `btn.reconnect` | Reconectar Google |
| `btn.see_pending` | Ver pendientes |
| `btn.stop_summary` | No enviar más |
| `btn.reviews` | Reseñas |
| `btn.hours` | Horario |
| `btn.other` | Otra cosa |

### 9.3 Formatos y fragmentos

| Clave | Contenido |
|---|---|
| `fmt.date_long` | Fecha completa: "martes 8 de diciembre" |
| `fmt.date_range` | "del viernes 27 al domingo 29 de noviembre" |
| `fmt.time` | Hora en 24 horas: "10:00" |
| `fmt.hours_range` | "de 10:00 a 14:00"; partido: "de 10:00 a 14:00 y de 17:00 a 20:00" |
| `fmt.stars` | "1 estrella", "{n} estrellas" (singular y plural) |
| `fmt.star_split` | "tres de 5 estrellas y una de 2" |
| `fmt.weekdays`, `fmt.months` | Nombres de los días y de los meses |
| `fmt.channel` | "correo", "Telegram", "el panel" |
| `fmt.profile_state` | "suspendida", "sin verificar" |
| `fmt.review_issue` | "datos personales del cliente", "un insulto", "una oferta" |
| `fmt.footer` | "Fichaitor" |

### 9.4 Correo y panel

| Clave | Contenido |
|---|---|
| `email.subject.*` | Los 12 asuntos de la sección 7, uno por fila |
| `email.link.*` | Textos de los enlaces del correo |
| `panel.state.sent` | "Enviado a Google" |
| `panel.state.visible` | "Visible en Google" |
| `panel.state.expired` | "Caducada" |
| `panel.state.no_google` | "Sin conexión con Google" |
| `panel.quota_notice` | Aviso de mensajes agotados de la pantalla Inicio |

---

## 10. Cobertura de `01-flujos.md`

### Estados de una propuesta

| Estado | Dónde se cubre |
|---|---|
| programada → enviada | Primer mensaje de cada flujo |
| programada → cancelada | Sin mensaje |
| enviada → recordada | Flujo 8 |
| recordada → caducada | Flujo 9 |
| recopilando (aclaraciones) | `hours.clarify_*`, `offer.clarify_*`, `review.clarify_*` |
| por confirmar | `change.confirm` o confirmación en un toque |
| descartada | `generic.cancelled`, `review.skipped`, `keydate.declined`, `improve.declined` |
| ejecutando → aplicada | `change.sent`, `review.sent`, `holiday.sent_*`, `keydate.sent`, `close_today.sent` |
| ejecutando → fallida | `google.failed` |

### Excepciones

| Excepción | Dónde se cubre |
|---|---|
| Horario ambiguo | `hours.clarify_1`, `hours.clarify_2`, `hours.options` |
| Fecha ambigua | Fecha completa en la confirmación (flujo 5) |
| Dato que contradice la ficha | `offer.clarify_service` |
| Fuera de la ventana de 24 horas | Sección 1.6 y plantillas |
| Respuesta a una propuesta caducada | Flujo 9 |
| Dos mensajes que se contradicen | Sección 1.5 |
| Google rechaza el cambio | Sección 1.8 |
| El acceso a Google caduca | Flujo 10 |
| Audio que no se entiende | `audio.unclear` |
| Insultos o mensaje sin relación | `generic.neutral` |
| Reseña con instrucciones | Sección 1.2: el texto de terceros es un dato |

## 11. Decisiones abiertas

| Decisión | Propuesta del guion |
|---|---|
| ¿Los posts de oferta entran en el MVP? | Si no entran, se retira la rama `Tengo oferta` del flujo 4 |
| ¿Qué pasa si el dueño escribe con el plan agotado? | `quota.reply` una vez al día; "hoy cierro" se atiende siempre |
| ¿El mensaje de resultado cuenta en los 80? | Sí. Es el que da la prueba del cambio |
| ¿Se acepta `key_date_v1` como marketing si Meta la reclasifica? | Sí, tras intentar enviarla en ventana abierta |
| ¿Un número compartido o uno por cliente? | No afecta a los textos. Afecta a `unlinked.reply` y a la franja gratuita |

## Sin confirmar

| Punto | Por qué |
|---|---|
| Código de idioma `es_ES` | No figura en la página de componentes consultada |
| Reglas del nombre de plantilla (minúsculas y guiones bajos) y de la posición de las variables | No figuran en las páginas consultadas. El guion las respeta por prudencia |
| Que las variables de plantilla no admitan saltos de línea | No figura en las páginas consultadas. El guion los elimina por prudencia |
| Que los botones de respuesta solo puedan enviarse dentro de la ventana | Pendiente desde `docs/fase0/whatsapp.md`. La página de precios solo dice que fuera de la ventana hay que usar plantillas |
| Categoría real de cada plantilla | La decide Meta al aprobarlas |
| Consumo de 45 mensajes al mes | Estimación con supuestos. La beta mide el dato real |
| Límites técnicos de Telegram | No se han consultado en esta fase |

## Fuentes

Consultadas el 2026-11-11.

- [Meta: categorización de plantillas](https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/template-categorization)
- [Meta: directrices de categorías de plantilla](https://developers.facebook.com/docs/whatsapp/updates-to-pricing/new-template-guidelines)
- [Meta: componentes de plantilla](https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates/components)
- [Meta: precios de WhatsApp Business Platform](https://developers.facebook.com/docs/whatsapp/pricing)
- `docs/fase0/whatsapp.md`: tarifa de España y límites de los botones de respuesta
- `docs/fase0/precios.md`: plan de 80 mensajes
- `docs/fase0/matriz-api.md`: los cambios pasan por revisión de Google
