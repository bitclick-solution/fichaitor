# Calendario de fechas clave

Este documento describe los datos del calendario, su origen, cómo se actualizan cada año y qué reglas deciden qué fecha se propone a cada negocio.

Estado a 12 de noviembre de 2026.

## Piezas

| Pieza | Ruta | Qué hace |
|---|---|---|
| Datos | `data/calendar/` | Festivos por año, fechas comerciales y mapa de sectores, en JSON |
| Cargador | `app/adapters/calendar/loader.py` | Lee los JSON y valida su forma al arrancar |
| Importador del BOE | `app/adapters/calendar/boe_import.py` | Convierte el XML oficial de la resolución anual en el JSON de festivos |
| Reglas | `app/domain/calendar.py` | Reglas puras: qué toca proponer hoy, agrupación, aprendizaje y texto |
| Pruebas | `tests/test_calendar.py`, `tests/test_calendar_loader.py` | Reglas, datos reales, cargador e importador |

El dominio no lee archivos ni consulta el reloj: recibe el calendario ya cargado y el día como argumentos.

## Datos

### Festivos nacionales y autonómicos

Un archivo por año: `data/calendar/<año>-festivos.json`.

| Archivo | Cobertura | Fuente oficial |
|---|---|---|
| `2027-festivos.json` | Año completo, 17 comunidades, Ceuta y Melilla | [Resolución de 8 de octubre de 2026 (BOE-A-2026-21254)](https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-21254), BOE del 13 de octubre de 2026 |
| `2026-festivos.json` | Del 11 de noviembre al 31 de diciembre de 2026 | [Resolución de 17 de octubre de 2025 (BOE-A-2025-21667)](https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667), BOE del 28 de octubre de 2025 |

Los dos archivos se han generado con el importador a partir del XML oficial del BOE, no copiando la tabla a mano.

Cada entrada de `holidays` tiene estos campos:

| Campo | Contenido |
|---|---|
| `date` | Fecha ISO (`2027-12-08`) |
| `name` | Nombre tal como figura en el anexo |
| `scope` | `national` si la fiesta figura en los 19 territorios; si no, el código de la comunidad |
| `substitutable` | `true` si el BOE la marca con `**`: fiesta nacional que la comunidad podía sustituir y no ha sustituido |
| `boe_code` | Código literal del anexo: `*` nacional no sustituible, `**` nacional no sustituida, `***` fiesta de la comunidad |

Los códigos de comunidad son los de ISO 3166-2:ES: `AN`, `AR`, `AS`, `IB`, `CN`, `CB`, `CM`, `CL`, `CT`, `EX`, `GA`, `MD`, `MC`, `NC`, `PV`, `RI`, `VC`, `CE` y `ML`.

El archivo incluye además:

- `source`: título, referencia, URL, fecha de publicación, fecha de consulta (`consulted`) y fuentes de contraste (`cross_checked_with`).
- `coverage`: intervalo de fechas que cubre el archivo.
- `regions`: los 19 territorios, cada uno con `verified`.
- `subregional`: fiestas que solo valen en parte de una comunidad, tomadas de las notas de la resolución. No se proponen todavía. Consulta [Qué queda pendiente](#qué-queda-pendiente).
- `notes`: las notas aclaratorias literales de la resolución.

### Festivo en domingo trasladado al lunes

Las reglas no trasladan fechas. Cuando una fiesta nacional cae en domingo, la resolución ya indica qué comunidades descansan el lunes y cuáles la sustituyen por otro día, y el archivo recoge ese resultado. Ejemplos de 2027, con el 15 de agosto en domingo:

- Andalucía, Aragón, Asturias, Canarias, Castilla y León y Madrid tienen el lunes 16 de agosto.
- Extremadura lo sustituye por el lunes 11 de octubre.
- El resto no tiene festivo por la Asunción. El 15 de agosto no figura en el archivo.

### Fechas comerciales

`data/calendar/fechas-comerciales.json`. Cada evento tiene una regla de cálculo, no una lista de fechas por año.

| Evento | Tipo | Antelación | Regla | Sectores |
|---|---|---|---|---|
| Rebajas de invierno | `commercial` | 14 días | 7 de enero (inicio habitual) | Comercio |
| San Valentín | `commercial` | 14 días | 14 de febrero | Hostelería, comercio, belleza y salud |
| Día del Padre | `commercial` | 14 días | 19 de marzo | Hostelería, comercio, belleza y salud |
| Semana Santa | `season` | 21 días | Domingo de Ramos (Pascua menos 7 días), 9 días | Todos |
| Día de la Madre | `commercial` | 14 días | Primer domingo de mayo | Hostelería, comercio, belleza y salud |
| Horario de verano | `season` | 21 días | 1 de julio (convención) | Todos |
| Rebajas de verano | `commercial` | 14 días | 1 de julio (inicio habitual) | Comercio |
| Vuelta al cole | `season` | 21 días | 1 de septiembre (convención) | Comercio, belleza y salud, otros |
| Black Friday | `commercial` | 14 días | Día siguiente al cuarto jueves de noviembre | Comercio, belleza y salud |
| Cyber Monday | `commercial` | 14 días | Lunes siguiente a Black Friday | Comercio |
| Navidad | `season` | 21 días | 24 de diciembre, 2 días | Todos |
| Nochevieja | `season` | 21 días | 31 de diciembre, 2 días | Todos |

Reglas disponibles en el campo `rule`:

- `fixed`: `month` y `day`.
- `nth_weekday`: enésimo día de la semana de un mes (`month`, `weekday` con 0 para el lunes, `n` de 1 a 4).
- `easter`: Domingo de Pascua, calculado en `easter_sunday`.
- `offset_days`: opcional en cualquiera de las tres; suma o resta días.

**Importante:** Black Friday no es siempre el cuarto viernes de noviembre. Es el día siguiente a Acción de Gracias, que es el cuarto jueves. Cuando noviembre empieza en viernes, cae en el quinto viernes: 29 de noviembre en 2019 y en 2024, y de nuevo en 2030. La regla del archivo usa el cuarto jueves más un día.

**Nota:** Las fechas de rebajas, horario de verano y vuelta al cole son costumbres, no normas. El campo `note` de cada evento lo indica.

### Sectores

`data/calendar/sectores.json` asigna cada categoría de Google (tipo de Places API) a uno de los cinco sectores: `hospitality`, `retail`, `beauty_health`, `professional_services` y `other`.

`sector_for` busca primero la categoría principal y después las secundarias. Para cada una prueba la categoría exacta (`google_types`) y luego la terminación (`suffixes`: `_restaurant`, `_store`, `_shop`). Si nada coincide, el sector es `other`.

### Fiestas locales

No hay fuente nacional. El dueño declara las dos fiestas locales de su municipio en el alta y se guardan como `LocalHoliday(date, name)` en `Business.local_holidays`. `check_local_holidays` rechaza fechas repetidas y más de dos por año.

Una fiesta local declarada se propone igual que un festivo, con el texto "es fiesta local". Si coincide con un festivo oficial, no se duplica.

## Reglas

`due_proposals(business, today, calendar, already_proposed)` devuelve las propuestas que toca enviar hoy.

| Regla | Comportamiento |
|---|---|
| Antelación | Festivo: 5 días. Fecha comercial: 14. Temporada: 21. La antelación de cada evento comercial sale de su `lead_days` |
| Ventana | Una fecha se puede proponer desde su día de aviso hasta que caduca: el festivo y la temporada, el mismo día; la fecha comercial, 2 días antes |
| Sin repetir | `already_proposed` contiene las claves (`KeyDate.key`) de las fechas que ya tienen propuesta. Quien llama las guarda al crear la propuesta |
| Relevancia | Un evento se propone si el sector del negocio está en `sectors` o si el dueño ya lo usó (`accepted_events`). Los festivos valen para todos |
| Agrupación | Las fechas separadas por 3 días o menos van en una sola propuesta. Una fecha que aún no toca pero cae junto a otra que sí se adelanta a la misma propuesta |
| Descarte | Una fecha descartada (`Dismissal`) no se vuelve a proponer ese año |
| Aprendizaje | Un evento comercial o de temporada descartado dos años seguidos deja de proponerse. Vuelve si el dueño lo pide (`accepted_events`). Los festivos nunca se silencian |
| Sin verificar | Los festivos de una comunidad con `verified: false` no se proponen |
| Texto | Fecha completa en español ("martes 8 de diciembre"), con tablas propias de días y meses; no depende del locale del sistema |

Ejemplos de texto:

- Un festivo: "El martes 8 de diciembre es festivo. ¿Abrís?"
- Dos festivos: "El lunes 7 de diciembre y el martes 8 de diciembre son festivos. ¿Abrís?"
- Una fecha comercial: la pregunta del evento, con la fecha.
- Combinada: "Se acercan varias fechas: Día del Padre (viernes 19 de marzo) y festivo el viernes 19 de marzo. ¿Tienes alguna oferta especial o cambia tu horario?"

`key_dates(business, calendar, years)` devuelve todas las fechas de un negocio para la pantalla Calendario.

El límite de mensajes por semana, el horario de silencio, los recordatorios y los botones no están aquí: son del planificador y de la propuesta.

## Preguntar por el horario

Desde el 2026-11-12 el producto pregunta al dueño, por cada ficha y día, si abre y con qué horario. Las reglas puras están en `app/domain/key_dates.py`; el cambio de horario, en `app/domain/special_hours.py`. Manda el código: `due_proposals` (agrupación de varias fechas en un mensaje) no se usa para esto.

| Regla | Comportamiento |
|---|---|
| Qué días | Los festivos de la ficha (nacionales, de su comunidad y, si los datos los tienen, de su municipio) y el primer día de las temporadas `christmas` y `new_years_eve`: **Nochebuena** y **Nochevieja**. Las fechas comerciales (Black Friday, rebajas) piden una oferta, no un horario: esperan a las publicaciones |
| Antelación | Festivo: 5 días (`FICHAITOR_KEY_DATES_LEAD_DAYS`). Nochebuena y Nochevieja: los 21 días de su temporada (`lead_days` del archivo) |
| Ventana | Desde el aviso hasta el día anterior. El mismo día ya no se pregunta |
| Una por día | Una propuesta por ficha y día, con clave `<loc_…>:<AAAA-MM-DD>`. Si coinciden un festivo y Nochebuena, gana el festivo. Repetir la planificación no crea nada |
| Ya resuelto en Google | No se pregunta un día que ya tiene horario especial en la ficha (`facts.special_dates` de la última sincronización) |
| Comunidad | La guarda la sincronización en `facts.region`, a partir del código postal de la dirección (`storefrontAddress.postalCode`, solo si `regionCode` es `ES`): las dos primeras cifras son la provincia. Si no está (fichas sincronizadas antes), se saca del código postal de la dirección guardada. Sin ninguno, solo festivos nacionales |
| Municipio | No se conoce todavía (la ficha no trae el código INE): las fiestas locales precargadas no se proponen |
| Qué fichas | Las elegidas de cuentas no dadas de baja ni desconectadas, sin baja programada, no retiradas, cuya última sincronización no perdió el acceso y guardó sus hechos. Con chat vinculado o sin él: el panel las muestra igual |
| Cuándo salen | El job `plan_key_dates` corre una vez al día a las 9:05 (hora peninsular). Los mensajes los envía la conversación con sus reglas: horario de silencio (de 21:00 a 9:00), límite semanal y cola |
| Caducidad | La propuesta sin respuesta caduca a las 00:00 del día (flujo 3 del guion); el dueño todavía puede responderla ese día desde el chat (flujo 9) o desde el panel. Una confirmada que el worker no consigue aplicar antes de que acabe el día falla con `date_passed` |

Las respuestas posibles: **cerrado**, **horario normal** (se copia el horario normal de ese día de la semana que la ficha tenga en Google al enviarlo) u **otro horario** (una o dos franjas, sin cruzar la medianoche). Consulta `docs/canales.md` y `docs/contrato-web-api.md`, "Fechas clave".

## Actualizar los festivos cada año

La resolución se publica en el BOE en octubre. Haz la carga antes del 1 de diciembre: el aviso del 1 de enero sale el 27 de diciembre y la temporada de Nochevieja incluye el Año Nuevo.

1. Busca en [boe.es](https://www.boe.es/buscar/) "relación de fiestas laborales para el año" y anota la referencia (`BOE-A-AAAA-NNNNN`).
2. Descarga el XML oficial:

   ```bash
   curl -L -o resolucion.xml "https://www.boe.es/diario_boe/xml.php?id=BOE-A-AAAA-NNNNN"
   ```

3. Genera el archivo del año:

   ```bash
   uv run python -m app.adapters.calendar.boe_import resolucion.xml --out data/calendar/2028-festivos.json
   ```

   El importador falla si la tabla cambia de columnas o si aparece un código que no conoce. Todas las comunidades salen con `"verified": false`.

4. Comprueba que cada comunidad suma 12 fiestas. Si alguna suma menos, lee las notas (`notes`): suele ser una fiesta por isla o por territorio.
5. Contrasta cada comunidad con una segunda fuente, por ejemplo el decreto de la comunidad o un calendario laboral publicado. Si coincide, cambia su `verified` a `true`. Si no coincide, prevalece el BOE; anótalo en `verification_note`.
6. Rellena `source.consulted` con la fecha y `source.cross_checked_with` con las URL de contraste.
7. Pasa a `subregional` las fiestas de las notas (islas de Canarias, Arán).
8. Ajusta en `tests/test_calendar_loader.py` la lista de archivos esperados y añade las comprobaciones del año nuevo.
9. Ejecuta las pruebas:

   ```bash
   uv run pytest -q tests/test_calendar.py tests/test_calendar_loader.py
   ```

10. Cuando el año anterior haya terminado, borra su archivo.

**Precaución:** Una comunidad puede corregir su calendario después de la resolución. El BOE publica entonces una corrección de errores. Revisa en enero si la hay.

Las fechas comerciales no necesitan carga anual. Revisa `fechas-comerciales.json` una vez al año por si cambia una costumbre.

## Qué queda sin verificar

Festivos:

| Datos | Estado |
|---|---|
| 2027, los 19 territorios | Verificados contra el XML oficial del BOE. Contrastados con dos fuentes secundarias con desglose por comunidad ([fedratecalc](https://fedratecalc.com/es/calendario-laboral-2027/), [PayFit](https://payfit.com/es/contenido-practico/calendario-laboral-2027/)) y, para los nueve nacionales, con [Garrigues](https://www.garrigues.com/es_ES/noticia/publicado-boe-calendario-laboral-espana-2027). Única discrepancia: PayFit atribuye el 5 de agosto a Melilla; el BOE lo asigna a Ceuta y eso figura en el archivo |
| 2026, del 11 de noviembre al 31 de diciembre | Verificados contra el XML oficial del BOE. El contraste secundario es parcial: confirma el 8 y el 25 de diciembre y la lista del 7 de diciembre ([TeamSystem](https://teamsystem.es/magazine/calendario-laboral-2026/), [Sage](https://www.sage.com/es-es/blog/calendario-laboral-festivos-comunes-en-toda-espana/)). Una fuente añade Ceuta al 7 de diciembre, que el BOE no recoge. El 26 de diciembre en Illes Balears solo está confirmado por el BOE |

No hay ninguna comunidad con `verified: false`.

Datos que no vienen de una fuente oficial:

- Las fechas de inicio de rebajas, horario de verano y vuelta al cole son convenciones.
- La asignación de sectores a cada evento y el mapa de categorías de Google son criterio propio. Los nombres de categoría no se han comprobado uno a uno contra la lista actual de Places API.

## Qué queda pendiente

- **Fiestas por isla y Arán.** En Canarias, la duodécima fiesta es distinta en cada isla. En Arán, una fiesta de Cataluña se sustituye por el 17 de junio. Están en `subregional` con el campo `area`, pero no se proponen: falta la correspondencia de municipio a isla o territorio. Hasta entonces, el dueño las declara en el alta como fiesta local. En Arán, además, el 29 de marzo de 2027 y el 26 de diciembre de 2026 se proponen como festivos aunque allí no lo son.
- **Fiestas locales precargadas.** Solo está definido el formato. Consulta la sección siguiente.
- **Años sin datos.** Si falta el archivo de un año, ese año no se propone ningún festivo oficial. `CalendarData.holiday_years` indica qué años hay cargados, para que el arranque avise.

## Añadir las fiestas locales precargadas de una comunidad

Andalucía es la primera candidata. No está cargada.

### Fuente

La Junta de Andalucía publica cada año en el BOJA una resolución con las fiestas locales de todos los municipios, y mantiene la relación en su web: [Calendario laboral y fiestas locales](https://www.juntadeandalucia.es/organismos/empleoempresaytrabajoautonomo/areas/relaciones-laborales/calendario-fiestas.html). Suele publicarse entre octubre y diciembre del año anterior. La resolución de 2027 no se ha localizado todavía.

### Formato

Un archivo por año y comunidad: `data/calendar/<año>-locales-<comunidad>.json`. El cargador ya lo reconoce y lo valida.

```json
{
  "year": 2027,
  "region": "AN",
  "source": {
    "title": "Título literal de la resolución del BOJA con las fiestas locales de 2027",
    "url": "https://www.juntadeandalucia.es/boja/...",
    "consulted": "2026-12-01"
  },
  "holidays": [
    { "date": "2027-06-03", "name": "Corpus Christi", "municipality": "18087" }
  ]
}
```

- `municipality` es el código INE de 5 cifras, el mismo que `Business.municipality_code`.
- Cada municipio tiene como máximo dos entradas.
- La fecha del ejemplo es inventada para mostrar el formato.

### Pasos

1. Descarga la relación de la resolución del BOJA.
2. Asigna a cada municipio su código INE con la relación de municipios y códigos que publica el INE (ine.es). Los nombres del BOJA no siempre coinciden con los del INE: revisa a mano los que no casen.
3. Escribe el archivo con el formato anterior y ejecuta las pruebas.
4. En el alta, si el municipio del negocio tiene fiestas precargadas, el agente las propone y el dueño solo confirma. Si el dueño declara otras, valen las suyas.

Un festivo precargado solo se aplica a los negocios cuyo `municipality_code` coincide. Un negocio sin código de municipio no recibe ninguno.

## Qué hace falta fuera de estas rutas

- **Imagen de Docker.** Hecho: el `Dockerfile` copia `data`.
- **Arranque.** El proceso de la conversación llama a `load_calendar()` al arrancar. Si un archivo está mal formado, registra `key_dates_calendar_error` y sigue sin fechas clave (el resto del producto no se detiene).
- **Persistencia.** El LLD prevé `event_preference(declined_count)`. Para la regla de dos años seguidos hace falta el año de cada descarte: guarda una fila por descarte con el identificador del evento y la fecha, que es lo que recibe `Business.dismissed`.
- **Alta.** Falta la pregunta de las dos fiestas locales y su confirmación cada diciembre.
- **Planificador.** Hecho para el horario (`plan_key_dates`): la clave de cada propuesta es la ficha y el día, y la base impide repetirla. El aprendizaje por descartes (`Dismissal`) no se aplica todavía a los festivos, que nunca se silencian.
