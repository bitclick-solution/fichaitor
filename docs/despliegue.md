# Despliegue en producción

Esta guía explica cómo publicar Fichaitor en el VPS de Bitclick y cómo actualizarlo después. Sigue el modelo de los demás servicios de Bitclick: imágenes en GHCR, una carpeta por servicio en `/opt/bitclick`, un `deploy.sh` con vuelta atrás y Nginx Proxy Manager delante.

> **Importante:** antes de abrir el sitio al público, repasa la [lista de comprobación previa a publicar](#lista-de-comprobación-previa-a-publicar). Hoy tiene puntos abiertos que impiden publicar.

## Cómo queda montado

| Contenedor | Qué es | Redes | Archivo de entorno |
|---|---|---|---|
| `fichaitor-db` | PostgreSQL 16 | `fichaitor-interna` | `db.env` |
| `fichaitor-api` | API (FastAPI) | `fichaitor-interna` | `api.env` |
| `fichaitor-worker` | Worker, con la misma imagen que la API | `fichaitor-interna` | `worker.env` |
| `fichaitor-web` | Sitio público, `/app` y reenvío de `/api/` | `fichaitor-interna` y `bitclick-network` | Ninguno |

- Ningún contenedor publica puertos en el servidor. Nginx Proxy Manager (`bitclick-proxy`) llega a `fichaitor-web:8080` por la red `bitclick-network`. La base de datos y la API solo son visibles dentro de `fichaitor-interna`.
- Las imágenes `fichaitor-api` y `fichaitor-web` se etiquetan con el commit. Actualizar es cambiar la etiqueta, aplicar el esquema y recrear los contenedores.
- La clave privada de los tokens de Google solo está en `worker.env`. La API recibe la clave pública.

Archivos del repositorio:

| Archivo | Uso |
|---|---|
| `.github/workflows/deploy.yml` | Construye las imágenes, las sube a GHCR y lanza el despliegue por SSH |
| `deploy/docker-compose.prod.yml` | Compose de producción. En el servidor se llama `docker-compose.yml` |
| `deploy/deploy.sh` | Despliegue en el servidor: copia, esquema, recreación, salud y vuelta atrás |
| `deploy/init-roles.sh` | Crea los usuarios de la base de datos con contraseñas generadas en el servidor |
| `deploy/backup.sh` y `deploy/restore.md` | Copia de seguridad y restauración |
| `deploy/env.api.example` y `deploy/env.worker.example` | Plantillas de `api.env` y `worker.env` |

Carpeta del servidor, `/opt/bitclick/fichaitor`:

| Archivo | Quién lo escribe | Contiene secretos |
|---|---|---|
| `docker-compose.yml`, `deploy.sh`, `backup.sh`, `init-roles.sh`, `db/*.sql` | El flujo de GitHub, en cada despliegue | No |
| `db.env`, `api.env`, `worker.env` | Tú, una vez. `init-roles.sh` añade las URL de la base de datos | Sí (modo 600) |
| `.env` | `deploy.sh`. Solo las dos imágenes en uso | No |
| `.versions` | `deploy.sh`. Las últimas cinco etiquetas desplegadas | No |
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

5. Crea estos secretos en el entorno `production`:

   | Secreto | Valor |
   |---|---|
   | `SSH_HOST` | Dirección del VPS |
   | `SSH_USER` | Usuario de despliegue |
   | `SSH_PRIVATE_KEY` | Clave privada SSH de ese usuario |
   | `SSH_PORT` | Puerto SSH. Opcional; por defecto, 22 |
   | `SSH_KNOWN_HOSTS` | Salida de `ssh-keyscan -p <puerto> <host>`. Opcional, pero recomendado: sin él, el flujo no verifica la identidad del servidor |
   | `GHCR_TOKEN` | Token personal clásico con el permiso `read:packages`. El servidor lo usa para descargar las imágenes |

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
   ```

3. En el servidor, protege los dos archivos:

   ```bash
   chmod 600 /opt/bitclick/fichaitor/api.env /opt/bitclick/fichaitor/worker.env
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

3. Deja vacías `FICHAITOR_DATABASE_URL` y `FICHAITOR_WORKER_DATABASE_URL`. Las escribe `init-roles.sh` en el primer despliegue.

Reglas que `deploy.sh` comprueba antes de tocar nada:

- Los tres archivos tienen modo 600.
- `api.env` no contiene `FICHAITOR_TOKEN_PRIVATE_KEY`.
- Ningún archivo activa `FICHAITOR_FAKE_PLACES`, `FICHAITOR_FAKE_GOOGLE` ni `FICHAITOR_FAKE_SCREENSHOT`.
- El identificador del cliente OAuth coincide en los dos archivos.

Estos valores no van en los archivos: los impone `docker-compose.prod.yml` y ganan a lo que diga `api.env`.

| Variable | Valor en producción |
|---|---|
| `FICHAITOR_POST_CONNECT_URL` | `https://fichaitor.es/app` |
| `FICHAITOR_GOOGLE_REDIRECT_URI` | `https://fichaitor.es/api/v1/google/oauth/callback` |
| `FICHAITOR_COOKIE_SECURE` | `true` |
| `FICHAITOR_TRUST_PROXY_HEADERS` | `true` |
| `FICHAITOR_FAKE_PLACES`, `FICHAITOR_FAKE_GOOGLE`, `FICHAITOR_FAKE_SCREENSHOT` | `false` |

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

## Desplegar una versión nueva

Hay dos formas. Las dos exigen que el commit esté en `main` y que su integración continua haya terminado bien.

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
docker compose up -d --force-recreate fichaitor-api fichaitor-worker
```

**Ver la cola del worker:**

```bash
docker exec fichaitor-db psql -U fichaitor -d fichaitor \
  -c "SELECT state, count(*), min(run_at) FROM job GROUP BY state"
```

> **Precaución:** no ejecutes `docker compose down -v`. La opción `-v` borra el volumen de la base de datos.

## Lista de comprobación previa a publicar

Lo que sigue abierto hoy, 2026-11-12. No abras el sitio al público con los puntos marcados como "Bloquea" sin resolver.

### Google

| Punto | Estado | Bloquea |
|---|---|---|
| Acceso a la API de Business Profile en el proyecto nuevo | **Sin aprobar: la cuota es 0.** Con cuota 0, ninguna ficha se puede leer | Sí |
| Proyecto de Bitclick (`544696777336`) | Tiene acceso, pero su pantalla de consentimiento está en "Testing": **los tokens caducan a los 7 días** y cada cliente tendría que reconectar cada semana | Sí, si se usa ese proyecto |
| Pantalla de consentimiento "In production" | Pendiente. Requiere la página principal, la política de privacidad y las condiciones publicadas, y el dominio verificado en Search Console | Sí |
| Verificación de la app | Pendiente. Sin verificar: aviso de "app no verificada" y tope de 100 usuarios | No, para una beta |
| URI de redirección de producción en el cliente OAuth | Pendiente (paso 9) | Sí |
| Clave de Places con límite de gasto y clave de Gemini de un proyecto de pago | Por comprobar | Sí la primera |

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
| `SSH_KNOWN_HOSTS` | Recomendado. Sin él, el flujo no verifica la identidad del servidor |
| DNS de `fichaitor.es` | Apunta a IONOS (paso 7) |
| Host y certificado en Nginx Proxy Manager | Pendiente (paso 8) |
| Primer despliegue real | Sin hacer. `deploy.sh` y el flujo no se han ejecutado nunca contra un servidor |
