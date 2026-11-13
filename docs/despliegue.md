# Despliegue en producción

Esta guía explica cómo publicar Fichaitor en el VPS de Bitclick y cómo actualizarlo después. Sigue el modelo de los demás servicios de Bitclick: imágenes en GHCR, una carpeta por servicio en `/opt/bitclick`, un `deploy.sh` con vuelta atrás y Nginx Proxy Manager delante.

> **Importante:** antes de abrir el sitio al público, repasa la [lista de comprobación previa a publicar](#lista-de-comprobación-previa-a-publicar). Hoy tiene puntos abiertos que impiden publicar.

## Cómo queda montado

| Contenedor | Qué es | Redes | Archivo de entorno |
|---|---|---|---|
| `fichaitor-db` | PostgreSQL 16 | `fichaitor-interna` | `db.env` |
| `fichaitor-api` | API (FastAPI) | `fichaitor-interna` | `api.env` |
| `fichaitor-worker` | Worker, con la misma imagen que la API | `fichaitor-interna` | `worker.env` |
| `fichaitor-conversation` | Conversación por Telegram (`docs/canales.md`), con la misma imagen | `fichaitor-interna` | `conversation.env` |
| `fichaitor-web` | Sitio público, `/app` y reenvío de `/api/` | `fichaitor-interna` y `bitclick-network` | Ninguno |
| `fichaitor-billing` | El cobro: Stripe, facturas en Odoo, avisos de renovación, fin de la beta y conciliación ([Cobro: Stripe, Odoo y el túnel](#cobro-stripe-odoo-y-el-túnel)), con la misma imagen | `fichaitor-interna` | `billing.env` |
| `fichaitor-admin` | El backoffice, solo por túnel SSH: publicado en `127.0.0.1:8090` del VPS y en ninguna otra parte. No arranca hasta el paso 12 de `docs/admin-facturacion.md` (perfil `backoffice`) | `fichaitor-interna` | `admin.env` |
| `fichaitor-baja` | Solo con `docker compose run --rm`: la baja por correo ([Tareas habituales](#tareas-habituales)), con la imagen de la API. No arranca con el despliegue (perfil `herramientas`) | `fichaitor-interna` | `db.env` |

- Ningún contenedor publica puertos hacia fuera del servidor; el backoffice, solo en el *loopback* (`127.0.0.1:8090`), para el túnel SSH. Nginx Proxy Manager (`bitclick-proxy`) llega a `fichaitor-web:8080` por la red `bitclick-network`. La base de datos y la API solo son visibles dentro de `fichaitor-interna`.
- Las imágenes `fichaitor-api` y `fichaitor-web` se etiquetan con el commit. Actualizar es cambiar la etiqueta, aplicar el esquema y recrear los contenedores.
- La clave privada de los tokens de Google solo está en `worker.env`. La API recibe la clave pública. La conversación no recibe ninguna: crea la acción cuando el dueño confirma, pero solo el worker la ejecuta.

Archivos del repositorio:

| Archivo | Uso |
|---|---|
| `.github/workflows/deploy.yml` | Construye las imágenes, las sube a GHCR y lanza el despliegue por SSH |
| `deploy/docker-compose.prod.yml` | Compose de producción. En el servidor se llama `docker-compose.yml` |
| `deploy/deploy.sh` | Despliegue en el servidor: copia, esquema, recreación, salud y vuelta atrás |
| `deploy/init-roles.sh` | Crea los usuarios de la base de datos con contraseñas generadas en el servidor |
| `deploy/backup.sh` y `deploy/restore.md` | Copia de seguridad y restauración |
| `deploy/env.api.example`, `deploy/env.worker.example`, `deploy/env.conversation.example`, `deploy/env.billing.example` y `deploy/env.admin.example` | Plantillas de `api.env`, `worker.env`, `conversation.env`, `billing.env` y `admin.env` |

Carpeta del servidor, `/opt/bitclick/fichaitor`:

| Archivo | Quién lo escribe | Contiene secretos |
|---|---|---|
| `docker-compose.yml`, `deploy.sh`, `backup.sh`, `init-roles.sh`, `db/*.sql` | El flujo de GitHub, en cada despliegue | No |
| `db.env`, `api.env`, `worker.env`, `conversation.env`, `billing.env`, `admin.env` | Tú, una vez. `init-roles.sh` añade las URL de la base de datos | Sí (modo 600) |
| `.env` | `deploy.sh`. Solo las dos imágenes en uso | No |
| `.versions` | `deploy.sh`. Las últimas cinco etiquetas desplegadas | No |
| `vertex/cuenta-servicio.json` | Tú, si Gemini va por Vertex AI (ver «Gemini en Vertex AI»). Carpeta 500 y archivo 400, del UID 10001 | Sí |
| `backups/` | `backup.sh` | Sí (modo 700) |

### Dónde viven los secretos

Los demás servicios de Bitclick guardan los secretos de la aplicación en GitHub, y el flujo escribe el `.env` en el servidor en cada despliegue. Fichaitor se aparta de esa costumbre en un punto: **los secretos de la aplicación viven solo en el servidor** y el flujo no los lee ni los escribe. Motivos:

- La clave privada de los tokens no debe pasar por GitHub.
- Las contraseñas de la base de datos se generan en el servidor.
- Un flujo que reescribe el archivo en cada despliegue borraría lo que `init-roles.sh` ha escrito en él.

En GitHub solo están los secretos de acceso al servidor y a GHCR, con los mismos nombres que en Bitclick.

## Requisitos previos

- Acceso por SSH al VPS con un usuario del grupo `docker`.
- El repositorio de Fichaitor en GitHub, con la rama `main` y la integración continua en verde.
- Acceso al panel de Nginx Proxy Manager del VPS (túnel SSH al puerto 81).
- Acceso al panel de DNS de `fichaitor.es` en IONOS.
- Acceso a Google Cloud Console con la cuenta que administra el proyecto de Fichaitor.
- Un gestor de contraseñas donde guardar la clave privada de los tokens.

## Primera instalación

### 1. Prepara GitHub

1. Crea el repositorio en GitHub y sube la rama `main`. Comprueba que `web/Dockerfile`, `web/nginx.conf` y `web/app/` están en el repositorio: sin ellos, la imagen de la web no se construye.

2. Crea el entorno: **Settings > Environments > New environment**, con el nombre `production`.

3. En el entorno, activa **Required reviewers** y añádete. Así cada despliegue espera tu aprobación.

   > **Nota:** en un repositorio privado, los revisores obligatorios dependen del plan de GitHub. Si la opción no aparece, el despliegue sigue siendo manual, pero sin paso de aprobación.

4. En **Deployment branches and tags**, permite solo la rama `main` y las etiquetas `v*`.

   > **Importante:** esta regla es la protección real del despliegue, no las comprobaciones del flujo. `workflow_dispatch` se puede lanzar desde cualquier rama y entonces se ejecuta el `deploy.yml` **de esa rama**: quien pueda escribir en el repositorio puede quitar en su rama la condición del trabajo `desplegar` (`main` o una etiqueta `v*`), la comprobación «el commit está en `main`» y la de la integración continua. Lo que no puede saltarse es la regla del entorno: GitHub no entrega los secretos de `production` (la clave SSH y `GHCR_TOKEN`) a un trabajo de otra rama. Por eso hacen falta las dos cosas: esta regla y los revisores obligatorios del paso 3 (hallazgo I19 de `docs/seguridad/revision-2026-11-12-c.md`).

5. Crea estos secretos en el entorno `production`:

   | Secreto | Valor |
   |---|---|
   | `SSH_HOST` | Dirección del VPS |
   | `SSH_USER` | Usuario de despliegue |
   | `SSH_PRIVATE_KEY` | Clave privada SSH de ese usuario |
   | `SSH_PORT` | Puerto SSH. Opcional; por defecto, 22 |
   | `SSH_KNOWN_HOSTS` | Salida de `ssh-keyscan -p <puerto> <host>`, obtenida desde una red de confianza. Obligatorio: sin él, el flujo se detiene antes de conectar |
   | `GHCR_TOKEN` | Token personal **clásico** (no de grano fino) con el permiso `read:packages`. El servidor lo usa para descargar las imágenes |
   | `GHCR_USER` | Usuario de GitHub dueño de `GHCR_TOKEN`, con acceso al repositorio. No vale el nombre de la organización |

   Los cuatro primeros y `GHCR_TOKEN` son los mismos que usa el repositorio de Bitclick. Los secretos no se comparten entre repositorios: hay que crearlos de nuevo aquí.

6. Opcional: crea la variable `FICHAITOR_URL_PUBLICA` con el valor `https://fichaitor.es` cuando el dominio ya apunte al VPS. El flujo comprueba esa dirección al final de cada despliegue.

### 2. Prepara la carpeta en el VPS

1. Entra en el VPS con el usuario de despliegue.

2. Crea la carpeta:

   ```bash
   sudo mkdir -p /opt/bitclick/fichaitor
   sudo chown "$USER": /opt/bitclick/fichaitor
   chmod 750 /opt/bitclick/fichaitor
   ```

3. Comprueba que existe la red del proxy:

   ```bash
   docker network inspect bitclick-network --format '{{.Name}}'
   ```

   La orden debe imprimir `bitclick-network`. Si falla, no crees la red: averigua antes por qué falta, porque el resto de servicios de Bitclick depende de ella.

### 3. Crea los archivos de secretos

1. Crea `db.env` con la contraseña del propietario de la base de datos. La contraseña se genera en el servidor y no se muestra:

   ```bash
   cd /opt/bitclick/fichaitor
   ( umask 077; printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)" > db.env )
   ```

   > **Nota:** PostgreSQL solo lee esta contraseña al crear el volumen. Los scripts no la necesitan: entran por el socket local del contenedor.

2. Desde tu equipo, copia las dos plantillas al servidor:

   ```bash
   scp deploy/env.api.example <usuario>@<host>:/opt/bitclick/fichaitor/api.env
   scp deploy/env.worker.example <usuario>@<host>:/opt/bitclick/fichaitor/worker.env
   scp deploy/env.conversation.example <usuario>@<host>:/opt/bitclick/fichaitor/conversation.env
   ```

3. En el servidor, protege los tres archivos:

   ```bash
   chmod 600 /opt/bitclick/fichaitor/api.env /opt/bitclick/fichaitor/worker.env \
     /opt/bitclick/fichaitor/conversation.env
   ```

### 4. Genera el par de claves

El par cifra los tokens de Google. La API cifra con la pública; el worker descifra con la privada.

1. En tu equipo, en la raíz del repositorio, genera el par:

   ```bash
   uv run python scripts/gen_token_keys.py
   ```

2. Guarda la clave privada en el gestor de contraseñas **ahora**.

   > **Precaución:** si pierdes la clave privada, todos los clientes tienen que volver a conectar Google. La copia de la base de datos no la incluye. Consulta `deploy/restore.md`.

3. No dejes las claves en un archivo de tu equipo ni las pegues en un chat.

### 5. Rellena `api.env` y `worker.env`

1. En el servidor, edita `api.env` y rellena estas variables:

   | Variable | Valor |
   |---|---|
   | `FICHAITOR_PLACES_API_KEY` | Clave de Places API (New), con límite de gasto diario |
   | `FICHAITOR_GOOGLE_CLIENT_ID` | Identificador del cliente OAuth |
   | `FICHAITOR_GOOGLE_CLIENT_SECRET` | Secreto del cliente OAuth |
   | `FICHAITOR_OAUTH_STATE_SECRET` | Genera uno en el servidor con `openssl rand -base64 48 \| tr -d '\n=+/'` |
   | `FICHAITOR_TOKEN_PUBLIC_KEY` | La clave pública del paso anterior |
   | `FICHAITOR_GEMINI_API_KEY` | Opcional. Sin ella, la demostración por captura no existe |

2. Edita `worker.env` y rellena estas variables:

   | Variable | Valor |
   |---|---|
   | `FICHAITOR_TOKEN_PRIVATE_KEY` | La clave privada del paso anterior |
   | `FICHAITOR_GOOGLE_CLIENT_ID` | El mismo que en `api.env` |
   | `FICHAITOR_GOOGLE_CLIENT_SECRET` | El mismo que en `api.env` |

3. Edita `conversation.env` (Telegram; consulta [Webhook de Telegram](#webhook-de-telegram)):

   | Variable | Valor |
   |---|---|
   | `FICHAITOR_TELEGRAM_BOT_TOKEN` | El token de `@FichaitorBot` que da BotFather |
   | `FICHAITOR_LINK_CODE_PEPPER` | Genera uno con `openssl rand -hex 32` y pon **el mismo** en `api.env` |
   | `FICHAITOR_TELEGRAM_WEBHOOK_SECRET` | Genera uno con `openssl rand -hex 32` y pon **el mismo** en `api.env` |
   | `FICHAITOR_GEMINI_API_KEY` | La misma que en `api.env`. Sin ella, las reseñas no se proponen por Telegram |

4. Deja vacías `FICHAITOR_DATABASE_URL`, `FICHAITOR_WORKER_DATABASE_URL` y `FICHAITOR_CONVERSATION_DATABASE_URL`. Las escribe `init-roles.sh` en el primer despliegue.

Reglas que `deploy.sh` comprueba antes de tocar nada:

- Los cuatro archivos tienen modo 600.
- Ni `api.env` ni `conversation.env` contienen `FICHAITOR_TOKEN_PRIVATE_KEY`.
- `api.env` no contiene `FICHAITOR_WORKER_DATABASE_URL` ni `FICHAITOR_CONVERSATION_DATABASE_URL`.
- `FICHAITOR_LINK_CODE_PEPPER` existe, tiene 32 caracteres o más y es igual en `api.env` y `conversation.env`.
- `FICHAITOR_TELEGRAM_WEBHOOK_SECRET` tiene 32 caracteres o más en `api.env`, siempre.
- `conversation.env` no pide sondeo largo: `FICHAITOR_TELEGRAM_POLLING` vacío o `false`. En producción solo hay webhook (el Compose lo fuerza), así que pedir sondeo detiene el despliegue en vez de saltarse la medida del secreto (I22).
- Ningún archivo activa un modo de ejemplo: ninguna variable `FICHAITOR_FAKE_*` (`PLACES`, `GOOGLE`, `SCREENSHOT`, `MAIL` y las que se añadan) con un valor que no sea vacío, `false`, `0`, `no` u `off`.
- El identificador del cliente OAuth coincide en los dos archivos.

Estos valores no van en los archivos: los impone `docker-compose.prod.yml` y ganan a lo que diga `api.env`.

| Variable | Valor en producción |
|---|---|
| `FICHAITOR_POST_CONNECT_URL` | `https://fichaitor.es/app` |
| `FICHAITOR_GOOGLE_REDIRECT_URI` | `https://fichaitor.es/api/v1/google/oauth/callback` |
| `FICHAITOR_COOKIE_SECURE` | `true` |
| `FICHAITOR_TRUST_PROXY_HEADERS` | `true` |
| `FICHAITOR_FAKE_PLACES`, `FICHAITOR_FAKE_GOOGLE`, `FICHAITOR_FAKE_SCREENSHOT`, `FICHAITOR_FAKE_MAIL` | `false` |

El resto de variables son opcionales y tienen valor por defecto. Están todas, comentadas, en las dos plantillas.

### 6. Lanza el primer despliegue

1. En GitHub, abre **Actions > Despliegue > Run workflow**.

2. Elige la rama `main`.

3. Elige el valor de `legal_estricto`:

   - `1` (por defecto): la imagen de la web no se construye si `docs/legal` conserva marcas `[PENDIENTE]` o `[REVISAR]`. Hoy quedan marcas, así que el despliegue falla en ese paso.
   - `0`: publica los textos legales tal como están.

   > **Precaución:** desplegar con `0` es publicar una política de privacidad en borrador. Esa decisión es tuya. Sirve para montar y probar el servidor antes de abrir el sitio, no para publicarlo.

4. Pulsa **Run workflow** y aprueba el despliegue cuando GitHub lo pida.

5. Espera a que termine. El primer despliegue crea la base de datos, aplica el esquema, crea los usuarios `fichaitor_api_prod` y `fichaitor_worker_prod`, y arranca los cuatro contenedores.

6. En el servidor, comprueba el estado:

   ```bash
   cd /opt/bitclick/fichaitor
   docker compose ps
   ```

   Los cuatro contenedores deben aparecer en marcha. `fichaitor-db`, `fichaitor-api` y `fichaitor-web` deben estar `healthy`.

### 7. Apunta el dominio al VPS

Hoy `fichaitor.es` apunta a IONOS. El certificado del paso siguiente necesita que el dominio ya apunte al VPS.

1. Entra en el panel de DNS de `fichaitor.es` en IONOS.

2. Cambia el registro `A` de `@` a la dirección IPv4 del VPS.

3. Cambia el registro `A` de `www` a la misma dirección.

4. Borra los registros `AAAA` de `@` y de `www` si el VPS no tiene IPv6. Con un `AAAA` que apunta a IONOS, la petición del certificado falla.

5. No toques los registros `MX`, `TXT` ni los de correo: `soporte@fichaitor.es` depende de ellos. No cambies los servidores de nombres.

6. Comprueba el cambio desde tu equipo:

   ```bash
   dig +short fichaitor.es
   dig +short www.fichaitor.es
   ```

   Las dos órdenes deben imprimir la dirección del VPS.

### 8. Crea el host en Nginx Proxy Manager

1. Abre el panel de Nginx Proxy Manager.

2. Ve a **Hosts > Proxy Hosts > Add Proxy Host**.

3. En la pestaña **Details**, rellena:

   | Campo | Valor |
   |---|---|
   | Domain Names | `fichaitor.es` |
   | Scheme | `http` |
   | Forward Hostname / IP | `fichaitor-web` |
   | Forward Port | `8080` |
   | Cache Assets | Desactivado. La web ya fija su caché |
   | Block Common Exploits | Desactivado. Consulta la nota de abajo |
   | Websockets Support | Desactivado |

4. En la pestaña **SSL**, elige **Request a new SSL Certificate** y activa **Force SSL** y **HTTP/2 Support**. Pide el certificado en esta misma alta.

   > **Precaución:** no crees primero el host y pidas después el certificado. En este proxy, pedir un certificado para un host activo sin certificado falla. Let's Encrypt admite cinco fallos por hora y dominio.

5. Deja la pestaña **Advanced** vacía y guarda.

6. Crea la redirección de `www`: **Hosts > Redirection Hosts > Add Redirection Host**, con `www.fichaitor.es` hacia `fichaitor.es`, esquema `https`, **Preserve Path** activado y su propio certificado.

7. Cuando hayas comprobado que el sitio funciona por HTTPS, activa **HSTS Enabled** en la pestaña **SSL** del host. No actives **HSTS Subdomains**.

Sobre las cabeceras:

- **No añadas ni cambies cabeceras de reenvío.** Las que Nginx Proxy Manager pone por defecto (`X-Forwarded-For`, `X-Forwarded-Proto`, `X-Real-IP`) son las que espera el nginx de `fichaitor-web`. Ese nginx resuelve la IP real del visitante y entrega a la API una sola dirección; por eso la API lleva `FICHAITOR_TRUST_PROXY_HEADERS=true`.
- **No pongas `X-Robots-Tag: noindex`.** Esa línea es para los subdominios internos de Bitclick. `fichaitor.es` es un sitio público y debe indexarse.
- El nginx de `fichaitor-web` ya envía `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` y `Permissions-Policy`.

> **Nota:** deja **Block Common Exploits** desactivado al principio. Ese filtro rechaza peticiones con direcciones web en los parámetros, y el retorno de Google las trae. No está probado con este proxy. Si lo activas, repite después la conexión completa con Google.

> **Nota:** la protección contra direcciones IP falsas supone que delante de `fichaitor-web` solo está Nginx Proxy Manager. Si algún día pones una CDN u otro proxy delante, revisa `web/nginx.conf`.

### 9. Da de alta las direcciones de producción en Google

1. Abre Google Cloud Console con el proyecto de Fichaitor y ve a **Google Auth Platform > Clients**.

2. Abre el cliente OAuth de tipo "Web application".

3. En **Authorized redirect URIs**, añade exactamente:

   ```text
   https://fichaitor.es/api/v1/google/oauth/callback
   ```

4. Deja **Authorized JavaScript origins** vacío. Si la consola obliga a poner uno, usa `https://fichaitor.es`.

5. En **Branding**, comprueba la página principal (`https://fichaitor.es`), la política de privacidad (`https://fichaitor.es/privacidad/`), las condiciones (`https://fichaitor.es/condiciones-de-uso/`) y el dominio autorizado (`fichaitor.es`).

6. Guarda. Google puede tardar unos minutos en aplicar el cambio.

El estado del proyecto de Google y los trámites pendientes están en [fase0/google-acceso.md](fase0/google-acceso.md).

### 10. Comprueba el sitio

1. Comprueba la portada y las cabeceras:

   ```bash
   curl -s -D - -o /dev/null https://fichaitor.es/ | grep -i -E "^HTTP|strict-transport|x-frame"
   ```

2. Comprueba que `/api/` llega a la API. Una ruta que no existe debe responder 404, no 502:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://fichaitor.es/api/comprobacion
   ```

3. Abre `https://fichaitor.es/app` y conecta una cuenta de Google de prueba.

4. Comprueba que el worker ha sincronizado la ficha:

   ```bash
   docker logs --tail 20 fichaitor-worker
   ```

   Debe aparecer un evento `job_done`.

5. Comprueba el correo: en `/app`, **Ajustes**, pide un código de baja de ficha y no lo confirmes. Debe llegar desde `avisos@fichaitor.es`, y al responder debe ir a `soporte@fichaitor.es`. Si la web dice que no ha podido enviarlo, revisa `docker logs fichaitor-worker` (evento `send_mail`) y las variables `FICHAITOR_SMTP_*` de `worker.env`.

6. Envía el mapa del sitio en Google Search Console: propiedad `fichaitor.es`, **Sitemaps**, añade `https://fichaitor.es/sitemap.xml`. Comprueba también que `https://www.fichaitor.es/` redirige a `https://fichaitor.es/` (paso 8).

## Publicar solo la web

Sirve para tener `fichaitor.es` en línea, con la portada y las páginas legales, antes de abrir el producto: la pantalla de consentimiento de Google y Search Console necesitan la página principal y la política publicadas. No necesita base de datos ni archivos de secretos.

Con este modo, `/app` enseña «Muy pronto» y `/api/` responde 503. El primer despliegue completo sustituye el contenedor `fichaitor-web` sin pasos a mano (mismo nombre de proyecto y de contenedor).

> **Precaución:** no lances `solo_web = 1` con el producto completo en marcha. Sustituye su `fichaitor-web` por la del modo solo web (mismo proyecto y mismo contenedor) y **la API deja de estar accesible: `/api/` responde 503** para todos los clientes, aunque `fichaitor-api` siga en marcha. Para volver, despliega otra vez el producto completo (`solo_web = 0`) o haz `./deploy.sh rollback` a la etiqueta en uso (hallazgo I19 de `docs/seguridad/revision-2026-11-12-c.md`).

1. Haz los pasos 1 (GitHub) y 2 (carpeta en el VPS) de la primera instalación. Los pasos 3 a 6 no hacen falta.
2. Haz los pasos 7 (DNS) y 8 (host en Nginx Proxy Manager, con la redirección de `www`).
3. En GitHub, **Actions > Desplegar > Run workflow**, con `solo_web = 1` y `legal_estricto = 1`. Si los textos legales tienen marcas abiertas, el flujo se detiene antes de publicar.
4. Comprueba la portada y las páginas legales:

   ```bash
   for ruta in / /privacidad/ /condiciones-de-uso/; do curl -s -o /dev/null -w "%{http_code} $ruta
" "https://fichaitor.es$ruta"; done
   ```

   Las tres deben responder 200.
5. Pon las URL en la pantalla de consentimiento (paso 9 y `docs/fase0/google-acceso.md`) y envía el mapa del sitio en Search Console.

Lo hace `deploy/deploy-web.sh` con `deploy/docker-compose.web.yml`. Si la web nueva no pasa la comprobación de salud, vuelve a la anterior.

## Desplegar una versión nueva

Hay dos formas. Las dos exigen que el commit esté en `main` y que su integración continua haya terminado bien. El trabajo `desplegar` solo corre desde `main` o desde una etiqueta `v*`; la barrera que de verdad lo asegura es la regla de ramas del entorno `production` (paso 1).

**A mano:**

1. Abre **Actions > Despliegue > Run workflow**.
2. Elige `main` y el valor de `legal_estricto`.
3. Aprueba el despliegue.

**Con una etiqueta de versión:**

1. Crea y sube la etiqueta:

   ```bash
   git tag v1.2.3
   git push origin v1.2.3
   ```

2. Aprueba el despliegue en GitHub.

   En un despliegue por etiqueta, `LEGAL_ESTRICTO` vale `1`, salvo que definas la variable del repositorio `FICHAITOR_LEGAL_ESTRICTO`.

Un push a una rama no despliega nunca.

Qué hace cada despliegue, en este orden:

1. Comprueba los archivos de entorno.
2. Descarga las dos imágenes.
3. Hace una copia de seguridad de la base de datos.
4. Aplica `db/schema.sql` y `db/schema_worker.sql` en una sola transacción, con el usuario propietario.
5. Crea los usuarios de la base de datos que falten.
6. Recrea la API, el worker y la web.
7. Comprueba la salud. Si falla, vuelve sola a la versión anterior y el flujo termina en rojo.

Comprobaciones de salud:

| Servicio | Comprobación |
|---|---|
| `fichaitor-api` | `GET /healthz` responde 200 |
| `fichaitor-web` | `GET /` responde 200 y `/api/` llega a la API |
| `fichaitor-worker` | Sigue en marcha 20 segundos sin reiniciarse |

> **Nota:** el worker no tiene endpoint HTTP. La comprobación detecta un error de configuración o de conexión con la base de datos, porque el worker termina y Docker lo reinicia en bucle. No detecta una cola atascada. Consulta la lista de comprobación.

> **Nota:** el esquema se aplica con la versión anterior todavía en marcha. Por eso todo cambio de esquema debe ser compatible con el código anterior: añade tablas y columnas, no las borres ni las renombres en el mismo despliegue.

## Volver a una versión anterior

Si un despliegue falla la comprobación de salud, `deploy.sh` vuelve solo a la versión anterior. Para volver a mano:

1. En el servidor, mira las versiones desplegadas. La primera línea es la que está en uso:

   ```bash
   cat /opt/bitclick/fichaitor/.versions
   ```

2. Vuelve a la que elijas:

   ```bash
   /opt/bitclick/fichaitor/deploy.sh rollback <commit> <propietario>
   ```

   `<propietario>` es el usuario u organización de GitHub dueño de las imágenes, en minúsculas.

La vuelta atrás cambia las imágenes, no la base de datos. El esquema nuevo sigue aplicado. Si necesitas deshacer datos, restaura la copia `pre-<commit>` que hizo el despliegue: consulta `deploy/restore.md`.

## Copias de seguridad

- `deploy.sh` hace una copia antes de aplicar el esquema en cada despliegue.
- Para hacer una copia a mano, ejecuta `/opt/bitclick/fichaitor/backup.sh`.
- Las copias se guardan en `/opt/bitclick/fichaitor/backups` y se borran a los 14 días.
- La copia diaria programada y la restauración están en `deploy/restore.md`.

> **Importante:** las copias están en el mismo servidor que la base de datos y no hay copia externa. La política de copias y de retención está sin decidir.

## Webhook de Telegram

Telegram entrega cada mensaje del bot en `https://fichaitor.es/api/v1/webhooks/telegram`. La API solo acepta las peticiones que traen la cabecera `X-Telegram-Bot-Api-Secret-Token` con el secreto `FICHAITOR_TELEGRAM_WEBHOOK_SECRET`, comparada en tiempo constante; sin el secreto configurado, la ruta no existe. Encola cada actualización y responde 200; la conversación hace el resto.

El webhook se registra **a mano, una vez**, y otra vez solo si cambian la URL o el secreto. El despliegue no lo registra.

> **Precaución:** con el webhook registrado, `getUpdates` deja de funcionar. Nunca arranques en local `FICHAITOR_TELEGRAM_POLLING=true` con el token de producción: la prueba borra el webhook. Usa un bot de pruebas.

1. Genera el secreto en el servidor y ponlo, **igual**, en `api.env` y en `conversation.env` como `FICHAITOR_TELEGRAM_WEBHOOK_SECRET` (de 1 a 256 caracteres de `A-Z`, `a-z`, `0-9`, `_` y `-`; `openssl rand -hex 32` cumple):

   ```bash
   openssl rand -hex 32
   ```

2. Comprueba que `conversation.env` tiene `FICHAITOR_TELEGRAM_BOT_TOKEN`.

3. Recrea la API para que registre la ruta, y comprueba que existe. Sin la cabecera debe responder 403:

   ```bash
   cd /opt/bitclick/fichaitor
   docker compose up -d --force-recreate fichaitor-api fichaitor-conversation
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://fichaitor.es/api/v1/webhooks/telegram
   ```

4. Registra el webhook desde el contenedor de la conversación, que tiene el token y el secreto. No escribe ninguno de los dos en la pantalla:

   ```bash
   docker exec fichaitor-conversation python -m app.worker.conversation \
     --set-webhook https://fichaitor.es/api/v1/webhooks/telegram
   ```

   La orden llama a `setWebhook` con `secret_token` y `allowed_updates` `["message", "callback_query"]`. Debe terminar con `webhook_set`.

5. Comprueba el estado sin exponer el token en la línea de órdenes del servidor (lo lee del archivo y lo pasa por la entrada estándar):

   ```bash
   docker exec -i fichaitor-conversation python - <<'PY'
   import json, os, urllib.request
   url = f"https://api.telegram.org/bot{os.environ['FICHAITOR_TELEGRAM_BOT_TOKEN']}/getWebhookInfo"
   info = json.load(urllib.request.urlopen(url))["result"]
   print({k: info.get(k) for k in ("url", "pending_update_count", "last_error_message")})
   PY
   ```

   `url` debe ser la de arriba y `last_error_message`, vacío.

6. Envía `/ayuda` al bot desde tu Telegram. Debe contestar en unos segundos. Si no:
   - `docker logs --tail 80 fichaitor-conversation`: busca `job_failed` o `telegram_disabled`.
   - Comprueba que el job llegó: `docker exec fichaitor-db psql -U fichaitor -d fichaitor -c "SELECT state, count(*) FROM job WHERE kind = 'channel_inbound' GROUP BY state"`.

**Cambiar el secreto:** genera otro, cámbialo en los dos archivos, recrea `fichaitor-api` y `fichaitor-conversation`, y repite el paso 4. Mientras tanto Telegram reintenta las entregas rechazadas durante 24 horas.

**Si se filtra el token del bot:** en `@BotFather`, `/token` genera otro. Cámbialo en `conversation.env`, recrea `fichaitor-conversation` y repite el paso 4.

## WhatsApp

Fichaitor tiene **un solo número de WhatsApp**, en la WhatsApp Business Account (WABA) de Bitclick, y usa la Cloud API de Meta. Enviar desde un número propio no exige ser proveedor de tecnología (Tech Provider): eso solo hace falta para conectar números de los clientes, y queda fuera. Cómo funciona el canal: `docs/canales.md`, «WhatsApp». Las plantillas: `docs/whatsapp-plantillas.md`.

Todo lo de esta sección lo hace el propietario en Meta y en el servidor. Fichaitor no llama a Meta para configurar nada: solo envía mensajes y recibe el webhook.

> **Importante:** Los nombres de los menús de Meta cambian a menudo. Si un paso no coincide, busca la acción en la documentación de Meta citada en `docs/canales.md`, «WhatsApp», y anota aquí el cambio.

### 1. El número en la WABA

1. En **Meta Business Suite > Configuración del negocio > Cuentas > Cuentas de WhatsApp**, abre la WABA de Bitclick.
2. En **WhatsApp Manager > Números de teléfono**, añade el número de Fichaitor. Tiene que poder recibir un SMS o una llamada para verificarlo, y no puede estar en uso en la app de WhatsApp ni en WhatsApp Business.
3. Pon el **nombre visible** «Fichaitor». Meta lo revisa: hasta que lo aprueba, el número tiene límites más bajos.
4. Copia el **identificador del número** (Phone number ID: solo cifras). Es `FICHAITOR_WHATSAPP_PHONE_NUMBER_ID`.
5. Comprueba que la WABA tiene un **método de pago**. Desde el 1 de octubre de 2026 los mensajes de servicio fuera de los 1.000 gratis al mes y las plantillas de utilidad se cobran, y sin método de pago no se entregan.

### 2. La app y el webhook

1. En [developers.facebook.com](https://developers.facebook.com/apps), usa la app de Meta de Bitclick que tenga el producto **WhatsApp** y esté vinculada a la WABA, o crea una de tipo **Negocio** para Fichaitor.

   > **Precaución:** Una app tiene una sola URL de webhook. Si la app de Bitclick ya la usa para otra cosa (n8n, otros clientes), no la cambies: usa una app propia para Fichaitor, o la sustitución por número (`POST /<PHONE_NUMBER_ID>` con `webhook_configuration.override_callback_uri`, documentada en «Override the callback URL»). La API de Fichaitor ignora lo que no sea de su número, pero no lo reenvía a nadie.

2. En **Configuración de la app > Básica**, copia el **secreto de la app** (App Secret). Es `FICHAITOR_WHATSAPP_APP_SECRET`.
3. Genera el testigo de verificación en el servidor. Es `FICHAITOR_WHATSAPP_VERIFY_TOKEN`:

   ```bash
   openssl rand -hex 32
   ```

4. Rellena `api.env` y despliega (paso 4). La API registra `GET` y `POST /api/v1/webhooks/whatsapp` solo con las cuatro variables puestas.
5. En **WhatsApp > Configuración > Webhook**, pon la URL `https://fichaitor.es/api/v1/webhooks/whatsapp` y el testigo del paso 3, y pulsa **Verificar y guardar**. Meta hace un `GET` con `hub.challenge`; la API lo devuelve si el testigo coincide.
6. En la misma pantalla, suscríbete al campo **messages**. Ningún otro hace falta.

### 3. El usuario del sistema y el token permanente

1. En **Configuración del negocio > Usuarios > Usuarios del sistema**, crea un usuario del sistema **administrador** (por ejemplo «fichaitor-envios»), o usa el de Bitclick.
2. **Asignar activos**: la app (con **Administrar app**) y la WABA (con control total).
3. **Generar token**: elige la app, caducidad **Nunca**, y los permisos `whatsapp_business_messaging` y `whatsapp_business_management` (Meta pide también `business_management` en su guía). Copia el token: no se vuelve a mostrar. Es `FICHAITOR_WHATSAPP_ACCESS_TOKEN`.
4. Si la app no está ya suscrita a la WABA, suscríbela una vez (`POST /<WABA_ID>/subscribed_apps` con ese token, desde el Explorador de la API Graph). Sin eso no llegan los mensajes al webhook.

> **Precaución:** El token envía mensajes en nombre de Bitclick. Va **solo** en `conversation.env`. Si se filtra, en **Usuarios del sistema** revócalo, genera otro, cámbialo y recrea `fichaitor-conversation`.

### 4. Las variables y el despliegue

| Variable | Archivo | Qué es |
|---|---|---|
| `FICHAITOR_WHATSAPP_PHONE_NUMBER_ID` | `api.env` y `conversation.env` (el mismo) | Identificador del número |
| `FICHAITOR_WHATSAPP_APP_SECRET` | Solo `api.env` | Secreto de la app: firma del webhook. 32 caracteres |
| `FICHAITOR_WHATSAPP_VERIFY_TOKEN` | Solo `api.env` | Testigo de verificación. 32 caracteres o más |
| `FICHAITOR_WHATSAPP_DISPLAY_NUMBER` | Solo `api.env` | El número para el enlace `wa.me`: `+34 600 000 000` |
| `FICHAITOR_WHATSAPP_ACCESS_TOKEN` | Solo `conversation.env` | Token permanente del usuario del sistema. 100 caracteres o más |

`deploy.sh` (`comprobar_whatsapp`) se niega a desplegar si falta alguna, si no tiene su longitud mínima, si el identificador no coincide entre los dos archivos o si un secreto está en el archivo de otro proceso. Sin ninguna, WhatsApp queda apagado y todo lo demás sigue igual. La conversación, además, no arranca si recibe los secretos del webhook.

```bash
cd /opt/bitclick/fichaitor
docker compose up -d --force-recreate fichaitor-api fichaitor-conversation
# Sin firma debe responder 403:
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://fichaitor.es/api/v1/webhooks/whatsapp
```

> **Nota:** El nginx de la web acepta como mucho 64 KB en `/api/v1/webhooks/` (`web/nginx.conf`) y la API, lo mismo. Una entrega de Meta con un mensaje de texto ocupa alrededor de 1 KB, pero Meta admite hasta 3 MB. Si en el registro aparecen 413 en esta ruta, hay que subir los dos límites.

### 5. Las plantillas

Fuera de las 24 horas desde el último mensaje del dueño, WhatsApp solo admite plantillas aprobadas. Créalas **tú** en **WhatsApp Manager > Plantillas de mensajes**, exactamente como están en `docs/whatsapp-plantillas.md` (nombre, idioma `es`, categoría **Utilidad**, cuerpo, ejemplos y botones). Sin ellas, lo que haya que enviar fuera de la ventana no sale por WhatsApp (sale por Telegram, si está vinculado, o se queda en el panel). Anota la categoría que asigne Meta a cada una en `docs/fase0/whatsapp.md`.

### 6. La verificación del negocio

Una cartera sin verificar solo puede escribir, fuera de la ventana, a 250 personas distintas en 24 horas, y la cuenta de todos sus números es una sola. Para pasar a 2.000:

1. En **Configuración del negocio > Centro de seguridad**, pulsa **Iniciar verificación** y sube los documentos de Bitclick (escritura o CIF, y un justificante de domicilio o una factura con el nombre y la dirección).
2. Espera la aprobación (Meta suele tardar de unos días a dos semanas).

Los avisos dentro de la ventana no cuentan para ese límite.

### 7. Comprobar

1. En el panel de una cuenta con alguna ficha Pro+, pide el código de vinculación y abre `whatsapp_url` desde tu móvil. Pulsa **Enviar** sin cambiar el texto.
2. El número debe contestar «Listo…» en unos segundos. Si no:
   - `docker logs --tail 80 fichaitor-api | grep -i whatsapp`: un 403 es la firma (secreto de la app equivocado).
   - `docker logs --tail 80 fichaitor-conversation`: busca `whatsapp_disabled`, `job_failed` o «bolsa del mes agotada».
   - El job: `docker exec fichaitor-db psql -U fichaitor -d fichaitor -c "SELECT state, count(*) FROM job WHERE kind = 'channel_inbound' AND idempotency_key LIKE 'whatsapp:%' GROUP BY state"`.
3. `GET /api/v1/me` debe enseñar `whatsapp.used` en 1 (la bienvenida).

## Cobro: Stripe, Odoo y el túnel

El cobro tiene dos procesos propios (`docs/admin-facturacion.md`, 4.1): `fichaitor-billing`, el único que habla con Stripe y con Odoo, y `fichaitor-admin`, el backoffice, que solo se abre por un túnel SSH. Cada uno tiene su archivo de entorno y su usuario de base de datos, que crea `init-roles.sh`.

> **Importante:** mientras `billing_setting.launch_at` esté vacío no se cobra nada. Hasta el lanzamiento, usa solo claves de **prueba** (`rk_test_…`). Nunca pegues una clave en un chat ni la subas a GitHub.

### Alta de `billing.env` y `admin.env`

`deploy.sh` exige los dos archivos, con modo 600, desde este paso.

1. Desde tu equipo, copia las plantillas:

   ```bash
   scp deploy/env.billing.example <usuario>@<host>:/opt/bitclick/fichaitor/billing.env
   scp deploy/env.admin.example <usuario>@<host>:/opt/bitclick/fichaitor/admin.env
   ```

2. En el servidor, protégelos:

   ```bash
   chmod 600 /opt/bitclick/fichaitor/billing.env /opt/bitclick/fichaitor/admin.env
   ```

3. En `billing.env`, rellena:

   | Variable | Valor |
   |---|---|
   | `FICHAITOR_STRIPE_SECRET_KEY` | La clave restringida de Stripe, con los permisos de la sección 4.1 de `docs/admin-facturacion.md`. `rk_test_…` hasta el lanzamiento |
   | `FICHAITOR_STRIPE_LIVEMODE` | `false` con una clave de prueba; `true` solo con una `rk_live_…`. `deploy.sh` rechaza las combinaciones cruzadas |
   | `FICHAITOR_ODOO_URL`, `FICHAITOR_ODOO_DB` y `FICHAITOR_ODOO_API_KEY` | El Odoo de Bitclick, su base y la clave de API del usuario «Fichaitor facturación» |
   | `FICHAITOR_ODOO_JOURNAL_ID` y `FICHAITOR_ODOO_PAYMENT_JOURNAL_ID` | Los id del diario de ventas «Fichaitor» (FTR, con secuencia propia RFTR) y del diario de banco «Stripe» |
   | `FICHAITOR_ODOO_TAX_IVA_21`, `…_NO_SUJETO` y `…_ISP_UE` | Los id de los impuestos de venta del 21 %, no sujeto e inversión del sujeto pasivo |
   | `FICHAITOR_ODOO_PRODUCT_PRO` y `…_PRO_WHATSAPP` | Los id de los productos de cada plan |

   `FICHAITOR_BILLING_DATABASE_URL` la escribe `init-roles.sh`: déjala vacía. Sin `FICHAITOR_ODOO_URL`, el cobro funciona, pero ninguna factura sale: quedan pendientes y el registro avisa con `invoice_overdue`.

4. En `admin.env`, deja vacía `FICHAITOR_ADMIN_DATABASE_URL` (la escribe `init-roles.sh`). La clave del TOTP y la pimienta se rellenan en el paso 12, con `openssl rand -hex 32`.

   > **Precaución:** `admin.env` no puede contener ninguna variable de Stripe ni de Odoo (`STRIPE_` u `ODOO_`), ni vacía: `deploy.sh` se detiene. Tampoco SMTP ni Google.

5. En `api.env`, pon el secreto de firma del webhook, `FICHAITOR_STRIPE_WEBHOOK_SECRET=whsec_…`. Es lo único de Stripe que tiene la API.

6. Lanza un despliegue. `deploy.sh` comprueba los secretos, crea `fichaitor_billing_prod` y `fichaitor_admin_prod` y arranca `fichaitor-billing`. Al arrancar, el proceso revisa la configuración de Odoo y escribe en el registro `odoo_setup_problem` por cada cosa que falte:

   ```bash
   docker logs --tail 40 fichaitor-billing
   ```

### Lo que tienes que preparar en Stripe y en Odoo

- **Stripe:** la sección 8 de `docs/admin-facturacion.md`. Además, en **Developers > Webhooks**, crea el *endpoint* `https://fichaitor.es/api/v1/webhooks/stripe` con los eventos de la sección 4.2 y copia su secreto en `api.env`. Ejecuta `scripts/stripe_bootstrap.py` una vez, con la clave de prueba, para crear los productos, los precios y los impuestos.
- **Odoo (sin módulo, decisión 9.0.1):** el diario «Fichaitor» con prefijo `FTR` y secuencia propia de rectificativas, el diario de banco «Stripe», los tres impuestos de venta, un producto por plan, el redondeo de impuestos **por línea** y el usuario técnico «Fichaitor facturación» con permisos solo de facturación y su clave de API. VERI\*FACTU, como dice la sección 2.4.

### Facturas pendientes

`issue_invoice` reintenta durante unos tres días si Odoo no responde. Si una factura sigue pendiente pasado el día 10 del mes siguiente al cobro, emítela a mano en Odoo con la referencia `stripe:<in_…>` (el id de la factura de Stripe) en el diario «Fichaitor». El siguiente reintento, o la conciliación diaria, la encuentra por esa referencia y no la duplica.

### Túnel del backoffice

El backoffice escucha solo en `127.0.0.1:8090` del VPS: no hay host en Nginx Proxy Manager ni registro DNS, y desde fuera el puerto no responde.

1. En el VPS, crea un usuario sin intérprete de órdenes:

   ```bash
   sudo useradd --create-home --shell /usr/sbin/nologin fichaitor-tunel
   ```

2. Añade una línea por persona en `/home/fichaitor-tunel/.ssh/authorized_keys`, con estas opciones delante de su clave pública:

   ```text
   restrict,port-forwarding,permitopen="127.0.0.1:8090",command="/usr/sbin/nologin" ssh-ed25519 AAAA… ana
   ```

   Quitar el acceso a alguien es borrar su línea.

3. Desde tu equipo, abre el túnel y deja la terminal abierta:

   ```bash
   ssh -N -L 8090:127.0.0.1:8090 fichaitor-tunel@<vps>
   ```

4. Abre `http://localhost:8090`.

5. Comprueba desde fuera del VPS que el puerto no responde:

   ```bash
   nc -vz -w 5 <vps> 8090
   ```

   Debe fallar. Si conecta, no sigas: revisa `ports:` de `fichaitor-admin` en el Compose.

> **Nota:** el servicio `fichaitor-admin` está en el perfil `backoffice` hasta que exista `app.admin` (paso 12): el despliegue todavía no lo arranca.

## Probar un pago en local

Con Stripe en modo de prueba y el Odoo falso. Nada sale de tu equipo salvo las llamadas a Stripe.

1. En `.env`, en la raíz del repositorio, pon una clave de **prueba** (nunca `live`):

   ```bash
   FICHAITOR_STRIPE_SECRET_KEY=rk_test_...
   ```

2. Instala el [Stripe CLI](https://docs.stripe.com/stripe-cli), entra en la cuenta de prueba y reenvía los eventos a la web local:

   ```bash
   stripe login
   stripe listen --forward-to localhost:8080/api/v1/webhooks/stripe
   ```

   La orden imprime un secreto `whsec_…`. Ponlo en `.env` como `FICHAITOR_STRIPE_WEBHOOK_SECRET` y recrea la API.

3. Crea los precios y los impuestos de prueba una vez, con la misma clave:

   ```bash
   FICHAITOR_STRIPE_SECRET_KEY=rk_test_... uv run python scripts/stripe_bootstrap.py
   ```

4. Fija una fecha de lanzamiento en el pasado en la base de desarrollo (solo en local):

   ```bash
   docker compose exec -T db psql -U fichaitor -d fichaitor \
     -c "UPDATE billing_setting SET launch_at = now() - interval '1 day'"
   ```

5. Arranca todo, con el cobro:

   ```bash
   docker compose --profile cobro up -d --build
   ```

   El servicio `billing` solo recibe de `.env` lo suyo: la clave de Stripe, su URL de base de datos y la URL pública. El Odoo es el falso (`FICHAITOR_FAKE_ODOO=true`): las facturas salen numeradas `FTR/<año>/00001`, en memoria.

6. En `http://localhost:8080/app`, completa los datos de facturación, elige «Pásate a Pro» y paga en Checkout con la tarjeta `4242 4242 4242 4242`, cualquier fecha futura y cualquier CVC.

7. Comprueba que la ficha se activa, que hay una factura en Facturación y que el worker envía el correo (con `FICHAITOR_FAKE_MAIL=true`, en su registro):

   ```bash
   docker compose logs --tail 40 billing worker
   ```

Para probar un impago, usa la tarjeta `4000 0000 0000 0341` o `stripe trigger invoice.payment_failed`. Para volver a la beta: `UPDATE billing_setting SET launch_at = NULL`.

## Gemini en Vertex AI

La beta llama a Gemini por AI Studio (la API de Gemini, con `FICHAITOR_GEMINI_API_KEY`). Para producción, la decisión es Vertex AI (hoy «Gemini Enterprise Agent Platform»), en Google Cloud, en una ubicación europea. El cambio es solo de configuración: el código ya habla con los dos proveedores (`app/adapters/gemini_client.py`, `gemini_auth.py` y `gemini_provider.py`) con el mismo cuerpo, la misma salida estructurada (`responseJsonSchema`), los mismos límites, los mismos errores, sin reintentos y con el mismo registro de tokens.

En Vertex hay dos formas de autenticarse. La elegida para producción es la **clave de API de Agent Platform vinculada a una cuenta de servicio** (`FICHAITOR_VERTEX_API_KEY`); la cuenta de servicio con su archivo JSON queda como alternativa. Ver [Clave de API o cuenta de servicio](#clave-de-api-o-cuenta-de-servicio).

> **Importante:** A 2026-11-12 no se ha hecho nada de lo que sigue en Google Cloud. Producción sigue en AI Studio.

### Qué ubicación y por qué

Consultado en la documentación oficial el 2026-11-12. Precios en USD por millón de tokens, nivel Standard (pago por uso):

| Ubicación | `gemini-3.6-flash` | `gemini-3.8-flash` | Dónde se procesa | Hasta el 31-12-2026 (entrada / salida) | Desde el 01-01-2027 |
|---|---|---|---|---|---|
| `eu`, multirregión UE (`aiplatform.eu.rep.googleapis.com`) | Sí | Sí | Solo en Estados miembros de la UE (no Reino Unido ni Suiza) | 0,825 / 4,125 | 1,65 / 8,25 |
| `global` (`aiplatform.googleapis.com`) | Sí | Sí | En cualquier región de Google del mundo | 0,75 / 3,75 | 1,50 / 7,50 |
| Una región, como `europe-west1` (Bélgica) o `europe-southwest1` (Madrid) | No figura | No figura | — | — | — |

- Fuentes: [Deployments and endpoints](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations), [Data residency](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency), [ficha de Gemini 3.6 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-6-flash), [ficha de Gemini 3.8 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash), [Standard PayGo](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/standard-paygo) y [precios](https://cloud.google.com/vertex-ai/generative-ai/pricing).
- Todo lo que no es `global` paga la tarifa «Non-global», un 10 % más. Ninguna ubicación europea es más barata que otra.
- Para los Gemini Flash actuales, Google no ofrece regiones europeas sueltas en pago por uso: solo `global`, `us` y `eu`.
- `global` es lo más barato y da menos errores 429, pero Google la desaconseja cuando hay requisitos de residencia: no se controla dónde se procesa. Las multirregiones tienen su propia capacidad y pueden dar algún 429 en horas punta.

**Elegido: `eu`** (`FICHAITOR_VERTEX_LOCATION=eu`, el valor por defecto). Es la única opción que garantiza que el procesamiento no sale de la UE, y cuesta lo mismo que cualquier otra que no sea `global`. El sobrecoste frente a `global` es del 10 %.

> **Precaución:** En Vertex AI, `gemini-3.6-flash` tiene fecha de retirada el **19-11-2026**, y Google pide pasar a `gemini-3.8-flash`. En AI Studio no hay fecha anunciada. Al pasar a Vertex, pon `FICHAITOR_GEMINI_MODEL=gemini-3.8-flash` y prueba antes la captura, las propuestas de respuesta y el análisis de opinión con ese modelo.

### Clave de API o cuenta de servicio

Consultado en la documentación oficial el 2026-11-13:

- Agent Platform acepta claves de API solo en `generateContent` y `streamGenerateContent`, que son los únicos métodos que usa Fichaitor ([Authenticate to Agent Platform](https://docs.cloud.google.com/vertex-ai/docs/authentication)).
- Hay dos tipos de clave ([Use the Agent Platform remote MCP server](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/use-agent-platform-mcp)): la del **modo exprés** y la **vinculada a una cuenta de servicio** en un proyecto estándar. Una clave estándar sin vincular no vale: no da a IAM ninguna identidad que comprobar.
- **Modo exprés** ([overview](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/express-mode/overview), [API reference](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/start/express-mode/vertex-ai-express-mode-api-reference)): solo para cuentas `@gmail.com`, en vista previa (Pre-GA), con cuotas reducidas y una lista cerrada de modelos en la que **no están** `gemini-3.6-flash` ni `gemini-3.8-flash`. Su endpoint no lleva proyecto ni ubicación y es el global (`https://aiplatform.googleapis.com/v1/publishers/google/models/MODELO:generateContent`), así que **no garantiza que el procesamiento se quede en la UE**. No sirve para Fichaitor.
- **Clave vinculada a una cuenta de servicio** ([API keys](https://docs.cloud.google.com/docs/authentication/api-keys)): las peticiones se atribuyen a esa cuenta y IAM decide con sus permisos. `aiplatform.googleapis.com` está entre las API que la admiten. Fichaitor la usa con el mismo endpoint que la cuenta de servicio, con proyecto y ubicación: `https://aiplatform.eu.rep.googleapis.com/v1/projects/PROYECTO/locations/eu/publishers/google/models/MODELO:generateContent`.
- La clave va en la cabecera `x-goog-api-key`, nunca en `?key=`, para que no acabe en un registro de accesos. Google recomienda la cabecera y deja el parámetro para clientes que no pueden poner cabeceras.

> **Importante:** La documentación de Google muestra la clave con el endpoint global del modo exprés y no tiene un ejemplo explícito de clave vinculada con un endpoint multirregional (`eu`). Tampoco dice que no se pueda. Antes de anunciar el cambio, haz la comprobación del paso 5 de [Pasos en el servidor](#pasos-en-el-servidor): si `eu` respondiera `status 401` o `403` con una clave que en `global` funciona, la clave no admite `eu` y hay que volver a la cuenta de servicio, **no** pasar a `global`.

Cómo elige Fichaitor, con `FICHAITOR_GEMINI_PROVIDER=vertex`:

| Variables | Credencial | Endpoint |
|---|---|---|
| `FICHAITOR_VERTEX_API_KEY` y `FICHAITOR_VERTEX_PROJECT` | Clave vinculada, en `x-goog-api-key` | El del proyecto y la ubicación (`eu` por defecto) |
| `FICHAITOR_VERTEX_API_KEY` sin proyecto y `FICHAITOR_VERTEX_LOCATION=global` | Clave del modo exprés | Global sin proyecto |
| `FICHAITOR_VERTEX_API_KEY` sin proyecto y otra ubicación | — | El proceso no arranca y dice que esa clave solo admite `global` |
| Sin clave | Archivo de `FICHAITOR_VERTEX_CREDENTIALS_FILE` o, si está vacío, ADC | El del proyecto y la ubicación |

Si hay clave, el archivo de la cuenta de servicio no se lee aunque la variable siga puesta. La clave es un `SecretStr`: no sale en los `repr`, en los errores ni en los registros.

### Pasos en Google Cloud con clave de API

Necesitas permisos de propietario del proyecto. `PROYECTO` es el ID del proyecto, con facturación activa.

1. Activa la API de Vertex AI (Agent Platform): `gcloud services enable aiplatform.googleapis.com --project=PROYECTO`.
1. Crea la cuenta de servicio `fichaitor-gemini` y dale el permiso mínimo: pasos 2 y 3 de [Pasos en Google Cloud con cuenta de servicio](#pasos-en-google-cloud-con-cuenta-de-servicio). No hace falta su clave JSON.
1. Permite vincular claves a cuentas de servicio. La restricción de organización `constraints/iam.managed.disableServiceAccountApiKeyCreation` («Block service account API key bindings») lo impide por defecto: desactívala para este proyecto o añade `aiplatform.googleapis.com` a los servicios permitidos. Solo se puede cambiar en un proyecto que pertenece a una organización. Quien crea la clave necesita además `roles/iam.serviceAccountUser` y `roles/iam.serviceAccountApiKeyBindingAdmin`.
1. Crea la clave en la consola, en **Agent Platform > Agent Studio > Ajustes > API keys** (`https://console.cloud.google.com/agent-platform/studio/settings/api-keys`) o en **APIs y servicios > Credenciales > Crear credenciales > Clave de API**:
    1. Marca **Authenticate API calls through a service account** y elige `fichaitor-gemini`.
    1. En **Restricciones de API**, deja solo la API de Vertex AI (`aiplatform.googleapis.com`).
    1. En **Restricciones de aplicación**, elige **Direcciones IP** y pon la IP pública del VPS. Es la única restricción de aplicación que admiten estas claves.
1. Copia la clave a tu gestor de contraseñas. No la subas a GitHub ni la mandes por correo o chat.
1. Revisa el contrato de tratamiento de datos (ver [Datos y contrato](#datos-y-contrato-dpa)).

### Pasos en Google Cloud con cuenta de servicio

Necesitas permisos de propietario del proyecto y `gcloud`. En los ejemplos, `PROYECTO` es el ID del proyecto: puede ser el de Bitclick u otro con facturación activa.

1. Activa la API de Vertex AI:

    ```bash
    gcloud services enable aiplatform.googleapis.com --project=PROYECTO
    ```

1. Crea una cuenta de servicio solo para esto:

    ```bash
    gcloud iam service-accounts create fichaitor-gemini \
        --project=PROYECTO --display-name="Fichaitor: llamadas a Gemini"
    ```

1. Dale el permiso mínimo. Las peticiones al modelo exigen `aiplatform.endpoints.predict`. El rol predefinido `roles/aiplatform.user` lo incluye, pero también cientos de permisos más (crear y borrar conjuntos de datos, endpoints, agentes...). Usa un rol personalizado con ese único permiso:

    ```bash
    gcloud iam roles create fichaitorGemini --project=PROYECTO \
        --title="Fichaitor: llamar a Gemini" \
        --permissions=aiplatform.endpoints.predict --stage=GA
    gcloud projects add-iam-policy-binding PROYECTO \
        --member="serviceAccount:fichaitor-gemini@PROYECTO.iam.gserviceaccount.com" \
        --role="projects/PROYECTO/roles/fichaitorGemini"
    ```

    Si la primera llamada responde `status 403`, ese permiso no basta: asigna `roles/aiplatform.user` en su lugar y anótalo aquí.

1. Crea la clave JSON de la cuenta, mejor desde Cloud Shell. No la subas a GitHub ni la mandes por correo o chat:

    ```bash
    gcloud iam service-accounts keys create cuenta-servicio.json \
        --iam-account=fichaitor-gemini@PROYECTO.iam.gserviceaccount.com
    ```

    Si la política de la organización `iam.disableServiceAccountKeyCreation` lo impide, pide una excepción para este proyecto: el VPS no está en Google Cloud y no tiene una identidad de máquina que usar.

1. Revisa el contrato de tratamiento de datos (ver [Datos y contrato](#datos-y-contrato-dpa)).

### Pasos en el servidor

**Con clave de API:**

1. En `api.env`, `worker.env` y `conversation.env` (carpeta del despliegue en el VPS, junto a `vertex/`):

    ```bash
    FICHAITOR_GEMINI_PROVIDER=vertex
    FICHAITOR_GEMINI_MODEL=gemini-3.8-flash
    FICHAITOR_VERTEX_PROJECT=PROYECTO
    FICHAITOR_VERTEX_LOCATION=eu
    FICHAITOR_VERTEX_API_KEY=LA_CLAVE
    ```

    Deja `FICHAITOR_GEMINI_API_KEY` vacía. `FICHAITOR_VERTEX_CREDENTIALS_FILE` puede quedarse como está: con clave no se lee, y la carpeta `vertex/` puede estar vacía.
1. Comprueba que los tres archivos siguen con permisos `600`.
1. Despliega como siempre.
1. Comprueba la captura de reseñas en la web, una propuesta de respuesta por Telegram y el análisis de opinión. En `docker compose logs` no debe aparecer `status 401`, `status 403` ni `status 404`.
1. Si sale `status 401` o `403` y la clave, el proyecto y el permiso están bien, la clave no admite `eu` (ver la nota de [Clave de API o cuenta de servicio](#clave-de-api-o-cuenta-de-servicio)). Vacía `FICHAITOR_VERTEX_API_KEY` y sigue los pasos con cuenta de servicio. No cambies a `global` sin actualizar antes la política de privacidad.

**Rotar la clave de API:** crea otra igual, cámbiala en los tres archivos, recrea los tres servicios y borra la antigua en la consola. Hazlo al menos una vez al año, y en el momento si se filtra.

**Con cuenta de servicio:**

1. Copia la clave al servidor con `scp` a una ruta temporal y colócala en su carpeta, legible solo por el usuario de los contenedores (UID 10001):

    ```bash
    cd /opt/bitclick/fichaitor
    sudo install -d -m 500 -o 10001 -g 10001 vertex
    sudo install -m 400 -o 10001 -g 10001 /tmp/cuenta-servicio.json vertex/cuenta-servicio.json
    shred -u /tmp/cuenta-servicio.json
    ```

    Borra también la copia del equipo desde el que la subiste. Compose monta `vertex/` en `/run/secrets/vertex`, de solo lectura, en la API, el worker y la conversación: la clave no entra en la imagen ni en el entorno.

1. En `api.env`, `worker.env` y `conversation.env`:

    ```bash
    FICHAITOR_GEMINI_PROVIDER=vertex
    FICHAITOR_GEMINI_MODEL=gemini-3.8-flash
    FICHAITOR_VERTEX_PROJECT=PROYECTO
    FICHAITOR_VERTEX_LOCATION=eu
    FICHAITOR_VERTEX_CREDENTIALS_FILE=/run/secrets/vertex/cuenta-servicio.json
    ```

    Deja `FICHAITOR_GEMINI_API_KEY` vacía: con `vertex` no se usa. Cuando todo funcione, borra la clave en AI Studio.

1. Despliega como siempre. Si el archivo falta o no se puede leer, el proceso se para al arrancar con `credentials file cannot be read`: el mensaje nunca incluye el contenido.
1. Comprueba la captura de reseñas en la web, una propuesta de respuesta por Telegram y el análisis de opinión. En `docker compose logs` no debe aparecer `status 403` ni `credentials:`.

**Volver a AI Studio:** pon `FICHAITOR_GEMINI_PROVIDER=aistudio` y la clave en los tres archivos, y vuelve a desplegar.

**Rotar la clave JSON:** crea otra (paso 4 de [Pasos en Google Cloud con cuenta de servicio](#pasos-en-google-cloud-con-cuenta-de-servicio)), sustituye el archivo, recrea los tres servicios y borra la antigua con `gcloud iam service-accounts keys delete`. Hazlo al menos una vez al año, y en el momento si se filtra.

### Datos y contrato (DPA)

- Vertex AI se rige por las condiciones de Google Cloud y su [Cloud Data Processing Addendum](https://cloud.google.com/terms/data-processing-addendum) (CDPA): Google trata los datos como **encargado del tratamiento**. Acéptalo en la consola de la cuenta de facturación y rellena los datos de contacto de privacidad de Fichaitor.
- Según las [condiciones específicas del servicio](https://cloud.google.com/terms/service-terms), Google no usa los datos para entrenar ni ajustar modelos sin permiso.
- Para responder más deprisa, Google guarda en memoria lo que se le envía, como máximo 24 horas y en la ubicación que atiende la petición. Se puede desactivar con `disableCache` (requiere `roles/aiplatform.admin`). Ver [Data governance](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/data-governance).
- Google puede registrar lo que se le envía para detectar abusos (apartado 4.3 de las condiciones de Google Cloud). No se puede desactivar sin pedírselo a Google.
- Fichaitor no usa la búsqueda de Google como fuente (grounding), así que no le afecta el registro de 3 días de esa función.
- Antes del cambio, actualiza el proveedor y la ubicación en la política de privacidad (`docs/legal/privacidad.md`, apartados 5 y 6) y en el registro de actividades de tratamiento.

## Tareas habituales

**Ver el estado y los registros:**

```bash
cd /opt/bitclick/fichaitor
docker compose ps
docker logs --tail 80 fichaitor-api
docker logs --tail 80 fichaitor-worker
```

**Cambiar un secreto o una variable:**

1. Edita `api.env` o `worker.env` en el servidor.
2. Recrea el servicio afectado:

   ```bash
   docker compose up -d --force-recreate fichaitor-api
   ```

**Rotar las contraseñas de la base de datos:**

```bash
./init-roles.sh --rotar
docker compose up -d --force-recreate fichaitor-api fichaitor-worker fichaitor-conversation
```

**Ver la cola del worker:**

```bash
docker exec fichaitor-db psql -U fichaitor -d fichaitor \
  -c "SELECT state, count(*), min(run_at) FROM job GROUP BY state"
```

**Tramitar una baja pedida por correo:**

Cuando alguien pide por correo la baja de su ficha o de su cuenta, soporte la tramita con `scripts/baja_manual.py`. Hace lo mismo que la baja del panel, sin código: la baja queda **programada 48 horas** y a la dirección de la cuenta le llega el correo de «baja programada» con su enlace de restauración, igual que desde el panel. Si el correo no lo mandó el dueño, la restaura con un clic y no se ha borrado nada (hallazgo B15 de `docs/seguridad/revision-2026-11-12-c.md`). Qué hace y por qué la base lo admite está en `docs/retencion.md`, «Baja por correo».

Antes de ejecutarlo, comprueba:

- **Que el correo viene de la dirección de la cuenta, con `dmarc=pass` alineado.** Abre el original del mensaje y mira la cabecera `Authentication-Results` que puso **nuestro** servidor de correo (la primera, la más alta; las de más abajo las puede escribir cualquiera). Tiene que decir `dmarc=pass` y `header.from=` el dominio de la dirección del remitente (`From:`), que a su vez es la dirección de la cuenta. `spf=pass` o `dkim=pass` solos **no valen**: pueden pasar para el dominio de quien lo envía con un `From:` ajeno; solo DMARC liga la autenticación al dominio del `From:`. Si no hay `dmarc=pass` alineado (falta, `fail`, `none`, `bestguesspass` o el dominio no coincide), **no la tramites por correo**: contesta pidiendo que la haga desde el panel, en **Ajustes** («Desconectar Google» o borrar la cuenta), con el código que le llega a su correo.
- **Qué pide:** desconectar Google (`ficha`) o borrar la cuenta (`cuenta`). Si dice una ficha y la cuenta tiene varias, apunta cuál. Google concede el acceso por usuario: la baja de `ficha` desconecta todas las fichas de la cuenta. Si solo quiere dejar de usar una, que la desactive en el panel.
- **Una referencia para `--motivo`**, por ejemplo el número del ticket. Sin direcciones de correo ni otros datos personales: se guarda 12 meses en la auditoría.

Ejecútalo en el servidor, en un **contenedor de un solo uso** (servicio `fichaitor-baja` de `docker-compose.yml`, perfil `herramientas`), nunca dentro del worker: así la contraseña del propietario no entra en el entorno ni en el espacio de procesos del worker (hallazgo I20). El contenedor usa la imagen de la API, recibe solo `db.env`, compone la URL del propietario con `POSTGRES_PASSWORD` y se borra al terminar (`--rm`). La contraseña no pasa por la línea de órdenes ni por la sesión del servidor:

1. Mira lo que haría. Sin `--confirmar` no cambia nada:

   ```bash
   cd /opt/bitclick/fichaitor
   docker compose run --rm fichaitor-baja \
     --email dueno@example.com --scope ficha --motivo "Ticket 1234"
   ```

   Enseña la cuenta (con el correo enmascarado), sus fichas, si ya tiene una baja programada desde el panel (la nueva la sustituye) y qué va a pasar. Si la cuenta tiene más de una ficha y el alcance es `ficha`, pide `--location loc_...`: la ficha que nombra el correo, para comprobar que es de esa cuenta.

2. Si todo cuadra, repite la orden con `--confirmar`. Imprime el identificador de la baja, una línea JSON `baja_manual` de auditoría y `Programada: se hace efectiva el ...`. El worker envía en unos segundos el correo de baja programada a la dirección de la cuenta.

3. Contesta a la persona: la baja está programada; le llega un correo con el enlace para anularla durante 48 horas; pasado ese plazo se hace efectiva y lo obtenido de Google se borra en un máximo de 30 días.

**Baja inmediata (`--inmediata`), solo en casos excepcionales.** Hace la baja efectiva al momento, sin plazo, sin correo y sin enlace de restauración: no tiene vuelta atrás. Úsala solo con el visto bueno del responsable del tratamiento y por un motivo que conste en el ticket, por ejemplo una orden de una autoridad, o un dueño que ya no puede recibir correo en la dirección de la cuenta y ha acreditado su identidad por otra vía. Nunca porque «corra prisa»: la baja programada ya deja de leer la ficha y de enviar avisos desde el primer momento. Con `--inmediata`, el script espera hasta 60 segundos y escribe `Efectiva: ... Revocación en Google: revoked`; si dice que el worker todavía no la ha hecho efectiva, comprueba que `fichaitor-worker` está en marcha: los datos ya no se sirven y el worker la aplicará al tomar el job.

```bash
docker compose run --rm fichaitor-baja \
  --email dueno@example.com --scope cuenta --motivo "Ticket 1234: orden judicial" --inmediata --confirmar
```

| Código de salida | Qué significa |
|---|---|
| `0` | Hecho, o simulación correcta |
| `1` | No se ha tramitado: no hay cuenta con ese correo, hay más de una, la ficha no es de la cuenta, falta `--location`, la cuenta ya está desconectada o el motivo no vale. El mensaje lo explica |
| `2` | Uso o configuración: falta la URL, o no es el usuario propietario |

Para ver las bajas tramitadas por correo (`delete_at = confirmed_at` en las inmediatas):

```bash
docker exec fichaitor-db psql -U fichaitor -d fichaitor \
  -c "SELECT l.at, l.written_by, l.action_id AS baja, l.action_type, l.text AS motivo, o.delete_at = o.confirmed_at AS inmediata, o.restored_at FROM audit_log l LEFT JOIN offboarding o ON o.id::text = l.action_id WHERE l.event = 'offboarding_email_request' ORDER BY l.at DESC LIMIT 20"
```

> **Nota:** el script viaja en la imagen de la API desde la versión que lo incluye, y el servicio `fichaitor-baja` en el `docker-compose.yml` que copia el despliegue desde esta versión. Con una versión anterior, despliega primero.

> **Precaución:** no ejecutes `docker compose down -v`. La opción `-v` borra el volumen de la base de datos.

## Lista de comprobación previa a publicar

Lo que sigue abierto hoy, 2026-11-12. No abras el sitio al público con los puntos marcados como "Bloquea" sin resolver.

### Google

| Punto | Estado | Bloquea |
|---|---|---|
| Proyecto de Google Cloud | **Decidido 2026-11-12: el de Bitclick (`544696777336`).** Google rechazó el proyecto nuevo por duplicado: solo aprueba un proyecto por empresa. El de Bitclick ya tiene acceso a la API | No |
| Pantalla de consentimiento del proyecto de Bitclick | **Hecho 2026-11-13:** verificación de marca aprobada y app publicada «In production», con la marca de Fichaitor | No |
| Pantalla de consentimiento "In production" | Pendiente. Requiere la página principal, la política de privacidad y las condiciones publicadas, y el dominio verificado en Search Console | Sí |
| Verificación de la app | Pendiente. Sin verificar: aviso de "app no verificada" y tope de 100 usuarios | No, para una beta |
| URI de redirección de producción en el cliente OAuth | Pendiente (paso 9) | Sí |
| Clave de Places con límite de gasto y clave de Gemini de un proyecto de pago | Por comprobar | Sí la primera |
| Gemini en Vertex AI, ubicación `eu` y modelo `gemini-3.8-flash` (ver «Gemini en Vertex AI») | Pendiente. El código está listo; no hay nada creado en Google Cloud | No, para la beta |

Cambiar de proyecto de Google es cambiar `FICHAITOR_GOOGLE_CLIENT_ID` y `FICHAITOR_GOOGLE_CLIENT_SECRET` en `api.env` y en `worker.env`. Los clientes ya conectados tienen que volver a conectar.

### Textos legales

| Punto | Estado | Bloquea |
|---|---|---|
| Política de privacidad | **Tiene marcas `[PENDIENTE]` y `[REVISAR]`.** Con `LEGAL_ESTRICTO=1`, la imagen de la web no se construye | Sí |

Para ver las marcas que quedan:

```bash
grep -n -E "\[PENDIENTE\]|\[REVISAR\]" docs/legal/*.md
```

### Capacidad

| Punto | Estado |
|---|---|
| Réplicas de la API | **Una sola.** Los límites por IP, el tope diario de capturas y las cachés viven en la memoria de un proceso. Con varias réplicas, cada una cuenta por separado y los límites dejan de proteger el gasto. No escales la API hasta mover ese estado fuera del proceso |
| Reinicio de la API | Cada despliegue pone a cero los contadores y la caché de `place_id` |
| Réplicas del worker | **Una basta y varias están soportadas.** Según `docs/worker.md`, la cola reparte con `FOR UPDATE SKIP LOCKED` y dos workers nunca reciben el mismo job. Hace falta al menos uno en marcha. El Compose define uno; para añadir más hay que quitar su `container_name` |
| Límites de memoria del Compose | Son una estimación (512 MB la base de datos y la API, 384 MB el worker, 96 MB la web). Por contrastar con la memoria libre del VPS y con el uso real |

### Copias de seguridad

| Punto | Estado |
|---|---|
| Política de copias y retención | **Sin decidir.** Hoy: copia antes de cada despliegue y 14 días de retención, todo en el mismo disco |
| Copia diaria programada | Sin instalar. La línea de `cron` está en `deploy/restore.md` |
| Copia externa | No existe. Falta decidir el destino (en la UE), el cifrado y el plazo |
| Prueba de restauración | Sin hacer en el servidor |
| Clave privada de los tokens en el gestor de contraseñas | Por confirmar tras la primera instalación |

### Vigilancia

| Punto | Estado |
|---|---|
| Salud del worker | **No tiene endpoint HTTP.** El despliegue solo comprueba que no se reinicia. Nada avisa de una cola atascada, de jobs en `failed` ni de tokens rechazados |
| Alertas | No hay ninguna. El diseño (`docs/02-hld.md`) prevé avisos de cola atascada, fallos de Google y tokens revocados |
| Prueba de humo tras desplegar | Sin automatizar. `docs/06-estandares.md` pide una lectura y una escritura reversible sobre una ficha propia |

### Infraestructura

| Punto | Estado |
|---|---|
| Repositorio en GitHub, entorno `production` y secretos | Pendiente (paso 1) |
| `SSH_KNOWN_HOSTS` | Obligatorio. Sin él, el flujo se detiene: no despliega sin verificar la identidad del servidor |
| DNS de `fichaitor.es` | Apunta a IONOS (paso 7) |
| Host y certificado en Nginx Proxy Manager | Pendiente (paso 8) |
| Primer despliegue real | Sin hacer. `deploy.sh` y el flujo no se han ejecutado nunca contra un servidor |
