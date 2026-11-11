# Diseño UI/UX conversacional

## 1. Personalidad

El agente es **el experto de confianza que lleva la ficha del negocio**: cercano, directo y con antelación. No es un bot de marketing.

| Rasgo | Significa | No significa |
|---|---|---|
| Cercano | Tutea, frases cortas, lenguaje del dueño | Emojis en cada frase, exclamaciones |
| Experto | Da el motivo en una línea ("los posts con oferta duran más visibles") | Jerga de SEO, lecciones no pedidas |
| Proactivo | Avisa antes de que haga falta | Insistir o enviar por enviar |
| Prudente | Confirma antes de cambiar nada | Pedir permiso para cada detalle |

## 2. Reglas de mensaje

1. **Una idea por mensaje.** Lo primero que se lee es la pregunta o la acción.
2. **Máximo 3 líneas** antes de los botones. Los detalles van en el resumen de confirmación.
3. **Siempre hay salida fácil:** un botón para descartar.
4. **Fechas completas** en las confirmaciones: "viernes 27 de noviembre", no "el viernes".
5. **Nunca más de 2 aclaraciones** seguidas. A la tercera, opciones cerradas.
6. **Cada confirmación dice qué va a pasar y dónde:** "Lo publico en tu ficha ahora."
7. **Después de ejecutar, prueba:** enlace a la ficha o al post.

## 3. Antelación y frecuencia

| Tipo | Primer aviso | Recordatorio | Caduca |
|---|---|---|---|
| Festivo | 5 días antes | 1 día antes | El mismo día |
| Fecha comercial (Black Friday, rebajas) | 14 días antes | 7 días antes | 2 días antes |
| Temporada (verano, Navidad) | 21 días antes | 10 días antes | Al empezar |
| Reseña | Al momento (en horario) | A las 24 horas | A los 7 días |
| Mejora de la ficha | Semanal, si no hay nada más | No | A los 7 días |

- **Límite:** 2 mensajes proactivos por semana, sin contar reseñas. Configurable.
- **Horario de silencio:** nada entre las 21:00 y las 9:00, ni en las horas punta del sector (un restaurante no recibe avisos a las 14:00).
- **Agrupar:** si coinciden un festivo y una fecha comercial, un solo mensaje.
- **Reseñas en ráfaga:** más de 3 en un día se envían como resumen.

## 4. Patrones de conversación

### Fecha comercial

> **Agente:** Se acerca Black Friday (viernes 27). ¿Tienes alguna oferta especial o un horario distinto?
> `Tengo oferta` `Cambio horario` `Nada este año`
>
> **Dueño:** 20% en todo el fin de semana y el viernes abrimos hasta las 22
>
> **Agente:** Perfecto. Esto es lo que haría:
> • Post de oferta: 20% en todo, del viernes 27 al domingo 29.
> • Horario del viernes 27: cierre a las 22:00.
> ¿Lo publico?
> `Publicar` `Cambiar` `Cancelar`
>
> **Dueño:** *Publicar*
>
> **Agente:** Hecho. La oferta ya está en tu ficha y el horario del viernes, actualizado. [Ver ficha]

### Festivo

> **Agente:** El martes 8 de diciembre es festivo. ¿Abrís?
> `Abro normal` `Cierro` `Otro horario`

### Reseña

> **Agente:** Nueva reseña de Marta, 5★: "El arroz, espectacular."
> Respuesta propuesta: "¡Gracias, Marta! Nos alegra que disfrutaras el arroz. Te esperamos pronto."
> `Publicar` `Editar` `No responder`

### Orden del dueño

> **Dueño:** hoy cierro, estoy malo
>
> **Agente:** Que te mejores. Marco hoy, miércoles 11, como cerrado en tu ficha. ¿Confirmas?
> `Sí, cerrar hoy` `No`

### Consulta al experto

> **Dueño:** por qué no salgo cuando buscan pizzería
>
> **Agente:** Tu categoría principal es "Restaurante" y la palabra pizzería no aparece ni en la categoría ni en los servicios. Es lo primero que mira Google. ¿Cambio la categoría principal a "Pizzería"?
> `Sí, cambiar` `Ahora no`

## 5. Componentes por canal

| Componente | WhatsApp | Telegram | Email | Panel |
|---|---|---|---|---|
| Respuesta rápida | Hasta 3 botones, títulos de 20 caracteres como máximo | Botones en línea | Enlaces firmados | Botones |
| Más de 3 opciones | Lista | Botones en filas | Enlaces | Selector |
| Vista previa | Texto e imagen | Texto e imagen | HTML | Tarjeta |
| Entrada libre | Texto, audio, foto | Texto, audio, foto | Respuesta al correo | Formulario |
| Mensaje iniciado por el agente fuera de las 24 horas | Plantilla aprobada | Libre | Libre | Notificación |

Los límites de WhatsApp (número de botones, longitud, categorías de plantilla y precio) se verifican en la fase 0.

### Plantillas iniciales de WhatsApp

| Plantilla | Uso |
|---|---|
| `key_date_v1` | Aviso de fecha clave con 3 botones |
| `new_review_v1` | Reseña nueva con respuesta propuesta |
| `reminder_v1` | Recordatorio de una propuesta pendiente |
| `reconnect_google_v1` | El acceso a Google ha caducado |
| `weekly_summary_v1` | Resumen semanal |

## 6. Panel web

El panel repite lo que se puede hacer por WhatsApp y añade lo que no cabe en un chat. Diseño para móvil primero.

| Pantalla | Contenido |
|---|---|
| **Inicio** | Nota de la ficha, pendientes de tu respuesta, lo último que se ha hecho |
| **Bandeja** | Propuestas abiertas con sus botones |
| **Reseñas** | Todas, con estado, respuesta y termómetro de temas |
| **Publicaciones** | Propuestas, publicadas e historial |
| **Calendario** | Próximas fechas clave y lo que ya está preparado |
| **Rendimiento** | Búsquedas, llamadas, rutas, clics; comparación con el mes anterior |
| **Lo que he aprendido** | Reglas de voz que el agente ha deducido, para aprobar o retirar |
| **Ajustes** | Modo proponer o automático, canales, horario de silencio, plan y facturación |

### Estados que toda pantalla define

Vacío, cargando, error, sin conexión con Google y plan sin esa función.

## 7. Accesibilidad e idioma

- Contraste AA, objetivos táctiles de 44 px, navegación por teclado en el panel.
- Español de España en el lanzamiento; textos en catálogo para añadir idiomas.
- El agente responde en el idioma en que le escriba el dueño; las publicaciones salen en el idioma de la ficha.
