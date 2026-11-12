# Copias de seguridad y restauración

Esta guía explica qué copia `backup.sh`, cómo restaurar la base de datos de Fichaitor y qué ocurre si se pierde la clave privada de los tokens.

Todas las órdenes se ejecutan en el servidor, en `/opt/bitclick/fichaitor`.

## Qué necesitas para recuperar el servicio

| Qué | Dónde está | Si se pierde |
|---|---|---|
| Base de datos (`fichaitor-<fecha>.dump`) | `backups/` y la copia externa | Se pierden las cuentas, las fichas, las conexiones con Google y el historial |
| Roles (`fichaitor-<fecha>.roles.sql`) | `backups/`, junto a cada copia | Nada grave: `./init-roles.sh --rotar` crea usuarios nuevos |
| `api.env`, `worker.env`, `db.env` | Solo en el servidor (modo 600) | Se rehacen con los valores del gestor de contraseñas y `init-roles.sh` |
| **Clave privada de los tokens** (`FICHAITOR_TOKEN_PRIVATE_KEY`) | `worker.env` y el gestor de contraseñas | **Todos los clientes tienen que volver a conectar Google** |

> **Importante:** la copia de la base de datos no incluye la clave privada. Los tokens de Google van cifrados dentro de la copia y solo esa clave los descifra. Guarda la clave en el gestor de contraseñas el mismo día que la generas, en una entrada distinta de la copia de la base de datos.

> **Precaución:** una copia contiene datos personales (correos de las cuentas) y los tokens cifrados. Trátala como un secreto: modo 600, cifrada si sale del servidor y con un plazo de borrado.

## Hacer una copia

1. Lanza la copia:

   ```bash
   ./backup.sh
   ```

2. Comprueba que hay dos archivos nuevos:

   ```bash
   ls -l backups/ | tail -n 4
   ```

`deploy.sh` hace una copia con la etiqueta `pre-<commit>` antes de aplicar el esquema en cada despliegue.

`backup.sh` borra las copias de más de 14 días. Para cambiar el plazo, define `FICHAITOR_BACKUP_RETENCION_DIAS`; con `0` no borra nada.

### Programar la copia diaria

1. Abre el `crontab` del usuario que despliega:

   ```bash
   crontab -e
   ```

2. Añade esta línea. Hace una copia cada día a las 03:30 y guarda el resultado en un registro:

   ```cron
   30 3 * * * /opt/bitclick/fichaitor/backup.sh diaria >> /opt/bitclick/fichaitor/backups/backup.log 2>&1
   ```

> **Nota:** la copia programada se queda en el mismo servidor. Si el disco falla, se pierde con la base de datos. La copia externa y el plazo de retención definitivo están sin decidir: consulta la lista de comprobación de `docs/despliegue.md`.

## Restaurar sobre la base de datos actual

Usa este procedimiento para volver a una copia anterior en el mismo servidor, por ejemplo después de un despliegue que ha estropeado datos.

> **Precaución:** la restauración sustituye todos los datos por los de la copia. Lo que se haya escrito después de la copia se pierde.

1. Para los servicios que escriben en la base de datos:

   ```bash
   docker compose stop fichaitor-web fichaitor-api fichaitor-worker
   ```

2. Copia el estado actual, por si necesitas volver a él:

   ```bash
   ./backup.sh antes-de-restaurar
   ```

3. Elige la copia:

   ```bash
   ls -l backups/*.dump
   ```

4. Restaura. Sustituye `<copia>` por el nombre del archivo:

   ```bash
   docker exec -i fichaitor-db pg_restore -U fichaitor -d fichaitor \
     --clean --if-exists --single-transaction --exit-on-error < backups/<copia>.dump
   ```

   La orden no imprime nada si termina bien. Si falla, no cambia nada: va en una sola transacción.

5. Vuelve a desplegar la versión en uso. `deploy.sh` aplica el esquema actual sobre los datos restaurados y arranca los servicios:

   ```bash
   ./deploy.sh "$(head -n 1 .versions)" <propietario>
   ```

   `<propietario>` es el usuario u organización de GitHub dueño de las imágenes, en minúsculas.

6. Comprueba el resultado:

   ```bash
   docker compose ps
   docker exec fichaitor-db psql -U fichaitor -d fichaitor -c "SELECT count(*) FROM account"
   ```

## Restaurar en un servidor nuevo o con el volumen perdido

1. Sigue la primera instalación de `docs/despliegue.md` hasta crear `db.env`, `api.env` y `worker.env`. Rellena `api.env` y `worker.env` con los valores del gestor de contraseñas, incluida la clave privada. Deja vacías las dos URL de la base de datos.

2. Copia al servidor, a `backups/`, el archivo `.dump` y su `.roles.sql`.

3. Lanza el flujo **Despliegue** de GitHub con la versión que estaba en producción. El despliegue crea la base de datos vacía, aplica el esquema y arranca los servicios.

4. Para los servicios que escriben en la base de datos:

   ```bash
   docker compose stop fichaitor-web fichaitor-api fichaitor-worker
   ```

5. Restaura los datos:

   ```bash
   docker exec -i fichaitor-db pg_restore -U fichaitor -d fichaitor \
     --clean --if-exists --single-transaction --exit-on-error < backups/<copia>.dump
   ```

6. Genera contraseñas nuevas para los usuarios de la base de datos. Las de la copia no sirven: los archivos de entorno son nuevos.

   ```bash
   ./init-roles.sh --rotar
   ```

7. Vuelve a desplegar la versión en uso:

   ```bash
   ./deploy.sh "$(head -n 1 .versions)" <propietario>
   ```

> **Nota:** el archivo `.roles.sql` solo hace falta si quieres conservar las contraseñas antiguas de los usuarios, porque también conservas los `api.env` y `worker.env` antiguos. En ese caso, sustituye el paso 6 por `docker exec -i fichaitor-db psql -U fichaitor -d postgres < backups/<copia>.roles.sql`. Los avisos de que un rol ya existe son normales.

## Probar que una copia sirve

Una copia que nunca se ha restaurado no es una copia. Prueba una cada cierto tiempo en una base de datos desechable, sin tocar la de producción:

1. Arranca un PostgreSQL temporal:

   ```bash
   docker run -d --name fichaitor-prueba-restauracion -e POSTGRES_USER=fichaitor \
     -e POSTGRES_PASSWORD="$(openssl rand -hex 16)" -e POSTGRES_DB=fichaitor postgres:16-alpine
   ```

2. Crea los roles y restaura:

   ```bash
   docker exec -i fichaitor-prueba-restauracion psql -U fichaitor -d postgres < backups/<copia>.roles.sql
   docker exec -i fichaitor-prueba-restauracion pg_restore -U fichaitor -d fichaitor \
     --single-transaction --exit-on-error < backups/<copia>.dump
   ```

3. Comprueba que hay datos:

   ```bash
   docker exec fichaitor-prueba-restauracion psql -U fichaitor -d fichaitor -c "SELECT count(*) FROM account"
   ```

4. Borra el contenedor temporal:

   ```bash
   docker rm -f -v fichaitor-prueba-restauracion
   ```

## Si se pierde la clave privada de los tokens

La API cifra cada token de renovación de Google con la clave pública. Solo la clave privada, que tiene el worker, los descifra. No hay forma de recuperarla ni de descifrar los tokens sin ella.

Qué ocurre:

- El worker no puede leer ninguna ficha. Cada sincronización falla sin reintentos y el panel muestra un error.
- Las cuentas, las fichas elegidas y el historial se conservan.
- **Todos los clientes tienen que volver a conectar Google.** Al hacerlo, la API guarda un token nuevo cifrado con la clave nueva.

Qué hacer:

1. Genera un par nuevo, como en la primera instalación (`docs/despliegue.md`, "Genera el par de claves").

2. Pon la clave pública nueva en `api.env` (`FICHAITOR_TOKEN_PUBLIC_KEY`) y sube `FICHAITOR_TOKEN_KEY_VERSION` en uno.

3. Pon la clave privada nueva en `worker.env` (`FICHAITOR_TOKEN_PRIVATE_KEY`) y guárdala en el gestor de contraseñas.

4. Recrea la API y el worker:

   ```bash
   docker compose up -d --force-recreate fichaitor-api fichaitor-worker
   ```

5. Marca las fichas para que el panel pida la reconexión en lugar de mostrar un error:

   ```bash
   docker exec fichaitor-db psql -U fichaitor -d fichaitor \
     -c "UPDATE profile_summary SET status = 'needs_reconnect', status_at = now()"
   ```

6. Avisa a los clientes de que tienen que volver a conectar su cuenta de Google.

> **Nota:** el worker usa una sola clave privada. Tras cambiar el par, los tokens cifrados con la clave anterior dejan de poder leerse aunque conserves esa clave. Por eso una rotación voluntaria del par también obliga a reconectar a todos los clientes.

Lo mismo ocurre si restauras una copia de la base de datos y la clave privada que tienes no es la pareja de la clave pública con la que se cifraron esos tokens.
