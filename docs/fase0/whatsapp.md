# Fase 0.3 — WhatsApp: coste, límites y riesgos

Investigación documental del 2026-11-11. Sin pruebas reales todavía: falta enviar una plantilla y confirmar la tarifa en la hoja oficial de Meta.

## Lo confirmado en la documentación de Meta

| Punto | Dato |
|---|---|
| Qué se cobra | Las cuatro categorías: marketing, utilidad, autenticación y servicio |
| Cambio del 1 de octubre de 2026 | Los mensajes de servicio (texto libre dentro de la ventana de 24 horas) y las plantillas de utilidad dentro de la ventana **dejaron de ser gratis** |
| Franja gratuita | Los primeros 1.000 mensajes de servicio al mes **por número de teléfono**. No se acumulan |
| Mensajes del usuario al negocio | Nunca se cobran |
| Tarifa | Por mercado, según el prefijo del destinatario. España tiene tarifa propia |
| Descuentos por volumen | Solo en utilidad y autenticación, por tramos mensuales |
| Botones de respuesta | Máximo 3; título de 20 caracteres como máximo y distinto en cada botón |
| Cuerpo y pie del mensaje con botones | 1.024 y 60 caracteres |

## Tarifa de España: confirmada

Hoja oficial de Meta en euros, vigente desde el 1 de octubre de 2026 (aportada por el propietario el 2026-11-11).

| Categoría | Precio por mensaje |
|---|---|
| Marketing | 0,0585 € |
| Utilidad | 0,0166 € |
| Autenticación | 0,0166 € |
| Servicio | 0,0166 € |
| Meta Business Agent | 2,00 USD por millón de tokens (no se usa) |

## Qué implica para el producto

1. **El supuesto "las respuestas dentro de 24 horas son gratis" ya no vale.** Lo afirmé en la propuesta de planes; era cierto hasta el 30 de septiembre de 2026.
2. **Un número compartido agota la franja gratuita enseguida.** Con un solo número del agente para todos los clientes, los 1.000 mensajes gratis duran unos 20 negocios. A partir de ahí, cada mensaje nuestro cuesta.
3. **Estimación de coste por negocio y mes** (hipótesis sin medir: 8 avisos proactivos y 50 mensajes nuestros dentro de conversación). Con todo como utilidad o servicio: 58 × 0,0166 = **0,96 €**. Si 4 de los avisos se clasifican como marketing: 4 × 0,0585 + 54 × 0,0166 = **1,13 €**. Es compatible con un precio de 19 a 29 €. Cada mensaje nuestro que se ahorre son 1,66 céntimos.
4. **Riesgo de categoría.** "Se acerca Black Friday, ¿tienes oferta?" puede clasificarse como marketing y no como utilidad. Los avisos de reseña y de festivo encajan mejor como utilidad. La categoría la decide Meta al aprobar la plantilla.
5. **Diseñar para gastar menos mensajes:** un mensaje por turno, sin confirmaciones redundantes, y resúmenes agrupados.
6. **Telegram y el panel no tienen coste por mensaje.** Conviene ofrecerlos como canal alternativo desde el principio, y medir el coste real de WhatsApp en la beta.

## Pendiente

- ~~Decidir el acceso~~: decidido (2026-11-13). Cloud API directa, con la WABA de Bitclick.
- ~~Decidir entre un número compartido o uno por cliente~~: decidido (2026-11-13). Un solo número de Fichaitor; conectar números de clientes (Tech Provider) queda fuera.
- ~~Confirmar que los botones solo van dentro de la ventana~~: confirmado (2026-11-13). Fuera de la ventana solo se pueden enviar plantillas, con respuestas rápidas (`docs/canales.md`, «WhatsApp»).
- Enviar a aprobación las siete plantillas de `docs/whatsapp-plantillas.md` (sustituyen a las del guion) y anotar aquí la categoría que les asigna Meta.

## Fuentes

- [Meta: precios de WhatsApp Business Platform](https://developers.facebook.com/docs/whatsapp/pricing)
- [Meta: mensajes con botones de respuesta](https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-reply-buttons-messages)
- [whautomate: precios en España 2026](https://whautomate.com/whatsapp-business-api-pricing-spain)
- [ChatMaxima: precios en España 2026](https://chatmaxima.com/whatsapp-api-pricing/spain/)
- [formbeep: tarifas por país 2026](https://formbeep.com/whatsapp-api-pricing/)
- [Infobip: precios de WhatsApp](https://www.infobip.com/whatsapp-business/pricing)
- [engrana: cobro por mensaje desde octubre de 2026](https://engrana.es/en/blog/whatsapp-message-charges-october-2026)
