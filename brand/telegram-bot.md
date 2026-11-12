# Bot de Telegram de Fichaitor: ficha de marca

Valores para rellenar el bot en [@BotFather](https://t.me/BotFather). El canal de Telegram aún no está construido en el backend: esto deja el bot con su identidad, no lo hace funcionar.

**Importante:** el token que entrega BotFather es un secreto. Va en el archivo de entorno del servidor, nunca en el repositorio ni en un chat.

## Pasos en BotFather

1. `/newbot` → escribe el nombre y luego el nombre de usuario.
2. `/setuserpic` → sube el avatar.
3. `/setabouttext` → pega el texto de «Información».
4. `/setdescription` → pega la descripción.
5. `/setcommands` → pega la lista de comandos, tal cual.
6. `/setjoingroups` → **Disable**. El bot habla con un dueño, no en grupos.
7. `/setprivacy` → **Enable** (valor por defecto).

## Valores

| Campo | Valor | Límite de Telegram |
|---|---|---|
| Nombre | `Fichaitor` | 64 caracteres |
| Nombre de usuario | `FichaitorBot`. Si está ocupado y eliges otro (`Fichaitor_bot`, `FichaitorEsBot`), avisa a backend: el enlace de vinculación del panel lleva el nombre de usuario (`https://t.me/FichaitorBot?start=…`) | 5–32, acaba en `bot` |
| Avatar | `brand/icon-512.png` | Cuadrado; Telegram lo recorta en círculo |

El avatar es el símbolo «F.» sobre negro. Cabe entero dentro del recorte circular.

### Información (`/setabouttext`)

Se ve en el perfil del bot.

```text
Tu ficha de Google, al día. Reseñas, festivos y novedades. Nunca cambia nada sin tu confirmación. fichaitor.es
```

### Descripción (`/setdescription`)

Se ve en la pantalla vacía, antes de pulsar «Iniciar».

```text
Soy Fichaitor, un asistente de inteligencia artificial que te ayuda a llevar la ficha de Google de tu negocio.

Te aviso de cada reseña nueva con una respuesta ya redactada, te pregunto por tus horarios antes de cada festivo y te ayudo a publicar novedades y fotos.

Nunca cambio nada en tu ficha sin tu confirmación.

Para empezar, conecta tu ficha en fichaitor.es y pulsa Iniciar.
```

### Comandos (`/setcommands`)

Sin tildes ni eñes en el nombre del comando: Telegram solo admite letras minúsculas, números y guion bajo.

**Ahora.** La primera entrega del canal solo atiende estos tres, así que el menú del bot no ofrece nada más:

```text
start - Vincular tu ficha y empezar
ayuda - Qué puedo hacer por ti
privacidad - Qué datos uso y cómo desconectar
```

El resto de la conversación va con botones en cada propuesta: ante una reseña nueva, «Publicar», «Editar» y «No responder»; ante un festivo, «Abro normal», «Cierro» y «Otro horario».

**Más adelante.** Se añaden a la lista cuando el backend los implemente, no antes:

```text
resenas - Ver tus reseñas sin responder
festivos - Revisar los horarios de los próximos festivos
ficha - Ver el estado de tu ficha
cerrar - Marcar hoy como cerrado
```

## Reglas de voz en el bot

Las de `docs/04-ux-conversacional.md` y `brand/DESIGN.md`, apartado 8:

- El bot se presenta como inteligencia artificial.
- Tutea, frases cortas, una idea por mensaje.
- Sin emojis en los textos fijos ni exclamaciones.
- Cada propuesta lleva botones y una salida fácil.

## Pendiente antes de anunciarlo

- Que el backend implemente el canal y estos comandos. Hasta entonces, el bot no responde.
- Añadir Telegram como canal activo en `docs/legal/privacidad.md` cuando funcione. Hoy figura como función prevista.
