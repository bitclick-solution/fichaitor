"""Genera las páginas legales de web/site a partir de docs/legal y copia los iconos.

Uso, desde la raíz del repositorio:

    uv run --no-project --with markdown python web/build.py

El aviso de borrador (la cita inicial que empieza por «BORRADOR») no se publica.
"""

import re
import shutil
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "web" / "site"

PAGES = {
    "privacidad": ("privacidad.md", "Política de Privacidad"),
    "condiciones-de-uso": ("condiciones-de-uso.md", "Condiciones de uso"),
}
ICONS = ["logo.svg", "favicon.ico", "favicon-32.png", "apple-touch-icon.png", "icon-192.png", "icon-512.png"]

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
<link rel="stylesheet" href="/styles.css">
</head>
<body>
<header class="top">
  <div class="wrap">
    <a class="brand" href="/"><img src="/logo.svg" alt="" width="40" height="40">Fichaitor</a>
    <nav class="links" aria-label="Principal">
      <a href="/privacidad/">Privacidad</a>
      <a href="/condiciones-de-uso/">Condiciones de uso</a>
    </nav>
  </div>
</header>
<main>
<div class="wrap">
<article class="legal">
{body}
</article>
</div>
</main>
<footer>
  <div class="wrap">
    <nav aria-label="Legal">
      <a href="/">Inicio</a>
      <a href="/privacidad/">Política de Privacidad</a>
      <a href="/condiciones-de-uso/">Condiciones de uso</a>
      <a href="mailto:soporte@fichaitor.es">soporte@fichaitor.es</a>
    </nav>
    <p>Fichaitor es un producto de BITCLICK SOLUTIONS S.L.U. · C.I.F. B88994777 · C/ Cuenca nº 17, 18230 Atarfe (Granada), España.</p>
    <p class="small">Fichaitor no está afiliado a Google ni patrocinado por Google. Google y Google Business Profile son marcas de Google LLC.</p>
  </div>
</footer>
</body>
</html>
"""


def main() -> None:
    pending = []
    for slug, (source, title) in PAGES.items():
        text = (ROOT / "docs" / "legal" / source).read_text(encoding="utf-8")
        text = re.sub(r"^> \*\*BORRADOR.*?\n\n", "", text, count=1, flags=re.M | re.S)
        if "[PENDIENTE" in text:
            pending.append(source)
        body = markdown.markdown(text, extensions=["tables"])
        out = SITE / slug / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(TEMPLATE.format(title=title, slug=slug, body=body), encoding="utf-8", newline="\n")
        print(f"ok  {out.relative_to(ROOT)}")
    for name in ICONS:
        shutil.copyfile(ROOT / "brand" / name, SITE / name)
    if pending:
        print("AVISO: quedan datos [PENDIENTE] en: " + ", ".join(pending) + ". No publiques hasta resolverlos.")


if __name__ == "__main__":
    main()
