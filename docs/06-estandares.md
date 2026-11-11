# Estándares técnicos y buenas prácticas

## 1. Stack

| Capa | Elección | Por qué |
|---|---|---|
| API y worker | Python 3.12, FastAPI, Pydantic v2 | Asíncrono, contrato OpenAPI nativo y validación de esquemas, que es justo lo que exige la salida estructurada del modelo. Ya es el stack de Bitclick Social |
| Datos | PostgreSQL 16, SQL explícito con asyncpg | Seguridad a nivel de fila, `SKIP LOCKED` para la cola y jsonb para estados. Sin ORM: el esquema y las políticas se leen tal cual |
| Cola | Tabla `job` en PostgreSQL | Una pieza menos que operar; transaccional con los datos. Sustituible por un broker detrás de la misma interfaz |
| Web | React 18, Vite, TypeScript estricto, Tailwind | Bundle estático servido por nginx en el mismo origen: sin CORS, cookies estrictas |
| Modelo de lenguaje | Adaptador propio con contrato común | El proveedor se elige con el conjunto de evaluación y se puede cambiar sin tocar el dominio |
| Mensajería | WhatsApp Cloud API; Telegram Bot API; correo transaccional | Decisión de acceso a WhatsApp en la fase 0 |
| Pagos | Pasarela con suscripciones y webhooks | A elegir en la fase 1 |
| Objetos | Almacenamiento compatible con S3 | Fotos fuera de la base de datos |
| Infraestructura | Docker Compose en VPS, despliegue por integración continua, alojamiento en la UE | Mismo modelo operativo que el resto de Bitclick; suficiente para miles de cuentas |

Alternativas descartadas: Django (su ORM y sus migraciones chocan con el esquema explícito y con la seguridad a nivel de fila), Flask (síncrono y sin contrato nativo), n8n como motor (sin pruebas ni aislamiento por cuenta), frameworks de agentes autónomos (el modelo no debe decidir cuándo escribe).

## 2. Separación de responsabilidades

```text
app/
  domain/          Reglas puras: propuesta y sus estados, validación de acciones,
                   reglas de calendario, límites de frecuencia. Sin E/S.
  application/     Casos de uso: procesar mensaje, planificar el día,
                   ejecutar acción, vincular canal. Orquestan dominio y puertos.
  ports/           Interfaces: GoogleProfile, Messaging, LanguageModel,
                   Billing, Storage, Clock, JobQueue, repositorios.
  adapters/
    google/        Implementación real y doble falso
    whatsapp/      "
    llm/           "
    db/            Repositorios SQL
  agents/          Prompts versionados, esquemas de salida y evaluadores
  api/             Rutas HTTP y webhooks. Solo validan, autorizan y encolan
  worker/          Bucle de jobs y registro de manejadores
web/               Interfaz. Solo habla con la API
db/                schema.sql idempotente, políticas y roles
```

Reglas:

1. `domain` no importa nada de `adapters`, `api` ni `worker`. Se comprueba en integración continua.
2. `api` nunca llama a un servicio externo.
3. `agents` nunca tiene acceso a puertos de escritura.
4. Solo `application.execute_action` usa los métodos de escritura de `GoogleProfile`.
5. Todo acceso a tiempo real pasa por `Clock`, para poder probar fechas clave y caducidades.
6. Configuración solo por variables de entorno, validadas al arrancar.

## 3. Contenedores

| Contenedor | Contenido | Red |
|---|---|---|
| `web` | nginx sin privilegios, estáticos y proxy de `/api` | Pública |
| `api` | FastAPI | Interna |
| `worker` | Planificador, orquestador y ejecutor. Único con la clave de cifrado y las credenciales externas | Interna, con salida |
| `db` | PostgreSQL con volumen | Interna |
| `storage` | Almacenamiento de objetos (en local) | Interna |
| `fakes` | Dobles de Google, WhatsApp y el modelo (solo perfiles `dev` y `test`) | Interna |

- Una imagen por servicio, usuario sin privilegios, sistema de archivos de solo lectura donde se pueda.
- Perfiles de Compose: `dev`, `test` y `prod`. El mismo archivo arranca el entorno completo en local con un comando.
- Comprobaciones de salud en todos los servicios; `api` y `worker` esperan a `db`.
- Imágenes etiquetadas por commit; actualizar es cambiar la etiqueta y aplicar `schema.sql`.

## 4. Estrategia de pruebas

Las pruebas se escriben antes que el código y todo se ejecuta en local antes de cualquier despliegue.

| Nivel | Qué cubre | Con qué |
|---|---|---|
| **Unitarias** | Máquina de estados, validación de acciones, reglas de calendario y antelación, límites de frecuencia, ventana de 24 horas | pytest, sin E/S |
| **Integración** | Repositorios y políticas de aislamiento contra PostgreSQL real; cola de jobs con concurrencia; adaptadores contra dobles | pytest y contenedor de base de datos |
| **Contrato** | La API cumple el OpenAPI; los adaptadores cumplen respuestas grabadas de Google y Meta | schemathesis, respuestas grabadas |
| **Conversacionales** | Cada flujo de `01-flujos.md` como guion: mensajes entrantes simulados y mensajes salientes y acciones esperados, con el modelo falso | Arnés propio |
| **Evaluación del modelo** | Conjunto de mensajes y reseñas reales con salida esperada; umbral mínimo por agente | Se ejecuta al cambiar un prompt o un modelo |
| **E2E** | Compose completo con dobles: alta, conexión, vinculación, fecha clave hasta el cambio "en Google", reseña hasta la respuesta publicada | Playwright y simulador de webhooks |
| **Seguridad** | Fuga entre cuentas, webhooks sin firma, repetición de mensajes, inyección desde reseñas, acción sin confirmación | Casos fijos en la suite |
| **Humo en producción** | Ficha de pruebas propia: una lectura y una escritura reversible tras cada despliegue | Automático |

### Puertas de calidad en integración continua

1. Formato y análisis estático (ruff, mypy estricto, eslint, tsc).
2. Unitarias e integración.
3. Contrato.
4. Conversacionales y E2E en Compose limpio.
5. Evaluación del modelo si cambian `agents/` o la configuración del modelo.
6. Análisis de dependencias y de secretos.
7. Despliegue solo desde la rama principal con todo en verde.

### Invariantes que siempre tienen prueba

- Ninguna acción se ejecuta sin confirmación registrada o regla de modo automático.
- Un mensaje repetido no produce dos acciones.
- Una cuenta no lee datos de otra.
- Un teléfono no vinculado no puede crear propuestas.
- El texto de una reseña no cambia el tipo de acción que se propone.

## 5. Convenciones

- Código, tablas y contratos en inglés; textos para el usuario en español, en catálogo.
- Commits convencionales; una tarea en curso cada vez; cada cambio de comportamiento empieza por actualizar la especificación.
- Prompts versionados en el repo; nunca en la base de datos ni editados en caliente.
- Ningún secreto en el repo, en los logs ni en las exportaciones.
