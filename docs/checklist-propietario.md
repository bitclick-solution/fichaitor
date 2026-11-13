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
- [ ] SMTP de IONOS en `worker.env` (contraseña de `avisos@`). En `api.env`, solo el host y el remitente.
- [ ] Clave de Gemini en `api.env`, `worker.env` y `conversation.env`, o Vertex AI (punto siguiente).
- [ ] Registrar el webhook de Telegram en producción con su `secret_token`, siguiendo `docs/despliegue.md`, «Webhook de Telegram».
- [ ] Variable `FICHAITOR_URL_PUBLICA=https://fichaitor.es` en GitHub, para que el flujo compruebe la web al terminar.
- [ ] Lanzar «Desplegar» desde `main` con `solo_web = 0` y `legal_estricto = 1`.
- [ ] Tras desplegar, comprobar en el servidor que el usuario de la API no puede crear tablas temporales:
  `SELECT has_database_privilege('fichaitor_api_prod', current_database(), 'TEMPORARY');` debe dar `f`.

## Decisiones pendientes

- [ ] Gemini: seguir en AI Studio o pasar a Vertex AI en la UE (`eu`). En Vertex, `gemini-3.6-flash` se retira el 2026-11-19 y hay que usar `gemini-3.8-flash`. Pasos en `docs/despliegue.md`, «Gemini en Vertex AI».
- [ ] Pasarela de pago para cuando acabe la beta: proveedor y fecha.

## Cobro con Stripe y factura en Odoo (`docs/admin-facturacion.md`, sección 8)

- [ ] Cuenta de Stripe de Bitclick: alta, verificación y modo de prueba. Pásame solo que está lista; las claves nunca por el chat.
- [ ] Gestoría: confirmar los puntos marcados «(gestoría)» del documento, sobre todo la fecha de VERI\*FACTU y la serie de facturas de Fichaitor en Odoo.
- [ ] Alta en el ROI (operadores intracomunitarios), para vender a la UE con NIF-IVA, y presentar el modelo 349 cuando toque.
- [ ] Odoo de Bitclick: usuario técnico «Fichaitor facturación» con solo permisos de facturación, su clave de API, un diario «Fichaitor» y la serie de facturas; ponerlo en `billing.env` del servidor. Sin módulos nuevos (como en Conectores ERP). VERI\*FACTU según confirme la gestoría.
- [ ] Antes de activar el cobro: avisar a la sesión de interfaz para pasar Stripe a proveedores activos en la política de privacidad.
- [ ] Decidir si hay oferta para las cuentas de la beta al terminarla (cupón de un solo uso, porcentaje y fechas).

## Externo y legal

- [ ] Meta: respuesta a la solicitud de proveedor de tecnología para WhatsApp. Cuando llegue, número y plantillas.
- [ ] Revisión profesional de `docs/legal/privacidad.md` y `docs/legal/condiciones-de-uso.md`, con los puntos de `docs/legal/notas-revision.md`.
- [ ] Contrato de encargado del tratamiento (DPA) con Google para Gemini (AI Studio de pago o Google Cloud).
- [ ] Registro de actividades de tratamiento con Fichaitor dado de alta.

## Recordatorios

- [ ] Renovar `GHCR_TOKEN` antes de que caduque (lo creaste con caducidad de 1 año).
- [ ] Guardar copia de la clave privada de los tokens fuera del servidor. Si se pierde, todos los clientes tienen que volver a conectar Google.
