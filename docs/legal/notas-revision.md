# Notas para la revisión de los textos legales

Notas internas. No se publican. Recogen de dónde sale cada afirmación delicada de `privacidad.md` y qué debe mirar quien revise los textos. Los textos siguen siendo borradores hasta esa revisión.

## Decisiones del propietario

| Fecha | Decisión |
|---|---|
| 2026-11-11 | Responsable: BITCLICK SOLUTIONS S.L.U., con los datos de `bitclick.solutions/privacidad`. Correo: `soporte@fichaitor.es` |
| 2026-11-11 | Lista de espera: 12 meses o hasta el lanzamiento. Texto de consentimiento `waitlist-2026-11-11` |
| 2026-11-11 | Proveedor del modelo de IA: Google, API de Gemini, con clave de un proyecto con facturación activa (confirmado a través de la sesión de backend) |
| 2026-11-12 | Alojamiento: IONOS Cloud S.L.U., Madre de Dios nº 21, 26004 Logroño (La Rioja) |
| 2026-11-12 | Base jurídica de la prueba con una captura de reseñas: interés legítimo (art. 6.1.f) |
| 2026-11-12 | Mecanismo de transferencia de Google: el del Anexo de Tratamiento de Datos de Cloud (clientes) |
| 2026-11-12 | Correo: también IONOS. Hoy aloja el buzón `soporte@fichaitor.es`; enviará los avisos por correo cuando esa función se active |

## Fuentes consultadas

| Afirmación en la política | Fuente | Consultada |
|---|---|---|
| En el servicio de pago, Google no usa lo enviado ni las respuestas para mejorar sus productos | Condiciones adicionales de la API de Gemini, `https://aistudio.google.com/docs/terms`, en vigor desde el 23 de marzo de 2026 | 2026-11-11 |
| Google registra lo enviado y las respuestas durante un periodo limitado, solo para detectar usos prohibidos | La misma. No dice cuánto dura ese periodo | 2026-11-11 |
| Para usuarios del Espacio Económico Europeo solo se pueden usar los servicios de pago | La misma | 2026-11-11 |
| Los datos pueden tratarse en cualquier país donde Google o sus subencargados tengan instalaciones | Anexo de Tratamiento de Datos de Cloud (clientes), sección 10.1, `https://services.google.com/fh/files/misc/es-419-cloud-data-processing-addendum-customers.pdf` | 2026-11-12 |
| Orden de las garantías de transferencia: país adecuado, marco de protección de datos adoptado y, si no, Cláusulas Contractuales Tipo | El mismo anexo, sección 4.1 de las condiciones sobre la ley europea | 2026-11-12 |

## Puntos que debe mirar quien revise

1. **Qué anexo de Google se aplica a la API de Gemini.** Las condiciones de la API de Gemini remiten al «Google Data Processing Addendum for Products Where Google is a Data Processor» (`https://business.safety.google/processorterms/`, versión 10, 7 de mayo de 2026). El propietario ha indicado el Anexo de Tratamiento de Datos de Cloud (clientes). Son documentos distintos. Los dos fijan el mismo orden de garantías de transferencia y la misma regla de ubicación, que es lo único que afirma la política, así que el texto vale con cualquiera de los dos. Conviene confirmar cuál es el aplicable según cómo esté contratada la clave (Google AI Studio o Google Cloud).
2. **Entidad de Google que contrata.** Ninguno de los dos anexos la nombra: remiten a «la entidad de Google que es parte del acuerdo». La política dice «la entidad del grupo Google con la que está contratado el servicio». El nombre exacto aparece en las facturas de Google Cloud.
3. **Interés legítimo en la prueba con captura.** La captura incluye el nombre y la foto de perfil de los autores de las reseñas, y la imagen completa llega a Google. A favor del interés legítimo: las reseñas son públicas, las sube el titular del negocio al que se refieren, Fichaitor no guarda la imagen ni devuelve esos datos, y el tratamiento dura segundos. Falta documentar la ponderación (interés, necesidad y equilibrio con los derechos de los autores), que el RGPD exige para esta base.
4. **Encargo de tratamiento con Google para Gemini.** Es un tratamiento nuevo respecto de la conexión con Google Business Profile.
5. **Funciones previstas.** La política describe en futuro los avisos por correo, Telegram y WhatsApp, los pagos con Stripe, Google Analytics y el píxel de TikTok. Ninguno está activo. Antes de activar cada uno hay que actualizar la política, y para la analítica y el píxel hacen falta un banner de consentimiento y una política de cookies.
6. **Referencia del propietario:** las condiciones de Opinas (`https://opinas.app/es/aviso-legal`) usan el mismo reparto que este borrador: responsable para la relación con el cliente y encargado para los datos de reseñas y perfiles conectados. Su política de privacidad no se ha podido consultar.
