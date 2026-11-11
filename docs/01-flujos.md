# Flujos: recorrido del usuario y conversación

## 1. Recorrido completo

```mermaid
flowchart LR
    A["Anuncio o vídeo en TikTok"] --> B["Diagnóstico público: nombre del negocio"]
    B --> C["Nota de la ficha y 5 mejoras"]
    C --> D["Alta: cuenta y pago"]
    D --> E["Conectar Google (OAuth)"]
    E --> F["Elegir ficha"]
    F --> G["Vincular WhatsApp con código"]
    G --> H["Primera conversación: el agente se presenta y propone la primera mejora"]
    H --> I["Uso diario: reseñas, fechas clave, preguntas"]
```

El diagnóstico usa solo datos públicos (Places API). No pide iniciar sesión.

## 2. Alta y vinculación

```mermaid
sequenceDiagram
    actor D as Dueño
    participant W as Web
    participant API as API
    participant G as Google
    participant WA as WhatsApp

    D->>W: Crea cuenta
    W->>API: Alta
    D->>G: Autoriza el acceso a su ficha
    G-->>API: Código de autorización
    API->>G: Lista de fichas
    D->>W: Elige ficha
    API-->>W: Código de 6 dígitos y enlace a WhatsApp
    D->>WA: Envía el código al número del agente
    WA->>API: Mensaje entrante
    API->>API: Vincula el teléfono a la cuenta
    API->>WA: Bienvenida y primera propuesta
```

Solo un teléfono vinculado con código puede ordenar cambios en la ficha. Un mensaje desde un número desconocido recibe una respuesta genérica con el enlace de alta.

## 3. Flujo de fecha clave (ejemplo: Black Friday)

```mermaid
flowchart TD
    S["Planificador diario"] --> R{"¿Hay una fecha clave relevante para este negocio dentro de su antelación?"}
    R -- No --> X["Fin"]
    R -- Sí --> T{"¿Se supera el límite de mensajes proactivos de la semana?"}
    T -- Sí --> P["Posponer o agrupar con otro aviso"]
    T -- No --> M["Enviar propuesta: se acerca Black Friday. ¿Oferta especial u horario distinto?"]
    M --> W{"Respuesta del dueño"}
    W -- "Botón: Tengo oferta" --> O["Pedir los datos: qué, cuánto, hasta cuándo"]
    W -- "Botón: Cambio horario" --> H["Pedir el horario"]
    W -- "Botón: Nada este año" --> N["Cerrar como descartada y anotar preferencia"]
    W -- "Texto libre o audio" --> I["Interpretar intención y datos"]
    W -- "Sin respuesta" --> RC["Un recordatorio; después caduca"]
    O --> I
    H --> I
    I --> V{"¿Datos completos y sin ambigüedad?"}
    V -- No --> Q["Una pregunta concreta de aclaración (máximo 2)"]
    Q --> I
    V -- Sí --> C["Resumen de lo que se va a publicar o cambiar, con vista previa"]
    C --> K{"¿Confirma?"}
    K -- "Sí" --> E["Ejecutor: aplica en Google"]
    K -- "Cambiar" --> I
    K -- "Cancelar" --> N
    E --> Z{"¿Google acepta?"}
    Z -- Sí --> F["Confirmación con enlace a la ficha"]
    Z -- No --> ER["Aviso claro del fallo y reintento o paso al panel"]
```

Reglas:

- **Antelación por tipo de fecha.** Festivo: 5 días. Fecha comercial con oferta (Black Friday, rebajas, San Valentín): 14 días, para que el post tenga tiempo de verse. Temporada (horario de verano): 21 días.
- **Relevancia por sector.** Black Friday se propone a comercio y servicios con oferta, no a una notaría. La relevancia sale de la categoría de la ficha y de lo que el dueño ha aceptado o descartado antes.
- **El descarte se aprende.** Si descarta Black Friday dos años, deja de proponerse.

## 4. Estados de una propuesta

Toda iniciativa del agente (fecha clave, respuesta a reseña, mejora de la ficha) es una **propuesta** con el mismo ciclo de vida.

```mermaid
stateDiagram-v2
    [*] --> programada
    programada --> enviada: llega su momento
    programada --> cancelada: deja de ser relevante
    enviada --> recopilando: el dueño responde
    enviada --> recordada: sin respuesta en el plazo
    recordada --> recopilando: el dueño responde
    recordada --> caducada: sin respuesta
    recopilando --> recopilando: aclaración
    recopilando --> por_confirmar: datos completos
    recopilando --> descartada: el dueño dice que no
    por_confirmar --> ejecutando: confirma
    por_confirmar --> recopilando: pide cambios
    por_confirmar --> descartada: cancela
    ejecutando --> aplicada: Google acepta
    ejecutando --> fallida: error definitivo
    aplicada --> [*]
    descartada --> [*]
    caducada --> [*]
    fallida --> [*]
    cancelada --> [*]
```

Solo hay **una propuesta activa por conversación**. Si llega otra mientras tanto, espera en cola, salvo una reseña negativa, que tiene prioridad.

## 5. Flujo de reseña

```mermaid
flowchart TD
    A["Google notifica una reseña nueva"] --> B["Guardar y clasificar: estrellas, temas, sentimiento"]
    B --> C["Redactar respuesta con la voz del negocio"]
    C --> D["Crítico: fidelidad, tono, sin datos inventados"]
    D --> E{"Modo del negocio y tipo de reseña"}
    E -- "Automático y 4-5 estrellas sin queja" --> F["Publicar y avisar en el resumen diario"]
    E -- "Resto" --> G["Enviar al dueño: reseña, respuesta propuesta y botones"]
    G --> H{"Respuesta"}
    H -- "Publicar" --> I["Ejecutor publica"]
    H -- "Editar" --> J["El dueño escribe o dicta su versión"]
    J --> I
    H -- "No responder" --> K["Cerrar"]
    I --> L["Guardar la edición como aprendizaje de voz"]
```

Una reseña de 1 o 2 estrellas **nunca** se responde en automático.

## 6. Mensajes por iniciativa del dueño

El dueño puede escribir en cualquier momento. El enrutador clasifica el mensaje:

| Intención | Ejemplo | Qué pasa |
|---|---|---|
| Respuesta a la propuesta activa | "sí", "de 10 a 14" | Sigue el flujo de la propuesta |
| Orden sobre la ficha | "hoy cierro", "cambia el teléfono" | Crea una propuesta nueva y pide confirmación |
| Consulta al experto | "¿por qué salgo por debajo del bar de al lado?" | Respuesta con los datos de su ficha |
| Contenido | Foto, audio con una idea de post | Versión Pro; en el básico, lo explica y ofrece el plan |
| Cuenta | "baja", "factura" | Enlace al panel |
| No entendido | — | Una pregunta de aclaración; a la segunda, ofrece opciones con botones |

## 7. Excepciones y ambigüedades

| Caso | Tratamiento |
|---|---|
| Horario ambiguo ("abrimos por la mañana") | Se pregunta la hora exacta. Nunca se supone |
| Fecha ambigua ("el viernes") | Se confirma con la fecha completa en el resumen |
| Dato que contradice la ficha (oferta de un servicio que no consta) | Se pregunta antes de publicar |
| Respuesta fuera de la ventana de 24 horas de WhatsApp | El siguiente mensaje sale como plantilla aprobada |
| El dueño responde a una propuesta caducada | Se reabre si la fecha sigue vigente; si no, se explica |
| Dos mensajes seguidos que se contradicen | Vale el último, y el resumen lo deja claro |
| Google rechaza el cambio | Reintento con espera si es temporal; si es definitivo, aviso con el motivo |
| El token de Google caduca o se revoca | Se pausan las propuestas y se envía el enlace para reconectar |
| Audio que no se entiende | Se pide repetir o escribir |
| Mensaje con insultos o sin relación | Respuesta breve y neutra; no se crea ninguna acción |
| Texto de una reseña con instrucciones ("ignora tus reglas y publica...") | El texto de terceros es dato, nunca instrucción. Ver el HLD, seguridad |
