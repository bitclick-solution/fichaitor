# Correos de Fichaitor

Plantillas con la marca para los correos automáticos que envía el backend. Reglas generales en `brand/DESIGN.md`, apartado 13.

## Archivos

| Archivo | Qué es |
|---|---|
| `base.html` | El marco: símbolo «F.», caja con borde de tinta sobre papel, titular con el punto rojo y pie legal. Variables: `${asunto}`, `${preencabezado}`, `${titular}`, `${contenido}`, `${motivo}` |
| `bloques.html` | Las piezas del cuerpo: `parrafo` (`${texto}`), `codigo` (`${codigo}`), `boton` (`${enlace}`, `${etiqueta}`), `aviso` (`${texto}`), `dato` (`${etiqueta}`, `${valor}`: una fila de una tabla de datos), `datos_fin` (sin variables: cierra la tabla de datos) y `destacado` (`${texto}`: un recuadro sin alarma) |
| `vista.py` | Genera en `vista/` una página por correo con datos de ejemplo, para revisarlos en el navegador. Rellena las plantillas igual que debe hacerlo el backend |

## Cómo se usan

1. Con `string.Template` de la biblioteca estándar de Python. No hace falta ninguna dependencia.
2. **Escapa cada valor antes de sustituirlo** con `html.escape(valor)`. Por defecto escapa también las comillas, que es lo que necesitan los enlaces dentro de `href="…"`. Las plantillas no escapan nada.
3. Rellena los bloques del cuerpo en el orden de la tabla de cada correo y concaténalos en `${contenido}`. `${contenido}` es lo único que no se escapa, porque ya es HTML de los bloques.
4. Usa `substitute`, no `safe_substitute`: si falta una variable, mejor un error que un correo con `${hueco}` a la vista.
5. Envía siempre también la versión de texto plano (`multipart/alternative`). Está al final de cada correo.

### Cabeceras

| Cabecera | Valor |
|---|---|
| `From` | `Fichaitor <avisos@fichaitor.es>` |
| `Reply-To` | `soporte@fichaitor.es`. **Obligatoria**: `avisos@` no se lee |
| `Subject` | El asunto de la tabla |

### El enlace del botón

- `${enlace}` es siempre una URL absoluta `https://fichaitor.es/…` que monta el backend con su propia configuración (la URL pública del servicio) y un token que genera él.
- Nunca se construye con datos que lleguen en la petición: ni la cabecera `Host`, ni parámetros, ni texto del usuario. Un `Host` falso convertiría el correo en un enlace a otro dominio.
- El token va en el fragmento (`/app#restaurar=<token>`), no en la consulta: el fragmento no llega a los registros del servidor ni al `Referer`.

### Reglas de forma

- Sin imágenes, ni la del logo: muchos clientes de correo las bloquean y, servidas desde nuestro dominio, dejarían rastro de apertura en el registro. El símbolo se dibuja con una celda.
- Sin fuentes web. «Archivo Black» se usa si está instalada; si no, Arial Black y Arial.
- Una sola acción por correo, el código o el botón, y arriba: como mucho, detrás de un párrafo que diga qué se pidió.
- Nada obtenido de Google en el correo: ni reseñas, ni cifras, ni el nombre del negocio. **Excepción:** los correos del cobro llevan el nombre de la ficha (`${ficha}`), solo para identificar qué se cobra, como la factura. Así lo dice la política de privacidad.
- El código nunca va en el asunto ni en el preencabezado: se ven en las notificaciones del móvil.

## Los correos

Fechas como las da `spanish_date`: «14 de noviembre de 2026 a las 12:16 (hora peninsular)».

El pie de todos los correos de texto plano es el mismo. La primera línea es `-- `, con un espacio al final: es el separador de firma estándar y permite a los clientes de correo plegar el pie.

```text
-- 
¿Dudas? Escríbenos a soporte@fichaitor.es.
Fichaitor es un producto de Bitclick Solutions S.L.U. · C/ Cuenca nº 17, 18230 Atarfe (Granada), España.
```

### 1. Código de confirmación de la baja

| Campo | Valor |
|---|---|
| Asunto | Tu código de confirmación de Fichaitor |
| Preencabezado | Lo necesitas para ${que}. Caduca en ${plazo}. |
| Titular | Tu código |
| Cuerpo | `parrafo`: «Has pedido ${que}. Escribe este código en Fichaitor para confirmarlo:» · `codigo` · `parrafo`: «Caduca en ${plazo} y solo sirve una vez.» · `aviso`: «Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu sesión: escríbenos a soporte@fichaitor.es.» |
| Motivo (pie) | Te llega este correo porque alguien ha pedido ${que} desde tu cuenta de Fichaitor. |

`${que}`: «desconectar tu ficha» o «borrar tu cuenta de Fichaitor».

`${plazo}`: la duración con su unidad, en singular o plural según el número: «10 minutos», «1 minuto». Así nunca sale «1 minutos».

Texto plano:

```text
TU CÓDIGO

Has pedido ${que}. Escribe este código en Fichaitor para confirmarlo:

    ${codigo}

Caduca en ${plazo} y solo sirve una vez.

Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu sesión: escríbenos a soporte@fichaitor.es.

Te llega este correo porque alguien ha pedido ${que} desde tu cuenta de Fichaitor.
```

### 2. Baja programada

| Campo | Ficha | Cuenta |
|---|---|---|
| Asunto | Tu ficha se desconectará de Fichaitor pronto | Tu cuenta de Fichaitor se borrará pronto |
| Preencabezado | Puedes restaurarla hasta el ${fecha}. | Puedes restaurarla hasta el ${fecha}. |
| Titular | Desconexión en marcha | Baja en marcha |

Cuerpo, en este orden:

1. `parrafo`, ficha: «Has pedido desconectar tu ficha de Fichaitor. Desde ahora no la leemos ni te enviamos avisos.» Cuenta: «Has pedido borrar tu cuenta de Fichaitor. Desde ahora no leemos tus fichas ni te enviamos avisos, y hemos cerrado tu sesión en todos los dispositivos.»
2. `boton`: `${etiqueta}` «Restaurar», `${enlace}` el enlace de restauración.
3. `aviso`: «Si no has sido tú, pulsa Restaurar. No se ha borrado nada.»
4. `parrafo`, ficha: «Si no haces nada, el ${fecha} retiraremos el acceso a tu cuenta de Google. Lo obtenido de Google se borra en un máximo de 30 días, y el registro de las respuestas que publicaste, a los 12 meses. Tu cuenta sigue existiendo y podrás volver a conectar.» Cuenta: «Si no haces nada, el ${fecha} retiraremos el acceso a tu cuenta de Google y cerraremos tu cuenta. Lo obtenido de Google se borra en un máximo de 30 días, y el registro de las respuestas que publicaste, a los 12 meses.»

Motivo (pie): «Te llega este correo porque se ha confirmado una baja con un código enviado a esta dirección.»

Texto plano (ficha; la cuenta cambia el titular y los dos párrafos como en la lista):

```text
DESCONEXIÓN EN MARCHA

Has pedido desconectar tu ficha de Fichaitor. Desde ahora no la leemos ni te enviamos avisos.

Restaurar: ${enlace}

Si no has sido tú, abre el enlace. No se ha borrado nada.

Si no haces nada, el ${fecha} retiraremos el acceso a tu cuenta de Google. Lo obtenido de Google se borra en un máximo de 30 días, y el registro de las respuestas que publicaste, a los 12 meses. Tu cuenta sigue existiendo y podrás volver a conectar.

Te llega este correo porque se ha confirmado una baja con un código enviado a esta dirección.
```

### 3. Baja cancelada

Se envía al restaurar, desde el panel o desde el enlace. Hay dos variantes: al confirmar la baja de la cuenta se cerraron todas las sesiones, y siguen cerradas.

| Campo | Ficha | Cuenta |
|---|---|---|
| Asunto | Has restaurado Fichaitor | Has restaurado tu cuenta de Fichaitor |
| Preencabezado | No se ha borrado nada. | Entra de nuevo para seguir donde lo dejaste. |
| Titular | Restaurada | Restaurada |
| Cuerpo | `parrafo`: «Hemos cancelado la desconexión. No se ha borrado nada.» · `parrafo`: «Fichaitor vuelve a leer tus fichas de Google y a avisarte cuando haya algo que decidir.» | `parrafo`: «Hemos cancelado la baja de tu cuenta. No se ha borrado nada.» · `parrafo`: «Al confirmar la baja cerramos tu sesión en todos los dispositivos. Entra de nuevo en Fichaitor para seguir donde lo dejaste.» · `boton`: «Entrar en Fichaitor», `${enlace}` `https://fichaitor.es/app/` (de la configuración, como el de restaurar) |
| Motivo (pie) | Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor. | Igual |

Texto plano (ficha):

```text
RESTAURADA

Hemos cancelado la desconexión. No se ha borrado nada.

Fichaitor vuelve a leer tus fichas de Google y a avisarte cuando haya algo que decidir.

Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.
```

Texto plano (cuenta):

```text
RESTAURADA

Hemos cancelado la baja de tu cuenta. No se ha borrado nada.

Al confirmar la baja cerramos tu sesión en todos los dispositivos. Entra de nuevo en Fichaitor para seguir donde lo dejaste.

Entrar en Fichaitor: ${enlace}

Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.
```

### 4. Baja completada

**No se envía** (decisión del backend, 2026-11-12): con la baja de la cuenta, la dirección de correo se borra en ese mismo momento y habría que guardarla solo para este aviso. La política de privacidad no lo exige. Se conserva el diseño por si cambia.

| Campo | Ficha | Cuenta |
|---|---|---|
| Asunto | Tu ficha se ha desconectado de Fichaitor | Tu cuenta de Fichaitor se ha borrado |
| Preencabezado | Hemos retirado el acceso a Google. | Hemos retirado el acceso a Google. |
| Titular | Desconectada | Hasta aquí |
| Cuerpo | `parrafo`: «Hemos retirado el acceso a tu cuenta de Google. Lo obtenido de Google se borrará en un máximo de 30 días.» · `parrafo`: «Si quieres volver, conecta tu ficha de nuevo en fichaitor.es.» | `parrafo`: «Hemos retirado el acceso a tu cuenta de Google y cerrado tu cuenta. Lo obtenido de Google se borrará en un máximo de 30 días.» · `parrafo`: «Gracias por haber probado Fichaitor.» |
| Motivo (pie) | Te llega este correo porque se ha completado una baja que pediste. | Igual |

Texto plano (ficha; la cuenta cambia el titular y los párrafos como en la tabla):

```text
DESCONECTADA

Hemos retirado el acceso a tu cuenta de Google. Lo obtenido de Google se borrará en un máximo de 30 días.

Si quieres volver, conecta tu ficha de nuevo en fichaitor.es.

Te llega este correo porque se ha completado una baja que pediste.
```

## Correos del cobro

Los envía el worker al cliente, desde `avisos@` y con `Reply-To` a `soporte@`, como los de la baja. Decisiones del propietario en `docs/admin-facturacion.md` (1.1, 9.0 y 9.0.1): cada ficha de pago se cobra por años completos desde su activación y se renueva sola en su fecha; solo con tarjeta; sin devoluciones salvo excepción; una plaza sin ficha no se renueva ni se cobra; si un cobro falla, toda la cuenta queda en pausa hasta el pago, sin plazo de gracia, y la pausa no borra nada.

Todas las variables llegan **ya formateadas** y se escapan como las demás:

| Variable | Ejemplo |
|---|---|
| `${numero}`, `${numero_original}` | `FCH/2027/00042` (número de Odoo) |
| `${fecha}`, `${fecha_renovacion}`, `${fecha_fin}` | `14 de marzo de 2027` (sin hora) |
| `${importe}` | `42,35 €` (total con IVA; en Canarias, Ceuta y Melilla, sin IVA) |
| `${plan}` | `Pro` o `Pro WhatsApp` |
| `${ficha}` | `Bar La Parra` |
| `${periodo}` | `del 14/03/2027 al 13/03/2028` |
| `${motivo}` | `la tarjeta ha sido rechazada` o `falta confirmar el pago con tu banco` (en minúscula, sin punto final) |
| `${oferta}` | Una o dos frases, con su punto final |
| `${enlace}` | URL absoluta de la configuración, como en la baja: Facturación (`https://fichaitor.es/app#facturacion`), el pago pendiente o el panel |

Las tablas de datos son filas `dato` seguidas de un `datos_fin`. En texto plano, cada fila es `Etiqueta: valor`.

### 5. Factura emitida (`invoice_issued`)

Tras cada cobro, con la factura de Odoo **adjunta en PDF**.

| Campo | Valor |
|---|---|
| Asunto | Tu factura de Fichaitor ${numero} |
| Preencabezado | ${plan} para ${ficha}, ${periodo}. La factura va adjunta en PDF. |
| Titular | Tu factura |
| Cuerpo | `parrafo`: «Hemos cobrado ${importe} por el plan ${plan} de ${ficha}. Te adjuntamos la factura en PDF.» · `dato` «Factura» / ${numero} · `dato` «Fecha» / ${fecha} · `dato` «Plan» / ${plan} · `dato` «Ficha» / ${ficha} · `dato` «Periodo» / ${periodo} (con la primera letra en mayúscula: «Del 14/03/2027 al 13/03/2028») · `dato` «Total» / ${importe} · `datos_fin` · `boton`: «Ver Facturación», ${enlace} · `parrafo`: «Esta ficha se renovará sola al terminar el periodo, salvo que canceles la renovación desde Facturación. Te avisaremos 30 días antes.» |
| Motivo (pie) | Te llega este correo porque se ha cobrado un plan de pago de tu cuenta de Fichaitor. |

En `dato` «Periodo», el valor empieza en mayúscula. Si `${periodo}` llega en minúscula, pon en mayúscula solo su primera letra al rellenar la fila; en el párrafo y en el preencabezado va tal cual.

Texto plano:

```text
TU FACTURA

Hemos cobrado ${importe} por el plan ${plan} de ${ficha}. Te adjuntamos la factura en PDF.

Factura: ${numero}
Fecha: ${fecha}
Plan: ${plan}
Ficha: ${ficha}
Periodo: ${periodo}
Total: ${importe}

Ver Facturación: ${enlace}

Esta ficha se renovará sola al terminar el periodo, salvo que canceles la renovación desde Facturación. Te avisaremos 30 días antes.

Te llega este correo porque se ha cobrado un plan de pago de tu cuenta de Fichaitor.
```

### 6. Aviso de renovación (`renewal_notice` y `renewal_notice_unassigned`)

30 días antes de la renovación de cada plaza. `${importe}` es el precio congelado en ese aviso, con IVA. La variante `renewal_notice_unassigned` es para una plaza sin ficha asignada: no se renovará ni se cobrará.

| Campo | Con ficha | Sin ficha |
|---|---|---|
| Asunto | Tu plan ${plan} de ${ficha} se renueva el ${fecha_renovacion} | Una plaza ${plan} no se renovará el ${fecha_renovacion} |
| Preencabezado | Cobraremos ${importe} con tu tarjeta. Si no quieres renovarlo, cancélalo antes. | No tiene ninguna ficha, así que no te la cobraremos. |
| Titular | Se renueva pronto | Plaza sin ficha |
| Motivo (pie) | Te avisamos 30 días antes de cada renovación, como dicen las condiciones de uso. | Igual |

Cuerpo con ficha, en este orden:

1. `parrafo`: «El ${fecha_renovacion} se renueva por un año más el plan ${plan} de ${ficha}. Cobraremos ${importe} con la tarjeta que tienes guardada.»
2. `dato` «Ficha» / ${ficha} · `dato` «Plan» / ${plan} · `dato` «Renovación» / ${fecha_renovacion} · `dato` «Total» / ${importe} · `datos_fin`
3. `parrafo`: «Si quieres seguir, no tienes que hacer nada. Si no, cancela la renovación desde Facturación antes del ${fecha_renovacion}: una vez cobrada, no se devuelve.»
4. `boton`: «Ir a Facturación», ${enlace}

Cuerpo sin ficha, en este orden:

1. `parrafo`: «Tienes una plaza del plan ${plan} (un año pagado) que se renueva el ${fecha_renovacion}, pero ahora no tiene ninguna ficha asignada.»
2. `parrafo`: «Si sigue así, no se renovará ni te la cobraremos. Si quieres conservarla, activa una ficha en ella antes del ${fecha_renovacion}: entonces se renovará por ${importe}.»
3. `boton`: «Ir a Facturación», ${enlace}

Texto plano (con ficha):

```text
SE RENUEVA PRONTO

El ${fecha_renovacion} se renueva por un año más el plan ${plan} de ${ficha}. Cobraremos ${importe} con la tarjeta que tienes guardada.

Ficha: ${ficha}
Plan: ${plan}
Renovación: ${fecha_renovacion}
Total: ${importe}

Si quieres seguir, no tienes que hacer nada. Si no, cancela la renovación desde Facturación antes del ${fecha_renovacion}: una vez cobrada, no se devuelve.

Ir a Facturación: ${enlace}

Te avisamos 30 días antes de cada renovación, como dicen las condiciones de uso.
```

Texto plano (sin ficha):

```text
PLAZA SIN FICHA

Tienes una plaza del plan ${plan} (un año pagado) que se renueva el ${fecha_renovacion}, pero ahora no tiene ninguna ficha asignada.

Si sigue así, no se renovará ni te la cobraremos. Si quieres conservarla, activa una ficha en ella antes del ${fecha_renovacion}: entonces se renovará por ${importe}.

Ir a Facturación: ${enlace}

Te avisamos 30 días antes de cada renovación, como dicen las condiciones de uso.
```

### 7. Cuenta en pausa (`billing_blocked`)

Cuando un cobro falla o no se completa. Sin alarma: dice qué ha pasado, cómo se arregla y qué sigue disponible.

| Campo | Valor |
|---|---|
| Asunto | Tu cuenta de Fichaitor está en pausa |
| Preencabezado | No hemos podido cobrar ${importe}. En cuanto se pague, todo vuelve a funcionar. |
| Titular | Cuenta en pausa |
| Motivo (pie) | Te llega este correo porque no se ha podido cobrar un plan de pago de tu cuenta de Fichaitor. |

Cuerpo, en este orden:

1. `parrafo`: «No hemos podido cobrar ${importe} del plan de ${ficha}: ${motivo}. Hasta que se pague, tu cuenta queda en pausa.»
2. `boton`: «Pagar ahora», ${enlace}
3. `parrafo`: «Durante la pausa, Fichaitor no lee ninguna de tus fichas, tampoco la del plan Gratis, no te envía avisos ni aplica cambios. No se ha borrado nada y la pausa no borra nada: en cuanto se pague, todo vuelve a funcionar.»
4. `parrafo`: «Mientras tanto, en el panel puedes seguir usando Facturación, desactivar fichas, desvincular chats, desconectar Google o darte de baja.»

Texto plano:

```text
CUENTA EN PAUSA

No hemos podido cobrar ${importe} del plan de ${ficha}: ${motivo}. Hasta que se pague, tu cuenta queda en pausa.

Pagar ahora: ${enlace}

Durante la pausa, Fichaitor no lee ninguna de tus fichas, tampoco la del plan Gratis, no te envía avisos ni aplica cambios. No se ha borrado nada y la pausa no borra nada: en cuanto se pague, todo vuelve a funcionar.

Mientras tanto, en el panel puedes seguir usando Facturación, desactivar fichas, desvincular chats, desconectar Google o darte de baja.

Te llega este correo porque no se ha podido cobrar un plan de pago de tu cuenta de Fichaitor.
```

### 8. Pago recibido (`billing_unblocked`)

Cuando se paga lo pendiente y la pausa termina. La factura de ese cobro llega aparte (`invoice_issued`).

| Campo | Valor |
|---|---|
| Asunto | Pago recibido: todo vuelve a funcionar |
| Preencabezado | Fichaitor vuelve a leer tus fichas y a avisarte. |
| Titular | Todo en marcha |
| Cuerpo | `parrafo`: «Hemos recibido el pago del plan de ${ficha}. Tu cuenta ya no está en pausa.» · `parrafo`: «Fichaitor vuelve a leer tus fichas y a avisarte cuando haya algo que decidir. La factura te llega en otro correo.» · `boton`: «Entrar en Fichaitor», ${enlace} |
| Motivo (pie) | Te llega este correo porque se ha pagado un cobro pendiente de tu cuenta de Fichaitor. |

Texto plano:

```text
TODO EN MARCHA

Hemos recibido el pago del plan de ${ficha}. Tu cuenta ya no está en pausa.

Fichaitor vuelve a leer tus fichas y a avisarte cuando haya algo que decidir. La factura te llega en otro correo.

Entrar en Fichaitor: ${enlace}

Te llega este correo porque se ha pagado un cobro pendiente de tu cuenta de Fichaitor.
```

### 9. Factura rectificativa (`credit_note_issued`)

Solo en el caso excepcional de un reembolso, con la rectificativa de Odoo **adjunta en PDF**. Sin botón.

| Campo | Valor |
|---|---|
| Asunto | Factura rectificativa de Fichaitor ${numero} |
| Preencabezado | Rectifica la factura ${numero_original}. Va adjunta en PDF. |
| Titular | Factura rectificativa |
| Cuerpo | `parrafo`: «Hemos emitido una factura rectificativa de la factura ${numero_original}, por ${importe}. Te la adjuntamos en PDF.» · `dato` «Rectificativa» / ${numero} · `dato` «Rectifica a» / ${numero_original} · `dato` «Total» / ${importe} · `datos_fin` · `parrafo`: «Devolvemos el importe a la tarjeta con la que pagaste. Según tu banco, puede tardar unos días en verse.» |
| Motivo (pie) | Te llega este correo porque hemos devuelto un importe cobrado en tu cuenta de Fichaitor. |

Texto plano:

```text
FACTURA RECTIFICATIVA

Hemos emitido una factura rectificativa de la factura ${numero_original}, por ${importe}. Te la adjuntamos en PDF.

Rectificativa: ${numero}
Rectifica a: ${numero_original}
Total: ${importe}

Devolvemos el importe a la tarjeta con la que pagaste. Según tu banco, puede tardar unos días en verse.

Te llega este correo porque hemos devuelto un importe cobrado en tu cuenta de Fichaitor.
```

### 10. Fin de la beta (`beta_ending`)

30 días antes de que termine la beta. `${oferta}` es **opcional**: si hay oferta para las cuentas beta, se añade el bloque `destacado` entre el primer párrafo y el botón; si no, no se añade nada.

| Campo | Valor |
|---|---|
| Asunto | La beta de Fichaitor termina el ${fecha_fin} |
| Preencabezado | Elige el plan de cada ficha antes de esa fecha. |
| Titular | Fin de la beta |
| Motivo (pie) | Te llega este correo porque tienes una cuenta de Fichaitor en la beta. |

Cuerpo, en este orden:

1. `parrafo`: «Gracias por probar Fichaitor durante la beta. Termina el ${fecha_fin} y, desde ese día, las fichas de pago se cobran. Elige en Facturación el plan de cada ficha.»
2. `destacado`: «${oferta}» (solo si hay oferta)
3. `boton`: «Elegir los planes», ${enlace}
4. `parrafo`: «Cada ficha de pago se cobra con tarjeta por años completos desde su activación y se renueva sola cada año, salvo que canceles la renovación. Lo cobrado no se devuelve, así que elige con calma.»
5. `parrafo`: «Si no eliges plan antes del ${fecha_fin}, tu ficha más antigua pasará al plan Gratis y las demás se desactivarán: dejaremos de leerlas y de avisarte de ellas, y si no las vuelves a activar en 30 días, borraremos lo que tenemos de ellas. No te cobraremos nada que no hayas pedido.»

Texto plano (la línea de la oferta y la línea en blanco que la sigue solo van si hay oferta):

```text
FIN DE LA BETA

Gracias por probar Fichaitor durante la beta. Termina el ${fecha_fin} y, desde ese día, las fichas de pago se cobran. Elige en Facturación el plan de cada ficha.

${oferta}

Elegir los planes: ${enlace}

Cada ficha de pago se cobra con tarjeta por años completos desde su activación y se renueva sola cada año, salvo que canceles la renovación. Lo cobrado no se devuelve, así que elige con calma.

Si no eliges plan antes del ${fecha_fin}, tu ficha más antigua pasará al plan Gratis y las demás se desactivarán: dejaremos de leerlas y de avisarte de ellas, y si no las vuelves a activar en 30 días, borraremos lo que tenemos de ellas. No te cobraremos nada que no hayas pedido.

Te llega este correo porque tienes una cuenta de Fichaitor en la beta.
```
