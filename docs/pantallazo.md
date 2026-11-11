# Demostración por pantallazo de reseñas

El dueño de un negocio sube una captura de la pestaña **Reseñas** de su ficha de Google, hecha por él en su móvil. Fichaitor le dice cuántas de las reseñas que se ven están sin contestar y le propone una respuesta para una de ellas.

Es una demostración para quien todavía no quiere conectar su ficha. El gancho principal del producto es "Conectar con Google": la captura solo enseña unas pocas reseñas y el total solo se sabe conectando.

No se consulta Google Places ni ninguna otra API de Google Maps: los datos los aporta el propio dueño. Por eso no le aplican las dudas de [places-coste.md](fase0/places-coste.md), sección 5.

## Cómo funciona

1. La web envía la captura a `POST /api/v1/public/diagnosis/screenshot` como `multipart/form-data`, en el campo `image`. El campo `source` es opcional y sigue el mismo patrón que en la lista de espera.
2. La API comprueba el tamaño y el tipo del archivo. El tipo se decide por los primeros bytes (PNG, JPEG o WebP), no por el nombre ni por la cabecera que envía el cliente.
3. La API aplica el límite por IP y el límite diario global.
4. **Primera llamada al modelo (lectura).** El modelo recibe la imagen y devuelve, en JSON, qué pantalla es y las reseñas que se ven. Solo transcribe: no cuenta, no valora y no redacta.
5. El dominio acota la salida (`parse_facts`), cuenta las reseñas sin contestar, compone la frase de resumen y elige la reseña que recibe la respuesta propuesta. Todo con reglas fijas.
6. **Segunda llamada al modelo (respuesta).** Solo si hay una reseña sin contestar con estrellas legibles. El modelo recibe las estrellas y el texto de esa reseña, sin imagen y sin nombres, y redacta una respuesta.
7. El dominio valida la respuesta (`validate_reply`). Si incumple alguna regla, se descarta: `suggested_reply` va a `null` y el resto de la respuesta se mantiene.

El modelo propone y el código decide: ningún número ni la frase de resumen salen del modelo, y la única frase que redacta se publica solo si pasa la validación.

### Dónde está cada cosa

| Capa | Archivo | Qué hace |
|---|---|---|
| Dominio | `app/domain/screenshot_diagnosis.py` | Acota la lectura, cuenta, compone `headline`, elige la reseña y valida la respuesta. Sin E/S |
| Puerto | `app/ports/screenshot_reader.py` | `ScreenshotReader`: `read` y `write_reply` |
| Adaptador real | `app/adapters/screenshot_reader/gemini.py` | API de Gemini por REST con `httpx` |
| Adaptador falso | `app/adapters/screenshot_reader/fake.py` | Respuestas fijas para pruebas y desarrollo |
| Prompts y esquemas | `app/adapters/screenshot_reader/prompt.py` | Versionados con `PROMPT_VERSION` |
| Caso de uso | `app/application/screenshot_service.py` | Tamaño, tipo, límites, tiempo máximo y orquestación |
| Ruta | `app/api/screenshot.py` | Lectura del formulario en memoria, errores y forma de la respuesta |
| Ajustes | `app/screenshot_config.py` | Variables `FICHAITOR_*` de esta función |

## Qué se extrae de la captura

| Dato | Detalle |
|---|---|
| Pantalla | `reviews_tab`, `profile_overview` u `other`, con una confianza de 0 a 1 |
| Nombre del negocio | Si aparece. 120 caracteres como máximo |
| Reseñas visibles | 10 como máximo. De cada una: estrellas (1 a 5), texto (600 caracteres como máximo; puede venir vacío), antigüedad tal como se ve ("hace 2 meses") y si tiene respuesta del propietario |

Lo que no se ve se trata como "no visible", nunca se supone. Una reseña cortada por el borde, de la que no se sabe si tiene respuesta, **no** cuenta como sin contestar.

**No se extrae el nombre ni la foto de quien escribió cada reseña.** El esquema de salida no tiene campo para ello, el prompt lo prohíbe y el dominio ignora cualquier campo que no conozca.

### Reglas de la respuesta de la ruta

- `unanswered`: reseñas en las que la captura muestra que no hay respuesta del propietario.
- `unanswered_low`: de ellas, las de 1 o 2 estrellas.
- `headline`: frase fija según los recuentos. Por ejemplo: "Veo 4 reseñas y 3 están sin contestar. 1 de ellas es de 1 o 2 estrellas." o "Todas las reseñas que veo están contestadas."
- Reseña que recibe la respuesta propuesta: entre las que están sin contestar y tienen texto, la de menos estrellas; si hay empate, la que aparece antes. Si ninguna tiene texto, la más reciente según su antigüedad.
- `locked_checks`: los mismos que en el diagnóstico (`app/domain/diagnosis.py`). El primero ya es "Cuántas reseñas tienes sin responder".

### Reglas de la respuesta propuesta

El prompt las pide y el código las comprueba. Si falla una, no se muestra ninguna respuesta.

| Regla | Comprobación en código |
|---|---|
| De 2 a 4 frases, un solo párrafo | Recuento de frases y longitud de 40 a 600 caracteres |
| Sin emojis ni formato | Se rechazan símbolos, emojis y los caracteres `<>{}[]` |
| Sin datos de contacto | Se rechazan correos, enlaces, dominios, cifras con forma de teléfono y menciones a WhatsApp o Telegram |
| Sin ofertas ni compensaciones | Se rechazan "descuento", "gratis", "regalo", "invitamos", "reembolso", "compensar", `%`, `€` y similares |
| Sin promesas | Se rechazan "prometemos", "garantizamos", "no volverá" y similares |
| 1 o 2 estrellas: disculpa y hablarlo en privado | Debe contener una disculpa y una invitación a hablarlo en privado |
| 4 o 5 estrellas: agradecimiento | Debe contener "gracias" o "agradecemos" |
| 3 estrellas | Debe contener un agradecimiento o una disculpa |

El prompt pide además español de España, tono neutro y cercano, no inventar hechos del negocio y no mencionar el nombre del cliente. Estas cuatro no se pueden comprobar en código con fiabilidad: dependen del modelo. La reseña llega al modelo sin el nombre de su autor.

> **Nota:** La comprobación es por palabras clave y prefiere descartar de más. Una respuesta correcta que diga "bono" o "garantizar" se descarta igual.

## Seguridad

- **El texto de la captura es dato, nunca instrucción.** Los dos prompts lo dicen. La reseña viaja a la segunda llamada dentro de un objeto JSON, separada de las instrucciones. La salida de las dos llamadas se valida contra un esquema estricto en el adaptador y se acota de nuevo en el dominio: estrellas fuera de 1 a 5 pasan a "no visible", los textos se recortan y se limpian de caracteres de control, y los campos desconocidos se ignoran.
- **Tamaño.** `FICHAITOR_SCREENSHOT_MAX_BYTES` (6 MB por defecto). El cuerpo se corta mientras se recibe, aunque no declare su tamaño. Respuesta: 413.
- **Tipo.** PNG, JPEG o WebP por sus primeros bytes. Respuesta: 415.
- **Límite por IP.** 3 capturas por hora por defecto, frente a 10 del diagnóstico de texto: cada captura son hasta dos llamadas de pago. Respuesta: 429. Los archivos rechazados por tamaño o tipo no cuentan; las capturas que sí llegan al modelo cuentan todas.
- **Límite diario global.** 300 capturas cada 24 horas entre todos los clientes. Pone techo a la factura si alguien rota direcciones. Respuesta: 503.
- **Tiempo máximo.** `FICHAITOR_SCREENSHOT_TIMEOUT_SECONDS` (30 s) para las dos llamadas juntas. Si la lectura no termina o falla: 503 con un mensaje genérico. Si solo falla la respuesta propuesta: 200 con `suggested_reply` a `null`.
- **Sin filtrar detalles.** La clave viaja en la cabecera `x-goog-api-key`, nunca en la URL. Los errores del proveedor se registran solo como un texto fijo ("status 403", "timeout"): el mensaje del proveedor puede citar la petición y no se copia ni al log ni a la respuesta.
- **Filtros de seguridad del modelo.** Una respuesta bloqueada, vacía, cortada o que no cumple el esquema se trata como fallo. Una reseña con lenguaje fuerte puede activar los filtros.

> **Importante:** Los dos límites viven en la memoria del proceso, igual que `SlidingWindowLimiter` en el resto de la API. Con más de un proceso de API, cada uno cuenta por separado.

## Privacidad

Una captura de reseñas contiene nombres y fotos de terceros: las personas que escribieron las reseñas.

| Qué | Dónde acaba |
|---|---|
| La imagen completa | En memoria durante la petición y **en el proveedor del modelo (Google, API de Gemini)**, que la recibe entera, con los nombres y las fotos que contenga. No se guarda en disco, en base de datos ni en logs |
| Estrellas y texto de una reseña | En el proveedor del modelo, en la segunda llamada, sin el nombre del autor. No se guardan |
| Nombres de los autores de las reseñas | No se extraen, no se devuelven y no se registran |
| Nombre del negocio y texto recortado de la reseña elegida | Se devuelven a quien subió la captura. No se guardan ni se registran |
| Evento de embudo | Solo el tipo (`screenshot_diagnosis` o `screenshot_rejected`) y `source`. Sin nota, sin `place_id` y sin nada leído de la captura |

Detalles de implementación que lo sostienen:

- El formulario se lee en memoria con un analizador propio. El de Starlette pasa a un archivo temporal en disco cualquier subida de más de 1 MB.
- Se usa `generateContent`, que no guarda conversación. La API nueva de Gemini, Interactions, guarda cada interacción por defecto (`store=true`; 55 días en el nivel de pago), y por eso no se usa.
- La web no debe guardar la respuesta más allá de la pantalla abierta, y muestra `business_name`, `review_excerpt` y `reply` como texto, nunca como HTML.

### Pendiente de comprobar en las condiciones de Google

Nada de esto está confirmado. Compruébalo en las condiciones de la API de Gemini antes de redactar la política de privacidad:

| Punto | Qué hay que saber |
|---|---|
| Región de tratamiento | Dónde se procesan las imágenes y si se puede fijar la UE |
| Conservación | Cuánto tiempo conserva Google las entradas y las salidas en el nivel de pago, también para la supervisión de abusos |
| Uso para mejorar los modelos | La página de precios dice que en el nivel de pago el contenido no se usa para mejorar los productos y en el gratuito sí. Hay que confirmarlo en las condiciones y asegurar que la clave es de un proyecto de pago |
| Encargado del tratamiento | Si las condiciones aplicables incluyen un acuerdo de tratamiento de datos válido para clientes del EEE |

## Modelo y coste

- Modelo: `gemini-3.6-flash`, configurable con `FICHAITOR_GEMINI_MODEL`. Se usa el mismo para las dos llamadas.
- Razonamiento: `thinkingLevel: LOW`. Es una transcripción y una respuesta de cuatro frases.

Precio de `gemini-3.6-flash` en el nivel de pago, según la [página de precios de la API de Gemini](https://aistudio.google.com/docs/pricing), consultada el 2026-11-11:

| Periodo | Entrada (USD por millón de tokens) | Salida, razonamiento incluido |
|---|---|---|
| Hasta el 2026-12-31 | 0,75 | 3,75 |
| Desde el 2027-01-01 | 1,50 | 7,50 |

Estimación por captura con los precios vigentes hasta final de 2026:

| Llamada | Entrada | Salida | Coste |
|---|---|---|---|
| Lectura | Unos 3.000 tokens: unos 2.100 de imagen y 900 de prompt y esquema | Unos 1.000 tokens | 0,006 USD |
| Respuesta propuesta | Unos 550 tokens | Unos 300 tokens | 0,0015 USD |
| **Total con dos llamadas** | | | **0,0075 USD** |
| Total con una llamada (nada que contestar) | | | 0,006 USD |

Desde el 2027-01-01 los importes se duplican: unos 0,015 USD por captura con dos llamadas.

Techo por captura, si el modelo agotara `maxOutputTokens` (4.096 y 1.024): unos 0,022 USD con los precios de 2026. Con el límite diario de 300 capturas, el gasto diario típico máximo ronda 2,25 USD.

Sin confirmar:

- **Tokens de la imagen.** La cifra sale de la regla de la [guía de imágenes](https://aistudio.google.com/docs/image-understanding): 258 tokens por tesela de 768 px, 8 teselas para una captura de 1080 × 2400. No consta que los modelos Gemini 3 cuenten igual.
- **Tokens de salida.** Son una estimación; no se ha medido con capturas reales. Mide `usageMetadata` con las primeras capturas y corrige esta tabla.
- **Precio en euros.** La lista está en USD.

> **Nota:** `gemini-3.6-flash` figura en la página de precios como el Flash de la generación anterior. `gemini-3.5-flash-lite` cuesta 0,30 y 2,50 USD por millón, menos de la mitad. Cambiar de modelo es cambiar una variable, pero hay que evaluar antes la lectura con capturas reales.

## Forma de la llamada a Gemini

`POST https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent`, con la clave en la cabecera `x-goog-api-key`.

```json
{
  "systemInstruction": { "parts": [{ "text": "..." }] },
  "contents": [
    {
      "role": "user",
      "parts": [
        { "inlineData": { "mimeType": "image/png", "data": "<base64>" } },
        { "text": "Transcribe lo que se ve en la captura anterior." }
      ]
    }
  ],
  "generationConfig": {
    "responseMimeType": "application/json",
    "responseJsonSchema": { "type": "object", "...": "..." },
    "maxOutputTokens": 4096,
    "thinkingConfig": { "thinkingLevel": "LOW" }
  }
}
```

Fuentes, consultadas el 2026-11-11. Las direcciones antiguas `ai.google.dev/gemini-api/docs/...` redirigen hoy a estas:

| Qué | Página |
|---|---|
| Ruta, `inlineData`, `systemInstruction`, `generationConfig`, `finishReason` y `promptFeedback.blockReason` | [Referencia de `generateContent`](https://aistudio.google.com/docs/api/generate-content) |
| Salida estructurada y palabras clave de esquema admitidas | [Structured output](https://aistudio.google.com/docs/structured-output) |
| Formatos de imagen, límite de 20 MB por petición y tokens por imagen | [Image understanding](https://aistudio.google.com/docs/image-understanding) |
| `generateContent` es "legacy" pero sigue "fully supported"; Interactions guarda por defecto | [Interactions API](https://aistudio.google.com/docs/interactions) |
| Precios y uso del contenido por nivel | [Pricing](https://aistudio.google.com/docs/pricing) |

Diferencias con lo que se sabía de otros proyectos:

- **Campo del esquema.** La referencia vigente documenta `generationConfig.responseJsonSchema` (JSON Schema). No se ha podido confirmar en ella `responseSchema`, así que se usa `responseJsonSchema`. El nombre está en una sola línea de `gemini.py`, en `_generate`.
- **`null` y uniones.** La guía vigente dice que el esquema admite `null` en una lista de tipos. Aun así, el esquema que se envía no usa `null` ni uniones: lo que no se ve viaja como centinela (cadena vacía, `0`, `"unknown"`) y el adaptador lo convierte en "no visible". Funciona con las dos lecturas de la documentación.
- **`maxLength`.** La guía dice que el servidor lo ignora. Las longitudes se acotan en el dominio.

> **Importante:** La forma de la petición se ha comprobado contra la documentación y con un transporte simulado, no contra la API real. Antes de publicar, haz una llamada real con una captura de prueba. Consulta [Cómo probarlo](#cómo-probarlo-en-local).

## Variables de entorno

| Variable | Por defecto | Qué es |
|---|---|---|
| `FICHAITOR_GEMINI_API_KEY` | vacía | Clave de la API de Gemini. De un proyecto de pago |
| `FICHAITOR_GEMINI_MODEL` | `gemini-3.6-flash` | Modelo. Con o sin el prefijo `models/` |
| `FICHAITOR_FAKE_SCREENSHOT` | `false` | `true`: adaptador falso, sin modelo y sin clave |
| `FICHAITOR_SCREENSHOT_MAX_BYTES` | `6000000` | Tamaño máximo de la captura. 12 MB como mucho |
| `FICHAITOR_SCREENSHOT_RATE_LIMIT` | `3` | Capturas por IP en cada ventana |
| `FICHAITOR_SCREENSHOT_RATE_WINDOW_SECONDS` | `3600` | Ventana del límite por IP |
| `FICHAITOR_SCREENSHOT_DAILY_LIMIT` | `300` | Capturas cada 24 horas entre todos los clientes |
| `FICHAITOR_SCREENSHOT_TIMEOUT_SECONDS` | `30` | Tiempo máximo de las dos llamadas juntas |
| `FICHAITOR_SCREENSHOT_MIN_CONFIDENCE` | `0.6` | Confianza mínima sobre qué pantalla es la captura |

**Sin clave y sin modo falso, la ruta no existe** (404) y la API arranca con un aviso en el log. La demostración es opcional: una clave que falta no debe tumbar la conexión con Google ni el panel. Es el mismo criterio que ya sigue la API con las rutas de OAuth cuando Google no está configurado.

## Cómo probarlo en local

### Con el modo falso

1. Monta el router en `app/api/main.py` (el texto exacto está en el informe de entrega de esta función).
2. Arranca la API sin Google y sin modelo:

   ```bash
   FICHAITOR_FAKE_PLACES=true FICHAITOR_FAKE_SCREENSHOT=true uv run uvicorn app.api.main:create_app --factory --port 8000
   ```

3. Envía una imagen:

   ```bash
   curl -s -F "image=@captura.png" -F "source=prueba" http://localhost:8000/api/v1/public/diagnosis/screenshot
   ```

El adaptador falso responde según el **formato** de la imagen, para poder maquetar todas las pantallas:

| Formato | Respuesta |
|---|---|
| PNG | 200: 4 reseñas, 3 sin contestar, 1 de ellas de 1 estrella, con respuesta propuesta |
| JPEG | 422: no es una ficha de Google |
| WebP | 422: es la ficha, pero no la pestaña de reseñas |

Para el resto de los estados: un archivo que no sea imagen da 415, uno de más de 6 MB da 413 y la cuarta captura en una hora da 429.

### Con el modelo real

1. Define `FICHAITOR_GEMINI_API_KEY` y deja `FICHAITOR_FAKE_SCREENSHOT` sin definir.
2. Envía una captura real de la pestaña de reseñas con el mismo `curl`.
3. Comprueba que los recuentos coinciden con lo que se ve y que ningún nombre de autor aparece en la respuesta.

### Pruebas

```bash
uv run pytest -q tests/test_screenshot_domain.py tests/test_screenshot_gemini.py tests/test_screenshot_api.py
```

Ninguna llama a una API real: el adaptador de Gemini se prueba con `httpx.MockTransport`.

## Lo que queda fuera

- **Conjunto de evaluación.** [06-estandares.md](06-estandares.md) pide un conjunto de capturas reales con salida esperada para cada agente. No existe todavía: la lectura y la calidad de las respuestas no se han medido.
- **Umbrales de los filtros de seguridad.** Se usan los que trae el modelo por defecto. Si bloquean capturas legítimas con lenguaje fuerte, hay que valorar `safetySettings`.
- **Capturas de otros idiomas o de la versión de escritorio.** El prompt está pensado para Google Maps en español en el móvil.
