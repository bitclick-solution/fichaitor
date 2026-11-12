"""Genera web/site: la portada (web/src/index.html), las páginas legales (docs/legal) y los iconos (brand).

Uso, desde la raíz del repositorio:

    uv run --no-project --with markdown python web/build.py

El aviso de borrador (la cita inicial que empieza por «BORRADOR») no se publica.

Con `--estricto`, termina con error si los textos legales conservan marcas [PENDIENTE] o
[REVISAR]. Es el modo pensado para la compilación de producción.

Modo «solo web» (FICHAITOR_SOLO_WEB=true en el contenedor, sin API detrás): además genera
web/site/_solo/ con otra portada y la página de /app. En web/src/index.html, cada bloque que
necesita la API va entre <!--api--> y <!--solo-->, seguido de lo que lo sustituye en este
modo y de <!--/api-->. La imagen saca _solo/ de la raíz pública (ver web/Dockerfile).
"""

import re
import shutil
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "web" / "site"
SOLO = SITE / "_solo"
BLOQUE_API = re.compile(r"<!--api-->\n?(.*?)<!--solo-->\n?(.*?)<!--/api-->\n?", re.S)

PAGES = {
    "privacidad": (
        "privacidad.md",
        "Política de Privacidad",
        "Qué datos trata Fichaitor, para qué, con qué proveedores y cómo ejercer tus derechos o desconectar tu ficha de Google.",
    ),
    "condiciones-de-uso": (
        "condiciones-de-uso.md",
        "Condiciones de uso",
        "Condiciones del servicio Fichaitor para empresas y autónomos: planes, renovación, baja, responsabilidades y uso aceptable.",
    ),
}
ICONS = [
    "logo.svg",
    "favicon.ico",
    "favicon-32.png",
    "apple-touch-icon.png",
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-512.png",
    "og-fichaitor.png",
]

CABEZA_LEGAL = '''<meta name="description" content="{descripcion}">
<meta property="og:title" content="{title} — Fichaitor">
<meta property="og:description" content="{descripcion}">
<meta property="og:url" content="https://fichaitor.es/{slug}/">
<meta property="og:image" content="https://fichaitor.es/og-fichaitor.png">
<meta property="og:locale" content="es_ES">
<link rel="canonical" href="https://fichaitor.es/{slug}/">'''

# Página no encontrada: la sirve nginx para cualquier ruta que no exista (error_page 404).
# Rutas absolutas en todo, porque se muestra en cualquier dirección.
NO_ENCONTRADA = '''<section class="no-hay">
  <h1 class="cartel">Aquí no hay nada<span class="pt">.</span></h1>
  <p class="lead">O el enlace está mal, o esta página ya no existe. Tu ficha sigue donde estaba.</p>
  <div class="acc">
    <a class="btn" href="/">Volver al inicio <span class="fl" aria-hidden="true">→</span></a>
    <a class="btn sec" href="/app/">Entrar en el panel</a>
  </div>
</section>'''

# /app en el modo «solo web»: la aplicación necesita la API, así que se sirve esto en su lugar.
MUY_PRONTO = '''<section class="no-hay">
  <h1 class="cartel">Muy pronto<span class="pt">.</span></h1>
  <p class="lead">El panel de Fichaitor todavía no está abierto. Estamos terminando las pruebas.</p>
  <div class="acc">
    <a class="btn" href="/">Volver al inicio <span class="fl" aria-hidden="true">→</span></a>
  </div>
</section>'''

TEMPLATE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Fichaitor</title>
{cabeza}
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
{contenido}
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


def variante(html: str, solo: bool) -> str:
    """Deja en cada bloque <!--api-->…<!--solo-->…<!--/api--> la parte normal o la de «solo web»."""
    return BLOQUE_API.sub(lambda m: m.group(2 if solo else 1), html)


def main() -> None:
    marca = palabra()
    portada = (ROOT / "web" / "src" / "index.html").read_text(encoding="utf-8")
    portada = portada.replace("{{palabra}}", marca).replace(
        "{{palabra_grande}}", marca.replace("<svg ", '<svg width="100%" ')
    )
    for html in (variante(portada, False), variante(portada, True)):
        if "<!--api-->" in html or "<!--solo-->" in html or "<!--/api-->" in html:
            sys.exit("ERROR: web/src/index.html tiene un bloque <!--api--> mal cerrado.")
    (SITE / "index.html").write_text(variante(portada, False), encoding="utf-8", newline="\n")
    print("ok  web/site/index.html")
    SOLO.mkdir(exist_ok=True)
    (SOLO / "index.html").write_text(variante(portada, True), encoding="utf-8", newline="\n")
    (SOLO / "app.html").write_text(
        TEMPLATE.format(title="Muy pronto", cabeza='<meta name="robots" content="noindex">', contenido=MUY_PRONTO, palabra=marca),
        encoding="utf-8",
        newline="\n",
    )
    print("ok  web/site/_solo/ (modo solo web)")
    pending = []
    for slug, (source, title, descripcion) in PAGES.items():
        text = (ROOT / "docs" / "legal" / source).read_text(encoding="utf-8")
        text = re.sub(r"^> \*\*BORRADOR.*?\n\n", "", text, count=1, flags=re.M | re.S)
        if "[PENDIENTE" in text or "[REVISAR" in text:
            pending.append(source)
        body = markdown.markdown(text, extensions=["tables"])
        out = SITE / slug / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            TEMPLATE.format(
                title=title,
                cabeza=CABEZA_LEGAL.format(title=title, slug=slug, descripcion=descripcion),
                contenido=f'<article class="legal">\n{body}\n</article>',
                palabra=marca,
            ),
            encoding="utf-8",
            newline="\n",
        )
        print(f"ok  {out.relative_to(ROOT)}")
    (SITE / "404.html").write_text(
        TEMPLATE.format(title="Página no encontrada", cabeza='<meta name="robots" content="noindex">', contenido=NO_ENCONTRADA, palabra=marca),
        encoding="utf-8",
        newline="\n",
    )
    print("ok  web/site/404.html")
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
