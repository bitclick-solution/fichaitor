# Correos de Fichaitor

Plantillas con la marca para los correos automáticos que envía el backend. Reglas generales en `brand/DESIGN.md`, apartado 13.

## Archivos

| Archivo | Qué es |
|---|---|
| `base.html` | El marco: símbolo «F.», caja con borde de tinta sobre papel, titular con el punto rojo y pie legal. Variables: `${asunto}`, `${preencabezado}`, `${titular}`, `${contenido}`, `${motivo}` |
| `bloques.html` | Las piezas del cuerpo: `parrafo` (`${texto}`), `codigo` (`${codigo}`), `boton` (`${enlace}`, `${etiqueta}`) y `aviso` (`${texto}`) |
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
- Nada obtenido de Google en el correo: ni reseñas, ni cifras, ni el nombre del negocio.
- El código nunca va en el asunto ni en el preencabezado: se ven en las notificaciones del móvil.

## Los correos

Fechas como las da `spanish_date`: «14 de noviembre de 2026 a las 12:16 (hora peninsular)».

El pie de todos los correos de texto plano es el mismo:

```text
--
¿Dudas? Escríbenos a soporte@fichaitor.es.
Fichaitor es un producto de Bitclick Solutions S.L.U. · C/ Cuenca nº 17, 18230 Atarfe (Granada), España.
```

### 1. Código de confirmación de la baja

| Campo | Valor |
|---|---|
| Asunto | Tu código de confirmación de Fichaitor |
| Preencabezado | Lo necesitas para ${que}. Caduca en ${minutos} minutos. |
| Titular | Tu código |
| Cuerpo | `parrafo`: «Has pedido ${que}. Escribe este código en Fichaitor para confirmarlo:» · `codigo` · `parrafo`: «Caduca en ${minutos} minutos y solo sirve una vez.» · `aviso`: «Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu sesión: escríbenos a soporte@fichaitor.es.» |
| Motivo (pie) | Te llega este correo porque alguien ha pedido ${que} desde tu cuenta de Fichaitor. |

`${que}`: «desconectar tu ficha» o «borrar tu cuenta de Fichaitor».

Texto plano:

```text
TU CÓDIGO

Has pedido ${que}. Escribe este código en Fichaitor para confirmarlo:

    ${codigo}

Caduca en ${minutos} minutos y solo sirve una vez.

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

Opcional.

| Campo | Valor |
|---|---|
| Asunto | Has restaurado Fichaitor |
| Preencabezado | Todo sigue como estaba. |
| Titular | Restaurada |
| Cuerpo | `parrafo`: «La baja está cancelada. Todo sigue como estaba y no se ha borrado nada.» · `parrafo`: «Fichaitor vuelve a leer tu ficha y a avisarte cuando haya algo que decidir.» |
| Motivo (pie) | Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor. |

Texto plano:

```text
RESTAURADA

La baja está cancelada. Todo sigue como estaba y no se ha borrado nada.

Fichaitor vuelve a leer tu ficha y a avisarte cuando haya algo que decidir.

Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.
```

### 4. Baja completada

Opcional también: la política de privacidad no lo exige.

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
