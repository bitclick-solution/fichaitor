# Planes y precios (hipótesis de lanzamiento)

Decidido por el propietario el 2026-11-11. Precios anuales, por ficha. Se revisan con los datos de la beta.

| Plan | Precio al año | Canal | Incluye |
|---|---|---|---|
| Gratis | 0 € | Web instalable, correo o Telegram | Diagnóstico, avisos de reseñas, 25 respuestas con IA al mes, QR básico |
| Pro | 35 € | Web instalable, correo o Telegram | Respuestas ilimitadas, modo automático, festivos y fechas clave, QR imprimibles por empleado |
| Pro WhatsApp | 59 € | WhatsApp | Lo mismo que Pro, por WhatsApp, con 80 mensajes nuestros al mes |
| Agencias | Por definir, tras el lanzamiento | — | Con clave propia del modelo de IA |

## Varias fichas en una cuenta

Decidido por el propietario el 2026-11-12 (`docs/multinegocio.md`, sección 15). Una cuenta puede tener varias fichas activas, cada una con su plan.

| Plan | Primera ficha de pago | Segunda y siguientes |
|---|---|---|
| Pro | 35 € | 33,25 € |
| Pro WhatsApp | 59 € | 56,05 € |

- **Descuento del 5 %** desde la segunda ficha **de pago** de la cuenta.
- La ficha Gratis no cuenta como primera: con una Gratis y una Pro, la Pro paga 35 €.
- Como mucho una ficha Gratis por cuenta, y una ficha de Google solo puede estar activa en Gratis en una cuenta de Fichaitor.
- Se pueden mezclar planes. WhatsApp solo avisa de las fichas Pro WhatsApp; las demás avisan por Telegram, correo o panel.
- Paga el precio completo la ficha del plan más caro y, si empatan, la más antigua. El descuento se calcula al cobrar; no se guarda en la ficha.
- Los 80 mensajes de WhatsApp son por ficha Pro WhatsApp y se suman en la bolsa de la cuenta, porque el chat es uno.
- **Beta:** cada cuenta tiene un derecho de 3 fichas Pro sin cobro hasta el lanzamiento, ampliable a mano.

El cálculo está en `app/domain/plans.py` (`pricing`, `yearly_price`).

## Reglas

- **Sin monedero.** Al agotar los 80 mensajes del mes, los avisos siguen llegando por correo, web o Telegram hasta el mes siguiente. El servicio no se corta.
- Solo cuentan los mensajes que envía Fichaitor. Los del dueño no cuestan.
- Un aviso clasificado por Meta como marketing descuenta 3 o 4 mensajes, porque cuesta 3,5 veces más.
- No se construye el "filtro de reseñas negativas" que ofrece la competencia: desviar a los clientes descontentos va contra las normas de reseñas de Google. Pendiente de confirmar en la política vigente.

## Cuentas

| Plan | Ingreso | Coste variable estimado | Queda |
|---|---|---|---|
| Pro | 35 € | Modelo de IA: céntimos | Unos 34 € |
| Pro WhatsApp | 59 € | WhatsApp: hasta 16 € (80 × 12 × 0,0166 €) | Al menos 43 € |

Falta descontar la comisión de la pasarela de pago y el IVA, que se muestra aparte al ser venta a negocios.

## Referencia de mercado

Opinas: gratis con 25 respuestas al mes; Pro desde 49 € al año por ficha, con respuestas ilimitadas, modo automático, análisis, vigilancia de competidores y programación de posts. No anuncia WhatsApp ni avisos de fechas clave.

## Qué mide la beta

1. Mensajes nuestros al mes por negocio en WhatsApp. Con 30, el plan puede bajar a 49 €; con 150, hay que subir el precio o recortar la conversación.
2. Qué porcentaje elige WhatsApp frente al canal gratuito.
3. Paso de gratis a Pro.
