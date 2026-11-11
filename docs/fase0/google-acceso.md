# Fase 0.1 — Proyecto de Google, acceso a la API y verificación

Son **tres trámites distintos**, con plazos distintos. Los dos primeros se pueden hacer hoy. El tercero necesita que el producto exista.

| Trámite | Qué desbloquea | Cuándo |
|---|---|---|
| A. Proyecto nuevo y acceso a la API | Que el proyecto pueda llamar a la API (cuota de 0 a 300 consultas por minuto) | Ya |
| B. Pantalla de consentimiento en producción | Que los tokens no caduquen a los 7 días | Ya, tras el A |
| C. Verificación de la app | Quitar el aviso de "app no verificada" y el tope de 100 usuarios | Cuando haya dominio, política de privacidad y una demo que funcione (fase 2 o 3) |

Requisitos confirmados en la documentación de Google el 2026-11-11. Los de la verificación (trámite C) vienen de conocimiento previo y hay que contrastarlos con el asistente de la consola cuando llegue el momento.

## Estado a 2026-11-11

- Dominio registrado: `fichaitor.es`. Correo de soporte: `soporte@fichaitor.es`.
- Proyecto nuevo de Google Cloud creado y cliente OAuth creado.
- **El permiso `business.manage` figura como "no sensible" en la consola.** Si se confirma al publicar, el trámite C se reduce a la verificación de marca (nombre y logo): sin vídeo, sin justificación de permisos y sin auditoría externa.
- Pantalla de consentimiento: página principal `https://fichaitor.es`, privacidad `https://fichaitor.es/privacidad/`, condiciones `https://fichaitor.es/condiciones-de-uso/`, dominio autorizado `fichaitor.es`.
- Publicación: detrás del Nginx Proxy Manager de Bitclick, como el resto de webs.

### Cliente OAuth: valores

El flujo es de servidor (el código de autorización lo recibe la API), así que los orígenes de JavaScript no hacen falta.

| Campo | Valor |
|---|---|
| Orígenes autorizados de JavaScript | Vacío. Si la consola obliga a poner uno: `https://fichaitor.es` |
| URI de redireccionamiento (producción) | `https://fichaitor.es/api/v1/google/oauth/callback` |
| URI de redireccionamiento (desarrollo) | `http://localhost:8000/api/v1/google/oauth/callback` |

La ruta coincide con la del contrato de la API (`docs/03-lld.md`). El secreto del cliente va en el `.env` del servidor, nunca en el repositorio.

### Lo que falta para pasar a producción

| Requisito | Estado |
|---|---|
| Nombre de la app y correo de asistencia | Listos: Fichaitor, `soporte@fichaitor.es` |
| Logo (cuadrado, 120 × 120 px, PNG o JPG, menos de 1 MB) | Falta |
| Página principal pública en `https://fichaitor.es` | Falta |
| Política de privacidad publicada en `/privacidad/` | Falta |
| Condiciones de uso publicadas en `/condiciones-de-uso/` | Falta |
| Dominio verificado en Google Search Console | Falta |
| Correo de contacto del desarrollador | Por rellenar |

## A. Proyecto nuevo y acceso a la API

### Requisitos que ya cumples

- Una ficha verificada y activa desde hace más de 60 días (la de Bitclick Solutions).
- Un sitio web que representa a ese negocio (bitclick.solutions).
- Un correo que figura como propietario o gestor de la ficha.

### Pasos

1. Entra en Google Cloud Console con la cuenta que administra la ficha de Bitclick.
2. Crea un proyecto nuevo. Nombre sugerido: el nombre comercial del producto, o `agente-gbp-prod` si aún no hay nombre. El nombre se puede cambiar después; el número de proyecto, no.
3. En el panel del proyecto, copia el **Project Number** de la tarjeta "Project info".
4. Abre el formulario de contacto de la API de Business Profile y elige **"Application for Basic API Access"**.
5. Rellénalo con los textos de abajo y envíalo desde el correo propietario de la ficha.
6. Espera el correo de respuesta. Para comprobar el estado, mira la cuota de las API de Business Profile en el proyecto: **0** significa pendiente; **300** significa aprobado.
7. Cuando esté aprobado, habilita estas API en la biblioteca del proyecto:
   - My Business Account Management API
   - My Business Business Information API
   - Business Profile Performance API
   - Google My Business API (`mybusiness.googleapis.com`; si no sale en el buscador, se abre por enlace directo)
   - My Business Notifications API
   - My Business Verifications API
   - Places API (New)
   - Cloud Pub/Sub API

### Textos para el formulario

Las preguntas son las mismas que respondiste en octubre para el proyecto `544696777336`. Adapta el número de proyecto.

**Motivo principal de la solicitud**

> Bitclick Solutions desarrolla una herramienta para que los propietarios de negocios locales gestionen su Perfil de Negocio de Google desde un asistente conversacional. Cada propietario conecta su propia ficha mediante OAuth y autoriza expresamente el acceso. La herramienta le avisa de reseñas nuevas y le propone una respuesta, le pregunta por sus horarios en festivos y fechas señaladas, y le ayuda a publicar novedades y fotos. Ningún cambio se aplica en la ficha sin la confirmación previa del propietario.

**Cómo se usará la API**

> Usaremos la API para leer la información de la ficha (horarios, categorías, atributos), actualizar horarios especiales y datos básicos cuando el propietario lo confirme, leer reseñas y publicar las respuestas que el propietario apruebe, crear publicaciones y subir fotos, y consultar las métricas de rendimiento para mostrárselas al propietario. Recibiremos avisos de reseñas nuevas mediante la API de notificaciones.

**Quién usará la herramienta**

> Propietarios y gestores de negocios locales en España (hostelería, comercio y servicios) que administran su propia ficha. Cada usuario accede únicamente a las fichas sobre las que tiene permisos en Google.

**¿Es una herramienta interna o para terceros?**

> Para terceros. Es un producto de software como servicio que Bitclick Solutions ofrece a otros negocios. Bitclick Solutions también lo usa con su propia ficha.

**Datos del solicitante**

- Empresa: Bitclick Solutions
- Sitio web: https://bitclick.solutions
- Ficha verificada hace más de 60 días: sí
- Número de proyecto: el del proyecto nuevo

No prometas en el formulario funciones que la API no permite (preguntas y respuestas, borrar reseñas).

## B. Pantalla de consentimiento en producción

Mientras la app esté en "Testing", los tokens de renovación caducan a los 7 días. Es lo que rompió la credencial de GBP Autopilot.

1. En el proyecto nuevo, abre **Google Auth Platform**.
2. En **Branding**, pon el nombre de la app, el correo de soporte y, cuando existan, el dominio, la página de inicio y la política de privacidad.
3. En **Audience**, elige tipo de usuario **External**.
4. En **Data Access**, añade el permiso `https://www.googleapis.com/auth/business.manage`. La consola indica ahí si lo clasifica como sensible o restringido; anótalo, porque determina el trámite C.
5. En **Clients**, crea un cliente OAuth de tipo "Web application". La URL de redirección será la del producto; para pruebas, la de n8n.
6. En **Audience**, pulsa **Publish app** para pasarla a "In production".

Sin verificar, la app en producción funciona con dos limitaciones: muestra el aviso de "Google no ha verificado esta app" y admite 100 usuarios como máximo. Es suficiente para el desarrollo y para la beta.

## C. Verificación de la app

No se puede enviar todavía. Google revisa el producto real, así que necesita:

| Requisito | Estado |
|---|---|
| Dominio propio verificado en Search Console | Falta el nombre y el dominio |
| Página de inicio pública que explique el producto | Falta |
| Política de privacidad en el mismo dominio, que explique qué datos de Google se usan y para qué | Falta |
| Vídeo de demostración del flujo de OAuth y del uso de los datos | Falta el producto |
| Justificación de cada permiso | Texto preparado abajo |

Si `business.manage` figura como permiso **sensible**, basta con esta revisión. Si figurara como **restringido**, Google exige además una evaluación de seguridad externa, con coste y renovación anual. Hay que comprobarlo en el paso B.4.

### Justificación del permiso (en inglés, que es como se revisa)

> Our app lets local business owners manage their own Google Business Profile through a conversational assistant. We request `business.manage` because every core feature reads or updates the owner's profile on their behalf: reading profile details and performance metrics to show them to the owner; updating special hours for public holidays after the owner confirms; reading new reviews and publishing the reply the owner approves; creating posts and uploading photos the owner sends. There is no narrower scope that covers these Business Profile operations. No change is ever applied without the owner's explicit confirmation, data is used only to provide these features to the owner who granted access, and it is never sold or shared with third parties.

### Guion del vídeo

1. Página de inicio del producto, con la URL visible.
2. Alta y pulsación de "Conectar con Google".
3. Pantalla de consentimiento de Google mostrando el nombre de la app y el permiso.
4. Selección de la ficha.
5. Una función por cada uso declarado: ver la ficha, confirmar un horario especial, aprobar la respuesta a una reseña, publicar un post.
6. Cómo se desconecta la cuenta y se borran los datos.

## Orden recomendado

1. Hoy: A.1 a A.5 (crear el proyecto y enviar el formulario).
2. Al aprobarse: A.7 y el bloque B.
3. Fase 0.9: decidir nombre y dominio.
4. Fase 2 o 3: bloque C, con la demo funcionando.
