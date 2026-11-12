"""Genera web/site: la portada (web/src/index.html), las páginas legales (docs/legal) y los iconos (brand).

Uso, desde la raíz del repositorio:

    uv run --no-project --with markdown python web/build.py

El aviso de borrador (la cita inicial que empieza por «BORRADOR») no se publica.

Con `--estricto`, termina con error si los textos legales conservan marcas [PENDIENTE] o
[REVISAR]. Es el modo pensado para la compilación de producción.
"""

import re
import shutil
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "web" / "site"

PAGES = {
    "privacidad": ("privacidad.md", "Política de Privacidad"),
    "condiciones-de-uso": ("condiciones-de-uso.md", "Condiciones de uso"),
}
ICONS = [
    "logo.svg",
    "favicon.ico",
    "favicon-32.png",
    "apple-touch-icon.png",
    "icon-192.png",
    "icon-512.png",
]

TEMPLATE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Fichaitor</title>
<link rel="canonical" href="https://fichaitor.es/{slug}/">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="preload" href="/fonts/Archivo.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="/styles.css">
</head>
<body class="legal">
<header class="menu" data-tema="claro">
  <a class="marca" href="/" aria-label="Fichaitor, inicio"><img src="/logo.svg" alt="" width="36" height="36">{palabra}</a>
  <nav aria-label="Principal">
    <a href="/privacidad/">Privacidad</a>
    <a href="/condiciones-de-uso/">Condiciones de uso</a>
  </nav>
  <a class="btn" href="/">Inicio</a>
</header>
<main>
<div class="caja">
<article class="legal">
{body}
</article>
</div>
</main>
<footer class="pie">
  <div class="caja">
    <div class="fila-pie">
      <div>
        <p>Fichaitor es un producto de BITCLICK SOLUTIONS S.L.U. · C.I.F. B88994777 · C/ Cuenca nº 17, 18230 Atarfe (Granada), España.</p>
        <p class="mini">Fichaitor no está afiliado a Google ni patrocinado por Google. Google y Google Business Profile son marcas de Google LLC.</p>
      </div>
      <nav aria-label="Legal">
        <a href="/">Inicio</a>
        <a href="/privacidad/">Política de Privacidad</a>
        <a href="/condiciones-de-uso/">Condiciones de uso</a>
        <a href="mailto:soporte@fichaitor.es">soporte@fichaitor.es</a>
      </nav>
    </div>
  </div>
</footer>
</body>
</html>
"""


def palabra() -> str:
    """La palabra en curvas, sin la declaración xmlns duplicada, para incrustarla en el HTML."""
    svg = (ROOT / "brand" / "fichaitor-wordmark-tinta.svg").read_text(encoding="utf-8").strip()
    return svg.replace('role="img" aria-label="Fichaitor"', 'aria-hidden="true"')


def main() -> None:
    marca = palabra()
    portada = (ROOT / "web" / "src" / "index.html").read_text(encoding="utf-8")
    portada = portada.replace("{{palabra}}", marca).replace(
        "{{palabra_grande}}", marca.replace("<svg ", '<svg width="100%" ')
    )
    (SITE / "index.html").write_text(portada, encoding="utf-8", newline="\n")
    print("ok  web/site/index.html")
    pending = []
    for slug, (source, title) in PAGES.items():
        text = (ROOT / "docs" / "legal" / source).read_text(encoding="utf-8")
        text = re.sub(r"^> \*\*BORRADOR.*?\n\n", "", text, count=1, flags=re.M | re.S)
        if "[PENDIENTE" in text or "[REVISAR" in text:
            pending.append(source)
        body = markdown.markdown(text, extensions=["tables"])
        out = SITE / slug / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            TEMPLATE.format(title=title, slug=slug, body=body, palabra=marca),
            encoding="utf-8",
            newline="\n",
        )
        print(f"ok  {out.relative_to(ROOT)}")
    for name in ICONS:
        shutil.copyfile(ROOT / "brand" / name, SITE / name)
    if pending:
        print(
            "AVISO: quedan datos [PENDIENTE] o [REVISAR] en: "
            + ", ".join(pending)
            + ". No publiques hasta resolverlos."
        )
        if "--estricto" in sys.argv[1:]:
            sys.exit("ERROR: compilación estricta y los textos legales tienen marcas abiertas.")


if __name__ == "__main__":
    main()
