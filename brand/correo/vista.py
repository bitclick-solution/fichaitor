"""Genera en brand/correo/vista/ una página por correo, con datos de ejemplo, para revisarlos.

Uso, desde la raíz del repositorio:

    uv run --no-project python brand/correo/vista.py

Rellena las plantillas exactamente como debe hacerlo el backend (brand/correo/LEEME.md):
string.Template, valores escapados con html.escape y substitute (falla si falta una variable).
"""

import html
import re
from pathlib import Path
from string import Template

AQUI = Path(__file__).resolve().parent
BASE = Template((AQUI / "base.html").read_text(encoding="utf-8"))
BLOQUES = {
    nombre: Template(cuerpo.strip())
    for nombre, cuerpo in re.findall(r"== (\w+) ==\n(.*?)(?=\n== |\Z)", (AQUI / "bloques.html").read_text(encoding="utf-8"), flags=re.S)
}

FECHA = "14 de noviembre de 2026 a las 12:16 (hora peninsular)"
ENLACE = "https://fichaitor.es/app#restaurar=ejemplo-de-token-de-un-solo-uso"


def b(nombre: str, **valores: str) -> str:
    """Un bloque con sus valores escapados."""
    return BLOQUES[nombre].substitute({k: html.escape(v, quote=True) for k, v in valores.items()})


def correo(asunto: str, pre: str, titular: str, bloques: list[str], motivo: str) -> str:
    return BASE.substitute(
        asunto=html.escape(asunto),
        preencabezado=html.escape(pre),
        titular=html.escape(titular),
        contenido="\n".join(bloques),
        motivo=html.escape(motivo),
    )


CORREOS = {
    "1-codigo": correo(
        "Tu código de confirmación de Fichaitor",
        "Lo necesitas para desconectar tu ficha. Caduca en 10 minutos.",
        "Tu código",
        [
            b("parrafo", texto="Has pedido desconectar tu ficha. Escribe este código en Fichaitor para confirmarlo:"),
            b("codigo", codigo="482913"),
            b("parrafo", texto="Caduca en 10 minutos y solo sirve una vez."),
            b("aviso", texto="Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu sesión: escríbenos a soporte@fichaitor.es."),
        ],
        "Te llega este correo porque alguien ha pedido desconectar tu ficha desde tu cuenta de Fichaitor.",
    ),
    "2-baja-ficha": correo(
        "Tu ficha se desconectará de Fichaitor pronto",
        f"Puedes restaurarla hasta el {FECHA}.",
        "Desconexión en marcha",
        [
            b("parrafo", texto="Has pedido desconectar tu ficha de Fichaitor. Desde ahora no la leemos ni te enviamos avisos."),
            b("boton", etiqueta="Restaurar", enlace=ENLACE),
            b("aviso", texto="Si no has sido tú, pulsa Restaurar. No se ha borrado nada."),
            b(
                "parrafo",
                texto=f"Si no haces nada, el {FECHA} retiraremos el acceso a tu cuenta de Google. Lo obtenido de Google se borra en un máximo "
                "de 30 días, y el registro de las respuestas que publicaste, a los 12 meses. Tu cuenta sigue existiendo y podrás volver a conectar.",
            ),
        ],
        "Te llega este correo porque se ha confirmado una baja con un código enviado a esta dirección.",
    ),
    "2-baja-cuenta": correo(
        "Tu cuenta de Fichaitor se borrará pronto",
        f"Puedes restaurarla hasta el {FECHA}.",
        "Baja en marcha",
        [
            b(
                "parrafo",
                texto="Has pedido borrar tu cuenta de Fichaitor. Desde ahora no leemos tus fichas ni te enviamos avisos, "
                "y hemos cerrado tu sesión en todos los dispositivos.",
            ),
            b("boton", etiqueta="Restaurar", enlace=ENLACE),
            b("aviso", texto="Si no has sido tú, pulsa Restaurar. No se ha borrado nada."),
            b(
                "parrafo",
                texto=f"Si no haces nada, el {FECHA} retiraremos el acceso a tu cuenta de Google y cerraremos tu cuenta. Lo obtenido de "
                "Google se borra en un máximo de 30 días, y el registro de las respuestas que publicaste, a los 12 meses.",
            ),
        ],
        "Te llega este correo porque se ha confirmado una baja con un código enviado a esta dirección.",
    ),
    "3-restaurada": correo(
        "Has restaurado Fichaitor",
        "Todo sigue como estaba.",
        "Restaurada",
        [
            b("parrafo", texto="La baja está cancelada. Todo sigue como estaba y no se ha borrado nada."),
            b("parrafo", texto="Fichaitor vuelve a leer tu ficha y a avisarte cuando haya algo que decidir."),
        ],
        "Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.",
    ),
    "4-completada-ficha": correo(
        "Tu ficha se ha desconectado de Fichaitor",
        "Hemos retirado el acceso a Google.",
        "Desconectada",
        [
            b("parrafo", texto="Hemos retirado el acceso a tu cuenta de Google. Lo obtenido de Google se borrará en un máximo de 30 días."),
            b("parrafo", texto="Si quieres volver, conecta tu ficha de nuevo en fichaitor.es."),
        ],
        "Te llega este correo porque se ha completado una baja que pediste.",
    ),
    "4-completada-cuenta": correo(
        "Tu cuenta de Fichaitor se ha borrado",
        "Hemos retirado el acceso a Google.",
        "Hasta aquí",
        [
            b(
                "parrafo",
                texto="Hemos retirado el acceso a tu cuenta de Google y cerrado tu cuenta. Lo obtenido de Google se borrará en un máximo de 30 días.",
            ),
            b("parrafo", texto="Gracias por haber probado Fichaitor."),
        ],
        "Te llega este correo porque se ha completado una baja que pediste.",
    ),
}


def main() -> None:
    salida = AQUI / "vista"
    salida.mkdir(exist_ok=True)
    for nombre, contenido in CORREOS.items():
        (salida / f"{nombre}.html").write_text(contenido, encoding="utf-8", newline="\n")
        print(f"ok  brand/correo/vista/{nombre}.html")


if __name__ == "__main__":
    main()
