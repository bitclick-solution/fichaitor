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
| 2026-11-12 | La clave de la API de Gemini se creó en Google AI Studio (confirmado a través de la sesión de backend). Valora pasarla a Google Cloud (Vertex AI) antes de abrir al público, para fijar región europea |
| 2026-11-12 | Activas en local, según la sesión de backend: guardar las reseñas de la ficha conectada sin autor, responderlas con registro de cada respuesta confirmada, y el análisis de lo que dicen (Gemini clasifica cada reseña; la nota la calcula el código) |
| 2026-11-12 | Plazos de conservación aprobados: revocar el acceso en Google al desconectar; borrar en 30 días como máximo todo lo derivado de Google; registro de respuestas publicadas, 12 meses desde la baja; sesiones caducadas, 30 días; lista de espera, tope de 12 meses |
| 2026-11-12 | Baja: «Desconectar Google» y «Borrar mi cuenta» piden un código de un solo uso enviado al correo de la cuenta, avisan por correo y dejan 48 horas para restaurar; pasado ese plazo, se elimina. Motivo: que una sesión abierta en un ordenador ajeno no baste para borrar nada |
| 2026-11-12 | Correos automáticos: remitente `avisos@fichaitor.es` (IONOS), nombre «Fichaitor», respuestas a `soporte@fichaitor.es`. Confirmado a través de la sesión de backend. En la política sigue como función prevista hasta que la baja esté construida |
| 2026-11-12 | Mensajes del chat de Telegram y WhatsApp (propuestas enviadas y respuestas del dueño): 90 días. Aprobado por el propietario en la sesión de backend. Función prevista: el canal aún no está en producción |
| 2026-11-12 | Finalidad del negocio buscado y la nota en la lista de espera: personalizar el aviso de lanzamiento (confirmado por el propietario, transmitido por la sesión de backend). El recuento de tipos de negocio se limita a totales anónimos para que quepa en el consentimiento actual («avisarte del lanzamiento»); un análisis con datos identificables necesitaría una versión nueva del texto de la casilla. Confirmado directamente por el propietario |
| 2026-11-12 | B7 de la revisión de seguridad (backend): la lista de espera guarda también el negocio buscado (texto escrito y place_id) y la nota del diagnóstico; el contacto se confirma antes del primer aviso; al reconectar, los tokens anteriores se borran tras la primera lectura correcta; una ficha a la que Google niega el acceso deja de mostrarse y se borra a los 30 días. Nueva marca [REVISAR] sobre la finalidad de guardar el negocio y la nota. El texto de la casilla no cambia: sigue waitlist-2026-11-11 |
| 2026-11-12 | Según backend (commit 746d028), la baja está construida: código por correo, 48 horas para restaurar, revocación del acceso en Google al hacerse efectiva, borrado en 30 días desde entonces, registro de respuestas 12 meses, sesiones 30 días, lista de espera 12 meses. El correo sale de avisos@fichaitor.es por IONOS. Quedan marcas [REVISAR] en lo que aún no hace: borrar al retirar el acceso desde Google, bajas por correo, baja de los canales, registros a 6 meses, copias de seguridad y la base jurídica del registro de respuestas |
| 2026-11-12 | Correo: también IONOS. Hoy aloja el buzón `soporte@fichaitor.es`; enviará los avisos por correo cuando esa función se active |

## Fuentes consultadas

| Afirmación en la política | Fuente | Consultada |
|---|---|---|
| En el servicio de pago, Google no usa lo enviado ni las respuestas para mejorar sus productos | Condiciones adicionales de la API de Gemini, `https://aistudio.google.com/docs/terms`, en vigor desde el 23 de marzo de 2026 | 2026-11-11 |
| Google registra lo enviado y las respuestas durante un periodo limitado, solo para detectar usos prohibidos | La misma. No dice cuánto dura ese periodo | 2026-11-11 |
| Para usuarios del Espacio Económico Europeo solo se pueden usar los servicios de pago | La misma | 2026-11-11 |
| Los datos pueden tratarse en cualquier país donde Google o sus subencargados tengan instalaciones | Anexo de Tratamiento de Datos de Cloud (clientes), sección 10.1, `https://services.google.com/fh/files/misc/es-419-cloud-data-processing-addendum-customers.pdf` | 2026-11-12 |
| Orden de las garantías de transferencia: país adecuado, marco de protección de datos adoptado y, si no, Cláusulas Contractuales Tipo | El mismo anexo, sección 4.1 de las condiciones sobre la ley europea | 2026-11-12 |
| Las dos afirmaciones anteriores, en el anexo que aplica a una clave de Google AI Studio | «Google Data Processing Addendum for Products Where Google is a Data Processor», versión 10, 7 de mayo de 2026, `https://business.safety.google/processorterms/`: sección 10.1 (ubicación) y apéndice 3A, sección 7 (transferencias) | 2026-11-12 |

## Puntos que debe mirar quien revise

1. **Qué anexo de Google se aplica a la API de Gemini.** Resuelto: la clave es de Google AI Studio, así que aplica el «Google Data Processing Addendum for Products Where Google is a Data Processor», al que remiten las condiciones de la API de Gemini, y no el Anexo de Tratamiento de Datos de Cloud que indicó el propietario. Los dos fijan el mismo orden de garantías de transferencia y la misma regla de ubicación, que es lo único que afirma la política, así que el texto no cambia. Si la clave pasa a Google Cloud (Vertex AI), aplicará el anexo de Cloud y habrá que revisar el apartado 5: cambian las condiciones de uso de los datos y se podrá fijar una región europea.
2. **Entidad de Google que contrata.** Ninguno de los dos anexos la nombra: remiten a «la entidad de Google que es parte del acuerdo». La política dice «la entidad del grupo Google con la que está contratado el servicio». El nombre exacto aparece en las facturas de Google Cloud.
3. **Interés legítimo en la prueba con captura.** La captura incluye el nombre y la foto de perfil de los autores de las reseñas, y la imagen completa llega a Google. A favor del interés legítimo: las reseñas son públicas, las sube el titular del negocio al que se refieren, Fichaitor no guarda la imagen ni devuelve esos datos, y el tratamiento dura segundos. Falta documentar la ponderación (interés, necesidad y equilibrio con los derechos de los autores), que el RGPD exige para esta base.
4. **Encargo de tratamiento con Google para Gemini.** Es un tratamiento nuevo respecto de la conexión con Google Business Profile.
5. **Funciones previstas.** La política describe en futuro los avisos por correo, Telegram y WhatsApp, los pagos con Stripe, Google Analytics y el píxel de TikTok. Ninguno está activo. Antes de activar cada uno hay que actualizar la política, y para la analítica y el píxel hacen falta un banner de consentimiento y una política de cookies.
6. **Referencia del propietario:** las condiciones de Opinas (`https://opinas.app/es/aviso-legal`) usan el mismo reparto que este borrador: responsable para la relación con el cliente y encargado para los datos de reseñas y perfiles conectados. Su política de privacidad no se ha podido consultar.

## Bloqueo de publicación: baja y conservación (2026-11-12)

La política promete que, al desconectar Google, se borran los tokens al momento y los datos en 30 días. Según la sesión de backend, a esta fecha el sistema no lo hace:

- No hay forma de desconectar la ficha ni de borrar la cuenta, y no se revoca el token en Google.
- No hay ninguna tarea de retención: tokens cifrados, reseñas, clasificaciones, análisis, historial de notas, sesiones caducadas y lista de espera se conservan sin límite.
- El registro de respuestas publicadas es de solo inserción.
- Las reseñas que Google deja de devolver se marcan como retiradas, no se borran.

`docs/legal/privacidad.md` lleva una marca `[REVISAR]` en «Cómo revocar el acceso y borrar los datos». Con ella, la compilación en modo estricto falla y la web no se puede publicar. Se cierra cuando:

1. El backend haya construido «desconectar y borrar» y la tarea de retención.
2. Plazos: aprobados por el propietario el 2026-11-12 (ver «Decisiones del propietario») y ya escritos en la política. Falta que quien revise los textos confirme la base jurídica del registro de respuestas (propuesta: interés legítimo en la defensa frente a reclamaciones, art. 6.1.f).
3. El apartado 3 y la tabla del apartado 2 de la política digan exactamente lo que hace el sistema.

Las Condiciones de uso remiten a esos plazos («borramos esos datos en los plazos indicados en la Política de Privacidad»), así que quedan cubiertas por el mismo cierre.
