# Fase 0 — Coste de cada diagnóstico en Google Places

Investigación documental del 2026-11-11. Sin factura real todavía: las cifras salen de la lista de precios oficial, no de un uso medido.

## Resumen

- Cada diagnóstico se factura hoy como **Text Search Enterprise + Atmosphere: 40 USD por 1.000 peticiones** (0,04 USD por diagnóstico). Las primeras 1.000 del mes son gratis.
- Con una búsqueda en dos pasos baja a **25 USD por 1.000** (un 37,5 % menos) sin perder ninguna regla.
- El tope de gasto se pone con una **cuota**, no con un presupuesto: el presupuesto solo avisa.
- **Bloqueante antes de abrir al público:** Bitclick factura desde España, así que aplican los términos del EEE. Esos términos limitan el contenido de Places a nueve usos permitidos y un diagnóstico público de fichas ajenas no encaja con claridad en ninguno. Consulta la [sección 5](#5-términos-de-uso-qué-se-puede-guardar).

Todos los precios están en dólares (USD), como en la lista oficial. El cambio a euros de la factura está sin confirmar.

## 1. A qué SKU se factura la petición

La petición es `POST https://places.googleapis.com/v1/places:searchText` con la máscara de `app/adapters/places/google.py`. Google factura **al SKU más alto** de los campos pedidos: "You are then billed at the highest SKU applicable to your request" ([uso y facturación de Places](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)).

| Campo de la máscara | SKU de Text Search |
|---|---|
| `id` | Essentials (IDs Only) |
| `displayName`, `formattedAddress`, `businessStatus`, `photos.name`, `primaryType`, `primaryTypeDisplayName`, `types`, `googleMapsUri` | Pro |
| `rating`, `userRatingCount`, `regularOpeningHours.periods`, `websiteUri`, `nationalPhoneNumber` | Enterprise |
| `reviews.publishTime`, `delivery`, `takeout`, `dineIn`, `reservable`, `outdoorSeating`, `servesBreakfast`, `servesLunch`, `servesDinner` | **Enterprise + Atmosphere** |

Fuente de la asignación: [campos de datos de Places (New)](https://developers.google.com/maps/documentation/places/web-service/data-fields), columna "Text Search SKU".

Resultado: **Places API Text Search Enterprise + Atmosphere** (SKU `120C-BEC3-B48F`). Nueve campos de la máscara suben la petición a esta categoría.

> **Nota:** La documentación asigna SKU al campo `reviews`. No dice de forma expresa que un subcampo como `reviews.publishTime` se facture igual. Se asume que sí. Sin confirmar hasta ver la primera factura.

### Precios, franja gratuita y descuentos por volumen

Precio por 1.000 peticiones, en USD. Fuente: [lista de precios global de Google Maps Platform](https://developers.google.com/maps/billing-and-pricing/pricing).

| SKU | Gratis al mes | Hasta 100.000 | 100.001–500.000 | 500.001–1.000.000 | 1.000.001–5.000.000 | Más de 5.000.000 |
|---|---|---|---|---|---|---|
| Text Search Essentials (IDs Only) | Ilimitado | — | — | — | — | — |
| Text Search Pro | 5.000 | 32,00 | 25,60 | 19,20 | 9,60 | 2,40 |
| Text Search Enterprise | 1.000 | 35,00 | 28,00 | 21,00 | 10,50 | 2,63 |
| **Text Search Enterprise + Atmosphere** | **1.000** | **40,00** | **32,00** | **24,00** | **12,00** | **3,40** |
| Place Details Essentials (IDs Only) | Ilimitado | — | — | — | — | — |
| Place Details Essentials | 10.000 | 5,00 | 4,00 | 3,00 | 1,50 | 0,38 |
| Place Details Pro | 5.000 | 17,00 | 13,60 | 10,20 | 5,10 | 1,28 |
| Place Details Enterprise | 1.000 | 20,00 | 16,00 | 12,00 | 6,00 | 1,51 |
| Place Details Enterprise + Atmosphere | 1.000 | 25,00 | 20,00 | 15,00 | 7,50 | 2,28 |
| Autocomplete Requests | 10.000 | 2,83 | 2,27 | 1,70 | 0,85 | 0,21 |
| Autocomplete Session Usage | Ilimitado | — | — | — | — | — |

Reglas que afectan al cálculo:

- La franja gratuita es **por SKU y por mes**. No hay crédito mensual común.
- El uso se suma **por cuenta de facturación**, no por proyecto: "Google aggregates service usage for all projects linked to the customer billing account". Si el proyecto de GBP Autopilot comparte cuenta de facturación y usa el mismo SKU, comparte las 1.000 gratuitas.
- El descuento por volumen empieza en la petición 100.001. Con 100.000 diagnósticos al mes no se llega.

## 2. Coste mensual

Fórmula: `coste = max(0, peticiones facturables − 1.000) × 0,040 USD`.

### Sin caché

| Diagnósticos al mes | Peticiones facturables | Coste (USD) |
|---|---|---|
| 1.000 | 0 | 0 |
| 10.000 | 9.000 | 360 |
| 100.000 | 99.000 | 3.960 |

### Con la caché de 24 horas

La caché evita la petición solo cuando se repite **la misma búsqueda** en 24 horas. El porcentaje de repeticiones no está medido. La tabla usa dos supuestos, sin datos que los respalden.

| Diagnósticos al mes | Sin caché | 10 % repetidos (supuesto) | 30 % repetidos (supuesto) |
|---|---|---|---|
| 1.000 | 0 | 0 | 0 |
| 10.000 | 360 | 320 | 240 |
| 100.000 | 3.960 | 3.560 | 2.760 |

Qué esperar en el experimento de TikTok: cada dueño busca su propio negocio, así que las repeticiones serán pocas. La caché protege sobre todo de recargas y de un vídeo que enseñe siempre el mismo negocio.

Dos límites de la caché actual (`app/application/diagnosis_service.py`):

- **La clave es el texto de la búsqueda, no el negocio.** "bar pepe granada" y "bar pepe, granada" son dos peticiones de pago para la misma ficha.
- **Vive en la memoria del proceso.** Se vacía en cada reinicio o despliegue y no se comparte entre procesos.

### Coste por contacto

Con la hipótesis H3 del experimento (el 15 % de los diagnósticos deja contacto), Places añade 0,04 / 0,15 = **0,27 USD por contacto**. El tope del experimento es de 2 € por contacto. Con la búsqueda en dos pasos baja a 0,17 USD.

## 3. Cómo abaratar

### Qué campos suben de categoría y qué regla se pierde al quitarlos

| Se quita | SKU resultante | Precio por 1.000 | Gratis al mes | Reglas que se pierden | Puntos que dejan de restar |
|---|---|---|---|---|---|
| Nada (hoy) | Text Search Enterprise + Atmosphere | 40 | 1.000 | — | — |
| `reviews.publishTime` y los 8 atributos de servicio | Text Search Enterprise | 35 | 1.000 | `stale_reviews`, `no_service_attributes` | 8 y 5 |
| Además: `rating`, `userRatingCount`, `regularOpeningHours`, `websiteUri`, `nationalPhoneNumber` | Text Search Pro | 32 | 5.000 | Además: `no_hours`, `low_rating`, `few_reviews`, `no_website`, `no_phone` | 20, 10, 10, 8 y 8 |

- Bajar a Enterprise ahorra un 12,5 % y cuesta dos reglas menores.
- Bajar a Pro ahorra un 20 % y deja el diagnóstico sin horario, nota, reseñas, web ni teléfono. No compensa.

> **Importante:** La regla `stale_reviews` ya es poco fiable. Google devuelve como máximo 5 reseñas, ordenadas por relevancia y no por fecha ("A maximum of 5 reviews can be returned", [referencia de Place](https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places)). La fecha más reciente de esas 5 puede no ser la de la última reseña real, y la regla puede acusar de "reseñas antiguas" a una ficha que recibió una ayer.

### Opciones

| Opción | Cómo funciona | Precio por 1.000 | 10.000 al mes | 100.000 al mes | Reglas perdidas |
|---|---|---|---|---|---|
| A. Hoy | Una petición Text Search con toda la máscara | 40 | 360 | 3.960 | Ninguna |
| B. Quitar Atmosphere | Una petición Text Search sin reseñas ni atributos | 35 | 315 | 3.465 | 2 |
| **C. Dos pasos** | Text Search solo con `places.id` (gratis) y Place Details con toda la máscara | **25** | **225** | **2.475** | **Ninguna** |
| D. Dos pasos sin Atmosphere | Como C, sin reseñas ni atributos (Place Details Enterprise) | 20 | 180 | 1.980 | 2 |
| E. Autocompletado con sesión | El dueño elige su negocio en una lista; la sesión termina con Place Details | 25 | 225 | 2.475 | Ninguna |

Los costes descuentan las 1.000 peticiones gratuitas del SKU de pago y no cuentan la caché.

**Opción C, en detalle.** El SKU Text Search Essentials (IDs Only) es gratuito e ilimitado y lo activan `places.id`, `places.name` y `places.attributions`. Place Details por identificador con campos de Atmosphere cuesta 25 USD frente a los 40 de Text Search. Además, el segundo paso permite poner la caché por `place_id`: dos búsquedas distintas que llegan al mismo negocio pagan una sola vez. Coste: una petición más por diagnóstico (más latencia) y dos cuotas que vigilar.

**Opción E, en detalle.** Reglas de la [facturación por sesión](https://developers.google.com/maps/documentation/places/web-service/session-pricing):

- Si la sesión termina con Place Details Pro, Enterprise o Enterprise + Atmosphere, las peticiones de autocompletado son gratis (SKU Autocomplete Session Usage).
- La petición final se factura como **Place Details Enterprise + Atmosphere "regardless of the fields requested"**: 25 USD por 1.000. Quitar campos no abarata esta vía.
- Una sesión abandonada paga cada pulsación como Autocomplete Requests: 2,83 USD por 1.000, con 10.000 gratis al mes.
- El token de sesión se genera en la página y viaja a través del servidor. La clave no sale del servidor.

Mismo precio que C, con una ventaja de producto: el dueño elige su negocio y no se diagnostica la ficha equivocada. Exige construir el autocompletado en la página.

**Más caché.** Alargar la caché más allá de 24 horas no es una opción: los términos no permiten guardar el contenido de Places ([sección 5](#5-términos-de-uso-qué-se-puede-guardar)). Lo que sí se puede hacer: guardar el `place_id` sin límite de tiempo y usarlo como clave.

### Recomendación

1. **Ahora: opción C.** Ahorra 135 USD al mes con 10.000 diagnósticos y 1.485 USD con 100.000, sin tocar las reglas. Es un cambio acotado al adaptador de Places.
2. **Con la página definitiva: opción E.** Mismo precio y menos diagnósticos de la ficha equivocada.
3. **No quitar los campos de Atmosphere todavía.** El ahorro adicional es de 45 USD al mes con 10.000 diagnósticos. Se decide con datos: si `stale_reviews` resulta tan poco fiable como indica la documentación, se retira la regla y se pasa a la opción D.
4. **Poner la cuota diaria antes de publicar el primer vídeo**, con cualquiera de las opciones.

## 4. Cómo poner un tope de gasto

Tres capas. Solo la primera corta el gasto.

### 4.1 Cuota diaria por API (corta el gasto)

Google lo indica así: "To put a hard cap on usage of an API, lower its per-day quota" ([gestionar costes](https://developers.google.com/maps/billing-and-pricing/manage-costs)).

1. En la consola de Google Cloud, selecciona el proyecto de Fichaitor.
2. Ve a **Google Maps Platform > Quotas**. También puedes llegar por **IAM & Admin > Quotas & System Limits** y filtrar por el servicio.
3. Selecciona **Places API (New)**.
4. Marca la casilla de la cuota del método que usa el diagnóstico (`SearchText`; con la opción C, también `GetPlace`).
5. Haz clic en **Edit**, escribe el valor nuevo y haz clic en **Submit request**.

Necesitas el rol Quota Administrator (`roles/servicemanagement.quotaAdmin`).

Valores propuestos para el experimento:

| Cuota diaria | Máximo al mes (31 días) | Peor mes con la opción A | Peor mes con la opción C |
|---|---|---|---|
| 100 | 3.100 | 84 USD | 52,50 USD |
| 200 | 6.200 | 208 USD | 130 USD |
| 500 | 15.500 | 580 USD | 362,50 USD |

Recomendado para empezar: **200 al día**. Se sube si un vídeo funciona.

Comportamiento:

- Al llegar al límite, las peticiones fallan el resto del día. La cuota diaria se reinicia a medianoche, hora del Pacífico (las 9:00 en España peninsular).
- El adaptador ya convierte cualquier respuesta distinta de 200 en `PlacesUnavailable`. La página debe mostrar "Vuelve a intentarlo mañana" y ofrecer la lista de espera.
- El paso gratuito de la opción C (solo `places.id`) también cuenta para la cuota.

> **Nota:** La documentación de Places (New) describe las cuotas como límites por minuto, por método y por proyecto, y no lista los nombres exactos de las cuotas diarias. Que exista una cuota diaria editable para `SearchText` y `GetPlace` está sin confirmar hasta verlo en la consola. Si no existe, baja la cuota por minuto y añade un contador diario global en la aplicación.

Segunda protección, en la aplicación: un contador global de peticiones a Places por día, además del límite por cliente que ya existe. Cuando la caché y los contadores pasen a PostgreSQL, este contador va con ellos.

### 4.2 Alertas de presupuesto (avisan, no cortan)

"Setting a budget does not automatically cap Google Cloud or Google Maps Platform usage or spending" ([crear presupuestos](https://docs.cloud.google.com/billing/docs/how-to/budgets)).

1. En la consola, ve a **Billing > Budgets & alerts** y elige la cuenta de facturación.
2. Haz clic en **Create budget**.
3. Elige **Alerts only**, ponle nombre y haz clic en **Next**.
4. En el ámbito, deja el periodo mensual, selecciona el proyecto de Fichaitor y, en **Services**, Places API (New).
5. En el importe, elige **Specified amount** y escribe el límite. Propuesta: 50 USD para el experimento.
6. En las acciones, deja los avisos al 50 %, 90 % y 100 %. Añade uno sobre el gasto previsto.
7. Haz clic en **Finish**.

Los avisos llegan por correo a los administradores de facturación. Para un corte automático haría falta conectar un tema de Pub/Sub y una función que desactive la API; con la cuota diaria no es necesario.

Necesitas el rol Billing Account Costs Manager o Billing Account Administrator.

### 4.3 Restricción de la clave

Fuente: [buenas prácticas de seguridad de las API](https://developers.google.com/maps/api-security-best-practices). Places API (New) es un servicio web: le corresponde la restricción por dirección IP.

1. En la consola, ve a **Google Maps Platform > Credentials** y abre la clave.
2. En **Key restrictions**, elige **Set an application restriction** y selecciona **IP addresses**.
3. Escribe la IP pública de salida del servidor (IPv4 o IPv6; admite rangos CIDR). Tiene que ser la que ve Google: si el servidor sale por NAT, la IP pública del NAT.
4. En **API restrictions**, elige **Restrict key**, abre **Select APIs** y marca solo **Places API (New)**.
5. Guarda.

Además:

- Usa una clave solo para el diagnóstico. No reutilices la de GBP Autopilot: si se filtra, se rota sin afectar a nada más.
- La clave no llega nunca al navegador. La página llama a la API de Fichaitor y la API llama a Google.
- Borra las claves que no se usen, después de comprobar que no tienen tráfico.

## 5. Términos de uso: qué se puede guardar

Bitclick factura desde España. Aplican los **términos del EEE**, no los generales: "These EEA Service Specific Terms apply to Customers who have a billing account address in the European Economic Area".

### Lo que dicen los términos

| Regla | Texto | Fuente |
|---|---|---|
| No guardar | "Customer will not cache Google Maps Content except as expressly permitted under the Maps Service Specific Terms" | [Términos del EEE](https://cloud.google.com/terms/maps-platform/eea), 3.3.2 (b) |
| No extraer | No se puede "pre-fetch, index, store, reshare, or rehost Google Maps Content" ni "copy and save business names, addresses, or user reviews" | [Términos del EEE](https://cloud.google.com/terms/maps-platform/eea), 3.3.2 (a) |
| No crear contenido | "Customer will not create content based on Google Maps Content" | [Términos del EEE](https://cloud.google.com/terms/maps-platform/eea), 3.3.2 (c) |
| El identificador sí | "Customer may cache (a) place_id from Places API…". Sin límite de tiempo: "You can therefore store place ID values indefinitely" | [Términos específicos del EEE](https://cloud.google.com/terms/maps-platform/eea/maps-service-terms), A.3, y [políticas de Places](https://developers.google.com/maps/documentation/places/web-service/policies) |
| Coordenadas, 30 días | "Customer may temporarily cache latitude and longitude values from the Places API for up to 30 consecutive calendar days" | [Términos específicos del EEE](https://cloud.google.com/terms/maps-platform/eea/maps-service-terms), 15.4 |
| Usos permitidos en el EEE | "Other than latitude, longitude, and place_id, Customer may only use the Google Maps Content from the Places API as permitted by the Places API EEA Permitted Uses" | [Términos específicos del EEE](https://cloud.google.com/terms/maps-platform/eea/maps-service-terms), 15.2 |

No hay ningún permiso expreso para guardar nombre, dirección, valoración, horario, fotos o reseñas de Places, ni siquiera durante un tiempo corto.

### Lo que hace Fichaitor, punto por punto

| Qué se guarda | Dónde | ¿Cumple? |
|---|---|---|
| `place_id` | `waitlist_entry`, `funnel_event` | **Sí.** Permiso expreso y sin límite de tiempo |
| Coordenadas | No se piden | No aplica |
| Caché de 24 horas del diagnóstico: nombre, dirección, categoría, enlace a Maps y mejoras con cifras de la ficha (fotos, nota media, número de reseñas) | Memoria del proceso | **No tiene permiso expreso.** El plazo corto no lo arregla: los términos solo autorizan el identificador y las coordenadas |
| Nota calculada (0 a 100) | `waitlist_entry.score`, `funnel_event.score` | **Dudoso.** Es un dato propio, pero derivado de contenido de Google. Puede chocar con "no crear contenido basado en Google Maps Content". Sin confirmar |
| Nombre del negocio | `waitlist_entry.business_name` | **Depende del origen.** Si lo escribe el dueño, es un dato suyo. Si se copia de la respuesta de Places, es el ejemplo exacto de lo prohibido ("copy and save business names") |

### El problema mayor: los usos permitidos en el EEE

Con dirección de facturación en el EEE, el contenido de Places solo puede usarse para nueve fines ([usos permitidos de Places en el EEE](https://cloud.google.com/terms/maps-platform/eea-places-api-permitted-uses), versión del 4 de junio de 2025): búsqueda y autocompletado de direcciones; mostrar los locales propios del cliente; gestionar clientes u oportunidades de un equipo de ventas; asociar tareas o notas a lugares; contexto de un anuncio inmobiliario; información de un lugar ligada a una transacción financiera; puntos de interés en un juego; etiquetar un lugar en una red social; y rutinas de domótica.

Un diagnóstico público de la ficha de cualquier negocio **no encaja con claridad en ninguno**. El más cercano, "display information about Customer's physical stores", habla de los locales del cliente de Google (Bitclick), no de los negocios de terceros.

Esto no es un dictamen jurídico. Es una lectura literal de los términos, y es un riesgo que hay que cerrar antes de llevar tráfico de pago al diagnóstico.

### Qué hacer

1. **Pregunta a Google por escrito** (soporte de Google Maps Platform o el contacto comercial) si el diagnóstico encaja en los usos permitidos del EEE y si se admite una caché breve de la respuesta. Guarda la contestación.
2. **Cambia la caché para que guarde solo lo permitido.** Clave: texto de búsqueda → `place_id`, sin caducidad. El resto se pide a Google en cada diagnóstico. Combinado con la opción C, la repetición de una búsqueda ahorra el primer paso, que ya es gratis; el ahorro real pasa a depender del límite por cliente y de la cuota.
3. **En la lista de espera, guarda el nombre que escribe el dueño**, no el que devuelve Places. Confírmalo en el código antes de publicar.
4. **Muestra la atribución de Google Maps** junto a los datos del diagnóstico. Las políticas exigen que quede claro qué contenido viene de Google Maps, y los vídeos de TikTok que enseñen el diagnóstico en pantalla también lo muestran.
5. **Ten preparada la alternativa:** el diagnóstico completo después de conectar Google, con las API de Business Profile y los datos del propio dueño. No depende de Places ni de sus términos, pero cambia el embudo: pide iniciar sesión antes de enseñar la nota.

## Sin confirmar

| Punto | Por qué |
|---|---|
| Que `reviews.publishTime` se facture como `reviews` | La documentación asigna SKU al campo, no al subcampo |
| Precio en euros | La lista oficial está en USD; no consta el tipo de cambio de la factura |
| Nombres y existencia de la cuota diaria de `SearchText` y `GetPlace` | La documentación de Places (New) solo describe límites por minuto |
| Código de error al agotar la cuota | No figura en las páginas consultadas |
| Porcentaje de búsquedas repetidas en 24 horas | No hay datos; las tablas usan supuestos |
| Si el diagnóstico encaja en los usos permitidos del EEE | Requiere respuesta de Google |
| Si la nota calculada cuenta como "contenido creado a partir de Google Maps Content" | Requiere respuesta de Google |
| Si la cuenta de facturación se comparte con el proyecto de GBP Autopilot | Hay que mirarlo en la consola; afecta a la franja gratuita |

## Fuentes

Todas consultadas el 2026-11-11.

- [Google Maps Platform: lista de precios global](https://developers.google.com/maps/billing-and-pricing/pricing)
- [Places API (New): campos de datos y SKU](https://developers.google.com/maps/documentation/places/web-service/data-fields)
- [Places API: uso y facturación](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)
- [Places API (New): Text Search](https://developers.google.com/maps/documentation/places/web-service/text-search)
- [Places API (New): facturación por sesión del autocompletado](https://developers.google.com/maps/documentation/places/web-service/session-pricing)
- [Places API (New): referencia del recurso Place](https://developers.google.com/maps/documentation/places/web-service/reference/rest/v1/places)
- [Places API: políticas y atribución](https://developers.google.com/maps/documentation/places/web-service/policies)
- [Google Maps Platform: gestionar costes](https://developers.google.com/maps/billing-and-pricing/manage-costs)
- [Google Maps Platform: buenas prácticas de seguridad de las API](https://developers.google.com/maps/api-security-best-practices)
- [Google Cloud: ver y gestionar cuotas](https://docs.cloud.google.com/docs/quotas/view-manage)
- [Cloud Billing: crear, editar o borrar presupuestos y alertas](https://docs.cloud.google.com/billing/docs/how-to/budgets)
- [Términos de servicio de Google Maps Platform para el EEE](https://cloud.google.com/terms/maps-platform/eea) (última modificación: 26 de agosto de 2026)
- [Términos específicos de los servicios para el EEE](https://cloud.google.com/terms/maps-platform/eea/maps-service-terms) (última modificación: 28 de octubre de 2026)
- [Usos permitidos de Places API en el EEE](https://cloud.google.com/terms/maps-platform/eea-places-api-permitted-uses) (última modificación: 4 de junio de 2025)
- [Términos específicos de los servicios, versión general](https://cloud.google.com/maps-platform/terms/maps-service-terms) (última modificación: 28 de octubre de 2026; no aplica a Bitclick, se usó para comparar)
