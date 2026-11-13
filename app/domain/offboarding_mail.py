"""The emails of a leave, in Spanish, with the brand templates: plain text and HTML.

Texts, order of the blocks and rules: brand/correo/LEEME.md. The templates come from
app/mail_templates (read once, at import); filling them is pure.

- Every value is escaped with html.escape before it goes into a template, except
  ${contenido}, which is the HTML of blocks already filled and escaped. The plain text
  is not escaped.
- The code is never in the subject nor in the preheader: both show up in notifications.
- They carry nothing obtained from Google: no review, no figure, no name of a business.
"""

from datetime import UTC, datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.offboarding import SCOPE_ACCOUNT
from app.mail_templates import BASE, BLOCKS
from app.ports.mailer import Email

_MONTHS = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()
RESTORE_PARAM = "restaurar"

# The same at the end of every plain text email. "-- " with its trailing space is the
# standard signature separator: mail clients can fold what follows.
_PLAIN_FOOTER = (
    "-- \n"
    "¿Dudas? Escríbenos a soporte@fichaitor.es.\n"
    "Fichaitor es un producto de Bitclick Solutions S.L.U. · C/ Cuenca nº 17, 18230 Atarfe "
    "(Granada), España.\n"
)


def restore_url(public_base_url: str, token: str) -> str:
    """The link of the email: absolute, made only from the public URL of the
    configuration (never from the request) and the token. The token goes in the
    fragment, which browsers never send to a server: it cannot end up in the access
    log of a proxy nor in a Referer."""
    return f"{public_base_url.rstrip('/')}/app#{RESTORE_PARAM}={quote(token, safe='')}"


def app_url(public_base_url: str) -> str:
    """Where the owner signs in again: absolute, made only from the public URL of the
    configuration, like the restore link."""
    return f"{public_base_url.rstrip('/')}/app/"


def spanish_term(minutes: int) -> str:
    """ "1 minuto", "10 minutos": never "1 minutos"."""
    return f"{minutes} minuto" if minutes == 1 else f"{minutes} minutos"


def spanish_date(moment: datetime) -> str:
    """Like "14 de noviembre de 2026 a las 12:16 (hora peninsular)". In UTC, and saying
    so, where the time zone data is not installed."""
    try:
        local, label = moment.astimezone(ZoneInfo("Europe/Madrid")), "hora peninsular"
    except ZoneInfoNotFoundError:
        local, label = moment.astimezone(UTC), "UTC"
    return (
        f"{local.day} de {_MONTHS[local.month - 1]} de {local.year} a las {local:%H:%M} ({label})"
    )


def _what(scope: str) -> str:
    return "borrar tu cuenta de Fichaitor" if scope == SCOPE_ACCOUNT else "desconectar tu ficha"


def _block(name: str, **values: str) -> str:
    """One block of the body, every value escaped (quotes too: links go in href)."""
    return BLOCKS[name].substitute({key: escape(value) for key, value in values.items()})


def _email(
    to: str,
    *,
    subject: str,
    preheader: str,
    headline: str,
    blocks: list[str],
    plain: list[str],
    reason: str,
    baja: str = "",
) -> Email:
    html = BASE.substitute(
        asunto=escape(subject),
        preencabezado=escape(preheader),
        titular=escape(headline),
        contenido="\n".join(blocks),  # already escaped, block by block
        motivo=escape(reason),
        # The unsubscribe block of a commercial communication, already escaped; empty
        # in every other email (brand/correo/LEEME.md, 12).
        baja=baja,
    )
    text = "\n\n".join([headline.upper(), *plain, reason]) + "\n\n" + _PLAIN_FOOTER
    return Email(to, subject, text, html)


def code_email(to: str, scope: str, code: str, minutes: int) -> Email:
    """1. The code that confirms a leave."""
    what, term = _what(scope), spanish_term(minutes)
    asked = f"Has pedido {what}. Escribe este código en Fichaitor para confirmarlo:"
    expires = f"Caduca en {term} y solo sirve una vez."
    not_you = (
        "Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu "
        "sesión: escríbenos a soporte@fichaitor.es."
    )
    return _email(
        to,
        subject="Tu código de confirmación de Fichaitor",
        preheader=f"Lo necesitas para {what}. Caduca en {term}.",
        headline="Tu código",
        blocks=[
            _block("parrafo", texto=asked),
            _block("codigo", codigo=code),
            _block("parrafo", texto=expires),
            _block("aviso", texto=not_you),
        ],
        plain=[asked, f"    {code}", expires, not_you],
        reason=(
            f"Te llega este correo porque alguien ha pedido {what} desde tu cuenta de Fichaitor."
        ),
    )


def scheduled_email(to: str, scope: str, delete_at: datetime, url: str) -> Email:
    """2. The leave is scheduled: the date and the restore link."""
    when = spanish_date(delete_at)
    terms = (
        "Lo obtenido de Google se borra en un máximo de 30 días, y el registro de las "
        "respuestas que publicaste, a los 12 meses."
    )
    if scope == SCOPE_ACCOUNT:
        subject, headline = "Tu cuenta de Fichaitor se borrará pronto", "Baja en marcha"
        first = (
            "Has pedido borrar tu cuenta de Fichaitor. Desde ahora no leemos tus fichas ni te "
            "enviamos avisos, y hemos cerrado tu sesión en todos los dispositivos."
        )
        last = (
            f"Si no haces nada, el {when} retiraremos el acceso a tu cuenta de Google y "
            f"cerraremos tu cuenta. {terms}"
        )
    else:
        subject, headline = "Tu ficha se desconectará de Fichaitor pronto", "Desconexión en marcha"
        first = (
            "Has pedido desconectar tu ficha de Fichaitor. Desde ahora no la leemos ni te "
            "enviamos avisos."
        )
        last = (
            f"Si no haces nada, el {when} retiraremos el acceso a tu cuenta de Google. {terms} "
            "Tu cuenta sigue existiendo y podrás volver a conectar."
        )
    return _email(
        to,
        subject=subject,
        preheader=f"Puedes restaurarla hasta el {when}.",
        headline=headline,
        blocks=[
            _block("parrafo", texto=first),
            _block("boton", etiqueta="Restaurar", enlace=url),
            _block("aviso", texto="Si no has sido tú, pulsa Restaurar. No se ha borrado nada."),
            _block("parrafo", texto=last),
        ],
        plain=[
            first,
            f"Restaurar: {url}",
            "Si no has sido tú, abre el enlace. No se ha borrado nada.",
            last,
        ],
        reason=(
            "Te llega este correo porque se ha confirmado una baja con un código enviado a "
            "esta dirección."
        ),
    )


def restored_email(to: str, scope: str, sign_in_url: str) -> Email:
    """3. The leave is cancelled, from the session or from the link.

    Deleting the account closed every session, and they stay closed: that variant says
    so and carries a button to sign in again (`sign_in_url`, from app_url)."""
    reason = "Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor."
    if scope == SCOPE_ACCOUNT:
        cancelled = "Hemos cancelado la baja de tu cuenta. No se ha borrado nada."
        again = (
            "Al confirmar la baja cerramos tu sesión en todos los dispositivos. Entra de nuevo "
            "en Fichaitor para seguir donde lo dejaste."
        )
        label = "Entrar en Fichaitor"
        return _email(
            to,
            subject="Has restaurado tu cuenta de Fichaitor",
            preheader="Entra de nuevo para seguir donde lo dejaste.",
            headline="Restaurada",
            blocks=[
                _block("parrafo", texto=cancelled),
                _block("parrafo", texto=again),
                _block("boton", etiqueta=label, enlace=sign_in_url),
            ],
            plain=[cancelled, again, f"{label}: {sign_in_url}"],
            reason=reason,
        )
    paragraphs = [
        "Hemos cancelado la desconexión. No se ha borrado nada.",
        "Fichaitor vuelve a leer tus fichas de Google y a avisarte cuando haya algo que decidir.",
    ]
    return _email(
        to,
        subject="Has restaurado Fichaitor",
        preheader="No se ha borrado nada.",
        headline="Restaurada",
        blocks=[_block("parrafo", texto=p) for p in paragraphs],
        plain=paragraphs,
        reason=reason,
    )
