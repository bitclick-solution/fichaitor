"""The emails of the charging, in Spanish, with the brand templates: plain text and HTML.

Texts, order of the blocks and rules: brand/correo/LEEME.md, «Correos del cobro» (5 to
10). Filled as the emails of a leave (app/domain/offboarding_mail.py): every value
escaped, `substitute` (a missing value is an error) and the same plain text footer.

Every value arrives already formatted (`euros`, `spanish_day`, `period_text`); the
functions here only put them in their place. The one exception to "nothing obtained from
Google in an email" is the name of the profile (`ficha`), only to say what is charged,
as the invoice does (decision 9.0, 8 of docs/admin-facturacion.md).

The links (`enlace`) are made by the worker from its configuration alone
(`billing_url`, `pay_url` and offboarding_mail.app_url): never from the payload of the
job.
"""

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.offboarding_mail import _block, _email
from app.domain.plans import Plan
from app.ports.mailer import Attachment, Email

# The `template` of a send_mail job, one per email of LEEME.md.
INVOICE_ISSUED = "invoice_issued"
RENEWAL_NOTICE = "renewal_notice"
RENEWAL_NOTICE_UNASSIGNED = "renewal_notice_unassigned"
BILLING_BLOCKED = "billing_blocked"
BILLING_UNBLOCKED = "billing_unblocked"
CREDIT_NOTE_ISSUED = "credit_note_issued"
BETA_ENDING = "beta_ending"
TRIAL_ENDING = "trial_ending"
TEMPLATES = (
    INVOICE_ISSUED,
    RENEWAL_NOTICE,
    RENEWAL_NOTICE_UNASSIGNED,
    BILLING_BLOCKED,
    BILLING_UNBLOCKED,
    CREDIT_NOTE_ISSUED,
    BETA_ENDING,
    TRIAL_ENDING,
)

PLAN_NAMES = {Plan.PRO: "Pro", Plan.PRO_WHATSAPP: "Pro+"}
MINUS = "−"  # the sign of a credit note: "−42,35 €"
_MONTHS = (
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre"
).split()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_PROFILE_CHARS = 80

RENEWAL_REASON = "Te avisamos 30 días antes de cada renovación, como dicen las condiciones de uso."


# --- formatting ------------------------------------------------------------------------


def euros(cents: int, *, negative: bool = False) -> str:
    """ "42,35 €", "1.234,00 €"; with `negative`, "−42,35 €"."""
    whole, part = divmod(abs(cents), 100)
    grouped = f"{whole:,}".replace(",", ".")
    sign = MINUS if negative or cents < 0 else ""
    return f"{sign}{grouped},{part:02d} €"


def spanish_day(day: date) -> str:
    """ "14 de marzo de 2027", without the time."""
    return f"{day.day} de {_MONTHS[day.month - 1]} de {day.year}"


def period_text(start: date, end: date) -> str:
    """ "del 14/03/2027 al 13/03/2028": `end` is the renewal day, so the last day paid is
    the one before (as on the invoice)."""
    last = date.fromordinal(end.toordinal() - 1)
    return f"del {start:%d/%m/%Y} al {last:%d/%m/%Y}"


def plan_name(plan: Plan | str) -> str:
    return PLAN_NAMES[Plan(plan)]


def profile_name(title: str | None, plan: Plan | str) -> str:
    """The name of the profile as the email says it, or "tu plaza Pro" for a seat without
    one. Without control characters and at most MAX_PROFILE_CHARS."""
    text = " ".join(_CONTROL.sub(" ", title or "").split())
    if len(text) > MAX_PROFILE_CHARS:
        text = text[: MAX_PROFILE_CHARS - 1].rstrip() + "…"
    return text or f"tu plaza {plan_name(plan)}"


def billing_url(public_base_url: str) -> str:
    """Facturación, in the panel."""
    return f"{public_base_url.rstrip('/')}/app#facturacion"


def pay_url(public_base_url: str) -> str:
    """The panel, where «Pagar ahora» asks Stripe for the pending invoice."""
    return f"{public_base_url.rstrip('/')}/app#pagar"


def pdf_filename(number: str) -> str:
    """ "FTR-2027-00001.pdf": the number of Odoo without its slashes."""
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", number).strip("-.") or "factura"
    return f"{clean[:60]}.pdf"


def _data(*rows: tuple[str, str]) -> tuple[list[str], str]:
    """A table of data: one `dato` per row and its `datos_fin`; in plain text, one
    `Etiqueta: valor` per line."""
    html = [_block("dato", etiqueta=label, valor=value) for label, value in rows]
    html.append(_block("datos_fin"))
    return html, "\n".join(f"{label}: {value}" for label, value in rows)


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


# --- 5. invoice issued ---------------------------------------------------------------


def invoice_issued_email(
    to: str,
    *,
    numero: str,
    fecha: str,
    plan: str,
    ficha: str,
    periodo: str,
    importe: str,
    enlace: str,
    pdf: bytes | None = None,
) -> Email:
    first = (
        f"Hemos cobrado {importe} por el plan {plan} de {ficha}. Te adjuntamos la factura en PDF."
    )
    renews = (
        "Esta ficha se renovará sola al terminar el periodo, salvo que canceles la renovación "
        "desde Facturación. Te avisaremos 30 días antes."
    )
    rows, plain_rows = _data(
        ("Factura", numero),
        ("Fecha", fecha),
        ("Plan", plan),
        ("Ficha", ficha),
        ("Periodo", _capital(periodo)),
        ("Total", importe),
    )
    mail = _email(
        to,
        subject=f"Tu factura de Fichaitor {numero}",
        preheader=f"{plan} para {ficha}, {periodo}. La factura va adjunta en PDF.",
        headline="Tu factura",
        blocks=[
            _block("parrafo", texto=first),
            *rows,
            _block("boton", etiqueta="Ver Facturación", enlace=enlace),
            _block("parrafo", texto=renews),
        ],
        plain=[first, plain_rows, f"Ver Facturación: {enlace}", renews],
        reason=(
            "Te llega este correo porque se ha cobrado un plan de pago de tu cuenta de Fichaitor."
        ),
    )
    return _attach(mail, numero, pdf)


def _attach(mail: Email, number: str, pdf: bytes | None) -> Email:
    if pdf is None:
        return mail
    return Email(
        mail.to, mail.subject, mail.text, mail.html, (Attachment(pdf_filename(number), pdf),)
    )


# --- 6. renewal notice ---------------------------------------------------------------


def renewal_notice_email(
    to: str, *, plan: str, ficha: str, fecha_renovacion: str, importe: str, enlace: str
) -> Email:
    first = (
        f"El {fecha_renovacion} se renueva por un año más el plan {plan} de {ficha}. "
        f"Cobraremos {importe} con la tarjeta que tienes guardada."
    )
    rows, plain_rows = _data(
        ("Ficha", ficha), ("Plan", plan), ("Renovación", fecha_renovacion), ("Total", importe)
    )
    cancel = (
        "Si quieres seguir, no tienes que hacer nada. Si no, cancela la renovación desde "
        f"Facturación antes del {fecha_renovacion}: una vez cobrada, no se devuelve."
    )
    return _email(
        to,
        subject=f"Tu plan {plan} de {ficha} se renueva el {fecha_renovacion}",
        preheader=(
            f"Cobraremos {importe} con tu tarjeta. Si no quieres renovarlo, cancélalo antes."
        ),
        headline="Se renueva pronto",
        blocks=[
            _block("parrafo", texto=first),
            *rows,
            _block("parrafo", texto=cancel),
            _block("boton", etiqueta="Ir a Facturación", enlace=enlace),
        ],
        plain=[first, plain_rows, cancel, f"Ir a Facturación: {enlace}"],
        reason=RENEWAL_REASON,
    )


def renewal_unassigned_email(
    to: str, *, plan: str, fecha_renovacion: str, importe: str, enlace: str
) -> Email:
    first = (
        f"Tienes una plaza del plan {plan} (un año pagado) que se renueva el "
        f"{fecha_renovacion}, pero ahora no tiene ninguna ficha asignada."
    )
    second = (
        "Si sigue así, no se renovará ni te la cobraremos. Si quieres conservarla, activa una "
        f"ficha en ella antes del {fecha_renovacion}: entonces se renovará por {importe}."
    )
    return _email(
        to,
        subject=f"Una plaza {plan} no se renovará el {fecha_renovacion}",
        preheader="No tiene ninguna ficha, así que no te la cobraremos.",
        headline="Plaza sin ficha",
        blocks=[
            _block("parrafo", texto=first),
            _block("parrafo", texto=second),
            _block("boton", etiqueta="Ir a Facturación", enlace=enlace),
        ],
        plain=[first, second, f"Ir a Facturación: {enlace}"],
        reason=RENEWAL_REASON,
    )


# --- 7. account paused ---------------------------------------------------------------

# account_billing_block.reason -> ${motivo}: lower case, without a full stop.
BLOCK_REASONS = {
    "payment_failed": "la tarjeta ha sido rechazada",
    "action_required": "falta confirmar el pago con tu banco",
    "dispute": "el banco ha devuelto el pago",
    "manual_unpaid": "no se ha recibido el pago",
}


def billing_blocked_email(to: str, *, importe: str, ficha: str, motivo: str, enlace: str) -> Email:
    first = (
        f"Tu cuenta está en pausa porque no hemos podido cobrar {importe} del plan de "
        f"{ficha}: {motivo}."
    )
    during = (
        "Durante la pausa, Fichaitor no lee ninguna de tus fichas, tampoco la del plan Gratis, "
        "no te envía avisos ni aplica cambios. No se ha borrado nada y la pausa no borra nada: "
        "en cuanto se pague, todo vuelve a funcionar."
    )
    meanwhile = (
        "Mientras tanto, en el panel puedes seguir usando Facturación, desactivar fichas, "
        "desvincular chats, desconectar Google o darte de baja."
    )
    return _email(
        to,
        subject="Tu cuenta de Fichaitor está en pausa",
        preheader=(
            f"No hemos podido cobrar {importe}. En cuanto se pague, todo vuelve a funcionar."
        ),
        headline="Cuenta en pausa",
        blocks=[
            _block("parrafo", texto=first),
            _block("boton", etiqueta="Pagar ahora", enlace=enlace),
            _block("parrafo", texto=during),
            _block("parrafo", texto=meanwhile),
        ],
        plain=[first, f"Pagar ahora: {enlace}", during, meanwhile],
        reason=(
            "Te llega este correo porque no se ha podido cobrar un plan de pago de tu cuenta "
            "de Fichaitor."
        ),
    )


# --- 8. payment received -------------------------------------------------------------


def billing_unblocked_email(to: str, *, ficha: str, enlace: str) -> Email:
    first = f"Hemos recibido el pago del plan de {ficha}. Tu cuenta ya no está en pausa."
    second = (
        "Fichaitor vuelve a leer tus fichas y a avisarte cuando haya algo que decidir. La "
        "factura te llega en otro correo."
    )
    return _email(
        to,
        subject="Pago recibido: todo vuelve a funcionar",
        preheader="Fichaitor vuelve a leer tus fichas y a avisarte.",
        headline="Todo en marcha",
        blocks=[
            _block("parrafo", texto=first),
            _block("parrafo", texto=second),
            _block("boton", etiqueta="Entrar en Fichaitor", enlace=enlace),
        ],
        plain=[first, second, f"Entrar en Fichaitor: {enlace}"],
        reason=(
            "Te llega este correo porque se ha pagado un cobro pendiente de tu cuenta de Fichaitor."
        ),
    )


# --- 9. credit note ------------------------------------------------------------------


def credit_note_email(
    to: str, *, numero: str, numero_original: str, importe: str, pdf: bytes | None = None
) -> Email:
    first = (
        f"Hemos emitido una factura rectificativa de la factura {numero_original}, por "
        f"{importe}. Te la adjuntamos en PDF."
    )
    rows, plain_rows = _data(
        ("Rectificativa", numero), ("Rectifica a", numero_original), ("Total", importe)
    )
    back = (
        "Devolvemos el importe a la tarjeta con la que pagaste. Según tu banco, puede tardar "
        "unos días en verse."
    )
    mail = _email(
        to,
        subject=f"Factura rectificativa de Fichaitor {numero}",
        preheader=f"Rectifica la factura {numero_original}. Va adjunta en PDF.",
        headline="Factura rectificativa",
        blocks=[_block("parrafo", texto=first), *rows, _block("parrafo", texto=back)],
        plain=[first, plain_rows, back],
        reason=(
            "Te llega este correo porque hemos devuelto un importe cobrado en tu cuenta de "
            "Fichaitor."
        ),
    )
    return _attach(mail, numero, pdf)


# --- 10. end of the beta -------------------------------------------------------------
# The beta window (owner, 2026-11-13): sent 30 days before the launch; the accounts of
# the beta then have 30 days more, until `fecha_fin` (launch + 30 days), to choose. The
# text changed: brand/correo has to follow it (the interface session lays it out).


def beta_ending_email(
    to: str, *, fecha_lanzamiento: str, fecha_fin: str, oferta: str, enlace: str
) -> Email:
    """`oferta` empty: no offer (decision 9.0, 10: not decided yet), no block for it."""
    first = (
        "Gracias por probar Fichaitor durante la beta. El "
        f"{fecha_lanzamiento} lanzamos la versión 1 y, desde ese día, las fichas de pago se "
        f"cobran. Como estás en la beta, tienes 30 días más, hasta el {fecha_fin}, para "
        "elegir en Facturación el plan de cada ficha. Hasta entonces todo sigue como ahora."
    )
    yearly = (
        "Cada ficha de pago se cobra con tarjeta por años completos y se renueva sola cada "
        f"año, salvo que canceles la renovación. Si eliges un plan antes del {fecha_fin}, "
        "guardamos tu tarjeta pero no cobramos nada hasta ese día. Lo cobrado no se devuelve, "
        "así que elige con calma."
    )
    otherwise = (
        f"Si no eliges plan antes del {fecha_fin}, tu ficha más antigua pasará al plan Gratis "
        "y las demás se desactivarán: dejaremos de leerlas y de avisarte de ellas, y si no las "
        "vuelves a activar en 30 días, borraremos lo que tenemos de ellas. No te cobraremos "
        "nada que no hayas pedido."
    )
    offer = " ".join(oferta.split())
    blocks = [_block("parrafo", texto=first)]
    plain = [first]
    if offer:
        blocks.append(_block("destacado", texto=offer))
        plain.append(offer)
    blocks += [
        _block("boton", etiqueta="Elegir los planes", enlace=enlace),
        _block("parrafo", texto=yearly),
        _block("parrafo", texto=otherwise),
    ]
    plain += [f"Elegir los planes: {enlace}", yearly, otherwise]
    return _email(
        to,
        subject=f"Fichaitor se lanza el {fecha_lanzamiento}: elige tu plan antes del {fecha_fin}",
        preheader="La beta sigue gratis hasta el lanzamiento y 30 días más.",
        headline="Fin de la beta",
        blocks=blocks,
        plain=plain,
        reason="Te llega este correo porque tienes una cuenta de Fichaitor en la beta.",
    )


# --- 11. end of the beta window (trial_ending) --------------------------------------
# New (owner, 2026-11-13): BETA_WINDOW_NOTICE_DAYS before the first charge of a seat a
# beta account bought in its window. Paragraphs and a button only, until the interface
# session lays it out in brand/correo.


def trial_ending_email(
    to: str, *, plan: str, ficha: str, fecha_cobro: str, importe: str, enlace: str
) -> Email:
    first = (
        f"Tu periodo de la beta termina el {fecha_cobro}. Ese día cobraremos {importe} "
        f"(IVA incluido) con la tarjeta que guardaste por el primer año del plan {plan} de "
        f"{ficha}."
    )
    cancel = (
        "Si quieres seguir, no tienes que hacer nada. Si no, cancela la plaza desde "
        f"Facturación antes del {fecha_cobro} y no te cobraremos nada. Una vez cobrado el "
        "año, no se devuelve."
    )
    yearly = (
        "Después, el plan se renueva solo cada año, salvo que canceles la renovación. Te "
        "avisaremos 30 días antes de cada renovación."
    )
    return _email(
        to,
        subject=f"El {fecha_cobro} cobraremos el plan {plan} de {ficha}",
        preheader=f"Primer cobro: {importe}. Si no quieres seguir, cancélalo antes.",
        headline="Termina tu beta",
        blocks=[
            _block("parrafo", texto=first),
            _block("parrafo", texto=cancel),
            _block("boton", etiqueta="Ir a Facturación", enlace=enlace),
            _block("parrafo", texto=yearly),
        ],
        plain=[first, cancel, f"Ir a Facturación: {enlace}", yearly],
        reason=(
            "Te avisamos 7 días antes del primer cobro de una plaza contratada durante la "
            "beta, como dicen las condiciones de uso."
        ),
    )


# --- the payloads of the send_mail jobs ------------------------------------------------
# The billing process formats here what each email says; the worker only puts it in its
# template (app/worker/send_mail.py). A payload carries no link.


def madrid_date(moment: datetime) -> date:
    """The day of `moment` in Spain (peninsula), as the invoice and the emails say it. In
    UTC where the time zone data is not installed."""
    try:
        return moment.astimezone(ZoneInfo("Europe/Madrid")).date()
    except ZoneInfoNotFoundError:
        return moment.astimezone(UTC).date()


def invoice_mail(
    to: str,
    *,
    number: str,
    issued_on: date,
    plan: Plan,
    title: str | None,
    period_start: date,
    period_end: date,
    total: int,
) -> dict:
    return {
        "template": INVOICE_ISSUED,
        "to": to,
        "numero": number,
        "fecha": spanish_day(issued_on),
        "plan": plan_name(plan),
        "ficha": profile_name(title, plan),
        "periodo": period_text(period_start, period_end),
        "importe": euros(total),
    }


def credit_note_mail(to: str, *, number: str, original_number: str, amount: int) -> dict:
    return {
        "template": CREDIT_NOTE_ISSUED,
        "to": to,
        "numero": number,
        "numero_original": original_number,
        "importe": euros(amount, negative=True),
    }


def renewal_mail(
    to: str, *, plan: Plan, title: str | None, assigned: bool, renews_on: date, total: int
) -> dict:
    """`assigned` False: the seat has no active profile and will not renew."""
    payload = {
        "template": RENEWAL_NOTICE if assigned else RENEWAL_NOTICE_UNASSIGNED,
        "to": to,
        "plan": plan_name(plan),
        "fecha_renovacion": spanish_day(renews_on),
        "importe": euros(total),
    }
    if assigned:
        payload["ficha"] = profile_name(title, plan)
    return payload


def blocked_mail(to: str, *, plan: Plan, title: str | None, total: int, reason: str) -> dict:
    return {
        "template": BILLING_BLOCKED,
        "to": to,
        "importe": euros(total),
        "ficha": profile_name(title, plan),
        "motivo": BLOCK_REASONS.get(reason, BLOCK_REASONS["manual_unpaid"]),
    }


def unblocked_mail(to: str, *, plan: Plan, title: str | None) -> dict:
    return {"template": BILLING_UNBLOCKED, "to": to, "ficha": profile_name(title, plan)}


def beta_mail(to: str, *, launch_on: date, ends_on: date, offer: str) -> dict:
    return {
        "template": BETA_ENDING,
        "to": to,
        "fecha_lanzamiento": spanish_day(launch_on),
        "fecha_fin": spanish_day(ends_on),
        "oferta": offer,
    }


def trial_mail(to: str, *, plan: Plan, title: str | None, charge_on: date, total: int) -> dict:
    return {
        "template": TRIAL_ENDING,
        "to": to,
        "plan": plan_name(plan),
        "ficha": profile_name(title, plan),
        "fecha_cobro": spanish_day(charge_on),
        "importe": euros(total),
    }


def with_tax(base_cents: int, percent: int) -> int:
    """A price with its VAT, rounded per line and half up, as Stripe and the invoice."""
    return base_cents + (base_cents * percent + 50) // 100
