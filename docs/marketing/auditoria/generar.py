# ruff: noqa: E501  -- plantilla HTML y textos para imprimir: las líneas largas son contenido, no código.
"""Genera la auditoría en una página de cada bar, en HTML y PDF, para imprimir.

Entrada: un JSON con lo que el PM ve en Google Maps de cada bar (ver bares.ejemplo.json).
La nota usa las mismas reglas que el diagnóstico público (app/domain/diagnosis.py).
Salida: docs/marketing/auditoria/salida/<id>.html y <id>.pdf (fuera de git).

Uso, desde la raíz del repositorio:
    uv run --no-project --with qrcode python docs/marketing/auditoria/generar.py docs/marketing/auditoria/bares.json
"""

from __future__ import annotations

import html
import json
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parents[2]
sys.path.insert(0, str(RAIZ))

from app.domain.diagnosis import Finding, Impact, PlaceSnapshot, _collect, score_and_rank  # noqa: E402

FUENTE = (RAIZ / "web" / "site" / "fonts" / "Archivo.woff2").as_uri()
LOGO = (RAIZ / "brand" / "fichaitor-lockup-tinta.svg").as_uri()
QR_URL = "https://fichaitor.es/?ref=visita&s=visita"
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

# Cómo se arregla cada fallo. Solo funciones que existen (docs/marketing/agents.md).
ARREGLO = {
    "festivo": "Fichaitor te pregunta antes de cada festivo si abres y lo deja puesto en Google.",
    "sin_responder": "Fichaitor te avisa de cada reseña con la respuesta ya escrita. Tú decides si la publicas.",
    "low_rating": "Contesta también las malas: quien lee ve tu versión. Fichaitor te avisa de cada una.",
    "few_reviews": "Pide reseña a todos tus clientes, no solo a los contentos. Sin regalos a cambio.",
    "no_hours": "Lo arreglas tú en 5 minutos desde tu ficha de Google.",
    "not_operational": "Revísalo hoy en tu ficha de Google: es lo más urgente.",
}
ARREGLO_TU = "Lo arreglas tú en 5 minutos desde tu ficha de Google."


def problemas(b: dict) -> tuple[int, str, list[dict]]:
    """Nota con las reglas públicas y los fallos de la hoja, el más urgente primero."""
    sabe_categorias = b.get("categorias_extra")
    snap = PlaceSnapshot(
        place_id=b["id"],
        name=b["negocio"],
        primary_type="bar",
        # Si no sabemos si tiene categorías secundarias, no lo penalizamos.
        types=("bar",) if sabe_categorias is False else ("bar", "restaurant"),
        operational=b.get("abierto", True),
        has_regular_hours=b.get("horario", True),
        photo_count=min(int(b.get("fotos", 0)), 10),
        rating=b.get("nota"),
        review_count=int(b.get("resenas", 0)),
        website="x" if b.get("web", True) else "",
        phone="x" if b.get("telefono", True) else "",
        service_attributes=1 if b.get("servicios", True) else 0,
    )
    found = _collect(snap)
    # Lo que solo se ve mirando la ficha a mano, con los pesos del diagnóstico de la
    # ficha conectada (app/domain/profile_diagnosis.py): festivo sin horario, 8;
    # reseñas sin responder, 8, o 15 si alguna es de 1 o 2 estrellas.
    if b.get("festivo_8_dic") is False:
        found.append(
            Finding(
                "festivo",
                "Tu ficha no dice si abres el martes 8 de diciembre",
                "Es festivo. Quien te busque ese día verá tu horario de un martes normal.",
                Impact.HIGH,
                8,
            )
        )
    sin = int(b.get("sin_responder", 0))
    malas = int(b.get("malas_sin_responder", 0))
    if sin > 0:
        detalle = "Quien lee las reseñas ve que nadie contesta. Una respuesta corta y educada cambia esa impresión."
        if malas > 0:
            detalle = f"{malas} son de 1 o 2 estrellas. Una crítica sin respuesta es lo que más frena a quien te compara."
        found.append(
            Finding(
                "sin_responder",
                f"{sin} de tus 10 últimas reseñas no tienen respuesta",
                detalle,
                Impact.HIGH if malas > 0 else Impact.MEDIUM,
                15 if malas > 0 else 8,
            )
        )
    score, label, ranked = score_and_rank(found)
    lista = [
        {"titulo": f.title, "detalle": f.detail, "arreglo": ARREGLO.get(f.code, ARREGLO_TU)}
        for f in ranked
    ]
    return score, label, lista[:3]


def qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=0)
    svg = img.to_string(encoding="unicode")
    return svg.replace("<svg ", '<svg class="qr" ', 1)


def pagina(b: dict) -> str:
    score, label, lista = problemas(b)
    e = html.escape
    filas = "".join(
        f"""<li><span class="num">0{i}</span><div><h3>{e(p["titulo"])}<b>.</b></h3>
        <p>{e(p["detalle"])}</p><p class="arreglo">{e(p["arreglo"])}</p></div></li>"""
        for i, p in enumerate(lista, 1)
    )
    if not lista:
        filas = '<li><span class="num">01</span><div><h3>Tu ficha está bien cuidada<b>.</b></h3><p>No hemos visto nada urgente. Fichaitor te ayuda a que siga así: te avisa de cada reseña y de cada festivo.</p></div></li>'
    calle = ""
    if b.get("calle"):
        calle = f'<p class="calle">{e(b["calle"])}</p>'
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>La ficha de Google de {e(b["negocio"])}</title>
<style>
@font-face {{ font-family: "Archivo"; src: url("{FUENTE}") format("woff2"); font-weight: 100 900; font-stretch: 62% 125%; }}
@page {{ size: A4; margin: 0; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: 210mm; height: 297mm; }}
body {{ background: #f4efe6; color: #0b0b0b; font-family: "Archivo", system-ui, sans-serif; font-size: 11pt; line-height: 1.4;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; padding: 14mm 15mm 12mm; display: flex; flex-direction: column; }}
.cartel {{ font-weight: 900; font-stretch: 125%; text-transform: uppercase; letter-spacing: -0.04em; line-height: .9; }}
b {{ color: #ff3b30; }}
header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #0b0b0b; padding-bottom: 5mm; }}
header img {{ height: 9mm; }}
header .fecha {{ font-size: 9pt; font-weight: 600; color: #55504a; text-align: right; }}
h1 {{ font-size: 26pt; margin: 8mm 0 2mm; }}
.sub {{ color: #55504a; font-size: 11pt; }}
.nota {{ display: flex; align-items: flex-end; gap: 6mm; margin: 7mm 0 7mm; background: #0b0b0b; color: #f4efe6; padding: 6mm 7mm; }}
.nota .cifra {{ font-size: 64pt; }}
.nota .de {{ font-size: 16pt; color: #b8b0a3; padding-bottom: 3mm; }}
.nota .etiqueta {{ margin-left: auto; text-align: right; padding-bottom: 2mm; }}
.nota .etiqueta strong {{ display: block; font-size: 15pt; font-weight: 800; font-stretch: 112%; }}
.nota .etiqueta span {{ font-size: 9pt; color: #b8b0a3; }}
h2 {{ font-size: 15pt; margin-bottom: 4mm; }}
ol {{ list-style: none; border-top: 2px solid #0b0b0b; }}
li {{ display: flex; gap: 5mm; padding: 4.5mm 0; border-bottom: 2px solid #0b0b0b; }}
.num {{ font-weight: 900; font-stretch: 125%; font-size: 22pt; line-height: 1; min-width: 14mm; }}
h3 {{ font-weight: 800; font-stretch: 112%; font-size: 12.5pt; letter-spacing: -0.02em; margin-bottom: 1mm; }}
li p {{ color: #0b0b0b; }}
.arreglo {{ margin-top: 1.5mm; font-weight: 600; color: #c4200f; }}
.calle {{ margin-top: 4mm; font-weight: 600; }}
footer {{ margin-top: auto; display: flex; gap: 7mm; align-items: center; border: 2px solid #0b0b0b; padding: 5mm 6mm; box-shadow: 4px 4px 0 #0b0b0b; }}
footer .texto {{ flex: 1; }}
footer .promesa {{ font-size: 13pt; margin-bottom: 2mm; }}
footer p {{ font-size: 10pt; }}
.qr {{ width: 26mm; height: 26mm; }}
.qr path {{ fill: #0b0b0b; }}
.origen {{ font-size: 8pt; color: #55504a; margin-top: 3mm; }}
</style></head>
<body>
<header><img src="{LOGO}" alt="Fichaitor"><p class="fecha">Revisado a mano en Google Maps<br>el {e(b["fecha"])}</p></header>
<h1 class="cartel">La ficha de Google de {e(b["negocio"])}<b>.</b></h1>
<p class="sub">Así te ve quien te busca en Google Maps antes de venir.</p>
<section class="nota"><span class="cifra cartel">{score}</span><span class="de cartel">/100</span>
<span class="etiqueta"><strong>{e(label)}</strong><span>Nota orientativa con lo que se ve desde fuera</span></span></section>
<h2 class="cartel">Tres cosas que se ven desde fuera<b>.</b></h2>
<ol>{filas}</ol>
{calle}
<footer>
<div class="texto">
<p class="promesa cartel">Nada cambia en tu ficha sin tu confirmación<b>.</b></p>
<p>Fichaitor te avisa al móvil de cada reseña nueva con la respuesta ya escrita, y antes de cada festivo te pregunta si abres. Ahora estamos en beta: <strong>el plan Pro es gratis hasta la versión definitiva</strong>, y después tienes 30 días para decidir si sigues.</p>
<p class="origen">fichaitor.es · soporte@fichaitor.es · Datos de Google Maps, revisados a mano.</p>
</div>
{qr_svg(QR_URL)}
</footer>
</body></html>"""


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    bares = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    salida = AQUI / "salida"
    salida.mkdir(exist_ok=True)
    for b in bares:
        h = salida / f"{b['id']}.html"
        h.write_text(pagina(b), encoding="utf-8")
        pdf = salida / f"{b['id']}.pdf"
        subprocess.run(
            [
                str(CHROME),
                "--headless=new",
                "--disable-gpu",
                "--allow-file-access-from-files",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf}",
                h.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        print(pdf)


if __name__ == "__main__":
    main()
