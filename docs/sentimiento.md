# Sentimiento del establecimiento

El sentimiento del establecimiento es un indicador del panel: una nota de 0 a 100, con historia, que dice cómo ve la gente el negocio según las reseñas de su ficha de Google. Lo acompañan los temas que más se elogian, los que más quejas reciben, un resumen y una sugerencia.

Este documento explica cómo se calcula, qué decide el modelo y qué decide el código, cuándo se recalcula, cuánto cuesta y qué se guarda. La forma exacta de la respuesta está en [contrato-web-api.md](contrato-web-api.md#sentimiento-del-establecimiento-2026-11-12).

## Reparto entre el modelo y el código

El principio es el del [HLD](02-hld.md): el modelo propone y el código decide. Lo que escribe un tercero es un dato, nunca una instrucción.

| Qué | Quién | Cómo se controla |
|---|---|---|
| Clasificar cada reseña con texto: `positive`, `neutral` o `negative`, y hasta 3 temas con su polaridad | El modelo, una sola vez por reseña | La respuesta se acepta solo si todos sus valores pertenecen a las listas cerradas. Si no, esa reseña queda sin clasificar |
| Clasificar las reseñas sin texto | El código | Regla fija sobre las estrellas. No se llama al modelo |
| La nota de 0 a 100 y la valoración general | El código | Fórmula determinista |
| Los recuentos por tema | El código | Cuenta reseñas. El modelo no puede decir "7 menciones" |
| Decidir cuándo se recalcula | El código | Función pura con pruebas |
| Redactar el resumen y la sugerencia | El modelo, en una segunda llamada | Se validan en código. Si no pasan, se usan textos construidos por reglas fijas, que existen siempre |

## Clasificación de las reseñas

- **Reseña sin texto.** Se clasifica por sus estrellas: 4 y 5 positiva, 3 neutra, 1 y 2 negativa. Sin temas y sin modelo.
- **Reseña con texto.** Se envía al modelo en lotes. Al modelo va solo un número de orden dentro de la llamada, las estrellas y el texto, cortado a 1.000 caracteres. Nunca un autor, el nombre del negocio ni un identificador nuestro.
- **Una sola vez.** La clasificación se guarda con una huella de las estrellas y el texto. Solo se vuelve a enviar una reseña nueva o una cuya huella ha cambiado.
- **Salida validada.** El esquema que se envía al modelo ya es cerrado (`responseJsonSchema` con `enum`, sin `null` ni uniones), pero es una petición, no una garantía. Cada elemento se valida otra vez en `app/domain/sentiment.py`: sentimiento, temas de la lista del sector, polaridad y un máximo de 3 temas. Un elemento que no cumple se descarta entero.
- **Reseña sin clasificar.** Queda marcada como pendiente y se reenvía en el siguiente análisis. Mientras tanto cuenta en la nota por sus estrellas, sin temas. Tras 3 intentos fallidos queda clasificada por sus estrellas de forma definitiva, para no pagar siempre por una reseña que el modelo rechaza.
- **Instrucciones dentro de una reseña.** No se obedecen: el texto viaja como dato en un objeto JSON, aparte de las instrucciones, y el prompt lo dice. Aunque el modelo obedeciera, la salida solo puede contener valores de las listas cerradas, y una reseña no cambia la clasificación de las demás porque cada elemento se valida por separado.
- **Fallo del modelo.** Una llamada que falla (tiempo agotado, respuesta bloqueada, cortada o mal formada) deja su lote pendiente. El job no falla y la nota se calcula igual.
- **Sin modelo configurado.** Todas las reseñas cuentan por sus estrellas, no hay temas y los textos salen de reglas. No se guarda ninguna clasificación de reseñas con texto, así que se clasifican cuando haya modelo.

### Temas

Los temas son una lista cerrada por sector, con etiquetas en español. Así los recuentos son comparables entre análisis. El modelo elige de la lista; lo que no encaja va a "Otro". La lista está en `TOPICS_BY_SECTOR`, en `app/domain/sentiment.py`.

| Sector | Temas |
|---|---|
| General | Trato del personal, Tiempo de espera, Calidad, Precio, Limpieza, Ambiente, Rapidez, Atención posventa, Variedad, Ubicación y acceso, Otro |
| Hostelería | Trato del personal, Tiempo de espera, Comida y bebida, Precio, Limpieza, Ambiente, Rapidez, Variedad, Ubicación y acceso, Otro |

El sector sale de la categoría principal de la ficha en Google (`sector_of`): bar, restaurante, cafetería y similares son hostelería; lo demás, general. "Otro" se guarda, pero no aparece en `praised` ni en `complaints`: no nombra nada.

Para añadir un sector, añade su lista a `TOPICS_BY_SECTOR` y su detección a `sector_of`. No cambies la etiqueta de un tema existente: las clasificaciones guardadas la llevan escrita.

## La nota

La nota es la proporción ponderada de reseñas positivas, en la que una neutra cuenta la mitad:

```text
nota = redondeo( 100 × Σ (peso × valor) / Σ peso )
```

- **Valor:** positiva 1, neutra 0,5, negativa 0.
- **Peso por antigüedad:** 1 para una reseña de hoy, baja en línea recta hasta 0,5 a los 12 meses, y 0,25 para las anteriores a 12 meses o sin fecha. La ventana principal son los últimos 12 meses; las reseñas anteriores cuentan, pero menos.
- **Redondeo:** al entero más cercano, con el medio hacia arriba.

Propiedades, todas con prueba en `tests/test_sentiment_domain.py`:

- **Determinista.** Con las mismas clasificaciones y la misma fecha de cálculo, la nota es siempre la misma, sea cual sea el orden. Se calcula con enteros (pesos en milésimas), sin decimales en coma flotante.
- **Añadir una reseña negativa nunca sube la nota**, y añadir una positiva nunca la baja.
- **Una reseña basta.** Con una reseña de 5 estrellas la nota es 100.

La valoración general sale de la nota:

| `overall` | Nota |
|---|---|
| `positivo` | 70 o más |
| `mixto` | De 40 a 69 |
| `negativo` | Menos de 40 |

> **Nota:** La nota depende de la fecha de cálculo, porque el peso depende de la antigüedad. Entre dos análisis no cambia: se guarda la del último.

## Recuentos por tema

- `praised` y `complaints` cuentan **reseñas**, no frases: un tema cuenta una vez por reseña. Ningún recuento supera `based_on`.
- Hasta 5 temas en cada lista, de más a menos menciones.
- Se cuentan las reseñas de los últimos 12 meses. Si en ese periodo hay menos de 5 reseñas con texto, se cuentan todas y `period` pasa de `"últimos 12 meses"` a `"todas las reseñas"`.

## Mínimos: provisional e insuficiente

| Situación | Estado | Qué se muestra |
|---|---|---|
| La ficha no tiene reseñas | `insuficiente` | Sin nota. Un mensaje útil y la sugerencia de conseguir las primeras reseñas |
| Al menos una reseña y menos de 5 con texto | `ok` con `provisional: true` | Nota y valoración. Temas con lo que haya. Resumen y sugerencia por reglas, honestos sobre la base. **Ninguna llamada al modelo para el resumen** |
| 5 reseñas con texto o más | `ok` | Todo. El modelo redacta el resumen y la sugerencia |

Decisión del propietario (2026-11-12): el análisis tiene que estar a la vista nada más conectar con Google y casi nunca debe salir vacío. Por eso basta una reseña para dar nota, y `insuficiente` queda solo para la ficha sin ninguna.

## Resumen y sugerencia

Solo cuando el análisis no es provisional, una segunda llamada al modelo redacta `summary` (2 frases como máximo) y `suggestion` (una acción concreta). Recibe la nota, la valoración, el número de reseñas, los recuentos por tema y hasta 6 extractos de reseñas recientes (las 3 más recientes que no son positivas y las 3 positivas más recientes), cada uno con sus estrellas y cortado a 280 caracteres. Sin autores.

El código valida cada texto (`validate_summary` y `validate_suggestion`):

- Longitud máxima y número de frases.
- Escrito en español.
- Sin datos de contacto, enlaces ni cifras con forma de teléfono.
- Sin promesas.
- Sin emojis ni marcas de formato.
- **Sin cifras que contradigan los datos.** Cualquier número del texto, en cifras o en letra, tiene que ser uno de los del análisis: la nota, 100, el número de reseñas, el de reseñas con texto, los recuentos por tema o los meses del periodo. Porcentajes y decimales se rechazan siempre.

Si el resumen no pasa, se descarta y se usan los textos construidos por reglas fijas a partir de los recuentos (`rule_summary` y `rule_suggestion`). Si solo falla la sugerencia, se usa la sugerencia por reglas. Los textos por reglas existen siempre y pasan las mismas validaciones; hay una prueba que lo comprueba para todas las combinaciones.

## Cuándo se calcula

El análisis es un job propio del worker, `analyze_sentiment`. Al terminar cada sincronización de la ficha (`sync_profile`), el worker decide si toca y lo encola. La decisión es la función pura `decide`, en `app/domain/sentiment.py`.

**Primer análisis, sin tope.** Se encola en cuanto se guardan las reseñas de la primera sincronización, con ejecución inmediata. No espera ventanas ni topes diarios. También se trata como primero cuando el análisis anterior falló y cuando el anterior no encontró reseñas y ahora las hay.

**Recálculos.** Como máximo uno cada 24 horas, con lo primero que ocurra:

| Disparador | Condición |
|---|---|
| `low_rating` | Llega una reseña nueva de 1 o 2 estrellas |
| `new_reviews` | Hay 3 reseñas nuevas desde el último análisis |
| `monthly` | Han pasado 30 días y hay al menos una reseña nueva |

- **Reseña nueva** es la que ningún análisis ha visto todavía o cuyo texto o estrellas han cambiado. La cuenta sale del estado guardado, no de eventos: una reseña que llega dentro del tope de 24 horas no se pierde, dispara el análisis en la primera sincronización posterior al tope.
- **Sin reseñas nuevas no se recalcula**, aunque pasen meses.
- **El job es idempotente.** Vuelve a decidir al ejecutarse: lanzado dos veces, el segundo no hace nada y no cuesta nada. Su clave de idempotencia agrupa por ficha y minuto.
- **Si el job agota sus intentos**, se anota `error` solo si la ficha no tenía análisis. Un análisis bueno nunca se sustituye por un error.

> **Nota:** Una reseña que Google deja de devolver sale del cálculo en el siguiente análisis, pero su desaparición no dispara uno por sí sola.

### Planes

Todavía no hay tabla de planes. El punto de extensión es una función que recibe la conexión y dice si aplican los disparadores por evento (`low_rating` y `new_reviews`) o solo el mensual: el parámetro `event_triggers` de `AnalyzeSentiment` y de `SentimentTrigger`, en `app/worker/analyze_sentiment.py`. Su valor por defecto es `all_triggers`, que devuelve siempre `True`: todos los disparadores para todos.

Para conectarlo cuando haya planes:

1. Escribe una función `(google_sub) -> bool` que consulte el plan de esa conexión.
2. Pásala como `event_triggers` a los dos objetos en `build_registry` (`app/worker/__main__.py`).

El primer análisis se hace siempre, con cualquier plan.

## Historia

Cada análisis con nota actualiza la foto del mes en curso: una fila por ficha y mes en `sentiment_history`. Los meses pasados no se tocan. Un mes sin análisis no tiene fila.

La ruta devuelve los últimos 12 meses con fila, de más antiguo a más reciente, y compara la nota actual con la del mes anterior que tenga fila (`previous_score` y `delta`).

## Coste

Límites configurables, que acotan el gasto de cada análisis:

| Variable | Por defecto | Qué limita |
|---|---|---|
| `FICHAITOR_SENTIMENT_BATCH_SIZE` | 30 | Reseñas por llamada de clasificación |
| `FICHAITOR_SENTIMENT_MAX_CALLS` | 6 | Llamadas al modelo por análisis. Una se reserva para el resumen: como mucho 5 de clasificación |
| `FICHAITOR_SENTIMENT_TIMEOUT_SECONDS` | 15 | Espera por llamada. `MAX_CALLS` × este valor debe quedar por debajo del tiempo máximo del job (120 s) |

Con los valores por defecto, un análisis clasifica como mucho 150 reseñas con texto, las más recientes primero. Las que no caben cuentan por sus estrellas y siguen siendo "nuevas": las toma el siguiente análisis.

### Estimación por análisis

> **Importante:** Estas cifras son una **estimación**. No se ha medido con reseñas reales ni se ha llamado al modelo real. El precio es el de `gemini-3.6-flash` en el nivel de pago que figura en [pantallazo.md](pantallazo.md#modelo-y-coste): 0,75 USD por millón de tokens de entrada y 3,75 USD por millón de salida hasta el 2026-12-31, según la [página de precios de la API de Gemini](https://aistudio.google.com/docs/pricing) consultada el 2026-11-11. No se ha vuelto a comprobar para este documento. Desde el 2027-01-01 los importes se duplican.

| Llamada | Entrada estimada | Salida estimada, razonamiento incluido | Coste estimado |
|---|---|---|---|
| Clasificar un lote de 10 reseñas | Unos 1.400 tokens: unos 600 de prompt y esquema y unos 80 por reseña | Unos 650 tokens | 0,0035 USD |
| Clasificar un lote lleno de 30 reseñas | Unos 3.000 tokens | Unos 1.400 tokens | 0,0075 USD |
| Resumen y sugerencia | Unos 900 tokens | Unos 350 tokens | 0,002 USD |

| Caso | Llamadas | Coste estimado |
|---|---|---|
| Primer análisis de una ficha típica (13 reseñas, 9 con texto) | 1 de clasificación y 1 de resumen | **Unos 0,006 USD** |
| Recálculo con 3 reseñas nuevas | 1 de clasificación y 1 de resumen | Unos 0,004 USD |
| Primer análisis de una ficha con 150 reseñas con texto o más | 5 de clasificación y 1 de resumen | Unos 0,04 USD |
| Análisis provisional o ficha solo con estrellas | 0 o 1 de clasificación, ninguna de resumen | De 0 a 0,002 USD |
| Techo teórico, si las 150 reseñas midieran 1.000 caracteres y el modelo agotara su salida (8.192 tokens por lote y 1.024 en el resumen) | 6 | Unos 0,20 USD |

Con el tope de un recálculo cada 24 horas, una ficha con reseñas nuevas todos los días costaría unos 0,12 USD al mes en recálculos típicos (30 recálculos de unos 0,004 USD).

### Uso real

Cada análisis añade una fila a `sentiment_usage` con el número de llamadas de clasificación y de resumen, las que fallaron, las reseñas enviadas y, si la respuesta de Gemini los trae en `usageMetadata`, los tokens de entrada y de salida (respuesta más razonamiento). Solo contadores: ningún texto.

Para ver el gasto real, con el usuario propietario:

```bash
docker compose exec -T db psql -U fichaitor -d fichaitor -c "select date_trunc('day', at) as dia, count(*) as analisis, sum(classify_calls + summary_calls) as llamadas, sum(input_tokens) as entrada, sum(output_tokens) as salida from sentiment_usage group by 1 order by 1 desc"
```

Corrige la tabla de estimaciones con las primeras cifras reales.

## Qué se guarda

Las tablas están en `db/schema_worker.sql`.

| Tabla | Qué guarda | Qué no guarda |
|---|---|---|
| `review_sentiment` | Por reseña: la huella de lo que se clasificó, el sentimiento, hasta 3 temas con polaridad, cómo se clasificó (`model`, `stars`, `stars_fallback` o `pending`) y cuántas veces se envió al modelo | Ningún texto de la reseña ni nada sobre quien la escribió |
| `sentiment_analysis` | Por ficha y conexión, el último análisis: estado, fecha, reseñas en las que se basa, nota, valoración, periodo, temas con sus recuentos, resumen, sugerencia, si los textos son del modelo o de reglas, y el disparador | Extractos de reseñas |
| `sentiment_history` | Por ficha y mes: la nota y el número de reseñas | — |
| `sentiment_usage` | Por análisis: contadores de llamadas y tokens | Textos |

- **Una fila por ficha y conexión** (`google_sub`, `location`), como el resumen: dos cuentas que administran la misma ficha tienen análisis separados y ninguna lee el de la otra.
- **El resumen y la sugerencia pueden parafrasear lo que dicen las reseñas**, pero se le pide al modelo que no copie frases, y nunca llevan autores: el modelo no los recibe.
- **Nada de esto va a los logs.** El worker registra contadores (reseñas, llamadas, tokens, nota) y nunca textos de reseñas ni resúmenes.

### Privilegios

| Rol | Puede | No puede |
|---|---|---|
| `fichaitor_worker` | Leer, insertar y actualizar `review_sentiment`, `sentiment_analysis` y `sentiment_history`; añadir filas a `sentiment_usage` | Borrar nada; leer o cambiar `sentiment_usage`, o fijar su fecha |
| `fichaitor_api` | Leer `sentiment_analysis` y `sentiment_history` | Leer la clasificación por reseña o el uso; escribir en ninguna de las cuatro |

## Qué se envía al modelo

| Llamada | Se envía | No se envía |
|---|---|---|
| Clasificación | La lista de temas y, por reseña, un número de orden, las estrellas y el texto | Autor, nombre del negocio, identificadores de Google o nuestros, fechas, respuestas del propietario |
| Resumen | Nota, valoración, número de reseñas, recuentos por tema y hasta 6 extractos con sus estrellas | Lo mismo |

Las reseñas son públicas en Google, pero su texto puede contener datos personales que escribió el cliente (por ejemplo, el nombre de un empleado). Tenlo en cuenta al elegir proveedor y región.

## Proveedor del modelo: beta y producción

**Decisión del propietario (2026-11-12):** durante la beta, el modelo se usa con la clave de Google AI Studio (`FICHAITOR_GEMINI_API_KEY`). En producción se usará Google Cloud (Vertex AI). Vertex AI no está implementado.

Toda la llamada HTTP está en un único módulo, `app/adapters/gemini_client.py`, que comparten el lector de capturas, el borrador de respuestas y el sentimiento. Para pasar a Vertex AI hay que cambiar ese módulo y la configuración, no los prompts, los esquemas ni el dominio:

| Qué cambia | Hoy (AI Studio) | Con Vertex AI |
|---|---|---|
| URL base | `https://generativelanguage.googleapis.com/v1beta/models/<modelo>:generateContent` (`BASE_URL` y `model_path`) | El punto de acceso de Vertex AI, que incluye el proyecto de Google Cloud y la región en la ruta |
| Autenticación | Clave de API en la cabecera `x-goog-api-key` | Token OAuth de una cuenta de servicio en la cabecera `Authorization`, que hay que renovar. Requiere dónde guardar la credencial y cómo renovarla |
| Región | No se elige | Hay que elegirla y configurarla. Añade una variable para ella |
| Configuración | `FICHAITOR_GEMINI_API_KEY` y `FICHAITOR_GEMINI_MODEL` (`app/screenshot_config.py`) | Proyecto, región y credencial de la cuenta de servicio |

Antes de implementarlo, comprueba en la documentación de Vertex AI que el cuerpo de la petición (`systemInstruction`, `generationConfig.responseJsonSchema`, `thinkingConfig`) y la respuesta (`candidates`, `usageMetadata`) son los mismos que usa hoy el cliente. No se ha verificado para este documento.

> **Importante:** La región se decide teniendo en cuenta la protección de datos, no solo el precio. Los textos de las reseñas pueden contener datos personales y los clientes son negocios españoles: lo previsible es una región de la Unión Europea, pero la decisión debe pasar por quien lleva la parte legal y quedar reflejada en la política de privacidad y en el registro de encargados del tratamiento.

## Variables de entorno

Las lee el worker, que es quien hace el análisis. La API lee `NEW_REVIEWS_TRIGGER` y `MONTHLY_DAYS` solo para redactar `next_update`: usa los mismos valores en los dos servicios.

| Variable | Por defecto | Uso |
|---|---|---|
| `FICHAITOR_SENTIMENT_WINDOW_MONTHS` | `12` | Ventana principal de la nota y de los temas |
| `FICHAITOR_SENTIMENT_MIN_TEXT_REVIEWS` | `5` | Con menos reseñas con texto, el análisis es provisional |
| `FICHAITOR_SENTIMENT_POSITIVE_MIN_SCORE` | `70` | Nota desde la que la valoración es `positivo` |
| `FICHAITOR_SENTIMENT_MIXED_MIN_SCORE` | `40` | Nota desde la que es `mixto` |
| `FICHAITOR_SENTIMENT_NEW_REVIEWS_TRIGGER` | `3` | Reseñas nuevas que disparan un recálculo |
| `FICHAITOR_SENTIMENT_MONTHLY_DAYS` | `30` | Días tras los que basta una reseña nueva |
| `FICHAITOR_SENTIMENT_MIN_HOURS_BETWEEN` | `24` | Tope: horas mínimas entre dos análisis. No aplica al primero |
| `FICHAITOR_SENTIMENT_BATCH_SIZE` | `30` | Reseñas por llamada |
| `FICHAITOR_SENTIMENT_MAX_CALLS` | `6` | Llamadas por análisis |
| `FICHAITOR_SENTIMENT_TIMEOUT_SECONDS` | `15` | Espera por llamada |

El modelo es el mismo que el de la captura y el de los borradores: `FICHAITOR_GEMINI_API_KEY` y `FICHAITOR_GEMINI_MODEL`, o `FICHAITOR_FAKE_SCREENSHOT=true` para el modelo de ejemplo. **El worker tiene que recibir esas variables**, además de la API.

## Probar en local

1. Aplica el esquema:

   ```bash
   docker compose exec -T db psql -q -U fichaitor -d fichaitor -v ON_ERROR_STOP=1 < db/schema_worker.sql
   ```

2. Arranca la API y el worker con `FICHAITOR_FAKE_GOOGLE=true` y `FICHAITOR_FAKE_SCREENSHOT=true`.
3. Conecta con Google, elige la ficha y pide `GET /api/v1/me/sentiment`. Responde `pendiente` unos segundos y después `ok` con la ficha de demostración.

Sin base de datos (`FICHAITOR_FAKE_PLACES=true`, sin `FICHAITOR_DATABASE_URL`), la API hace el análisis en memoria al elegir la ficha.

Pruebas:

```bash
uv run pytest -q tests/test_sentiment_*.py
```

Las de integración (`tests/test_sentiment_postgres.py`) necesitan las mismas variables que las del worker. Consulta [worker.md](worker.md#pruebas). Ninguna prueba llama a Gemini: el adaptador real se prueba con transporte HTTP simulado (`tests/test_sentiment_gemini.py`).

## Dónde está cada cosa

| Qué | Archivo |
|---|---|
| Reglas: clasificación, nota, recuentos, disparadores y textos | `app/domain/sentiment.py` |
| Job y disparador | `app/worker/analyze_sentiment.py` |
| Prompts y esquemas | `app/adapters/sentiment_model/prompt.py` |
| Modelo real y de ejemplo | `app/adapters/sentiment_model/gemini.py` y `fake.py` |
| Llamada HTTP a Gemini, compartida | `app/adapters/gemini_client.py` |
| Configuración | `app/sentiment_config.py` |
| Ruta y forma de la respuesta | `app/api/me.py` y `app/api/sentiment_out.py` |
| Tablas y privilegios | `db/schema_worker.sql` |
