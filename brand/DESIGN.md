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
| **Nota** | Peso 600, ancho 100, 13–14 px, mayúsculas, tracking 0,06 em | Numeración de pasos, metadatos |

### Escala

| Nivel | Tamaño |
|---|---|
| Cartel XL (héroe) | `clamp(3.25rem, 13.5vw, 15rem)` |
| Cartel L (sección) | `clamp(2.5rem, 8.5vw, 9rem)` |
| Cartel M | `clamp(2rem, 5vw, 4.5rem)` |
| Rótulo | `clamp(1.25rem, 1.8vw, 1.75rem)` |
| Lead | `clamp(1.125rem, 1.6vw, 1.5rem)` |
| Texto | `1.0625rem` |
| Nota | `0.8125rem` |

### Reglas

- Un titular, una idea. Termina en punto, y el punto es rojo.
- Como mucho una palabra en rojo por titular.
- Sin cursivas, sin tipografía manuscrita y sin monoespaciada.

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

Número en Nota (`01`), título en Rótulo y una frase. En rejilla con bordes de 2 px compartidos.

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

- Píldoras, insignias, etiquetas flotantes o esquinas redondeadas fuera de las maquetas.
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
  --cartel-xl: clamp(3.25rem, 13.5vw, 15rem);
  --cartel-l: clamp(2.5rem, 8.5vw, 9rem);
  --cartel-m: clamp(2rem, 5vw, 4.5rem);
  --rotulo: clamp(1.25rem, 1.8vw, 1.75rem);
  --lead: clamp(1.125rem, 1.6vw, 1.5rem);

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

## Archivos

| Pieza | Ruta |
|---|---|
| Marca e iconos | `brand/` |
| Generador de la palabra y de la fuente web | `brand/marca.py` |
| Fuente original y licencia | `brand/fuentes/` |
| Manual de marca maquetado | `brand/manual-marca.html` |
| Portada de referencia | `web/site/` |
