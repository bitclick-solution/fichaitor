"""Genera la imagen para compartir de la portada: brand/og-fichaitor.png (1200 × 630).

Uso, desde la raíz del repositorio:

    uv run --no-project --with pillow python brand/og.py

Tinta sobre papel, el titular de la portada con su punto rojo, el símbolo y el sello de estrellas.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FUENTE = ROOT / "brand" / "fuentes" / "Archivo-VF.ttf"
SALIDA = ROOT / "brand" / "og-fichaitor.png"

TINTA, PAPEL, ROJO, ESTRELLA = "#0b0b0b", "#f4efe6", "#ff3b30", "#ffc21a"
ANCHO, ALTO = 1200, 630
ESCALA = 2  # se dibuja al doble y se reduce, para bordes limpios


def fuente(tam: int, peso: int, ancho: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FUENTE), tam * ESCALA)
    ejes = {a["name"].lower() if isinstance(a["name"], str) else a["name"].decode().lower(): a for a in f.get_variation_axes()}
    valores = []
    for a in f.get_variation_axes():
        nombre = a["name"] if isinstance(a["name"], str) else a["name"].decode()
        valores.append(peso if nombre.lower().startswith("weight") else ancho)
    assert ejes, "la fuente no es variable"
    f.set_variation_by_axes(valores)
    return f


def estrella(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, color: str) -> None:
    import math

    puntos = []
    for i in range(10):
        radio = r if i % 2 == 0 else r * 0.45
        ang = -math.pi / 2 + i * math.pi / 5
        puntos.append((cx + radio * math.cos(ang), cy + radio * math.sin(ang)))
    d.polygon(puntos, fill=color)


def main() -> None:
    s = ESCALA
    im = Image.new("RGB", (ANCHO * s, ALTO * s), PAPEL)
    d = ImageDraw.Draw(im)
    margen = 72 * s

    # Símbolo «F.» arriba a la izquierda
    lado = 88 * s
    x0, y0 = margen, 64 * s
    d.rectangle([x0, y0, x0 + lado, y0 + lado], fill=TINTA)
    k = lado / 120
    f_puntos = [(34, 20), (90, 20), (90, 43), (58, 43), (58, 53), (82, 53), (82, 73), (58, 73), (58, 100), (34, 100)]
    d.polygon([(x0 + x * k, y0 + y * k) for x, y in f_puntos], fill=PAPEL)
    d.ellipse([x0 + (76 - 9) * k, y0 + (90 - 9) * k, x0 + (76 + 9) * k, y0 + (90 + 9) * k], fill=ROJO)
    marca = fuente(40, 900, 125)
    d.text((x0 + lado + 22 * s, y0 + lado / 2), "Fichaitor", font=marca, fill=TINTA, anchor="lm")
    ancho_marca = d.textlength("Fichaitor", font=marca)
    d.ellipse(
        [x0 + lado + 22 * s + ancho_marca + 4 * s, y0 + lado / 2 + 6 * s, x0 + lado + 22 * s + ancho_marca + 16 * s, y0 + lado / 2 + 18 * s],
        fill=ROJO,
    )

    # Titular de la portada, en mayúsculas, con el punto final en rojo
    titular = fuente(104, 900, 125)
    lineas = ["TU FICHA", "DE GOOGLE,", "AL DÍA"]
    y = 262 * s
    salto = 96 * s
    for i, linea in enumerate(lineas):
        d.text((margen, y + i * salto), linea, font=titular, fill=TINTA, anchor="ls")
    ultima = d.textlength(lineas[-1], font=titular)
    lado_punto = 22 * s
    yb = y + 2 * salto
    d.rectangle([margen + ultima + 8 * s, yb - lado_punto, margen + ultima + 8 * s + lado_punto, yb], fill=ROJO)

    # Pie: lo que hace, y el dominio
    texto = fuente(30, 600, 100)
    d.text((margen, 548 * s), "Reseñas, festivos y novedades. Nunca cambia nada sin tu confirmación.", font=texto, fill=TINTA, anchor="ls")
    d.text((margen, 590 * s), "fichaitor.es", font=fuente(30, 800, 112), fill=TINTA, anchor="ls")

    # Sello de estrellas, como en la portada: bloque negro inclinado con sombra roja
    sello = Image.new("RGBA", (360 * s, 120 * s), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sello)
    ds.rectangle([14 * s, 14 * s, 346 * s, 106 * s], fill=ROJO)
    ds.rectangle([0, 0, 332 * s, 92 * s], fill=TINTA)
    for i in range(5):
        estrella(ds, 50 * s + i * 58 * s, 46 * s, 24 * s, ESTRELLA)
    sello = sello.rotate(5, resample=Image.BICUBIC, expand=True)
    im.paste(sello, (ANCHO * s - sello.width - 56 * s, 70 * s), sello)

    im = im.resize((ANCHO, ALTO), Image.LANCZOS)
    im.save(SALIDA, optimize=True)
    print(f"ok  {SALIDA.relative_to(ROOT)}  {SALIDA.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
