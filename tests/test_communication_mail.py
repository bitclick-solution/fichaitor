"""Communications by email from the backoffice, without a database: the template
`comunicacion` (brand/correo/LEEME.md, 12), byte for byte against brand/correo/vista.py;
the rules of the text; the signed opt-out token; the List-Unsubscribe headers; the job
send_mail of a communication; and the public opt-out routes on the in-memory store.

The database side (audience, roles, TOTP, audit, retention) is in
tests/test_communication_postgres.py.
"""

import asyncio
import base64
import runpy
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.adapters.mailer.fake import FakeMailer, redact
from app.adapters.mailer.smtp import build_message
from app.adapters.places.fake import FakePlaces
from app.adapters.store.memory import MemoryStore
from app.api.main import create_app
from app.config import Settings
from app.domain import communication, unsubscribe
from app.domain.communication import CommunicationInvalid, Content, communication_email, validate
from app.domain.jobs import Job, JobError, PermanentJobError
from app.mail_config import MailSettings
from app.ports.mailer import Email, MailerError, MailRejected
from app.worker.send_mail import SendMail

ROOT = Path(__file__).resolve().parent.parent
SECRET = "secreto-de-baja-de-pruebas-con-mas-de-32-caracteres"
BASE = "https://fichaitor.es"
ACCOUNT = "2f1c6a4e-3b7d-4c39-9a51-0d4e8b6f7a21"

SERVICE = Content(
    kind="servicio",
    subject="Cambian las condiciones de uso de Fichaitor el 1 de febrero de 2027",
    preheader="Qué cambia y qué puedes hacer si no estás de acuerdo.",
    headline="Cambian las condiciones",
    paragraphs=(
        "El 1 de febrero de 2027 cambian las condiciones de uso de Fichaitor. Te contamos qué "
        "cambia antes de que entre en vigor.",
        "Explicamos mejor cuándo borramos lo que tenemos de una ficha desactivada y cómo pedir "
        "la factura a nombre de tu empresa. Los precios y lo que incluye cada plan no cambian.",
        "Si no estás de acuerdo, puedes darte de baja desde Ajustes antes de esa fecha, sin "
        "ningún coste. Si sigues usando Fichaitor después, se aplicarán las condiciones nuevas.",
    ),
    button_label="Leer las condiciones",
    button_url="https://fichaitor.es/condiciones-de-uso/",
)

COMMERCIAL = Content(
    kind="comercial",
    subject="Novedad en Fichaitor: contesta tus reseñas desde WhatsApp",
    preheader="Te avisamos de cada reseña nueva y la respondes desde el mismo chat.",
    headline="Reseñas por WhatsApp",
    paragraphs=(
        "Con el plan Pro+, Fichaitor te avisa por WhatsApp cuando entra una reseña nueva en tu "
        "ficha. Te propone una respuesta y la publicas desde el mismo chat, sin abrir el panel.",
        "Puedes pasar a Pro+ cualquier ficha desde Facturación. Nada se publica en tu ficha sin "
        "que lo confirmes.",
    ),
    button_label="Ver el plan Pro+",
    button_url="https://fichaitor.es/app#facturacion",
)

PLAIN_FOOTER = (
    "-- \n"
    "¿Dudas? Escríbenos a soporte@fichaitor.es.\n"
    "Fichaitor es un producto de Bitclick Solutions S.L.U. · C/ Cuenca nº 17, 18230 Atarfe "
    "(Granada), España.\n"
)


@pytest.fixture(scope="module")
def vista():
    # vista.py is only read: run without __main__, it writes nothing.
    return runpy.run_path(str(ROOT / "brand" / "correo" / "vista.py"), run_name="vista")


# --- the template ------------------------------------------------------------------------


def test_the_service_html_is_byte_for_byte_the_one_of_the_brand_preview(vista):
    mail = communication_email("dueno@example.com", SERVICE)
    assert mail.html == vista["CORREOS"]["12-comunicacion-servicio"]


def test_the_commercial_html_is_byte_for_byte_the_one_of_the_brand_preview(vista):
    assert COMMERCIAL.button_url == vista["FACTURACION"]
    mail = communication_email("dueno@example.com", COMMERCIAL, vista["BAJA"])
    assert mail.html == vista["CORREOS"]["12-comunicacion-comercial"]


def test_the_motives_are_the_ones_of_the_brand(vista):
    assert communication.MOTIVE["servicio"] == vista["MOTIVO_SERVICIO"]
    assert communication.MOTIVE["comercial"] == vista["MOTIVO_COMERCIAL"]


def test_the_service_plain_text_has_the_button_after_the_first_paragraph_and_no_opt_out():
    mail = communication_email("dueno@example.com", SERVICE)
    p1, p2, p3 = SERVICE.paragraphs
    assert mail.text == (
        f"CAMBIAN LAS CONDICIONES\n\n{p1}\n\n"
        "Leer las condiciones: https://fichaitor.es/condiciones-de-uso/\n\n"
        f"{p2}\n\n{p3}\n\n"
        "Te escribimos porque tienes una cuenta en Fichaitor y esto afecta a tu servicio.\n\n"
        + PLAIN_FOOTER
    )
    assert "baja-comunicaciones" not in mail.html
    assert mail.headers == ()
    assert mail.subject == SERVICE.subject


def test_the_commercial_plain_text_ends_with_the_opt_out_line_right_under_the_motive():
    link = unsubscribe.page_url(BASE, "tok")
    mail = communication_email("dueno@example.com", COMMERCIAL, link)
    p1, p2 = COMMERCIAL.paragraphs
    assert mail.text == (
        f"RESEÑAS POR WHATSAPP\n\n{p1}\n\n"
        "Ver el plan Pro+: https://fichaitor.es/app#facturacion\n\n"
        f"{p2}\n\n"
        "Te escribimos porque eres cliente de Fichaitor.\n"
        "Darme de baja de estas comunicaciones: https://fichaitor.es/baja-comunicaciones#tok\n\n"
        + PLAIN_FOOTER
    )


def test_without_a_button_neither_the_block_nor_its_plain_lines_are_there():
    content = Content("servicio", "Asunto", "Pre", "Titular", ("Uno.", "Dos."))
    mail = communication_email("dueno@example.com", content)
    assert "&rarr;" not in mail.html
    assert mail.text.startswith("TITULAR\n\nUno.\n\nDos.\n\nTe escribimos porque")


def test_every_value_written_by_the_person_is_escaped_in_the_html_and_left_alone_in_the_text():
    evil = "<script>alert(\"x\")</script> & 'y'"
    content = Content(
        "comercial",
        evil,
        evil,
        "Hola <b>",
        (evil, f"segundo {evil}"),
        button_label=evil[:30],
        button_url="https://fichaitor.es/app?a=1&b=2",
    )
    mail = communication_email("dueno@example.com", content, unsubscribe.page_url(BASE, "t"))
    assert "<script>" not in mail.html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#x27;y&#x27;" in mail.html
    assert "Hola &lt;b&gt;" in mail.html
    assert 'href="https://fichaitor.es/app?a=1&amp;b=2"' in mail.html
    assert evil in mail.text


def test_a_commercial_one_needs_its_link_and_a_service_one_never_carries_it():
    with pytest.raises(ValueError):
        communication_email("dueno@example.com", COMMERCIAL)
    with pytest.raises(ValueError):
        communication_email("dueno@example.com", SERVICE, unsubscribe.page_url(BASE, "t"))


# --- the rules of the text ---------------------------------------------------------------


def test_a_valid_content_is_trimmed():
    content = validate(
        Content(
            "servicio",
            "  Asunto ",
            " Pre ",
            " Titular ",
            (" Uno. ",),
            " Ir ",
            " https://fichaitor.es/x ",
        )
    )
    assert (content.subject, content.headline, content.paragraphs) == (
        "Asunto",
        "Titular",
        ("Uno.",),
    )
    assert (content.button_label, content.button_url) == ("Ir", "https://fichaitor.es/x")


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"kind": "promo"}, "kind"),
        ({"subject": ""}, "subject"),
        ({"subject": "a" * 91}, "subject"),
        ({"subject": "dos\nlíneas"}, "subject"),
        ({"preheader": "a" * 121}, "preheader"),
        ({"headline": "a" * 41}, "headline"),
        ({"headline": "Con punto."}, "headline"),
        ({"paragraphs": ()}, "paragraphs"),
        ({"paragraphs": ("a",) * 9}, "paragraphs"),
        ({"paragraphs": ("a" * 1001,)}, "paragraphs"),
        ({"paragraphs": ("uno", "  ")}, "paragraphs"),
        ({"paragraphs": ("tab\tdentro",)}, "paragraphs"),
        ({"button_label": "a" * 31}, "button_label"),
        ({"button_label": None}, "button_url"),
        ({"button_url": None}, "button_url"),
    ],
)
def test_the_limits_of_the_brand_are_enforced(change, field):
    base = {
        "kind": "comercial",
        "subject": "Asunto",
        "preheader": "Pre",
        "headline": "Titular",
        "paragraphs": ("Uno.",),
        "button_label": "Ir",
        "button_url": "https://fichaitor.es/app",
    }
    with pytest.raises(CommunicationInvalid) as error:
        validate(Content(**{**base, **change}))
    assert error.value.field == field


def test_a_line_break_inside_a_paragraph_is_allowed_and_not_turned_into_br():
    content = validate(Content("servicio", "A", "B", "C", ("uno\ndos",)))
    mail = communication_email("dueno@example.com", content)
    assert "uno\ndos" in mail.html and "<br>uno" not in mail.html


@pytest.mark.parametrize(
    "url",
    [
        "https://fichaitor.es/",
        "https://fichaitor.es/app#facturacion",
        "https://fichaitor.es/condiciones-de-uso/?utm_source=correo",
    ],
)
def test_the_button_may_point_to_fichaitor(url):
    assert communication.check_button_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://fichaitor.es/",
        "https://fichaitor.es",
        "https://fichaitor.es.evil.com/",
        "https://evil.com/fichaitor.es/",
        "https://user@fichaitor.es/",
        "https://fichaitor.es:8443/",
        "https://www.fichaitor.es/",
        "javascript:alert(1)//https://fichaitor.es/",
        'https://fichaitor.es/" onmouseover="x',
        "https://fichaitor.es/a b",
        "https://fichaitor.es/<x>",
        "https://fichaitor.es/\\evil",
        "//fichaitor.es/",
        "https://fichaitor.es/" + "a" * 500,
    ],
)
def test_the_button_may_point_nowhere_else(url):
    with pytest.raises(CommunicationInvalid):
        communication.check_button_url(url)


# --- the opt-out token -------------------------------------------------------------------


def test_the_token_is_signed_for_one_account_and_verified():
    token = unsubscribe.sign(SECRET, ACCOUNT)
    assert len(token) == 64 and set(token) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    assert unsubscribe.verify(SECRET, token) == ACCOUNT
    assert unsubscribe.sign(SECRET, ACCOUNT) == token  # one per account, no expiry


def test_a_token_of_another_secret_purpose_or_account_is_not_valid():
    token = unsubscribe.sign(SECRET, ACCOUNT)
    assert unsubscribe.verify("otro-secreto-de-mas-de-32-caracteres-xx", token) is None
    raw = bytearray(base64.urlsafe_b64decode(token + "=="))
    raw[0] ^= 1  # another account with the same signature
    forged = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    assert unsubscribe.verify(SECRET, forged) is None
    # The same HMAC without the purpose label (what another use of the secret would make).
    import hashlib
    import hmac

    account = uuid.UUID(ACCOUNT).bytes
    other = account + hmac.new(SECRET.encode(), account, hashlib.sha256).digest()
    assert unsubscribe.verify(SECRET, base64.urlsafe_b64encode(other).decode()) is None


@pytest.mark.parametrize("token", ["", "prueba", "x" * 201, "!!!", "a" * 64, None, 5])
def test_malformed_tokens_are_not_valid(token):
    assert unsubscribe.verify(SECRET, token) is None


def test_without_a_secret_nothing_is_signed_nor_valid():
    with pytest.raises(ValueError):
        unsubscribe.sign("", ACCOUNT)
    assert unsubscribe.verify("", unsubscribe.sign(SECRET, ACCOUNT)) is None


def test_the_links_come_from_the_configuration_with_the_token_in_the_fragment():
    assert unsubscribe.page_url("https://fichaitor.es/", "t") == (
        "https://fichaitor.es/baja-comunicaciones#t"
    )
    assert unsubscribe.one_click_url("https://fichaitor.es", "t") == (
        "https://fichaitor.es/api/v1/public/unsubscribe/one-click?token=t"
    )


def test_the_list_unsubscribe_headers_follow_rfc_8058():
    assert unsubscribe.headers(BASE, "t") == (
        ("List-Unsubscribe", "<https://fichaitor.es/api/v1/public/unsubscribe/one-click?token=t>"),
        ("List-Unsubscribe-Post", "List-Unsubscribe=One-Click"),
    )


def test_the_smtp_message_carries_the_opt_out_headers():
    email = Email("dueno@example.com", "A", "t", "<p>h</p>", headers=unsubscribe.headers(BASE, "t"))
    message = build_message(email, sender="avisos@fichaitor.es", reply_to="soporte@fichaitor.es")
    assert message["List-Unsubscribe"] == (
        "<https://fichaitor.es/api/v1/public/unsubscribe/one-click?token=t>"
    )
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert message["Reply-To"] == "soporte@fichaitor.es"


@pytest.mark.parametrize(
    "headers",
    [
        (("Bcc", "otro@example.com"),),
        (("From", "x@example.com"),),
        (("List-Unsubscribe", "<https://x>\r\nBcc: otro@example.com"),),
        (("List-Unsubscribe", "a\nb"),),
    ],
)
def test_an_email_refuses_other_headers_and_header_injection(headers):
    with pytest.raises(ValueError):
        Email("dueno@example.com", "A", "t", "h", headers=headers)


def test_the_fake_mailer_never_writes_the_token():
    token = unsubscribe.sign(SECRET, ACCOUNT)
    assert token not in redact(unsubscribe.page_url(BASE, token))


# --- the job send_mail of a communication --------------------------------------------------


class FakeCommunications:
    def __init__(self, content: Content, deliver: bool = True) -> None:
        self.row = {
            "kind": content.kind,
            "subject": content.subject,
            "preheader": content.preheader,
            "headline": content.headline,
            "paragraphs": list(content.paragraphs),
            "button_label": content.button_label,
            "button_url": content.button_url,
            "deliver": deliver,
        }
        self.asked: list[tuple[str, str | None]] = []
        self.results: list[tuple[str, str]] = []

    async def mail(self, communication_id, account_id):
        self.asked.append((communication_id, account_id))
        return self.row

    async def result(self, communication_id, outcome):
        self.results.append((communication_id, outcome))


class FakeJobs:
    def __init__(self) -> None:
        self.scrubbed: list[str] = []

    async def scrub_job(self, job_id):
        self.scrubbed.append(job_id)

    async def scrub_finished_jobs(self, kind):
        return 0

    async def set_code_mail(self, code_id, state):
        raise AssertionError("una comunicación no tiene código")


CID = "9d0c2c5e-5a4e-4d6b-8f7e-111111111111"


def job(test: bool = False, **extra) -> Job:
    payload = {"template": "comunicacion", "to": "dueno@example.com", "communication_id": CID}
    payload.update({"test": True} if test else {"account_id": ACCOUNT})
    payload.update(extra)
    return Job("job-1", "send_mail", payload)


def handler(content, *, deliver=True, mailer=None, secret=SECRET):
    comms = FakeCommunications(content, deliver)
    jobs = FakeJobs()
    mailer = mailer or FakeMailer()
    return (
        SendMail(jobs, mailer, BASE, communications=comms, unsubscribe_secret=secret),
        comms,
        jobs,
        mailer,
    )


def test_a_commercial_email_carries_its_signed_link_and_headers_and_is_counted():
    send, comms, jobs, mailer = handler(COMMERCIAL)
    asyncio.run(send(job()))
    (mail,) = mailer.sent
    token = unsubscribe.sign(SECRET, ACCOUNT)
    assert f"https://fichaitor.es/baja-comunicaciones#{token}" in mail.html
    assert (
        f"Darme de baja de estas comunicaciones: https://fichaitor.es/baja-comunicaciones#{token}"
        in mail.text
    )
    assert mail.headers == unsubscribe.headers(BASE, token)
    assert mail.subject == COMMERCIAL.subject
    assert comms.asked == [(CID, ACCOUNT)]
    assert comms.results == [(CID, "sent")]
    assert jobs.scrubbed == ["job-1"]  # the address leaves the payload once sent


def test_a_service_email_has_no_opt_out_link_nor_headers():
    send, comms, _, mailer = handler(SERVICE, secret="")
    asyncio.run(send(job()))
    (mail,) = mailer.sent
    assert mail.headers == () and "baja-comunicaciones" not in mail.html
    assert comms.results == [(CID, "sent")]


def test_an_account_that_opted_out_after_the_confirmation_gets_nothing():
    send, comms, jobs, mailer = handler(COMMERCIAL, deliver=False)
    asyncio.run(send(job()))
    assert mailer.sent == []
    assert comms.results == [(CID, "skipped")]
    assert jobs.scrubbed == ["job-1"]


def test_the_test_email_is_marked_opts_out_nobody_and_is_not_counted():
    send, comms, jobs, mailer = handler(COMMERCIAL)
    asyncio.run(send(job(test=True)))
    (mail,) = mailer.sent
    assert mail.subject == f"[Prueba] {COMMERCIAL.subject}"
    assert "baja-comunicaciones#prueba" in mail.html
    assert mail.headers == unsubscribe.headers(BASE, unsubscribe.TEST_TOKEN)
    assert comms.asked == [(CID, None)]
    assert comms.results == []
    assert jobs.scrubbed == ["job-1"]


def test_no_commercial_email_goes_out_without_the_secret_of_the_links():
    send, comms, jobs, mailer = handler(COMMERCIAL, secret="")
    with pytest.raises(PermanentJobError):
        asyncio.run(send(job()))
    assert mailer.sent == []
    assert comms.results == [(CID, "failed")]
    assert jobs.scrubbed == ["job-1"]


def test_a_refused_email_counts_as_failed_and_a_busy_server_is_retried_without_counting():
    send, comms, jobs, _ = handler(COMMERCIAL, mailer=FakeMailer(fail=MailRejected))
    with pytest.raises(PermanentJobError):
        asyncio.run(send(job()))
    assert comms.results == [(CID, "failed")] and jobs.scrubbed == ["job-1"]

    send, comms, jobs, _ = handler(COMMERCIAL, mailer=FakeMailer(fail=MailerError))
    with pytest.raises(JobError) as error:
        asyncio.run(send(job()))
    assert not isinstance(error.value, PermanentJobError)
    assert comms.results == [] and jobs.scrubbed == []  # the payload stays for the retry
    asyncio.run(send.on_exhausted(job()))
    assert comms.results == [(CID, "failed")] and jobs.scrubbed == ["job-1"]


@pytest.mark.parametrize(
    "payload",
    [
        {"template": "comunicacion", "to": "dueno@example.com", "communication_id": CID},
        {"template": "comunicacion", "communication_id": CID, "account_id": ACCOUNT},
        {"template": "comunicacion", "to": "dueno@example.com", "account_id": ACCOUNT},
    ],
)
def test_a_payload_without_what_it_needs_fails_for_good(payload):
    send, _, jobs, mailer = handler(SERVICE)
    with pytest.raises(PermanentJobError):
        asyncio.run(send(Job("job-1", "send_mail", payload)))
    assert mailer.sent == [] and jobs.scrubbed == ["job-1"]


# --- the public opt-out routes (in memory) ---------------------------------------------------


def app_with(store: MemoryStore, secret: str = SECRET, **limits):
    settings = Settings(fake_places=True, **limits)
    return create_app(
        settings,
        places=FakePlaces(),
        store=store,
        mail_settings=MailSettings(unsubscribe_secret=secret),
    )


def store_with_account() -> tuple[MemoryStore, str]:
    store = MemoryStore()
    account = asyncio.run(store.upsert_account("sub-baja", "dueno@example.com"))
    return store, account


def test_a_valid_token_opts_the_account_out_once_and_again_answers_the_same():
    store, account = store_with_account()
    token = unsubscribe.sign(SECRET, account)
    with TestClient(app_with(store)) as client:
        first = client.post("/api/v1/public/unsubscribe", json={"token": token})
        again = client.post("/api/v1/public/unsubscribe", json={"token": token})
    assert first.status_code == again.status_code == 204
    assert store.communication_optout == {account: "enlace"}


@pytest.mark.parametrize(
    "token",
    ["", "prueba", "x" * 5000, unsubscribe.sign("otro-secreto-xxxxxxxxxxxxxxxxxxxxxxxxx", ACCOUNT)],
)
def test_an_invalid_token_gets_the_same_answer_and_changes_nothing(token):
    store, account = store_with_account()
    with TestClient(app_with(store)) as client:
        page = client.post("/api/v1/public/unsubscribe", json={"token": token})
        click = client.post(
            "/api/v1/public/unsubscribe/one-click",
            params={"token": token},
            content="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert page.status_code == click.status_code == 204
    assert page.content == click.content == b""
    assert store.communication_optout == {}


def test_a_token_of_an_account_that_does_not_exist_gets_the_same_answer():
    store, _ = store_with_account()
    with TestClient(app_with(store)) as client:
        reply = client.post(
            "/api/v1/public/unsubscribe", json={"token": unsubscribe.sign(SECRET, ACCOUNT)}
        )
    assert reply.status_code == 204
    assert store.communication_optout == {}


def test_the_one_click_route_takes_the_post_of_the_mail_client_without_session_nor_csrf():
    store, account = store_with_account()
    token = unsubscribe.sign(SECRET, account)
    url = unsubscribe.one_click_url("http://testserver", token)
    with TestClient(app_with(store)) as client:
        assert client.get(url).status_code == 405  # a link scanner opts nobody out
        assert store.communication_optout == {}
        reply = client.post(
            url,
            content="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert reply.status_code == 204
    assert store.communication_optout == {account: "un_clic"}


def test_both_routes_have_a_limit_per_address():
    store, account = store_with_account()
    token = unsubscribe.sign(SECRET, account)
    app = app_with(store, unsubscribe_rate_limit=2, unsubscribe_one_click_rate_limit=3)
    with TestClient(app) as client:
        pages = [client.post("/api/v1/public/unsubscribe", json={"token": token}) for _ in range(3)]
        clicks = [
            client.post("/api/v1/public/unsubscribe/one-click", params={"token": token})
            for _ in range(4)
        ]
    assert [r.status_code for r in pages] == [204, 204, 429]
    assert [r.status_code for r in clicks] == [204, 204, 204, 429]


def test_without_the_secret_the_routes_say_they_are_not_available():
    store, account = store_with_account()
    with TestClient(app_with(store, secret="")) as client:
        reply = client.post("/api/v1/public/unsubscribe", json={"token": "x"})
    assert reply.status_code == 503
    assert store.communication_optout == {}
