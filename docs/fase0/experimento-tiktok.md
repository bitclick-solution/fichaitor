# Fase 0.6 — Experimento de captación en TikTok

Objetivo: saber, antes de construir el producto, si TikTok puede traer dueños de negocios a un coste que los precios de Fichaitor soporten.

## El número que manda

| Dato | Valor | Origen |
|---|---|---|
| Ingreso medio por cliente y año | Unos 40 € | Mezcla de Pro (35 €) y Pro WhatsApp (59 €) |
| Permanencia | 1,5 a 2 años | Supuesto, sin datos |
| Valor de un cliente | 60 a 80 € | Los dos anteriores |
| Coste máximo por cliente de pago | Unos 20 € | Un tercio del valor |
| Paso de contacto a cliente de pago | 10 % | Supuesto, sin datos |
| **Coste máximo por contacto** | **2 €** | 20 € × 10 % |

Con estos precios, la publicidad de pago tiene muy poco margen. La apuesta principal es el **contenido orgánico**; los anuncios se prueban con poco dinero para medir, no para escalar.

## Qué se prueba

| Hipótesis | Cómo se mide | Se cumple si |
|---|---|---|
| H1. El gancho "¿qué nota tiene tu ficha de Google?" hace que un dueño pulse | Clics sobre visualizaciones | Más del 1 % en anuncios |
| H2. Quien llega hace el diagnóstico | Diagnósticos completados sobre visitas | Más del 30 % |
| H3. Quien ve su nota quiere que se lo arreglen | Contactos dejados sobre diagnósticos | Más del 15 % |
| H4. El contacto sale barato | Gasto entre contactos | Menos de 2 € en anuncios; cerca de 0 € en orgánico |
| H5. Quien se apunta es dueño de un negocio real | Fichas reales y verificadas entre los contactos | Más del 70 % |

## Lo mínimo que hay que construir

1. **Una página** en `fichaitor.es` con un campo: nombre del negocio y localidad.
2. **El diagnóstico**, con datos públicos de Google Places: nota de 0 a 100 y cinco mejoras. Reglas fijas, sin IA, para que sea rápido y gratis:
   - Horario completo y horarios de festivos.
   - Número y antigüedad de las fotos.
   - Reseñas: cantidad, media y cuántas tienen respuesta del propietario.
   - Descripción, web y teléfono.
   - Categorías.
3. **Lista de espera**: "Avísame cuando Fichaitor esté listo", con WhatsApp o correo, casilla de consentimiento y política de privacidad.
4. **Medición** de cada paso: visita, diagnóstico, contacto y origen (qué vídeo o anuncio).

No hace falta alta, pago ni conexión con Google.

El diseño de la página se decide en la sesión de diseño. Para el motor del diagnóstico hay dos opciones:

- **Rápida:** un webhook en n8n con la clave de Places que ya existe. Dos o tres días. Se tira después.
- **Definitiva:** primer servicio del repositorio nuevo, que luego es el diagnóstico del producto. Una o dos semanas, pero no se tira.

## Contenido

Voz en plural, como en BitclickLabs. Tres formatos para probar:

| Formato | Gancho | Qué enseña |
|---|---|---|
| Auditoría | "Le ponemos nota a la ficha de Google de este bar" | El diagnóstico en pantalla, con un negocio que haya dado permiso o con los datos tapados |
| Error común | "Tu ficha dice que abres el festivo y estás cerrado. Una estrella." | El fallo, lo que cuesta y cómo se evita |
| Dato | "Este negocio tiene 13 reseñas y no ha contestado ninguna" | Por qué importa contestar y la llamada al diagnóstico |

Cada vídeo termina igual: "Mira la nota de tu ficha, gratis, en fichaitor.es".

No se audita en público un negocio con nombre y apellidos sin su permiso.

## Plan de tres semanas

| Semana | Orgánico | Anuncios |
|---|---|---|
| 1 | Página y diagnóstico publicados. 3 vídeos, uno por formato | Nada |
| 2 | 4 vídeos más del formato que mejor funcionó | 100 € repartidos entre los 2 mejores vídeos, segmentados a España |
| 3 | 3 vídeos más | 150 € al mejor |

Presupuesto de anuncios: 250 €.

## Cómo se decide

| Resultado | Decisión |
|---|---|
| El orgánico trae contactos y los anuncios quedan por debajo de 2 € | Se construye el producto con TikTok como canal |
| El orgánico trae contactos pero los anuncios salen caros | Se construye, con captación solo orgánica y por los QR impresos; sin publicidad de pago |
| Hay diagnósticos pero casi nadie deja el contacto | El gancho funciona y la propuesta no: se revisa qué se promete antes de construir |
| No hay diagnósticos | TikTok no es el canal. Se prueba la venta por agencias o por asociaciones antes de construir |

## Requisitos previos

- Dominio `fichaitor.es` registrado.
- Diseño de la página (sesión de diseño).
- Política de privacidad y texto de consentimiento para la lista de espera.
- Cuenta de TikTok de la marca y, para los anuncios, cuenta de TikTok Ads.
- Límite de gasto en la clave de Places para que un pico de tráfico no genere una factura inesperada.

## Lo que este experimento no dice

No mide si la gente paga. Mide interés. El supuesto del 10 % de paso a pago se comprueba en la beta.
