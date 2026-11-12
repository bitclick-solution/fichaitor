"""Genera el icono adaptable (maskable) de la web instalable: brand/icon-maskable-512.png (512 × 512).

Uso, desde la raíz del repositorio:

    uv run --no-project --with pillow python brand/iconos.py

Android recorta los iconos adaptables con la máscara del fabricante (círculo, gota, cuadrado redondeado…).
Solo se garantiza visible el círculo central de radio 0,4 × lado (la «zona segura» del 80 %). Por eso el
fondo tinta va a sangre, sin margen, y el símbolo «F.» se reduce hasta caber entero dentro de ese círculo.
Las proporciones y la posición del símbolo son las de brand/logo.svg (lienzo de 120).
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SALIDA = ROOT / "brand" / "icon-maskable-512.png"

TINTA, PAPEL, ROJO = "#0b0b0b", "#f4efe6", "#ff3b30"
LADO = 512
ESCALA = 4  # se dibuja al cuádruple y se reduce, para bordes limpios

# Geometría de brand/logo.svg, en su lienzo de 120 × 120
LIENZO = 120
F_PUNTOS = [(34, 20), (90, 20), (90, 43), (58, 43), (58, 53), (82, 53), (82, 73), (58, 73), (58, 100), (34, 100)]
PUNTO, RADIO_PUNTO = (76, 90), 9

ZONA_SEGURA = 0.4  # radio del círculo seguro, en fracción del lado
HOLGURA = 0.92  # el símbolo ocupa el 92 % de ese radio: queda aire hasta el borde de la máscara


def distancia_maxima() -> float:
    """Distancia, en unidades del lienzo de 120, del centro al punto más alejado del símbolo."""
    c = LIENZO / 2
    vertices = max(math.hypot(x - c, y - c) for x, y in F_PUNTOS)
    punto = math.hypot(PUNTO[0] - c, PUNTO[1] - c) + RADIO_PUNTO
    return max(vertices, punto)


def main() -> None:
    lado = LADO * ESCALA
    centro = lado / 2
    radio_seguro = ZONA_SEGURA * lado
    k = radio_seguro * HOLGURA / distancia_maxima()  # píxeles por unidad del lienzo de 120

    def a_px(x: float, y: float) -> tuple[float, float]:
        # Mismo centro que el logo: el símbolo conserva su posición respecto al cuadrado
        return centro + (x - LIENZO / 2) * k, centro + (y - LIENZO / 2) * k

    im = Image.new("RGB", (lado, lado), TINTA)  # fondo tinta a sangre
    d = ImageDraw.Draw(im)
    d.polygon([a_px(x, y) for x, y in F_PUNTOS], fill=PAPEL)
    px, py = a_px(*PUNTO)
    r = RADIO_PUNTO * k
    d.ellipse([px - r, py - r, px + r, py + r], fill=ROJO)

    # Comprobación: todo el símbolo queda dentro del círculo seguro
    assert distancia_maxima() * k <= radio_seguro, "el símbolo se sale de la zona segura"

    im = im.resize((LADO, LADO), Image.LANCZOS)
    im.save(SALIDA, optimize=True)
    ocupa = distancia_maxima() * k / (lado / 2)
    print(f"ok  {SALIDA.relative_to(ROOT)}  {LADO}x{LADO}  símbolo hasta el {ocupa:.0%} del radio (zona segura: {ZONA_SEGURA * 2:.0%})")


if __name__ == "__main__":
    main()
