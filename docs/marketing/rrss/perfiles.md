# Perfiles de Fichaitor en TikTok e Instagram

Cómo dejar las dos cuentas configuradas igual, campo por campo. Copia y pega los textos tal cual. Fecha: 2026-11-13.

## Lo común a las dos

| Campo | Valor |
|---|---|
| Usuario | `@fichaitor`. Si está ocupado: `@fichaitor.es`. **El mismo en las dos redes** |
| Nombre visible | `Fichaitor` en TikTok. En Instagram, el de su apartado (el nombre de Instagram aparece en las búsquedas) |
| Foto de perfil | [`avatar-fichaitor-512.png`](avatar-fichaitor-512.png). Es la versión del símbolo «F.» con margen para el recorte circular: no la cambies por `brand/icon-512.png`, que pierde las esquinas |
| Correo de contacto | `soporte@fichaitor.es` |
| Tipo de cuenta | Empresa. Da estadísticas, el botón de contacto y el enlace en el perfil |
| Idioma y región | Español, España |
| Verificación en dos pasos | Activada, con el móvil y una app de autenticación. Las cuentas nuevas de marca son las que más roban |

### Reglas de los textos

- En plural, como equipo: «te avisamos».
- Sin emojis, sin exclamaciones y sin hashtags en la biografía.
- Solo funciones que existen: reseñas, festivos y la nota de la ficha. Nada de publicaciones, fotos ni modo automático.
- Sin «el primero en Maps».

### El enlace

Cada red lleva su código para saber de dónde llega cada diagnóstico:

- TikTok: `https://fichaitor.es/?s=tt_bio`
- Instagram: `https://fichaitor.es/?s=ig_bio`

Mientras `/app` siga en «Muy pronto», el enlace lleva a la portada y al diagnóstico gratis. Cuando se abra la beta en producción, cambiamos la última línea de la biografía (consulta «Cambios al abrir la beta»).

---

## TikTok

### Perfil

| Campo | Texto |
|---|---|
| Nombre | `Fichaitor` |
| Biografía (80 caracteres como máximo) | `Tu ficha de Google, al día. Te avisamos de cada reseña y festivo. Tú confirmas.` |
| Sitio web | `https://fichaitor.es/?s=tt_bio` |
| Categoría | Software o «Aplicaciones». Si no aparece, «Servicios profesionales» |
| Correo | `soporte@fichaitor.es` |

> **Nota:** Si TikTok todavía no te deja añadir el enlace, cambia la biografía por esta hasta que te lo permita: `Tu ficha de Google, al día. Reseñas y festivos. Mira tu nota en fichaitor.es` (76 caracteres).

### Ajustes

En **Configuración y privacidad**:

| Ajuste | Valor | Por qué |
|---|---|---|
| Comentarios | Todos | Los comentarios ayudan a que TikTok enseñe el vídeo a más gente |
| Filtro de comentarios | Activado, con palabras: insultos habituales y los nombres de bares de la zona | No queremos peleas entre locales en nuestros comentarios |
| Mensajes directos | Todos | La llamada a la acción es «Escríbenos FICHA» |
| Dúos y pegas (stitch) | Activados | Un dueño que nos cita nos da visibilidad gratis |
| Descargas de vídeos | Desactivadas | Que no circulen sin nuestro perfil |
| Sugerir tu cuenta a otros | Activado, salvo «contactos del móvil» | Tus amigos ya te conocen; queremos llegar a hosteleros |

### Centro de mensajes

En **Herramientas para empresas → Centro de mensajes**. TikTok no tiene respuestas rápidas: lo automático se hace aquí y el resto se responde a mano con los textos de «Textos para responder a mano».

Tu enlace de mensajes es `https://tiktok.me/fichaitor`. Sirve para ponerlo en la auditoría impresa o en otra red, si alguna vez hace falta.

#### Enlace de mensaje directo

**Actívalo.** Pone un botón para escribirnos en los vídeos, y la llamada a la acción de los vídeos es justo esa: «Escríbenos FICHA».

#### Instrucciones de chat

Son **botones de mensaje rápido**: quien abre el chat los pulsa y el texto se envía tal cual, como si lo hubiera escrito. Cada botón lleva una palabra clave, así que la respuesta automática contesta sola al momento.

Borra el botón «Ficha» que hay ahora y crea estos tres, en este orden:

| Botón | Palabra clave que activa | Lo que contesta |
|---|---|---|
| `Quiero la nota de mi ficha` | `ficha` | Pide el nombre del negocio y la localidad |
| `¿Cuánto cuesta?` | `cuánto` | El precio de la beta y de después |
| `¿Cómo entro en la beta?` | `beta` | Qué es la beta y cómo entrar |

> **Importante:** Ningún botón puede llevar dos palabras clave de reglas distintas. Por ejemplo, «¿Cuánto cuesta la ficha?» activaría dos respuestas. Si cambias un botón, comprueba la tabla de palabras clave.

Después de guardarlos, ábrelos en **Previsualizar**, pulsa cada botón y comprueba que llega la respuesta correcta.

#### Mensaje de bienvenida

> Hola, somos Fichaitor. Si nos has escrito por tu ficha, dinos el nombre de tu negocio y la localidad, y te mandamos su nota y tres cosas que se pueden mejorar. Es gratis.

#### Respuesta de palabras clave

Es la pieza más útil: contesta sola cuando alguien escribe «FICHA» o pulsa un botón de las instrucciones de chat. Solo funciona en los mensajes, no en los comentarios. Crea tres reglas:

| Palabras clave | Respuesta automática |
|---|---|
| `ficha`, `FICHA`, `nota` | Perfecto. Dinos el nombre de tu negocio y la localidad, y te mandamos su nota y tres cosas que se pueden mejorar. Lo mira una persona del equipo, así que puede tardar unas horas. |
| `precio`, `cuánto`, `cuanto` | En beta, el plan Pro es gratis hasta la versión definitiva. Después cuesta 35 € al año por ficha, y tienes 30 días para decidir si sigues. |
| `beta` | La beta es para bares, restaurantes y negocios locales. El plan Pro es gratis hasta la versión definitiva. Dinos tu negocio y la localidad y te contamos cómo entrar. |

#### Preguntas recomendadas

Las mismas cuatro que las preguntas frecuentes de Instagram (consulta «Preguntas frecuentes del chat»).

### Lo que no usamos, de momento

| Opción de TikTok | Decisión | Por qué |
|---|---|---|
| **Consigue clientes potenciales** (formularios) | No | Los datos los recoge TikTok, fuera de nuestra web, y nuestra política de privacidad no cubre ese tratamiento. Además, perderíamos la medición del diagnóstico. Quien quiere su nota la pide por mensaje o en `fichaitor.es`, que ya tiene su texto de consentimiento. Si algún día lo usamos, antes hay que pedir a la sesión de textos legales que lo añada a la política |
| **TikTok Ads Manager** («Empezar») | No lo abras | Sin anuncios por ahora (decisión del 2026-11-13). Al darte de alta te pide un método de pago. Cuando un vídeo funcione en orgánico, te propongo una prueba con presupuesto y la decides tú |
| **Solicitudes de colaboración** | No | Es la red de socios de TikTok para agencias y desarrolladores, no para anunciantes |

## Textos para responder a mano

Para los mensajes que la respuesta automática no cubre, solo a quien nos ha escrito. Guárdalos en las notas del móvil. En Instagram se guardan como **respuestas guardadas**, con el atajo de la primera columna.

| Atajo | Texto |
|---|---|
| `/nota` | Aquí tienes la nota de la ficha de [negocio]: [nota] sobre 100. Lo que más se nota desde fuera: [fallo 1], [fallo 2] y [fallo 3]. Si quieres, te contamos cómo te avisamos de todo esto. |
| `/precio` | Ahora estamos en beta y el plan Pro es gratis hasta la versión definitiva. Después cuesta 35 € al año por ficha, y tienes 30 días para decidir si sigues. |
| `/acceso` | Fichaitor no cambia nada en tu ficha sin que lo confirmes, y puedes desconectarlo cuando quieras desde tu cuenta de Google. |

Para sacar la nota de quien la pide: escribe su negocio y su localidad en `https://fichaitor.es/?s=tt_dm` (o `?s=ig_dm` si llega por Instagram), haz una captura del resultado y mándasela con el texto `/nota`. Son 2 minutos.

---

## Instagram

### Perfil

| Campo | Texto |
|---|---|
| Nombre (sale en las búsquedas, 64 caracteres) | `Fichaitor · Tu ficha de Google al día` |
| Usuario | `fichaitor` |
| Biografía (150 caracteres) | Ver abajo |
| Enlace | `https://fichaitor.es/?s=ig_bio`, con el título «Mira la nota de tu ficha» |
| Categoría | «Software» o «Programa informático» |
| Mostrar la categoría | Sí |
| Botón de contacto | Correo: `soporte@fichaitor.es`. Sin teléfono |
| Pronombres | Vacío |

Biografía, en cuatro líneas (122 caracteres):

```text
Tu ficha de Google, al día.
Te avisamos de cada reseña y de cada festivo.
Tú confirmas.
Mira la nota de tu ficha, gratis →
```

### Tipo de cuenta

**Configuración → Tipo de cuenta y herramientas → Cambiar a cuenta profesional → Empresa.** Entre «Creador» y «Empresa», elige Empresa: da la categoría, el botón de correo y las preguntas frecuentes del chat.

### Ajustes

| Ajuste | Valor | Por qué |
|---|---|---|
| Cuenta privada | No | Nadie puede encontrarnos si no |
| Comentarios | Todos, con el filtro de palabras ofensivas activado y las mismas palabras que en TikTok | Igual que en TikTok |
| Mensajes | Todos | La llamada a la acción es «Escríbenos FICHA» |
| Compartir en historias y reutilizar reels (remix) | Activado | Visibilidad gratis |
| Conectar con Facebook | No, de momento | No hay anuncios y es una cuenta más que vigilar |

### Preguntas frecuentes del chat

En **Herramientas para empresas → Preguntas frecuentes**. Aparecen como botones cuando alguien abre un chat con nosotros.

| Pregunta | Respuesta |
|---|---|
| ¿Qué es Fichaitor? | Te avisamos de cada reseña nueva de tu ficha de Google con la respuesta ya escrita, y antes de cada festivo te preguntamos si abres. Tú confirmas y lo dejamos puesto. |
| ¿Cuánto cuesta? | En beta, el plan Pro es gratis hasta la versión definitiva. Después, 35 € al año por ficha, y tienes 30 días para decidir si sigues. |
| ¿Me miráis mi ficha? | Sí. Dinos el nombre de tu negocio y la localidad, y te mandamos su nota y tres cosas que se pueden mejorar. |
| ¿Publicáis algo sin preguntarme? | No. Nada cambia en tu ficha sin tu confirmación. |

### Respuestas guardadas

**Herramientas para empresas → Respuestas guardadas:** los tres de «Textos para responder a mano» (`/nota`, `/precio` y `/acceso`).

### Destacadas

Más adelante, cuando haya historias que guardar. Serán cuatro: «Cómo va», «Festivos», «Reseñas» y «Beta». Las portadas las genero yo con la marca.

---

## Tu marca personal

Una línea en tu biografía personal de TikTok e Instagram, cuando estén publicados los primeros vídeos:

```text
Ahora llevando @fichaitor
```

Comparte en tus historias cada vídeo de Fichaitor. **No publiques los vídeos en tu cuenta personal en lugar de la de Fichaitor**: queremos que los seguidores, los mensajes «FICHA» y las cifras se queden en la cuenta de la marca.

## YouTube

Para cuando subamos los Shorts. Reserva ya el identificador `@fichaitor` en YouTube, aunque no publiques nada, para que nadie lo ocupe. Tiempo: 5 minutos.

## Cambios al abrir la beta

Cuando se pueda dar de alta un bar en `fichaitor.es/app`:

| Dónde | Ahora | Pasa a |
|---|---|---|
| TikTok, biografía | `…Tú confirmas.` | `Tu ficha de Google, al día. Reseñas y festivos. Beta gratis para bares.` |
| Instagram, última línea | `Mira la nota de tu ficha, gratis →` | `Beta gratis para bares y restaurantes →` |
| Instagram, título del enlace | «Mira la nota de tu ficha» | «Entra en la beta» |

Te avisaré con el texto exacto el día que toque.

## Tu checklist de PM

1. **Completa TikTok:** cuenta de empresa, foto, biografía, enlace, ajustes y el centro de mensajes (enlace de mensaje directo, instrucciones, bienvenida, las tres palabras clave y las preguntas). Tiempo: 20 min.
2. **Crea y completa Instagram** con su apartado, y activa la verificación en dos pasos en las dos. Tiempo: 20 min.
3. **Reserva `@fichaitor` en YouTube** y pásame el usuario exacto que te ha quedado en cada red. Tiempo: 5 min.
