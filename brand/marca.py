"""Genera la palabra «Fichaitor.» en curvas y las fuentes web a partir de Archivo.

Uso, desde la raíz del repositorio:

    uv run --no-project --with "fonttools[woff]" --with brotli python brand/marca.py

Salida:
    brand/fichaitor-wordmark-{tinta,papel}.svg   palabra en curvas, sin depender de fuentes
    brand/fichaitor-lockup-{tinta,papel}.svg     símbolo + palabra
    web/site/fonts/Archivo.woff2                 variable (peso 100–900, ancho 62–125), latín
"""

from pathlib import Path

from fontTools import subset
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "brand" / "fuentes" / "Archivo-VF.ttf"

TINTA, PAPEL, ROJO = "#0b0b0b", "#f4efe6", "#ff3b30"
TEXTO = "Fichaitor"
TRACKING = -0.035  # em


def palabra() -> tuple[str, float, float, float]:
    """Devuelve (path, ancho, alto de mayúscula, unidades por em) de la palabra en Archivo Expanded Black."""
    font = instantiateVariableFont(TTFont(SRC), {"wght": 900, "wdth": 125})
    upm = font["head"].unitsPerEm
    cap = font["OS/2"].sCapHeight
    glyphs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
    pen = SVGPathPen(glyphs)
    x = 0.0
    for ch in TEXTO:
        name = cmap[ord(ch)]
        glyphs[name].draw(TransformPen(pen, (1, 0, 0, -1, x, cap)))
        x += hmtx[name][0] + TRACKING * upm
    return pen.getCommands(), x - TRACKING * upm, cap, upm


def svg_wordmark(color: str) -> str:
    d, w, cap, upm = palabra()
    r = cap * 0.115  # el punto: mismo ojo rojo que el símbolo
    gap = upm * 0.03
    total = w + gap + 2 * r
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total:.0f} {cap:.0f}" role="img" aria-label="Fichaitor">'
        f'<path d="{d}" fill="{color}"/>'
        f'<circle cx="{w + gap + r:.1f}" cy="{cap - r:.1f}" r="{r:.1f}" fill="{ROJO}"/></svg>\n'
    )


def svg_lockup(fondo_simbolo: str, letra_simbolo: str, color: str) -> str:
    d, w, cap, upm = palabra()
    alto = 120.0
    k = (alto * 0.46) / cap  # la palabra mide el 46 % del alto del símbolo
    r = cap * 0.115
    gap = upm * 0.03
    ancho_palabra = (w + gap + 2 * r) * k
    x0 = alto + alto * 0.25  # separación = x (un cuarto del símbolo)
    y0 = (alto - cap * k) / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x0 + ancho_palabra:.1f} {alto:.0f}" role="img" aria-label="Fichaitor">'
        f'<rect width="120" height="120" fill="{fondo_simbolo}"/>'
        f'<path d="M34 20H90V43H58V53H82V73H58V100H34Z" fill="{letra_simbolo}"/>'
        f'<circle cx="76" cy="90" r="9" fill="{ROJO}"/>'
        f'<g transform="translate({x0:.1f} {y0:.1f}) scale({k:.5f})">'
        f'<path d="{d}" fill="{color}"/>'
        f'<circle cx="{w + gap + r:.1f}" cy="{cap - r:.1f}" r="{r:.1f}" fill="{ROJO}"/></g></svg>\n'
    )


def fuente_web() -> None:
    out = ROOT / "web" / "site" / "fonts" / "Archivo.woff2"
    out.parent.mkdir(parents=True, exist_ok=True)
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern", "liga", "locl", "tnum", "case"]
    font = subset.load_font(str(SRC), opts)
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=list(range(0x20, 0x17F)) + [0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026, 0x20AC, 0x2192, 0x2605, 0x00AB, 0x00BB])
    sub.subset(font)
    subset.save_font(font, str(out), opts)
    print(f"ok  {out.relative_to(ROOT)}  {out.stat().st_size // 1024} KB")


def main() -> None:
    b = ROOT / "brand"
    (b / "fichaitor-wordmark-tinta.svg").write_text(svg_wordmark(TINTA), encoding="utf-8", newline="\n")
    (b / "fichaitor-wordmark-papel.svg").write_text(svg_wordmark(PAPEL), encoding="utf-8", newline="\n")
    (b / "fichaitor-lockup-tinta.svg").write_text(svg_lockup(TINTA, PAPEL, TINTA), encoding="utf-8", newline="\n")
    (b / "fichaitor-lockup-papel.svg").write_text(svg_lockup(PAPEL, TINTA, PAPEL), encoding="utf-8", newline="\n")
    print("ok  brand/fichaitor-wordmark-*.svg, brand/fichaitor-lockup-*.svg")
    fuente_web()


if __name__ == "__main__":
    main()
