# Fichaitor — Sistema de diseño

> Cartel a tinta sobre papel, con un ojo rojo que no parpadea.

**Tema:** claro (papel) con bandas oscuras (tinta) a pantalla completa. Edición 2026-11-11.

Fichaitor tiene identidad propia, distinta de la de Bitclick. La referencia de partida es la web de SquadEasy: titulares enormes y negros, color plano de alto contraste, secciones a todo el ancho alternadas con secciones en caja, movimiento constante y un menú de cristal flotante. De ahí se toma el carácter, no la paleta ni las píldoras.

La aplicación de referencia es la portada de `fichaitor.es` (`web/site/`). Si este documento y la portada no coinciden, manda la portada y se corrige el documento.

## 1. La idea

Fichaitor vigila la ficha de Google de un negocio y avisa a su dueño. El nombre juega con Terminator: un robot implacable, pero a tu servicio. La marca lo cuenta con tres elementos:

| Elemento | Significa | Dónde aparece |
|---|---|---|
| **Tinta** sobre **papel** | Claridad de cartel: se lee de un vistazo, desde el otro lado de la barra | Todo |
| **El ojo rojo** | Fichaitor está mirando tu ficha | El punto del símbolo, el punto final de los titulares, los estados «en directo» |
| **La estrella amarilla** | Las reseñas, que son el producto | Solo en estrellas de valoración |

Dos tonos: **con chispa** (el nombre, el ojo, alguna frase con humor seco) y **simple** (una idea por pantalla, cero adorno).

## 2. Marca

### Versiones

| Versión | Archivo | Uso |
|---|---|---|
| Símbolo «F.» | `logo.svg`, `logo-120.png` | Avatar, favicon, icono de app, pantalla de consentimiento de Google |
| Palabra, tinta | `fichaitor-wordmark-tinta.svg` | Sobre papel y fondos claros |
| Palabra, papel | `fichaitor-wordmark-papel.svg` | Sobre tinta, rojo y fotografía |
| Símbolo + palabra, tinta | `fichaitor-lockup-tinta.svg` | Cabeceras y documentos sobre fondo claro |
| Símbolo + palabra, papel | `fichaitor-lockup-papel.svg` | Cabeceras sobre fondo oscuro |
| Iconos | `favicon.ico`, `favicon-32.png`, `apple-touch-icon.png`, `icon-192.png`, `icon-512.png` | Navegador y web instalable |

El símbolo es una F de bloque en papel sobre tinta, con un punto rojo: el punto final de «F.» y el ojo. La palabra es «Fichaitor» en Archivo Expanded Black, con interletraje de −0,035 em, convertida en curvas y cerrada con el mismo punto rojo.

Para regenerar la palabra y la fuente web:

```bash
uv run --no-project --with "fonttools[woff]" --with brotli python brand/marca.py
```

### Reglas

- **Área de respeto:** x = una cuarta parte del alto del símbolo, libre en los cuatro lados.
- **Tamaño mínimo:** símbolo, 16 px; palabra, 20 px de alto en pantalla y 6 mm impresa.
- **El punto siempre es rojo** `#ff3b30`. Es el único elemento de la marca que no cambia de color.
- En recortes circulares, el símbolo cabe entero dentro del círculo.

### Usos incorrectos

- No estires, gires ni inclines la marca.
- No cambies el color del punto ni lo quites.
- No pongas el símbolo sobre un fondo que no sea tinta: el cuadrado negro es parte del símbolo.
- No añadas sombras, degradados, brillos ni contornos.
- No escribas la palabra con una fuente: usa el archivo.
- No la combines con los colores ni con el logotipo de Google.

## 3. Color

| Nombre | HEX | Token | Papel |
|---|---|---|---|
| Tinta | `#0b0b0b` | `--tinta` | Texto, titulares, bandas oscuras, bordes, botón principal sobre papel |
| Tinta 2 | `#1a1917` | `--tinta-2` | Superficies elevadas sobre tinta |
| Papel | `#f4efe6` | `--papel` | Lienzo, texto sobre tinta. Nunca blanco puro |
| Papel 2 | `#e8e0d2` | `--papel-2` | Bandas alternas y superficies hundidas sobre papel |
| Blanco ficha | `#ffffff` | `--ficha` | Solo dentro de las maquetas de la ficha de Google y del chat |
| Rojo ojo | `#ff3b30` | `--rojo` | El punto, una palabra de énfasis por titular, estados en directo, una banda a pantalla completa por página |
| Rojo tinta | `#c4200f` | `--rojo-tinta` | El rojo cuando es texto pequeño sobre papel |
| Estrella | `#ffc21a` | `--estrella` | Solo estrellas de valoración |
| Gris tinta | `#55504a` | `--gris` | Texto secundario sobre papel |
| Gris papel | `#b8b0a3` | `--gris-claro` | Texto secundario sobre tinta |
| Hilo | `rgba(11,11,11,.16)` | `--hilo` | Separadores finos sobre papel |

### Contraste

Ratios calculados con la fórmula de luminancia relativa de WCAG 2.1.

| Texto / fondo | Ratio | Uso permitido |
|---|---|---|
| Tinta / papel | 17,19 | Todo |
| Gris tinta / papel | 6,96 | Todo |
| Gris papel / tinta | 9,16 | Todo |
| Rojo tinta / papel | 5,15 | Todo |
| Rojo ojo / tinta | 5,55 | Todo |
| Tinta / rojo ojo | 5,55 | Todo. Sobre rojo, el texto va en tinta |
| Rojo ojo / papel | 3,10 | Solo titulares de 24 px o más y elementos gráficos |
| Papel / rojo ojo | 3,10 | Solo titulares de 24 px o más |
| Estrella / tinta | 12,17 | Estrellas |
| Estrella / papel | 1,41 | ✕ Sobre papel, las estrellas llevan contorno o fondo de tinta, o van dentro de la ficha con tono `#e8a600` |

### Reglas

- Color plano. Ningún degradado, brillo ni desenfoque de color.
- Tres fondos de sección: papel, tinta y rojo. El rojo se usa en una sola sección por página.
- El amarillo no es color de marca: no se usa en botones, textos ni fondos.
- Nunca juntos azul, rojo, amarillo y verde: es la combinación de Google.

## 4. Tipografía

Una sola familia, **Archivo** (SIL Open Font License), variable en peso (100–900) y en ancho (62–125). Se sirve desde nuestro dominio (`web/site/fonts/Archivo.woff2`), nunca desde fonts.googleapis.com.

| Papel | Ajuste | Uso |
|---|---|---|
| **Cartel** | Peso 900, ancho 125, mayúsculas, interlineado 0,86–0,92, tracking −0,04 em | Titulares h1 y h2, cifras grandes |
| **Rótulo** | Peso 800, ancho 112, tracking −0,02 em | h3, botones, navegación, etiquetas |
| **Texto** | Peso 400–500, ancho 100, interlineado 1,45, tracking −0,011 em | Párrafos, formularios, tablas |
| **Nota** | Peso 600–700, ancho 100, 13–15 px | Metadatos y letra pequeña. Nunca como antetítulo |

### Escala

| Nivel | Tamaño |
|---|---|
| Cartel XL (héroe) | `clamp(2.5rem, 10.4vw, 9.9rem)` |
| Cartel L (sección) | `clamp(2.1rem, 7vw, 6.8rem)` |
| Cartel M | `clamp(1.9rem, 4.6vw, 4.25rem)` |
| Rótulo | `clamp(1.2rem, 1.7vw, 1.65rem)` |
| Lead | `clamp(1.1rem, 1.5vw, 1.4rem)` |
| Texto | `1.0625rem` |
| Nota | `0.8125rem` |

### Reglas

- Un titular, una idea. Termina en punto, y el punto es rojo.
- Como mucho una palabra en rojo por titular.
- Sin cursivas, sin tipografía manuscrita y sin monoespaciada.
- **Sin antetítulos.** Ninguna etiqueta en mayúsculas, con o sin punto, encima de un titular. El titular va solo.

## 5. Forma

- **Esquinas rectas.** El radio es 0 en botones, tarjetas, campos, menús y bandas. Es la forma de la F.
- **El único círculo es el punto.** Punto rojo, punto de estado, y nada más.
- **Sin píldoras, sin etiquetas flotantes, sin insignias.** Si algo necesita destacar, se hace más grande o cambia de fondo.
- **Borde de 2 px en tinta** para delimitar. Hilo de 1 px solo en tablas.
- **Sombra dura**, sin desenfoque, para lo que se pulsa: `6px 6px 0 var(--tinta)`. Ninguna sombra difusa.
- **Excepción:** las maquetas que imitan interfaces ajenas (la ficha de Google, el chat) conservan sus esquinas redondeadas de 8–16 px, porque representan otra cosa.

### Espaciado

Base de 4 px. Escala: 4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 160.

- Ancho máximo del contenido en caja: 1240 px, con 24 px de margen lateral (16 px en móvil).
- Las secciones a pantalla completa miden como mínimo `100svh`.

## 6. Componentes

### Menú de cristal

Barra flotante fija, separada 12 px del borde superior, con fondo translúcido y desenfoque del contenido de detrás (`backdrop-filter: blur(14px) saturate(1.4)`), borde de 2 px y esquinas rectas. A la izquierda, el símbolo y la palabra; en el centro, los enlaces en Rótulo; a la derecha, el botón principal. Invierte sus colores cuando la sección de debajo es oscura.

### Botón principal

Bloque rectangular, relleno de tinta y texto en papel (invertido sobre tinta; tinta sobre la banda roja), en Rótulo, con flecha `→`. Sombra dura. Al pasar el cursor, el bloque se desplaza hacia la sombra y la flecha avanza. Uno por pantalla.

### Botón secundario

El mismo bloque, sin relleno, con borde de 2 px. Siempre acompaña al principal.

### Titular de cartel

Alineado a la izquierda, a todo el ancho. Las líneas entran desde abajo con máscara, una tras otra.

### Cinta

Banda horizontal a todo el ancho que se desplaza sin parar, con texto en Cartel y estrellas. Separa secciones.

### Maqueta de la ficha

Tarjeta blanca que representa una ficha de negocio: nombre, valoración con estrellas, categoría, horario, botones de acción y reseñas. Es una representación genérica: no lleva el logotipo de Google ni sus colores.

### Maqueta del chat

Burbujas de conversación con botones de respuesta rápida, según los patrones de `docs/04-ux-conversacional.md`.

### Paso numerado

Número grande en Cartel (`01`), título en Rótulo y una frase. En rejilla con bordes de 2 px compartidos.

### Tabla de planes

Tres columnas con borde de 2 px. El precio en Cartel M. La columna destacada se invierte a tinta.

## 7. Movimiento

El movimiento es parte de la marca: firme y mecánico, como una máquina que hace bien su trabajo.

| Regla | Valor |
|---|---|
| Curva | `cubic-bezier(.16, 1, .3, 1)` para entradas; lineal para lo que va ligado al scroll |
| Duración | 0,6–0,9 s en entradas; 0,15–0,2 s en respuestas al cursor |
| Entrada de titulares | Por líneas, desde abajo con máscara, escalonadas 80 ms |
| Scroll | Suavizado con Lenis; las escenas clave se anclan y avanzan con el scroll |
| Entre secciones | La sección siguiente sube y tapa a la anterior, que se queda fija |
| El ojo | El punto rojo late despacio: es lo único que se mueve sin que nadie lo toque, junto con la cinta |

- Librerías: **Motion** (`motion.dev`) y **Lenis**, servidas desde nuestro dominio.
- Con `prefers-reduced-motion: reduce` se desactivan el suavizado, los anclajes y las entradas: el contenido se muestra completo y estático.
- Nada de rebotes, giros ni partículas.

## 8. Voz

La del asistente, descrita en `docs/04-ux-conversacional.md`: tutea, frases cortas, lenguaje del dueño.

- Habla de **tu ficha**, **tus reseñas**, **tu bar**. Nunca «perfil de empresa», «SEO local» ni «reputación online».
- La promesa que no se toca: **nunca cambia nada sin tu confirmación**.
- Sin promesas de posiciones ni de resultados. Sin «garantizado», «ilimitado» ni «el mejor».
- El humor está en el nombre y en alguna frase seca. Sin exclamaciones ni emojis.

| Así no | Así sí |
|---|---|
| «Optimiza tu presencia en Google con IA.» | «Tu ficha de Google, al día.» |
| «¡Dispara tus reseñas!» | «Te avisa de cada reseña y te propone la respuesta.» |
| «Gestión automática de tu reputación.» | «Tú confirmas. Fichaitor lo hace.» |

## 9. Lo que se hace y lo que no

### Sí

- Titulares enormes en Cartel, en mayúsculas, con el punto rojo.
- Alternar secciones a todo el ancho (tinta, rojo) con secciones en caja (papel).
- Color plano, bordes de 2 px y sombra dura.
- Enseñar el producto: la ficha, las estrellas, la conversación.

### No

- Píldoras, insignias, etiquetas flotantes, antetítulos en mayúsculas con punto, o esquinas redondeadas fuera de las maquetas.
- Degradados, brillos, vidrio esmerilado fuera del menú, o sombras difusas.
- Iconos decorativos en cada tarjeta ni emojis.
- Fondos blancos puros como lienzo.
- Verde como color de marca: es el de Bitclick.

## 10. Inicio rápido

```css
:root {
  --tinta: #0b0b0b;
  --tinta-2: #1a1917;
  --papel: #f4efe6;
  --papel-2: #e8e0d2;
  --ficha: #ffffff;
  --rojo: #ff3b30;
  --rojo-tinta: #c4200f;
  --estrella: #ffc21a;
  --gris: #55504a;
  --gris-claro: #b8b0a3;
  --hilo: rgba(11, 11, 11, 0.16);

  --fuente: "Archivo", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --cartel-xl: clamp(2.5rem, 10.4vw, 9.9rem);
  --cartel-l: clamp(2.1rem, 7vw, 6.8rem);
  --cartel-m: clamp(1.9rem, 4.6vw, 4.25rem);
  --rotulo: clamp(1.2rem, 1.7vw, 1.65rem);
  --lead: clamp(1.1rem, 1.5vw, 1.4rem);

  --borde: 2px solid var(--tinta);
  --sombra: 6px 6px 0 var(--tinta);
  --curva: cubic-bezier(0.16, 1, 0.3, 1);
  --ancho: 1240px;
}

@font-face {
  font-family: "Archivo";
  src: url("/fonts/Archivo.woff2") format("woff2");
  font-weight: 100 900;
  font-stretch: 62% 125%;
  font-display: swap;
}

.cartel {
  font-weight: 900;
  font-stretch: 125%;
  text-transform: uppercase;
  line-height: 0.88;
  letter-spacing: -0.04em;
}
```

## 11. Reglas de interfaz: la portada

La portada (`fichaitor.es`) es un cartel que se pasa hoja a hoja. Reglas fijadas por el propietario el 2026-11-12.

| Regla | Qué significa |
|---|---|
| **Seccional** | Cada idea ocupa una sección. Una sección, un titular, una acción como mucho |
| **A todo el ancho y a todo el alto** | Cada sección mide como mínimo `100svh` y su fondo llega de borde a borde. El contenido va en caja de 1240 px |
| **El scroll salta de sección** | Al soltar el scroll, la página se asienta en la sección más cercana. Nunca se queda a medias entre dos |
| **Suave** | El desplazamiento usa Lenis. El salto dura menos de un segundo y no rebota |
| **Con animación** | Los titulares entran por líneas; la sección siguiente sube y tapa a la anterior, que se encoge y se apaga |

### Cómo se aplica el salto

- En una sección normal, el punto de asiento es su inicio.
- En las escenas ancladas (la ficha paso a paso, la promesa), cada paso es un punto de asiento: el scroll avanza de paso en paso.
- Si una sección no cabe en la pantalla (móviles bajos), dentro de ella el scroll es libre; solo se asienta al acercarse a su inicio o al de la siguiente.
- El salto nunca lucha con quien está haciendo scroll: espera a que se detenga.
- Con `prefers-reduced-motion: reduce` no hay salto, ni suavizado, ni entradas.
- Mientras hay un panel abierto a pantalla completa (resultado de la prueba con captura), el salto se desactiva.

### Lo que no se hace en la portada

- Secciones a media altura ni dos ideas en la misma pantalla.
- Carruseles, pestañas o contenido escondido tras un clic, salvo el resultado de la prueba.
- Antetítulos, píldoras o iconos decorativos (apartados 4 y 5).

## 12. Reglas de interfaz: la aplicación del cliente

La aplicación (`/app`) es una herramienta de trabajo para alguien con prisa, de pie y con el móvil. Comparte marca con la portada, pero no su teatralidad. Reglas fijadas por el propietario el 2026-11-12.

| Regla | Qué significa |
|---|---|
| **Por secciones** | El panel se divide en secciones con nombre: resumen, opinión, reseñas, mejoras. Cada una responde a una pregunta |
| **Scroll suave** | Lenis, como en la portada, pero sin salto entre secciones: aquí se lee y se trabaja, y el contenido tiene alturas distintas |
| **Números que cuentan** | Toda cifra principal parte de cero y cuenta hasta su valor la primera vez que entra en pantalla, en torno a un segundo |
| **Estrellas** | La valoración real de la ficha se muestra sobre 5, con las estrellas saltando una a una y rellenándose hasta la media. Las estrellas de cada reseña son estáticas |
| **Menos ruido** | En pantalla solo está lo que ayuda a decidir o a actuar. Ver «Ruido» más abajo |

### Principios de experiencia de uso

1. **Primero, lo que pide una decisión.** El orden del panel es el de la urgencia: la nota y el estado de la ficha, lo pendiente (reseñas sin responder, festivos, cambios de Google), lo que opina la gente, y al final las mejoras.
2. **Una acción principal por bloque.** Un solo botón relleno; lo demás, secundario o enlace. Si dos acciones compiten, sobra una.
3. **Nada cambia sin confirmación.** Antes de publicar algo en Google, el dueño ve el texto exacto y pulsa. La aplicación no publica por su cuenta ni reintenta sola.
4. **Decir la verdad sobre el estado.** «Enviada a Google» no es «publicada». Lo que está calculándose se dice («Leyendo tus reseñas…»); lo que es provisional, también. Nunca un hueco en blanco ni un dato inventado.
5. **Cada error dice qué pasó y qué hacer.** Una frase, sin jerga, con la salida al lado (reintentar, volver a conectar, escribirlo a mano).
6. **El lenguaje del dueño.** «Tu ficha», «tus reseñas», «festivos». Nunca «perfil de empresa», «sincronización» ni «sentimiento» en pantalla: ese bloque se llama «Qué dicen de tu negocio».
7. **El móvil manda.** Todo se diseña primero a 375 px. Objetivos táctiles de 44 px como mínimo, contraste AA, foco visible y todo manejable con teclado.
8. **Los datos de otras personas, los mínimos.** Las reseñas se muestran sin nombre ni foto de quien las escribió.
9. **El movimiento informa, no decora.** Un número que cuenta dice «esto es un dato vivo»; una sección que entra dice «aquí empieza otra cosa». Si una animación no dice nada, se quita. Todo se desactiva con «movimiento reducido».
10. **Coherencia antes que novedad.** Los mismos estados se ven igual en todas las secciones: cargando, sin datos, error, acceso caducado.

### Ruido: qué se quita

- Texto que repite lo que ya dice un número o un titular.
- Datos que el dueño ya conoce (su dirección) cuando no ayudan a decidir.
- Más de un nivel de borde dentro de un bloque. El borde de 2 px separa secciones; dentro, espacio.
- Barras, etiquetas o menús que compiten con el contenido. La navegación por secciones va al margen, como el índice de un documento, no encima del contenido.
- Avisos permanentes. Un aviso aparece cuando hay algo que avisar y se va cuando deja de haberlo.
- Dos cifras del mismo tipo sin jerarquía. Si hay dos notas sobre 100, una manda (la de la ficha, en la cabecera) y la otra vive dentro de su bloque.

### Estados que toda sección define

Cargando, vacío, con datos, provisional (pocos datos), error y acceso a Google caducado. Ninguna sección se entrega sin los seis.

## Archivos

| Pieza | Ruta |
|---|---|
| Marca e iconos | `brand/` |
| Generador de la palabra y de la fuente web | `brand/marca.py` |
| Fuente original y licencia | `brand/fuentes/` |
| Manual de marca maquetado | `brand/manual-marca.html` |
| Portada de referencia | `web/site/` |
