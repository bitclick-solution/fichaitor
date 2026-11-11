# Fase 0.4 — Calendario de festivos

Investigación documental del 2026-11-11.

## Cómo se publican los festivos en España

| Ámbito | Cuántos | Dónde se publican | Cuándo |
|---|---|---|---|
| Nacionales y autonómicos | Hasta 12 por comunidad | Una sola resolución de la Dirección General de Trabajo en el BOE | Octubre del año anterior. La de 2027 es la Resolución de 8 de octubre de 2026 (BOE del 13 de octubre) |
| Locales | 2 por municipio | Boletín de cada comunidad autónoma o provincia | Entre octubre y diciembre del año anterior |

## Hallazgo principal

**No he encontrado un conjunto de datos nacional con los festivos locales de todos los municipios.** Cada comunidad los publica por su cuenta y en formatos distintos:

- Andalucía: relación por municipio en la web de la Junta, a partir de una resolución anual.
- Región de Murcia: anuncio en el BORM con los 45 municipios.
- Madrid capital: portal de datos abiertos, en CSV y en ICS.
- El resto: boletines oficiales, normalmente en PDF.

Son unas 20 fuentes y más de 8.000 municipios. No queda confirmado si la Seguridad Social u otro organismo ofrece un agregado descargable; la búsqueda no lo encontró.

## Propuesta para el MVP

1. **Nacionales y autonómicos: carga anual desde el BOE.** Una fuente, una vez al año, revisada a mano. Es poco trabajo y cubre unos 12 de los 14 festivos de cada negocio.
2. **Locales: preguntarlos en el alta y confirmarlos cada año.** "¿Cuáles son las dos fiestas locales de tu municipio?" El dueño lo sabe, la respuesta es exacta y el producto no depende de 20 fuentes. Cada diciembre, el agente pregunta si siguen siendo las mismas.
3. **Precarga por comunidad, empezando por Andalucía.** Para los municipios donde ya tengamos el dato, el agente lo propone y el dueño solo confirma. Se añaden comunidades según dónde haya clientes.
4. **Clave de municipio: el código del INE**, que se obtiene de la dirección de la ficha.
5. **Fechas comerciales** (Black Friday, rebajas, San Valentín, Día de la Madre, Navidad): tabla propia, mantenida a mano, con relevancia por sector.

La opción 2 convierte un problema de datos en una pregunta de alta, y además cubre casos que ninguna fuente oficial recoge, como el negocio que cierra en las fiestas del pueblo aunque no sean festivo oficial.

## Pendiente

- Comprobar si existe un agregado oficial (Seguridad Social, datos.gob.es).
- Cargar la resolución del BOE de 2027 en el formato de `calendar_event`.
- Localizar la resolución de fiestas locales de Andalucía para 2027 y valorar su formato.

## Fuentes

- [Junta de Andalucía: calendario laboral y fiestas locales](https://www.juntadeandalucia.es/organismos/empleoempresaytrabajoautonomo/areas/relaciones-laborales/calendario-fiestas.html)
- [Ayuntamiento de Madrid: calendario laboral en datos abiertos](https://datos.madrid.es/dataset/300082-0-calendario_laboral/downloads)
- [Garrigues: publicado en el BOE el calendario laboral de 2027](https://www.garrigues.com/es_ES/noticia/publicado-boe-calendario-laboral-espana-2027)
- [Laboral Social: calendario laboral de 2027](https://www.laboral-social.com/node/30660)
- [Murcia Plaza: festivos de los 45 municipios para 2027](https://murciaplaza.com/murciaplaza/region/publican-el-calendario-de-festivos-de-cada-uno-de-los-45-municipios-de-la-region-de-murcia-para-2027)
