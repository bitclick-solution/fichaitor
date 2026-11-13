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

# Cobro: valores de ejemplo, ya formateados como los da el backend.
FACTURACION = "https://fichaitor.es/app#facturacion"
FICHA = "Bar La Parra"


def b(nombre: str, **valores: str) -> str:
    """Un bloque con sus valores escapados."""
    return BLOQUES[nombre].substitute({k: html.escape(v, quote=True) for k, v in valores.items()})


def datos(*filas: tuple[str, str]) -> list[str]:
    """Una tabla de datos: una fila `dato` por par y el cierre `datos_fin`."""
    return [b("dato", etiqueta=e, valor=v) for e, v in filas] + [BLOQUES["datos_fin"].substitute()]


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
    "3-restaurada-ficha": correo(
        "Has restaurado Fichaitor",
        "No se ha borrado nada.",
        "Restaurada",
        [
            b("parrafo", texto="Hemos cancelado la desconexión. No se ha borrado nada."),
            b("parrafo", texto="Fichaitor vuelve a leer tus fichas de Google y a avisarte cuando haya algo que decidir."),
        ],
        "Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.",
    ),
    "3-restaurada-cuenta": correo(
        "Has restaurado tu cuenta de Fichaitor",
        "Entra de nuevo para seguir donde lo dejaste.",
        "Restaurada",
        [
            b("parrafo", texto="Hemos cancelado la baja de tu cuenta. No se ha borrado nada."),
            b(
                "parrafo",
                texto="Al confirmar la baja cerramos tu sesión en todos los dispositivos. Entra de nuevo en Fichaitor para seguir donde lo dejaste.",
            ),
            b("boton", etiqueta="Entrar en Fichaitor", enlace="https://fichaitor.es/app/"),
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
    # ── Cobro ──
    "5-factura": correo(
        "Tu factura de Fichaitor FCH/2027/00042",
        f"Pro para {FICHA}, del 14/03/2027 al 13/03/2028. La factura va adjunta en PDF.",
        "Tu factura",
        [
            b("parrafo", texto=f"Hemos cobrado 42,35 € por el plan Pro de {FICHA}. Te adjuntamos la factura en PDF."),
            *datos(
                ("Factura", "FCH/2027/00042"),
                ("Fecha", "14 de marzo de 2027"),
                ("Plan", "Pro"),
                ("Ficha", FICHA),
                ("Periodo", "Del 14/03/2027 al 13/03/2028"),
                ("Total", "42,35 €"),
            ),
            b("boton", etiqueta="Ver Facturación", enlace=FACTURACION),
            b(
                "parrafo",
                texto="Esta ficha se renovará sola al terminar el periodo, salvo que canceles la renovación desde Facturación. Te avisaremos 30 días antes.",
            ),
        ],
        "Te llega este correo porque se ha cobrado un plan de pago de tu cuenta de Fichaitor.",
    ),
    "6-renovacion": correo(
        f"Tu plan Pro de {FICHA} se renueva el 14 de marzo de 2028",
        "Cobraremos 42,35 € con tu tarjeta. Si no quieres renovarlo, cancélalo antes.",
        "Se renueva pronto",
        [
            b(
                "parrafo",
                texto=f"El 14 de marzo de 2028 se renueva por un año más el plan Pro de {FICHA}. Cobraremos 42,35 € con la tarjeta que tienes guardada.",
            ),
            *datos(("Ficha", FICHA), ("Plan", "Pro"), ("Renovación", "14 de marzo de 2028"), ("Total", "42,35 €")),
            b(
                "parrafo",
                texto="Si quieres seguir, no tienes que hacer nada. Si no, cancela la renovación desde Facturación antes del 14 de marzo de 2028: "
                "una vez cobrada, no se devuelve.",
            ),
            b("boton", etiqueta="Ir a Facturación", enlace=FACTURACION),
        ],
        "Te avisamos 30 días antes de cada renovación, como dicen las condiciones de uso.",
    ),
    "6-renovacion-sin-ficha": correo(
        "Una plaza Pro no se renovará el 14 de marzo de 2028",
        "No tiene ninguna ficha, así que no te la cobraremos.",
        "Plaza sin ficha",
        [
            b(
                "parrafo",
                texto="Tienes una plaza del plan Pro (un año pagado) que se renueva el 14 de marzo de 2028, pero ahora no tiene ninguna ficha asignada.",
            ),
            b(
                "parrafo",
                texto="Si sigue así, no se renovará ni te la cobraremos. Si quieres conservarla, activa una ficha en ella antes del 14 de marzo de 2028: "
                "entonces se renovará por 40,23 €.",
            ),
            b("boton", etiqueta="Ir a Facturación", enlace=FACTURACION),
        ],
        "Te avisamos 30 días antes de cada renovación, como dicen las condiciones de uso.",
    ),
    "7-pausa": correo(
        "Tu cuenta de Fichaitor está en pausa",
        "No hemos podido cobrar 42,35 €. En cuanto se pague, todo vuelve a funcionar.",
        "Cuenta en pausa",
        [
            b(
                "parrafo",
                texto=f"Tu cuenta está en pausa porque no hemos podido cobrar 42,35 € del plan de {FICHA}: la tarjeta ha sido rechazada.",
            ),
            b("boton", etiqueta="Pagar ahora", enlace="https://fichaitor.es/app#pagar"),
            b(
                "parrafo",
                texto="Durante la pausa, Fichaitor no lee ninguna de tus fichas, tampoco la del plan Gratis, no te envía avisos ni aplica cambios. "
                "No se ha borrado nada y la pausa no borra nada: en cuanto se pague, todo vuelve a funcionar.",
            ),
            b(
                "parrafo",
                texto="Mientras tanto, en el panel puedes seguir usando Facturación, desactivar fichas, desvincular chats, desconectar Google o darte de baja.",
            ),
        ],
        "Te llega este correo porque no se ha podido cobrar un plan de pago de tu cuenta de Fichaitor.",
    ),
    "8-pago-recibido": correo(
        "Pago recibido: todo vuelve a funcionar",
        "Fichaitor vuelve a leer tus fichas y a avisarte.",
        "Todo en marcha",
        [
            b("parrafo", texto=f"Hemos recibido el pago del plan de {FICHA}. Tu cuenta ya no está en pausa."),
            b("parrafo", texto="Fichaitor vuelve a leer tus fichas y a avisarte cuando haya algo que decidir. La factura te llega en otro correo."),
            b("boton", etiqueta="Entrar en Fichaitor", enlace="https://fichaitor.es/app/"),
        ],
        "Te llega este correo porque se ha pagado un cobro pendiente de tu cuenta de Fichaitor.",
    ),
    "9-rectificativa": correo(
        "Factura rectificativa de Fichaitor FCH-R/2027/00003",
        "Rectifica la factura FCH/2027/00042. Va adjunta en PDF.",
        "Factura rectificativa",
        [
            b("parrafo", texto="Hemos emitido una factura rectificativa de la factura FCH/2027/00042, por −42,35 €. Te la adjuntamos en PDF."),
            *datos(("Rectificativa", "FCH-R/2027/00003"), ("Rectifica a", "FCH/2027/00042"), ("Total", "−42,35 €")),
            b("parrafo", texto="Devolvemos el importe a la tarjeta con la que pagaste. Según tu banco, puede tardar unos días en verse."),
        ],
        "Te llega este correo porque hemos devuelto un importe cobrado en tu cuenta de Fichaitor.",
    ),
    "10-fin-beta": correo(
        "Fichaitor se lanza el 1 de marzo de 2027: elige tu plan antes del 31 de marzo de 2027",
        "La beta sigue gratis hasta el lanzamiento y 30 días más.",
        "Fin de la beta",
        [
            b(
                "parrafo",
                texto="Gracias por probar Fichaitor durante la beta. El 1 de marzo de 2027 lanzamos la versión 1 y, desde ese día, las fichas d"
                "e pago se cobran. Como estás en la beta, tienes 30 días más, hasta el 31 de marzo de 2027, para elegir en Facturación el plan de cada ficha. Hasta entonces todo sigue como ahora.",
            ),
            b("boton", etiqueta="Elegir los planes", enlace=FACTURACION),
            b(
                "parrafo",
                texto="Cada ficha de pago se cobra con tarjeta por años completos y se renueva sola cada año, salvo que canceles la renovación."
                " Si eliges un plan antes del 31 de marzo de 2027, guardamos tu tarjeta pero no cobramos nada hasta ese día. Lo cobrado no se devuelve, así que elige con calma.",
            ),
            b(
                "parrafo",
                texto="Si no eliges plan antes del 31 de marzo de 2027, tu ficha más antigua pasará al plan Gratis y las demás se desactivarán"
                ": dejaremos de leerlas y de avisarte de ellas, y si no las vuelves a activar en 30 días, borraremos lo que tenemos de ellas. No te cobraremos nada que no hayas pedido.",
            ),
        ],
        "Te llega este correo porque tienes una cuenta de Fichaitor en la beta.",
    ),
    "10-fin-beta-oferta": correo(
        "Fichaitor se lanza el 1 de marzo de 2027: elige tu plan antes del 31 de marzo de 2027",
        "La beta sigue gratis hasta el lanzamiento y 30 días más.",
        "Fin de la beta",
        [
            b(
                "parrafo",
                texto="Gracias por probar Fichaitor durante la beta. El 1 de marzo de 2027 lanzamos la versión 1 y, desde ese día, las fichas d"
                "e pago se cobran. Como estás en la beta, tienes 30 días más, hasta el 31 de marzo de 2027, para elegir en Facturación el plan de cada ficha. Hasta entonces todo sigue como ahora.",
            ),
            b("destacado", texto="Texto de ejemplo de la oferta para las cuentas de la beta, todavía sin decidir."),
            b("boton", etiqueta="Elegir los planes", enlace=FACTURACION),
            b(
                "parrafo",
                texto="Cada ficha de pago se cobra con tarjeta por años completos y se renueva sola cada año, salvo que canceles la renovación."
                " Si eliges un plan antes del 31 de marzo de 2027, guardamos tu tarjeta pero no cobramos nada hasta ese día. Lo cobrado no se devuelve, así que elige con calma.",
            ),
            b(
                "parrafo",
                texto="Si no eliges plan antes del 31 de marzo de 2027, tu ficha más antigua pasará al plan Gratis y las demás se desactivarán"
                ": dejaremos de leerlas y de avisarte de ellas, y si no las vuelves a activar en 30 días, borraremos lo que tenemos de ellas. No te cobraremos nada que no hayas pedido.",
            ),
        ],
        "Te llega este correo porque tienes una cuenta de Fichaitor en la beta.",
    ),
    "11-fin-prueba": correo(
        f"El 31 de marzo de 2027 cobraremos el plan Pro de {FICHA}",
        "Primer cobro: 42,35 €. Si no quieres seguir, cancélalo antes.",
        "Termina tu beta",
        [
            b(
                "parrafo",
                texto=f"Tu periodo de la beta termina el 31 de marzo de 2027. Ese día cobraremos 42,35 € (IVA incluido) con la tarjeta que guardaste "
                f"por el primer año del plan Pro de {FICHA}.",
            ),
            b(
                "parrafo",
                texto="Si quieres seguir, no tienes que hacer nada. Si no, cancela la plaza desde Facturación antes del 31 de marzo de 2027 y no te "
                "cobraremos nada. Una vez cobrado el año, no se devuelve.",
            ),
            b("boton", etiqueta="Ir a Facturación", enlace=FACTURACION),
            b(
                "parrafo",
                texto="Después, el plan se renueva solo cada año, salvo que canceles la renovación. Te avisaremos 30 días antes de cada renovación.",
            ),
        ],
        "Te avisamos 7 días antes del primer cobro de una plaza contratada durante la beta, como dicen las condiciones de uso.",
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
