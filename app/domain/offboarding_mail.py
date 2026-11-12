"""The emails of a leave, in Spanish: plain text and a simple HTML version. No I/O.

They carry nothing obtained from Google: no review, no figure, no name of a business.
"""

from datetime import UTC, datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.offboarding import SCOPE_ACCOUNT
from app.ports.mailer import Email

_MONTHS = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()
RESTORE_PARAM = "restaurar"


def restore_url(public_base_url: str, token: str) -> str:
    """The link of the email. The token goes in the fragment, which browsers never
    send to a server: it cannot end up in the access log of a proxy."""
    return f"{public_base_url.rstrip('/')}/app#{RESTORE_PARAM}={quote(token, safe='')}"


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


def _html(paragraphs: list[str]) -> str:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        '<!doctype html><html lang="es"><body style="font-family:sans-serif;'
        f'line-height:1.5;color:#111">{body}</body></html>'
    )


def code_email(to: str, scope: str, code: str, minutes: int) -> Email:
    what = _what(scope)
    text = (
        f"Hola:\n\nTu código para {what} es:\n\n    {code}\n\n"
        f"Caduca en {minutes} minutos y solo sirve una vez.\n\n"
        "Si no lo has pedido tú, ignora este correo: no se hará ningún cambio.\n\n"
        "Fichaitor\n"
    )
    html = _html(
        [
            "Hola:",
            f"Tu código para {escape(what)} es:",
            f'<strong style="font-size:1.6em;letter-spacing:0.15em">{escape(code)}</strong>',
            f"Caduca en {minutes} minutos y solo sirve una vez.",
            "Si no lo has pedido tú, ignora este correo: no se hará ningún cambio.",
            "Fichaitor",
        ]
    )
    # The code is not in the subject: subjects show up in notifications and lists.
    return Email(to, "Tu código de confirmación de Fichaitor", text, html)


def scheduled_email(to: str, scope: str, delete_at: datetime, url: str) -> Email:
    when = spanish_date(delete_at)
    if scope == SCOPE_ACCOUNT:
        subject = "Tu cuenta de Fichaitor se borrará pronto"
        first = f"Has pedido borrar tu cuenta de Fichaitor. La baja se hará efectiva el {when}."
        then = (
            "Ese día revocaremos el acceso a tu cuenta de Google y cerraremos tu cuenta. "
            "Lo obtenido de Google se borra en un máximo de 30 días, y el registro de las "
            "respuestas que publicaste, a los 12 meses."
        )
    else:
        subject = "Tu ficha se desconectará de Fichaitor pronto"
        first = f"Has pedido desconectar tu ficha de Fichaitor. Se hará efectivo el {when}."
        then = (
            "Ese día revocaremos el acceso a tu cuenta de Google. Lo obtenido de Google se "
            "borra en un máximo de 30 días, y el registro de las respuestas que publicaste, "
            "a los 12 meses. Tu cuenta sigue existiendo y podrás volver a conectar."
        )
    meanwhile = "Desde ahora no leemos tu ficha ni te enviamos avisos."
    undo = "Si cambias de idea, puedes restaurarla hasta entonces con este enlace:"
    not_you = "Si no lo has pedido tú, usa ese enlace para cancelarla."
    text = f"Hola:\n\n{first}\n\n{meanwhile} {then}\n\n{undo}\n\n{url}\n\n{not_you}\n\nFichaitor\n"
    link = f'<a href="{escape(url, quote=True)}">Restaurar</a>'
    html = _html(
        [
            "Hola:",
            escape(first),
            f"{escape(meanwhile)} {escape(then)}",
            escape(undo),
            link,
            escape(not_you),
            "Fichaitor",
        ]
    )
    return Email(to, subject, text, html)
