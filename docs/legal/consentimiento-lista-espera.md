# Texto de consentimiento de la lista de espera

> **BORRADOR — no publicar sin revisión.** Debe revisarlo un profesional junto con la política de privacidad.

## Versión vigente

| Campo | Valor |
|---|---|
| `consent_version` | `waitlist-2026-11-11` |
| Fecha | 11 de noviembre de 2026 |
| Política enlazada | `https://fichaitor.es/privacidad/` |

### Texto de la casilla

La casilla va sin marcar por defecto y es obligatoria para enviar el formulario.

> Quiero que Fichaitor me avise por este medio cuando esté disponible. He leído la [Política de Privacidad](https://fichaitor.es/privacidad/).

### Información básica, bajo la casilla

> Responsable: Bitclick Solutions S.L.U. Finalidad: avisarte del lanzamiento de Fichaitor. Base: tu consentimiento. Puedes darte de baja cuando quieras respondiendo «baja» o en soporte@fichaitor.es.

## Reglas

- El formulario envía `consent: true` y `consent_version` con el valor de la tabla. El backend guarda la versión y la fecha (`consent_at`).
- Cualquier cambio en el texto de la casilla, por pequeño que sea, crea una versión nueva con el formato `waitlist-AAAA-MM-DD`. El texto anterior no se borra: se mueve al historial.
- El identificador admite 40 caracteres como máximo (`db/schema.sql`).
- El contacto solo se usa para avisar del lanzamiento. Enviar otras comunicaciones comerciales requiere otro consentimiento.

## Historial

| `consent_version` | Vigente desde | Texto |
|---|---|---|
| `waitlist-2026-11-11` | Pendiente de publicar | El de arriba |
