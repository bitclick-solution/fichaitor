# Auditoría en una página

La hoja A4 que se deja impresa en cada bar: la nota de su ficha, tres cosas que se ven desde fuera y la oferta de la beta. Ejemplo: `salida/bar-ejemplo.pdf` (se genera con `bares.ejemplo.json`).

## Por qué se rellena a mano

El diagnóstico público de `fichaitor.es` está cerrado en producción («Fichaitor aún no está abierto»). Mientras tanto, el PM mira cada ficha en Google Maps y apunta lo que ve. Con eso, `generar.py` calcula la nota con las mismas reglas del producto:

- las reglas del diagnóstico público (`app/domain/diagnosis.py`);
- el festivo sin horario y las reseñas sin respuesta, con los pesos del diagnóstico de la ficha conectada (`app/domain/profile_diagnosis.py`).

Por eso la hoja dice «nota orientativa» y «revisado a mano en Google Maps».

## Lo que apunta el PM de cada bar

Un minuto por bar, con la ficha abierta en Google Maps en el móvil. Mándamelo así, uno por línea:

```text
Bar La Esquina · Cañadío · festivo: no · fotos: 8 · nota: 4,2 (214) · sin responder: 7 de 10, 2 malas · web: no · teléfono: sí · servicios: sí
```

| Dato | Dónde se mira | Qué apuntar |
|---|---|---|
| Nombre y zona | Arriba de la ficha | Tal cual sale |
| Festivo | Horario: despliégalo y busca el martes 8 de diciembre | `sí` si tiene un horario especial para ese día; `no` si sale el de un martes normal o un aviso de «el horario podría ser diferente» |
| Fotos | Pestaña de fotos | Cuántas hay, o `10+` |
| Nota y reseñas | Bajo el nombre | La media y el número entre paréntesis |
| Sin responder | Reseñas → ordenar por **Más recientes** → las 10 primeras | Cuántas no tienen «Respuesta del propietario» y cuántas de esas son de 1 o 2 estrellas |
| Web y teléfono | Botones de la ficha | `sí` o `no` |
| Servicios | Pestaña «Información»: terraza, para llevar, reservas… | `sí` si hay alguno |
| Abierto y horario | Bajo el nombre | Solo si sale «Cerrado permanentemente» o no tiene horario |

> **Importante:** No apuntes nombres de quienes escribieron las reseñas. La hoja no los lleva nunca.

## Cómo se genera

Lo hace el agente de marketing. Desde la raíz del repositorio:

```bash
uv run --no-project --with qrcode python docs/marketing/auditoria/generar.py docs/marketing/auditoria/bares.json
```

- Los datos de los bares van en `bares.json` y las hojas en `salida/`. Ninguno de los dos entra en git (`.gitignore`): son datos de negocios reales sacados de Google Maps.
- Se borran después de la visita.
- Necesita Chrome en `C:\Program Files\Google\Chrome\Application\chrome.exe` para crear el PDF.

## Lo que lleva la hoja

| Bloque | Contenido |
|---|---|
| Cabecera | Logotipo y «Revisado a mano en Google Maps el [fecha]» |
| Titular | «La ficha de Google de [bar].» |
| Nota | De 0 a 100, con su etiqueta: Bien, Mejorable o Necesita atención |
| Tres cosas | Las tres más urgentes, con qué pasa y cómo se arregla: «Fichaitor te avisa…» cuando lo resuelve el producto, o «Lo arreglas tú en 5 minutos» cuando no |
| Tu calle (opcional) | Una frase anónima, por ejemplo «De los 5 locales de tu calle, 3 tienen puesto el festivo.» |
| Pie | La promesa, la oferta de la beta y un QR a `fichaitor.es/?ref=visita&s=visita` |

Reglas: sin nombres de autores de reseñas, sin promesas de posición, sin logotipo ni colores de Google, y ninguna cifra que no hayamos visto en la ficha.
