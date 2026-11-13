# Checklist del propietario

Lo que solo puedes hacer tú: cuentas, consolas, servidor, decisiones y pruebas reales. Marca cada punto con `[x]` cuando esté hecho. Si un punto te bloquea, déjalo sin marcar y pásamelo.

Última actualización: 2026-11-13. La guía de cada paso está en `docs/despliegue.md` (servidor y GitHub) y en `docs/fase0/google-acceso.md` (Google).

## Hecho

- [x] Dominio `fichaitor.es` y correo `soporte@` y `avisos@` (IONOS).
- [x] Proyecto de Google Cloud: Fichaitor usa el de Bitclick (`544696777336`).
- [x] Cliente OAuth propio de Fichaitor, con su ID y su secreto en el `.env` local.
- [x] Dominio verificado en Search Console.
- [x] Entorno `production` en GitHub con los secretos SSH, `GHCR_TOKEN` y `GHCR_USER`.
- [x] Carpeta `/opt/bitclick/fichaitor` en el VPS.
- [x] Host `fichaitor.es` en Nginx Proxy Manager, con certificado.
- [x] Web publicada en modo «solo web» (portada, privacidad, condiciones y «Muy pronto»).
- [x] Verificación de marca de Google aprobada y app publicada «In production».
- [x] Bot de Telegram `@FichaitorBot` creado, con su token en el `.env` local.

## Ahora (sin el producto en marcha)

- [x] Certificado propio para el host de redirección `www.fichaitor.es` en Nginx Proxy Manager. Hoy sirve el de `fichaitor.es` y el navegador avisa.
- [x] Comprobar que no queda el registro `AAAA` de `fichaitor.es` y `www` apuntando a IONOS.
- [x] Search Console: enviar el mapa del sitio `https://fichaitor.es/sitemap.xml`.
- [x] En el cliente OAuth de Fichaitor, añadir la URI de producción `https://fichaitor.es/api/v1/google/oauth/callback`, si no está.
- [x] Volver a conectar en n8n las credenciales de Google del proyecto de Bitclick. Con la app en producción, el token nuevo ya no caduca a los 7 días; el antiguo sí.
- [x] Decidir qué hacer con el proyecto nuevo de Google Cloud (`266363689710`), que Google rechazó por duplicado: borrarlo o dejarlo sin uso.
- [x] Clave de Places, cuotas por día en Google Cloud (APIs y servicios > Places API (New) > Cuotas): `SearchTextRequest per day` = 600 y `GetPlaceRequest per day` = 600; `per minute` de las dos = 30; el resto de cuotas de Places a 0 (no se usan).
- [x] Clave de Places, restricciones: solo «Places API (New)» y, en producción, solo la IP del VPS.
- [x] Alerta de presupuesto en Facturación (aviso, no tope): por ejemplo 50 € al mes.
- [x] Revisar en GitHub que el entorno `production` solo admite la rama `main` y las etiquetas `v*`, y tiene revisores obligatorios (tú).

## Pruebas reales en local (`http://localhost:8080/app`)

- [x] Selector de negocio: cambiar entre Bitclick y PubliExpe y comprobar que cada una muestra sus reseñas, su opinión y sus festivos.
- [ ] Telegram con dos negocios: los avisos llegan con el nombre del negocio. En PubliExpe, «Publicar» publica de verdad en la ficha de tu cliente.
- [ ] Filtro por chat: vincular un segundo chat limitado a un solo negocio.
- [ ] Baja de prueba: desconectar Google, recibir el código y el correo desde `avisos@` y restaurar con el enlace. Comprueba que, si respondes al correo, la respuesta va a `soporte@`.
- [ ] Horario especial real en la ficha de Bitclick: un día futuro, confirmarlo y comprobarlo en Google Maps. Después lo revertimos. Nunca en PubliExpe.

## Antes del despliegue completo (producto abierto en producción)

- [ ] Generar el par de claves de los tokens (`uv run python scripts/gen_token_keys.py`): la pública va en `api.env`, la privada en `worker.env` y en una copia en tu gestor de contraseñas.
- [ ] Crear en el servidor `db.env`, `api.env`, `worker.env` y `conversation.env` a partir de `deploy/env.*.example`, con modo 600.
- [ ] Generar con `openssl rand -hex 32` el pimiento de los códigos de vinculación (`FICHAITOR_LINK_CODE_PEPPER`, el mismo valor en `api.env` y `conversation.env`) y el secreto del webhook de Telegram (`FICHAITOR_TELEGRAM_WEBHOOK_SECRET`).
- [x] Generar con `openssl rand -hex 32` el pimiento de las respuestas gratuitas (`FICHAITOR_ALLOWANCE_PEPPER`), el mismo en `api.env` y `conversation.env`. **No cambiarlo nunca:** si cambia, cada ficha vuelve a tener sus 25 respuestas con IA.
- [ ] SMTP de IONOS en `worker.env` (contraseña de `avisos@`). En `api.env`, solo el host y el remitente.
- [ ] Clave de Gemini en `api.env`, `worker.env` y `conversation.env`, o Vertex AI (punto siguiente).
- [ ] Registrar el webhook de Telegram en producción con su `secret_token`, siguiendo `docs/despliegue.md`, «Webhook de Telegram».
- [ ] Variable `FICHAITOR_URL_PUBLICA=https://fichaitor.es` en GitHub, para que el flujo compruebe la web al terminar.
- [ ] Lanzar «Desplegar» desde `main` con `solo_web = 0` y `legal_estricto = 1`.
- [ ] Tras desplegar, comprobar en el servidor que el usuario de la API no puede crear tablas temporales:
  `SELECT has_database_privilege('fichaitor_api_prod', current_database(), 'TEMPORARY');` debe dar `f`.

## Beta y lanzamiento (decidido 2026-11-13)

- [x] Beta gratis hasta la v1; desde la v1, 30 días de prueba (una sola por ficha).
- [ ] Dos bares entran en la beta: preparar su alta cuando esté el despliegue completo.
- [ ] Despliegue completo cuando todo esté probado en local.

## WhatsApp (número propio, sin Tech Provider)

- [ ] Número de WhatsApp de Fichaitor dado de alta en la WABA de Bitclick (Cloud API).
- [ ] Usuario del sistema con token permanente (`whatsapp_business_messaging`) y el *app secret* de la app de Meta, solo en los archivos del servidor.
- [ ] Enviar a aprobación las plantillas de utilidad que prepare el agente de WhatsApp.
- [ ] Verificación del negocio en Meta, si todavía no está, para subir los límites de envío.

## Decisiones pendientes

- [x] Gemini: en producción por Vertex AI (decidido 2026-11-13).
- [ ] Si Vertex con clave de API de Agent Platform: la clave tiene que estar **vinculada a una cuenta de servicio** (no la del modo exprés). Comprobar con una llamada real que funciona en la ubicación `eu`; si `eu` da 401/403, usar la cuenta de servicio, nunca `global`. Variables en `api.env`, `worker.env` y `conversation.env`: `FICHAITOR_GEMINI_PROVIDER=vertex`, `FICHAITOR_GEMINI_MODEL=gemini-3.8-flash`, `FICHAITOR_VERTEX_PROJECT`, `FICHAITOR_VERTEX_LOCATION=eu` y `FICHAITOR_VERTEX_API_KEY` (`docs/despliegue.md`, «Gemini en Vertex AI»).
- [ ] Al pasar a Vertex: avisar a la sesión de interfaz para cambiar en la política la fila de Gemini (Vertex AI en la UE).
- [ ] Pasarela de pago para cuando acabe la beta: proveedor y fecha.

## Cobro con Stripe y factura en Odoo (`docs/admin-facturacion.md`, sección 8)

- [x] Cuenta de Stripe de Bitclick: alta, verificación y modo de prueba. Pásame solo que está lista; las claves nunca por el chat.
- [x] Gestoría: confirmar los puntos marcados «(gestoría)» del documento, sobre todo la fecha de VERI\*FACTU y la serie de facturas de Fichaitor en Odoo.
- [x] Alta en el ROI (operadores intracomunitarios), para vender a la UE con NIF-IVA, y presentar el modelo 349 cuando toque.
- [ ] Odoo de Bitclick (sin módulos nuevos), valores en `billing.env` del servidor:
  - [ ] Usuario técnico «Fichaitor facturación» solo con permisos de facturación y su clave de API: `FICHAITOR_ODOO_URL` (https), `FICHAITOR_ODOO_DB`, `FICHAITOR_ODOO_API_KEY`.
  - [ ] Diario de ventas «Fichaitor», prefijo FTR, con secuencia propia de rectificativas RFTR: `FICHAITOR_ODOO_JOURNAL_ID`.
  - [ ] Diario de banco «Stripe»: `FICHAITOR_ODOO_PAYMENT_JOURNAL_ID`.
  - [ ] Impuestos de venta sin IVA incluido: 21 % (`FICHAITOR_ODOO_TAX_IVA_21`), 0 % no sujeto (`FICHAITOR_ODOO_TAX_NO_SUJETO`), 0 % inversión del sujeto pasivo UE (`FICHAITOR_ODOO_TAX_ISP_UE`).
  - [ ] Productos de servicio «Fichaitor Pro» (`FICHAITOR_ODOO_PRODUCT_PRO`) y «Fichaitor Pro WhatsApp» (`FICHAITOR_ODOO_PRODUCT_PRO_WHATSAPP`).
  - [ ] Redondeo de impuestos por línea en la compañía.
  - [ ] Opcional: posiciones fiscales (`FICHAITOR_ODOO_FPOS_*`) y el campo de estado de VERI\*FACTU (`FICHAITOR_ODOO_VERIFACTU_FIELD`), según la gestoría.
- [ ] Stripe en modo de prueba: clave restringida `rk_test_…` con los permisos mínimos de `docs/contrato-web-api.md` («Cobro y paywall»), solo en `billing.env`.
- [x] Ejecutar `scripts/stripe_bootstrap.py` (primero con `--dry-run`) para crear productos, precios y Tax Rates en modo de prueba.
- [ ] Webhook de prueba en Stripe hacia `/api/v1/webhooks/stripe` con los eventos del contrato; su secreto `whsec_…` en `api.env` con `FICHAITOR_STRIPE_LIVEMODE=false`.
- [ ] En Stripe: desactivar los reintentos automáticos de cobro, dejar solo tarjeta y el portal solo para cambiar el método de pago.
- [ ] Antes de activar el cobro: avisar a la sesión de interfaz para pasar Stripe a proveedores activos en la política de privacidad.
- [x] Decidir si hay oferta para las cuentas de la beta al terminarla (cupón de un solo uso, porcentaje y fechas).

## Externo y legal

- [ ] Meta: respuesta a la solicitud de proveedor de tecnología para WhatsApp. Cuando llegue, número y plantillas.
- [ ] Revisión profesional de `docs/legal/privacidad.md` y `docs/legal/condiciones-de-uso.md`, con los puntos de `docs/legal/notas-revision.md`. Solo falta la revisión de los textos: Bitclick es empresa real, factura desde Odoo y está en el ROI.
- [x] Contrato de encargado del tratamiento (DPA) con Google para Gemini (AI Studio de pago o Google Cloud).
- [x] Registro de actividades de tratamiento con Fichaitor dado de alta.

## Recordatorios

- [ ] Renovar `GHCR_TOKEN` antes de que caduque (lo creaste con caducidad de 1 año).
- [ ] Guardar copia de la clave privada de los tokens fuera del servidor. Si se pierde, todos los clientes tienen que volver a conectar Google.
