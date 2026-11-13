# Plantillas de WhatsApp para enviar a aprobación

Las siete plantillas que Fichaitor usa fuera de la ventana de 24 horas. El código las define en `app/domain/whatsapp_templates.py` (`TEMPLATES`) y una prueba (`tests/test_whatsapp_templates.py`) comprueba que este documento dice lo mismo. Fecha: 2026-11-13.

> **Importante:** Las crea el propietario en **WhatsApp Manager > Plantillas de mensajes**, en la WABA de Bitclick. Fichaitor no las crea ni las envía a revisión. Copia el nombre, el cuerpo y los botones **exactamente**: el código las envía por su nombre, con idioma `es`, y pone en cada botón el identificador de la propuesta.

## Cuándo se usan

Dentro de las 24 horas desde el último mensaje (o pulsación) del dueño, Fichaitor envía el mensaje de sesión normal, con sus botones. Fuera de ellas, Meta solo admite plantillas aprobadas:

| Mensaje de la conversación | Plantilla |
|---|---|
| Reseña nueva con texto (`review.new.positive`, `review.new.negative`) | `resena_nueva_v1` |
| Reseña nueva sin texto (`review.new.no_text`) | `resena_sin_texto_v1` |
| Festivo (`holiday.ask`) | `festivo_v1` |
| Nochebuena y Nochevieja (`eve.ask`) | `dia_especial_v1` |
| Recordatorio (`reminder.pending`) | `recordatorio_v1` |
| Resultado de un cambio confirmado (`review.sent`, `holiday.sent_*`, `change.sent`, `google.failed`, …) | `resultado_cambio_v1` |
| Cualquier otra propuesta, o una reseña cuya respuesta no cabe entera en `resena_nueva_v1` | `aviso_pendiente_v1` |

- Los botones de `resena_*` y de `festivo_v1` y `dia_especial_v1` son los mismos del mensaje y en el mismo orden: el dueño confirma con un toque, igual que dentro de la ventana. Al pulsar, se abre la ventana y el resto va como mensaje de sesión.
- `Ver propuesta` y `Ver aviso` no confirman nada: vuelven a enseñar el mensaje completo, con sus botones, ya dentro de la ventana.
- La respuesta propuesta nunca se recorta (es lo que se confirma). Si no cabe, el extracto de la reseña cede; si aun así no cabe, va `aviso_pendiente_v1`.
- Los valores van en una sola línea: Meta no admite saltos de línea en las variables.
- Las respuestas de una conversación en curso (aclaraciones, «¿Lo envío?») siempre van dentro de la ventana: no tienen plantilla.

## Criterios para que sean de utilidad

Los de `docs/fase1/guion-conversacional.md`, 3.2, y la guía de Meta («Template categorization», consultada el 2026-11-13): cada plantilla nombra la ficha del dueño, habla de un hecho de su servicio contratado, no lleva palabras de venta ni consejos, y ninguna empieza o acaba con una variable ni es solo variables. Las siete se solicitan como **Utilidad**. Si Meta reclasifica alguna como marketing, costaría 0,0585 € en lugar de 0,0166 € y descontaría 4 mensajes de la bolsa (`app/domain/message_allowance.py`): avisa para cambiar la categoría en `app/domain/whatsapp_templates.py`.

## Cómo crearlas

1. En WhatsApp Manager, **Plantillas de mensajes > Crear plantilla**.
2. Categoría **Utilidad**, tipo **Personalizada**, nombre e idioma **Español** (`es`). No elijas «Español (ESP)» (`es_ES`): el código envía `es`.
3. Pega el cuerpo. Las variables son posicionales (`{{1}}`, `{{2}}`…): elige **Número** como tipo de variable.
4. Escribe los ejemplos de esta página.
5. En **Botones**, añade las respuestas rápidas en el orden indicado. Sin encabezado ni pie.
6. Envía a revisión. Anota en `docs/fase0/whatsapp.md` la categoría que asigna Meta.

Orden recomendado (guion, 3.6): primero `resena_nueva_v1`, `resena_sin_texto_v1`, `festivo_v1`, `resultado_cambio_v1` y `aviso_pendiente_v1`; después `recordatorio_v1` y `dia_especial_v1`.

## Las plantillas

### `resena_nueva_v1`

| Campo | Valor |
|---|---|
| Uso | Reseña nueva con texto y respuesta propuesta (review.new.positive y negative) |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | `Publicar` (respuesta rápida) · `Editar` (respuesta rápida) · `No responder` (respuesta rápida) |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
Nueva reseña en la ficha de {{1}}: {{2}}, «{{3}}». Respuesta preparada: «{{4}}». ¿La envío a Google?
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | Bar La Esquina |
| `{{2}}` | 5 estrellas |
| `{{3}}` | El arroz, espectacular. Volveremos. |
| `{{4}}` | Muchas gracias por tu reseña. Nos alegra que disfrutaras el arroz. |

Así se ve:

```text
Nueva reseña en la ficha de Bar La Esquina: 5 estrellas, «El arroz, espectacular. Volveremos.». Respuesta preparada: «Muchas gracias por tu reseña. Nos alegra que disfrutaras el arroz.». ¿La envío a Google?
```

### `resena_sin_texto_v1`

| Campo | Valor |
|---|---|
| Uso | Reseña nueva sin texto, con respuesta propuesta (review.new.no_text) |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | `Publicar` (respuesta rápida) · `Editar` (respuesta rápida) · `No responder` (respuesta rápida) |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
Nueva reseña en la ficha de {{1}}: {{2}}, sin texto. Respuesta preparada: «{{3}}». ¿La envío a Google?
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | Bar La Esquina |
| `{{2}}` | 4 estrellas |
| `{{3}}` | Gracias por tu valoración. Te esperamos pronto. |

Así se ve:

```text
Nueva reseña en la ficha de Bar La Esquina: 4 estrellas, sin texto. Respuesta preparada: «Gracias por tu valoración. Te esperamos pronto.». ¿La envío a Google?
```

### `festivo_v1`

| Campo | Valor |
|---|---|
| Uso | Festivo (holiday.ask) |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | `Abro normal` (respuesta rápida) · `Cierro` (respuesta rápida) · `Otro horario` (respuesta rápida) |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
El {{1}} es festivo ({{2}}). La ficha de {{3}} mostrará tu horario normal si no se cambia. ¿Abres ese día?
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | martes 8 de diciembre |
| `{{2}}` | Inmaculada Concepción |
| `{{3}}` | Bar La Esquina |

Así se ve:

```text
El martes 8 de diciembre es festivo (Inmaculada Concepción). La ficha de Bar La Esquina mostrará tu horario normal si no se cambia. ¿Abres ese día?
```

### `dia_especial_v1`

| Campo | Valor |
|---|---|
| Uso | Nochebuena y Nochevieja (eve.ask) |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | `Abro normal` (respuesta rápida) · `Cierro` (respuesta rápida) · `Otro horario` (respuesta rápida) |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
El {{1}} es {{2}}. La ficha de {{3}} mostrará tu horario normal si no se cambia. ¿Abres ese día?
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | jueves 24 de diciembre |
| `{{2}}` | Nochebuena |
| `{{3}}` | Bar La Esquina |

Así se ve:

```text
El jueves 24 de diciembre es Nochebuena. La ficha de Bar La Esquina mostrará tu horario normal si no se cambia. ¿Abres ese día?
```

### `recordatorio_v1`

| Campo | Valor |
|---|---|
| Uso | Recordatorio de una propuesta (reminder.pending) |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | `Ver propuesta` (respuesta rápida) |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
Sigue pendiente de tu respuesta en la ficha de {{1}}: {{2}}. Caduca el {{3}}. Pulsa «Ver propuesta» y te la enseño aquí.
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | Bar La Esquina |
| `{{2}}` | la reseña de 5 estrellas |
| `{{3}}` | lunes 16 de noviembre |

Así se ve:

```text
Sigue pendiente de tu respuesta en la ficha de Bar La Esquina: la reseña de 5 estrellas. Caduca el lunes 16 de noviembre. Pulsa «Ver propuesta» y te la enseño aquí.
```

### `resultado_cambio_v1`

| Campo | Valor |
|---|---|
| Uso | Resultado de un cambio que el dueño confirmó (review.sent, holiday.sent_*, change.sent, google.failed...) |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | Ninguno |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
Sobre el cambio que confirmaste en la ficha de {{1}}: {{2}} Tienes el detalle en tu panel de Fichaitor.
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | Bar La Esquina |
| `{{2}}` | Respuesta enviada a Google. Suele verse en unos minutos. |

Así se ve:

```text
Sobre el cambio que confirmaste en la ficha de Bar La Esquina: Respuesta enviada a Google. Suele verse en unos minutos. Tienes el detalle en tu panel de Fichaitor.
```

### `aviso_pendiente_v1`

| Campo | Valor |
|---|---|
| Uso | Cualquier otra propuesta, o una que no cabe en su plantilla |
| Idioma | `es` (español) |
| Categoría | Utilidad (`UTILITY`) |
| Botones | `Ver aviso` (respuesta rápida) |
| Encabezado y pie | Ninguno |

Cuerpo:

```text
Hay un aviso de la ficha de {{1}} pendiente de tu respuesta. Pulsa «Ver aviso» y te lo enseño aquí.
```

Ejemplos de las variables (Meta los exige):

| Variable | Ejemplo |
|---|---|
| `{{1}}` | Bar La Esquina |

Así se ve:

```text
Hay un aviso de la ficha de Bar La Esquina pendiente de tu respuesta. Pulsa «Ver aviso» y te lo enseño aquí.
```
