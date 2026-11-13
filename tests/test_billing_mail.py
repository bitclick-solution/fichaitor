"""The emails of the charging with the brand templates of brand/correo (LEEME.md, «Correos
del cobro», 5 to 10): the same HTML as the brand preview, byte for byte; the plain
texts; the links made from the configuration only; the PDF attached; and the file name and
sign of the documents the API lists. No database, no network."""

import email
import runpy
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.adapters.mailer.smtp import build_message
from app.api.billing import pdf_name, signed_total
from app.domain import billing_mail as bm
from app.domain.jobs import Job, JobError, PermanentJobError, retry_delay
from app.domain.plans import Plan
from app.ports.mailer import Attachment
from app.worker.send_mail import SendMail, build_email

ROOT = Path(__file__).resolve().parents[1]
TO = "facturas@laparra.es"
FOOTER = (
    "-- \n"
    "¿Dudas? Escríbenos a soporte@fichaitor.es.\n"
    "Fichaitor es un producto de Bitclick Solutions S.L.U. · C/ Cuenca nº 17, 18230 Atarfe "
    "(Granada), España.\n"
)
PDF = b"%PDF-1.7\n% prueba\n%%EOF\n"
BILLING_PREVIEWS = {
    "5-factura",
    "6-renovacion",
    "6-renovacion-sin-ficha",
    "7-pausa",
    "8-pago-recibido",
    "9-rectificativa",
    "10-fin-beta",
    "10-fin-beta-oferta",
    "11-fin-prueba",
}


# The beta window (owner, 2026-11-13) changed the text of `beta_ending`; brand/correo
# (vista.py and LEEME.md) is the interface session's, which lays it out next. Until then
# its HTML is not compared with the preview; its text is checked below.
PENDING_BRAND: set[str] = set()


def plain(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs) + "\n\n" + FOOTER


@pytest.fixture(scope="module")
def vista():
    # vista.py is only read: run without __main__, it writes nothing.
    return runpy.run_path(str(ROOT / "brand" / "correo" / "vista.py"), run_name="vista")


def ours(vista) -> dict:
    ficha, billing = vista["FICHA"], vista["FACTURACION"]
    return {
        "5-factura": bm.invoice_issued_email(
            TO,
            numero="FCH/2027/00042",
            fecha="14 de marzo de 2027",
            plan="Pro",
            ficha=ficha,
            periodo="del 14/03/2027 al 13/03/2028",
            importe="42,35 €",
            enlace=billing,
        ),
        "6-renovacion": bm.renewal_notice_email(
            TO,
            plan="Pro",
            ficha=ficha,
            fecha_renovacion="14 de marzo de 2028",
            importe="42,35 €",
            enlace=billing,
        ),
        "6-renovacion-sin-ficha": bm.renewal_unassigned_email(
            TO,
            plan="Pro",
            fecha_renovacion="14 de marzo de 2028",
            importe="40,23 €",
            enlace=billing,
        ),
        "7-pausa": bm.billing_blocked_email(
            TO,
            importe="42,35 €",
            ficha=ficha,
            motivo="la tarjeta ha sido rechazada",
            enlace="https://fichaitor.es/app#pagar",
        ),
        "8-pago-recibido": bm.billing_unblocked_email(
            TO, ficha=ficha, enlace="https://fichaitor.es/app/"
        ),
        "9-rectificativa": bm.credit_note_email(
            TO, numero="FCH-R/2027/00003", numero_original="FCH/2027/00042", importe="−42,35 €"
        ),
        "10-fin-beta": bm.beta_ending_email(
            TO,
            fecha_lanzamiento="1 de marzo de 2027",
            fecha_fin="31 de marzo de 2027",
            oferta="",
            enlace=billing,
        ),
        "10-fin-beta-oferta": bm.beta_ending_email(
            TO,
            fecha_lanzamiento="1 de marzo de 2027",
            fecha_fin="31 de marzo de 2027",
            oferta=(
                "Texto de ejemplo de la oferta para las cuentas de la beta, todavía sin decidir."
            ),
            enlace=billing,
        ),
        "11-fin-prueba": bm.trial_ending_email(
            TO,
            plan="Pro",
            ficha=ficha,
            fecha_cobro="31 de marzo de 2027",
            importe="42,35 €",
            enlace=billing,
        ),
    }


def test_the_html_is_byte_for_byte_the_one_of_the_brand_preview(vista):
    mails = ours(vista)
    assert set(mails) == BILLING_PREVIEWS
    assert {n for n in vista["CORREOS"] if int(n.split("-")[0]) >= 5} == BILLING_PREVIEWS
    for name, mail in mails.items():
        if name in PENDING_BRAND:
            continue
        assert mail.html == vista["CORREOS"][name], name


def test_the_invoice_email_has_the_exact_plain_text_and_the_pdf():
    mail = bm.invoice_issued_email(
        TO,
        numero="FTR/2027/00001",
        fecha="14 de marzo de 2027",
        plan="Pro+",
        ficha="Bar La Parra",
        periodo="del 14/03/2027 al 13/03/2028",
        importe="71,39 €",
        enlace="https://fichaitor.es/app#facturacion",
        pdf=PDF,
    )
    assert mail.subject == "Tu factura de Fichaitor FTR/2027/00001"
    assert mail.text == plain(
        "TU FACTURA",
        "Hemos cobrado 71,39 € por el plan Pro+ de Bar La Parra. Te adjuntamos la factura en PDF.",
        "Factura: FTR/2027/00001\nFecha: 14 de marzo de 2027\nPlan: Pro+\n"
        "Ficha: Bar La Parra\nPeriodo: Del 14/03/2027 al 13/03/2028\nTotal: 71,39 €",
        "Ver Facturación: https://fichaitor.es/app#facturacion",
        "Esta ficha se renovará sola al terminar el periodo, salvo que canceles la renovación"
        " desde Facturación. Te avisaremos 30 días antes.",
        "Te llega este correo porque se ha cobrado un plan de pago de tu cuenta de Fichaitor.",
    )
    assert [(a.filename, a.content) for a in mail.attachments] == [("FTR-2027-00001.pdf", PDF)]


def test_the_plain_texts_of_the_other_emails():
    blocked = bm.billing_blocked_email(
        TO,
        importe="42,35 €",
        ficha="Bar",
        motivo="falta confirmar el pago con tu banco",
        enlace="L",
    )
    assert blocked.text.startswith(
        "CUENTA EN PAUSA\n\nTu cuenta está en pausa porque no hemos podido cobrar 42,35 € del"
        " plan de Bar: falta confirmar el pago con tu banco.\n\nPagar ahora: L\n\n"
    )
    unassigned = bm.renewal_unassigned_email(
        TO, plan="Pro", fecha_renovacion="1 de abril de 2028", importe="40,23 €", enlace="L"
    )
    assert "Ir a Facturación: L\n\nTe avisamos 30 días antes" in unassigned.text
    dates = {"fecha_lanzamiento": "1 de marzo de 2027", "fecha_fin": "31 de marzo de 2027"}
    offer = bm.beta_ending_email(TO, **dates, oferta="Un 20 %.", enlace="L")
    assert "todo sigue como ahora.\n\nUn 20 %.\n\nElegir los planes: L" in offer.text
    none = bm.beta_ending_email(TO, **dates, oferta="  ", enlace="L")
    assert "ahora.\n\nElegir los planes: L" in none.text and "destacado" not in none.html
    credit = bm.credit_note_email(
        TO, numero="RFTR/2027/00001", numero_original="FTR/2027/00001", importe="−42,35 €"
    )
    assert "Rectificativa: RFTR/2027/00001\nRectifica a: FTR/2027/00001\nTotal: −42,35 €" in (
        credit.text
    )
    assert credit.attachments == ()


def test_the_values_are_formatted_as_the_guide_says():
    assert bm.euros(4235) == "42,35 €" and bm.euros(123456) == "1.234,56 €"
    assert bm.euros(4235, negative=True) == "−42,35 €" and bm.euros(5) == "0,05 €"
    assert bm.spanish_day(date(2027, 3, 14)) == "14 de marzo de 2027"
    assert bm.period_text(date(2027, 3, 14), date(2028, 3, 14)) == "del 14/03/2027 al 13/03/2028"
    assert bm.plan_name(Plan.PRO_WHATSAPP) == "Pro+"
    assert bm.profile_name(None, Plan.PRO) == "tu plaza Pro"
    assert bm.profile_name("Bar\x00 La\nParra", Plan.PRO) == "Bar La Parra"
    assert len(bm.profile_name("x" * 200, Plan.PRO)) == bm.MAX_PROFILE_CHARS
    assert bm.with_tax(3500, 21) == 4235 and bm.with_tax(3325, 21) == 4023
    assert bm.madrid_date(datetime(2027, 3, 13, 23, 30, tzinfo=UTC)) in (
        date(2027, 3, 14),  # with the time zone data
        date(2027, 3, 13),  # without it, in UTC
    )
    assert bm.blocked_mail(TO, plan=Plan.PRO, title=None, total=4235, reason="x")["motivo"] == (
        "no se ha recibido el pago"
    )


# --- the job ---------------------------------------------------------------------------


def test_every_link_comes_from_the_configuration_never_from_the_payload():
    payloads = [
        bm.blocked_mail(TO, plan=Plan.PRO, title="Bar", total=4235, reason="payment_failed"),
        bm.unblocked_mail(TO, plan=Plan.PRO, title="Bar"),
        bm.renewal_mail(
            TO, plan=Plan.PRO, title="Bar", assigned=True, renews_on=date(2028, 3, 14), total=4235
        ),
        bm.renewal_mail(
            TO, plan=Plan.PRO, title=None, assigned=False, renews_on=date(2028, 3, 14), total=4023
        ),
        bm.beta_mail(TO, launch_on=date(2027, 3, 1), ends_on=date(2027, 3, 31), offer=""),
        bm.trial_mail(TO, plan=Plan.PRO, title="Bar", charge_on=date(2027, 3, 31), total=4235),
    ]
    links = {
        "billing_blocked": "https://f.es/app#pagar",
        "billing_unblocked": "https://f.es/app/",
        "renewal_notice": "https://f.es/app#facturacion",
        "renewal_notice_unassigned": "https://f.es/app#facturacion",
        "beta_ending": "https://f.es/app#facturacion",
        "trial_ending": "https://f.es/app#facturacion",
    }
    for payload in payloads:
        evil = {**payload, "url": "https://evil.example/", "enlace": "https://evil.example/"}
        mail = build_email(evil, "https://f.es/")
        assert "evil" not in mail.text + mail.html, payload["template"]
        assert links[payload["template"]] in mail.text
        assert mail.to == TO


def test_a_payload_missing_a_value_or_a_document_without_pdf_fails_for_good():
    good = bm.invoice_mail(
        TO,
        number="FTR/2027/00001",
        issued_on=date(2027, 3, 14),
        plan=Plan.PRO,
        title="Bar",
        period_start=date(2027, 3, 14),
        period_end=date(2028, 3, 14),
        total=4235,
    )
    with pytest.raises(PermanentJobError):
        build_email(good, "https://f.es")  # without its PDF
    assert build_email(good, "https://f.es", PDF).attachments[0].content == PDF
    with pytest.raises(PermanentJobError):
        build_email({**good, "numero": ""}, "https://f.es", PDF)
    with pytest.raises(PermanentJobError):
        build_email({"template": "billing_blocked", "to": TO}, "https://f.es")


class Docs:
    def __init__(self, pdf: bytes | None) -> None:
        self._pdf = pdf
        self.asked: list[str] = []

    async def pdf(self, document_id: str) -> bytes | None:
        self.asked.append(document_id)
        return self._pdf


class Mailer:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, mail) -> None:
        self.sent.append(mail)


def test_the_worker_attaches_the_pdf_it_reads_by_the_document_id():
    import asyncio

    payload = {
        **bm.credit_note_mail(TO, number="RFTR/2027/00001", original_number="F/1", amount=4235),
        "document_id": "0b6f1a52-1111-4a4a-9a9a-000000000001",
    }
    docs, mailer = Docs(PDF), Mailer()
    asyncio.run(SendMail(None, mailer, "https://f.es", docs)(Job("m", "send_mail", payload)))
    assert docs.asked == [payload["document_id"]]
    assert mailer.sent[0].attachments[0].filename == "RFTR-2027-00001.pdf"
    missing = SendMail(None, Mailer(), "https://f.es", Docs(None))
    with pytest.raises(PermanentJobError):
        asyncio.run(missing(Job("m", "send_mail", payload)))


def test_the_smtp_message_keeps_text_and_html_and_carries_the_pdf():
    mail = bm.invoice_issued_email(
        TO,
        numero="FTR/2027/00001",
        fecha="f",
        plan="Pro",
        ficha="Bar",
        periodo="del 1/1/2027 al 31/12/2027",
        importe="1 €",
        enlace="L",
        pdf=PDF,
    )
    message = email.message_from_bytes(
        bytes(build_message(mail, sender="avisos@fichaitor.es", reply_to="soporte@fichaitor.es"))
    )
    assert message.get_content_type() == "multipart/mixed"
    parts = [p.get_content_type() for p in message.walk()]
    assert parts == [
        "multipart/mixed",
        "multipart/alternative",
        "text/plain",
        "text/html",
        "application/pdf",
    ]
    pdf = next(p for p in message.walk() if p.get_content_type() == "application/pdf")
    assert pdf.get_filename() == "FTR-2027-00001.pdf" and pdf.get_payload(decode=True) == PDF
    assert message["Reply-To"] == "soporte@fichaitor.es"


@pytest.mark.parametrize("name", ["", "a/b.pdf", 'x".pdf', "a\nb.pdf"])
def test_an_attachment_with_a_bad_name_is_refused(name):
    with pytest.raises(ValueError):
        Attachment(name, PDF)


# --- the invoices of the API -------------------------------------------------------------


def test_the_pdf_is_named_after_its_number_and_a_credit_note_is_negative():
    assert pdf_name("FTR/2027/00001") == "FTR_2027_00001.pdf"
    assert pdf_name('RFTR/2027/00002"; x=../') == "RFTR_2027_00002x_.pdf"
    assert pdf_name(None) == "factura.pdf"
    assert signed_total("rectificativa", 4235) == -4235
    assert signed_total("factura", 4235) == 4235 and signed_total("factura", None) is None


def test_a_job_error_may_ask_for_its_own_wait():
    job = Job("j", "issue_invoice", {}, attempts=3, max_attempts=40)
    assert retry_delay(job, JobError("x", retry_in=7200)) == 7200
    assert retry_delay(job, JobError("x")) == 120
    last = Job("j", "issue_invoice", {}, attempts=40, max_attempts=40)
    assert retry_delay(last, JobError("x", retry_in=7200)) is None


def test_the_billing_process_refuses_a_fake_odoo_in_live_mode_or_a_wrong_odoo(monkeypatch):
    from app.billing.config import BillingConfigError, BillingSettings

    base = {"_env_file": None, "billing_database_url": "postgresql://x/y", "fake_stripe": True}
    BillingSettings(**base, fake_odoo=True).check()
    monkeypatch.setenv("FICHAITOR_ODOO_URL", "http://odoo.example")
    with pytest.raises(BillingConfigError) as error:
        BillingSettings(**base).check()
    assert "FICHAITOR_ODOO" in str(error.value)
    monkeypatch.delenv("FICHAITOR_ODOO_URL")
    BillingSettings(**base).check()  # without Odoo: invoices wait, pending


# --- the beta window (owner, 2026-11-13) ---------------------------------------------


def test_beta_ending_announces_the_launch_and_the_30_days_more():
    mail = bm.beta_ending_email(
        TO,
        fecha_lanzamiento="1 de marzo de 2027",
        fecha_fin="31 de marzo de 2027",
        oferta="",
        enlace="https://fichaitor.es/app#facturacion",
    )
    assert mail.subject == (
        "Fichaitor se lanza el 1 de marzo de 2027: elige tu plan antes del 31 de marzo de 2027"
    )
    assert mail.text == plain(
        "FIN DE LA BETA",
        "Gracias por probar Fichaitor durante la beta. El 1 de marzo de 2027 lanzamos la"
        " versión 1 y, desde ese día, las fichas de pago se cobran. Como estás en la beta,"
        " tienes 30 días más, hasta el 31 de marzo de 2027, para elegir en Facturación el plan"
        " de cada ficha. Hasta entonces todo sigue como ahora.",
        "Elegir los planes: https://fichaitor.es/app#facturacion",
        "Cada ficha de pago se cobra con tarjeta por años completos y se renueva sola cada año,"
        " salvo que canceles la renovación. Si eliges un plan antes del 31 de marzo de 2027,"
        " guardamos tu tarjeta pero no cobramos nada hasta ese día. Lo cobrado no se devuelve,"
        " así que elige con calma.",
        "Si no eliges plan antes del 31 de marzo de 2027, tu ficha más antigua pasará al plan"
        " Gratis y las demás se desactivarán: dejaremos de leerlas y de avisarte de ellas, y si"
        " no las vuelves a activar en 30 días, borraremos lo que tenemos de ellas. No te"
        " cobraremos nada que no hayas pedido.",
        "Te llega este correo porque tienes una cuenta de Fichaitor en la beta.",
    )


def test_trial_ending_says_the_price_the_date_and_how_to_cancel():
    payload = bm.trial_mail(
        TO, plan=Plan.PRO_WHATSAPP, title="Bar La Parra", charge_on=date(2027, 3, 31), total=7139
    )
    assert payload == {
        "template": "trial_ending",
        "to": TO,
        "plan": "Pro+",
        "ficha": "Bar La Parra",
        "fecha_cobro": "31 de marzo de 2027",
        "importe": "71,39 €",
    }
    mail = build_email(payload, "https://fichaitor.es")
    assert mail.subject == "El 31 de marzo de 2027 cobraremos el plan Pro+ de Bar La Parra"
    assert mail.text == plain(
        "TERMINA TU BETA",
        "Tu periodo de la beta termina el 31 de marzo de 2027. Ese día cobraremos 71,39 €"
        " (IVA incluido) con la tarjeta que guardaste por el primer año del plan Pro+ de Bar"
        " La Parra.",
        "Si quieres seguir, no tienes que hacer nada. Si no, cancela la plaza desde"
        " Facturación antes del 31 de marzo de 2027 y no te cobraremos nada. Una vez cobrado"
        " el año, no se devuelve.",
        "Ir a Facturación: https://fichaitor.es/app#facturacion",
        "Después, el plan se renueva solo cada año, salvo que canceles la renovación. Te"
        " avisaremos 30 días antes de cada renovación.",
        "Te avisamos 7 días antes del primer cobro de una plaza contratada durante la beta,"
        " como dicen las condiciones de uso.",
    )
    with pytest.raises(PermanentJobError):
        build_email({**payload, "importe": ""}, "https://fichaitor.es")
