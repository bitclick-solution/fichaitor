# Fase 0.2 — Matriz de la API de Google, verificada

Pruebas reales contra la ficha de Bitclick Solutions el 2026-11-11, con el proyecto de Google Cloud `544696777336` (el de GBP Autopilot) y el permiso `business.manage`.

Estado: **casi completa**. Falta responder a una reseña, la carta de un restaurante, el aviso por Pub/Sub y las búsquedas con una ficha con tráfico. Las tres primeras necesitan otra ficha, porque la de Bitclick no tiene reseñas ni es de restauración.

## Resultados

| Función | Endpoint | Resultado | Notas |
|---|---|---|---|
| Leer la ficha | Business Information `GET locations/{id}?readMask=…` | 200 | Devuelve horario, categorías, servicios, descripción, teléfono, web y metadatos |
| Leer atributos | Business Information `GET locations/{id}/attributes` | 200 | Incluye el enlace de WhatsApp de la ficha |
| Detectar cambios hechos por Google | Business Information `GET locations/{id}:getGoogleUpdated` | 200 | Base para el aviso "Google ha cambiado tu ficha" |
| Buscar categorías | Business Information `GET categories?filter=displayName=…` | 200 | Sirve para proponer categoría ("pizzería" devuelve 2) |
| **Poner horario especial** | Business Information `PATCH locations/{id}?updateMask=specialHours` | 200 | Probado con un día cerrado (2027-08-15) |
| **Quitar horario especial** | El mismo `PATCH` con la lista vacía | 200 | La ficha quedó como estaba; comprobado con una lectura posterior |
| Búsquedas por las que te encuentran | Performance `searchkeywords/impressions/monthly` | 200, vacío | La ficha de Bitclick no tiene volumen suficiente. Hay que repetirlo con una ficha con tráfico |
| Leer reseñas | My Business v4 `reviews` | 200, vacío | La ficha de Bitclick no tiene reseñas. Responder queda sin probar |
| Leer posts | My Business v4 `localPosts` | 200 | Devuelve tipo (`STANDARD`, `OFFER`), estado, botón de acción y, en ofertas, fechas y condiciones |
| **Crear post** | My Business v4 `POST localPosts` | 200 | Nace en estado `PROCESSING`; no es visible al instante |
| **Borrar post** | My Business v4 `DELETE localPosts/{id}` | 200 | Borrado 3 segundos después; la lista posterior no lo incluye |
| Leer fotos | My Business v4 `media` | 200 | 3 elementos, con dimensiones y categoría |
| **Subir foto** | My Business v4 `POST media` con `sourceUrl` | 200 | La foto se sube desde una URL pública, no como archivo |
| **Borrar foto** | My Business v4 `DELETE media/{id}` | 200 | La lista posterior vuelve a tener 3 elementos |
| Carta | My Business v4 `foodMenus` | 400 | El endpoint existe; rechaza la ficha porque su categoría no es de restauración. Hay que probarlo con un restaurante |
| Notificaciones (Pub/Sub) | Notifications `notificationSetting` | 200 | Habilitada el 2026-11-11. Sin tema de Pub/Sub configurado todavía |
| Estado de verificación | Verifications `VoiceOfMerchantState` | 200 | Habilitada el 2026-11-11. Devuelve si la ficha está verificada y con autoridad |
| Preguntas y respuestas | Q&A `questions` | 404 | La API ya no existe. No se puede ofrecer |

## Hallazgos

1. **Las fechas clave y "hoy cierro" son viables.** Poner y quitar un horario especial funciona y la respuesta llega en unos 3 segundos.
2. **Los cambios pasan por revisión de Google.** Tras la escritura, la ficha devolvió `hasPendingEdits: true`. El cambio puede tardar en verse. El agente debe decir "enviado a Google", no "ya está publicado", y comprobarlo después.
3. **Con la app en modo "Testing", los tokens caducan a los 7 días.** La credencial de GBP Autopilot dejó de funcionar una semana después de crearse. Para el producto, la app tiene que estar en producción.
4. **El acceso a la API es por proyecto de Google Cloud.** El proyecto nuevo y exclusivo de este producto tiene que pedir su propio acceso a la API de Business Profile (el mismo formulario que se rellenó para `544696777336`) y habilitar todas las API de la tabla. Hasta entonces su cuota es cero. Esto se suma a la verificación de la pantalla de consentimiento: son dos trámites distintos.
5. **Las reseñas, los posts y las fotos dependen de la API v4**, que no está habilitada en el proyecto actual. Nada de lo que GBP Autopilot tenía previsto publicar habría funcionado.
6. **Preguntas y respuestas queda fuera del producto**, confirmado.

7. **Revertir no es instantáneo en el lado de Google.** Ocho minutos después de quitar el horario especial, la lectura de la ficha ya no lo tenía, pero `getGoogleUpdated` seguía devolviendo el día cerrado con `diffMask: specialHours` y la ficha marcaba `hasGoogleUpdated: true`. Hay dos versiones de cada ficha (la del propietario y la que Google publica) y tardan en converger. El producto necesita un job de comprobación posterior a cada cambio. Pendiente de volver a leer para confirmar que converge sola.
8. **La API v4 (`mybusiness.googleapis.com`) es distinta de "Business Profile Performance API".** En la biblioteca aparece como "Google My Business API" y a veces no sale en el buscador; se habilita desde su enlace directo. Si el botón no aparece, el proyecto no tiene concedido ese acceso.
9. **Preguntas y respuestas:** aunque "My Business Q&A API" sigue listada en la biblioteca, el endpoint devuelve el 404 genérico de Google y no el 403 de "API no habilitada" que dan las demás. Indica que el servicio ya no existe.

10. **Lecturas en PubliExpe (solo lectura, autorizadas el 2026-11-11).** Tiene 13 reseñas, media de 4,7, y **ninguna respondida**, incluida una de 1 estrella. Es el caso de venta del producto en una ficha real. Las reseñas con texto llegan con el formato "(Translated by Google) … (Original) …": hay que quedarse con el original.
11. **Las búsquedas funcionan con tráfico.** PubliExpe devuelve 7 términos en tres meses. Google solo da la cifra exacta por encima de un umbral (la marca, 93); el resto llega como "menos de 15". El informe debe decir "menos de 15", no inventar un número.
12. **Preguntas y respuestas, descartado definitivamente.** Con la API habilitada sigue devolviendo 404 en las dos fichas.

## Pendiente

- Confirmar que la versión de Google de la ficha de Bitclick deja de mostrar el día cerrado del 2027-08-15 (seguía ahí 11 minutos después de quitarlo).
- Respuesta a reseñas: necesita una ficha con reseñas. Se probará reenviando el mismo texto sobre una reseña ya respondida, porque responder avisa al autor y no se puede deshacer.
- Carta: necesita una ficha de restauración.
- Las fotos se suben por URL pública: el producto necesita almacenamiento con enlaces accesibles para Google (afecta al HLD).
- Notificaciones: crear un tema de Pub/Sub y comprobar que llega el aviso de reseña nueva.
- Repetir las búsquedas con una ficha con tráfico (PubliExpe).

## Cómo se hicieron las pruebas

Dos workflows manuales en n8n, sin publicar, que usan la credencial `GBP OAuth`:

- `[GBP] 28 - Matriz API (fase 0 agente-gbp)` (`EcgD3OyXopQK8uQD`): 12 lecturas.
- `[GBP] 29 - Matriz API escrituras (fase 0 agente-gbp)` (`XBA6h8tpagxvopmG`): horario especial, poner y quitar.

Se archivan al cerrar la fase 0.
