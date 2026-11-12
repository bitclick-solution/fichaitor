"""The emails of a leave with the brand templates of brand/correo (LEEME.md): exact
texts, escaping, the restore link, the headers and the templates that reach the image.
No database, no network."""

import runpy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import mail_templates
from app.adapters.mailer.smtp import build_message
from app.domain import offboarding_mail
from app.domain.jobs import PermanentJobError
from app.domain.offboarding_mail import (
    app_url,
    code_email,
    restore_url,
    restored_email,
    scheduled_email,
    spanish_date,
)
from app.mail_config import MailSettings
from app.worker.send_mail import build_email

ROOT = Path(__file__).resolve().parents[1]
ADDRESS = "dueno@example.com"
CODE = "042137"
DELETE_AT = datetime(2026, 11, 14, 11, 16, tzinfo=UTC)
# "14 de noviembre de 2026 a las 12:16 (hora peninsular)"; in UTC where tzdata is missing.
WHEN = spanish_date(DELETE_AT)
URL = "https://fichaitor.es/app#restaurar=TOKEN"

FOOTER = (
    "-- \n"  # the standard signature separator, with its space
    "¿Dudas? Escríbenos a soporte@fichaitor.es.\n"
    "Fichaitor es un producto de Bitclick Solutions S.L.U. · C/ Cuenca nº 17, 18230 Atarfe "
    "(Granada), España.\n"
)


def plain(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs) + "\n\n" + FOOTER


# --- the templates reach the image ------------------------------------------------------


@pytest.mark.parametrize("name", ["base.html", "bloques.html"])
def test_the_packaged_templates_are_identical_to_those_of_brand(name):
    packaged = (ROOT / "app" / "mail_templates" / name).read_bytes()
    assert packaged == (ROOT / "brand" / "correo" / name).read_bytes(), (
        f"app/mail_templates/{name} no es igual que brand/correo/{name}: cópialo de nuevo"
    )


def test_the_templates_have_the_placeholders_the_backend_fills():
    assert set(mail_templates.BASE.get_identifiers()) == {
        "asunto",
        "preencabezado",
        "titular",
        "contenido",
        "motivo",
    }
    assert {k: set(t.get_identifiers()) for k, t in mail_templates.BLOCKS.items()} == {
        "parrafo": {"texto"},
        "codigo": {"codigo"},
        "boton": {"enlace", "etiqueta"},
        "aviso": {"texto"},
    }


def test_the_dockerfile_copies_the_package_that_holds_the_templates():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY app ./app" in dockerfile
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert not any(p in dockerignore for p in ("*.html", "app/mail_templates"))


# --- 1. the code -----------------------------------------------------------------------


def test_the_code_email_has_the_exact_texts_of_the_brand():
    mail = code_email(ADDRESS, "ficha", CODE, 10)
    assert mail.to == ADDRESS
    assert mail.subject == "Tu código de confirmación de Fichaitor"
    assert mail.text == plain(
        "TU CÓDIGO",
        "Has pedido desconectar tu ficha. Escribe este código en Fichaitor para confirmarlo:",
        f"    {CODE}",
        "Caduca en 10 minutos y solo sirve una vez.",
        "Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu sesión:"
        " escríbenos a soporte@fichaitor.es.",
        "Te llega este correo porque alguien ha pedido desconectar tu ficha desde tu cuenta de"
        " Fichaitor.",
    )
    assert "\n\n-- \n¿Dudas?" in mail.text  # the separator keeps its trailing space
    html = mail.html
    assert "<title>Tu código de confirmación de Fichaitor</title>" in html
    assert ">Lo necesitas para desconectar tu ficha. Caduca en 10 minutos.</div>" in html
    assert '>Tu código<span style="color:#ff3b30;">.</span></h1>' in html
    assert f'letter-spacing:8px;color:#0b0b0b;">{CODE}</td>' in html
    for fragment in (
        "Has pedido desconectar tu ficha. Escribe este código en Fichaitor para confirmarlo:",
        "Caduca en 10 minutos y solo sirve una vez.",
        "Si no lo has pedido tú, no se hará ningún cambio, pero alguien tiene abierta tu sesión:"
        " escríbenos a soporte@fichaitor.es.",
        "Te llega este correo porque alguien ha pedido desconectar tu ficha desde tu cuenta de"
        " Fichaitor.",
    ):
        assert fragment in html
    # In the order of the table: paragraph, code, paragraph, notice.
    order = [
        html.index("Escribe este código"),
        html.index(f">{CODE}<"),
        html.index("solo sirve una vez"),
        html.index("alguien tiene abierta tu sesión"),
    ]
    assert order == sorted(order)
    assert "${" not in html


def test_the_code_email_for_the_account_says_so():
    mail = code_email(ADDRESS, "cuenta", CODE, 10)
    assert "Has pedido borrar tu cuenta de Fichaitor. Escribe este código" in mail.text
    assert "Lo necesitas para borrar tu cuenta de Fichaitor. Caduca en 10 minutos." in mail.html
    assert "alguien ha pedido borrar tu cuenta de Fichaitor desde tu cuenta" in mail.text


def test_the_term_of_the_code_says_its_unit_in_singular_or_plural():
    one = code_email(ADDRESS, "ficha", CODE, 1)
    assert ">Lo necesitas para desconectar tu ficha. Caduca en 1 minuto.</div>" in one.html
    assert "Caduca en 1 minuto y solo sirve una vez." in one.text
    assert "Caduca en 1 minuto y solo sirve una vez." in one.html
    assert "1 minutos" not in one.text + one.html
    many = code_email(ADDRESS, "ficha", CODE, 15)
    assert "Caduca en 15 minutos." in many.html and "Caduca en 15 minutos y" in many.text


@pytest.mark.parametrize("scope", ["ficha", "cuenta"])
def test_the_code_is_never_in_the_subject_nor_in_the_preheader(scope):
    mail = code_email(ADDRESS, scope, CODE, 10)
    preheader = mail.html.split('line-height:1px;">', 1)[1].split("</div>", 1)[0]
    assert CODE not in mail.subject and CODE not in preheader
    assert preheader.startswith("Lo necesitas para ")


# --- 2. the leave is scheduled ---------------------------------------------------------


def test_the_scheduled_email_for_the_profile_has_the_exact_texts():
    assert WHEN.startswith("14 de noviembre de 2026 a las ")
    mail = scheduled_email(ADDRESS, "ficha", DELETE_AT, URL)
    assert mail.subject == "Tu ficha se desconectará de Fichaitor pronto"
    assert mail.text == plain(
        "DESCONEXIÓN EN MARCHA",
        "Has pedido desconectar tu ficha de Fichaitor. Desde ahora no la leemos ni te enviamos"
        " avisos.",
        f"Restaurar: {URL}",
        "Si no has sido tú, abre el enlace. No se ha borrado nada.",
        f"Si no haces nada, el {WHEN} retiraremos el acceso a tu cuenta de Google. Lo obtenido de"
        " Google se borra en un máximo de 30 días, y el registro de las respuestas que publicaste,"
        " a los 12 meses. Tu cuenta sigue existiendo y podrás volver a conectar.",
        "Te llega este correo porque se ha confirmado una baja con un código enviado a esta"
        " dirección.",
    )
    html = mail.html
    assert f">Puedes restaurarla hasta el {WHEN}.</div>" in html
    assert ">Desconexión en marcha<span" in html
    assert f'<a href="{URL}" style="display:inline-block;' in html and "Restaurar &rarr;" in html
    assert "Si no has sido tú, pulsa Restaurar. No se ha borrado nada." in html
    order = [
        html.index("Desde ahora no la leemos"),
        html.index("Restaurar &rarr;"),
        html.index("pulsa Restaurar"),
        html.index("Si no haces nada"),
    ]
    assert order == sorted(order)
    assert "${" not in html


def test_the_scheduled_email_for_the_account_has_the_exact_texts():
    mail = scheduled_email(ADDRESS, "cuenta", DELETE_AT, URL)
    assert mail.subject == "Tu cuenta de Fichaitor se borrará pronto"
    assert mail.text == plain(
        "BAJA EN MARCHA",
        "Has pedido borrar tu cuenta de Fichaitor. Desde ahora no leemos tus fichas ni te enviamos"
        " avisos, y hemos cerrado tu sesión en todos los dispositivos.",
        f"Restaurar: {URL}",
        "Si no has sido tú, abre el enlace. No se ha borrado nada.",
        f"Si no haces nada, el {WHEN} retiraremos el acceso a tu cuenta de Google y cerraremos tu"
        " cuenta. Lo obtenido de Google se borra en un máximo de 30 días, y el registro de las"
        " respuestas que publicaste, a los 12 meses.",
        "Te llega este correo porque se ha confirmado una baja con un código enviado a esta"
        " dirección.",
    )
    assert ">Baja en marcha<span" in mail.html
    assert f">Puedes restaurarla hasta el {WHEN}.</div>" in mail.html


def test_the_token_is_never_in_the_subject_nor_in_the_preheader():
    for scope in ("ficha", "cuenta"):
        mail = scheduled_email(ADDRESS, scope, DELETE_AT, URL)
        preheader = mail.html.split('line-height:1px;">', 1)[1].split("</div>", 1)[0]
        assert "TOKEN" not in mail.subject and "TOKEN" not in preheader


# --- 3. the leave is cancelled ----------------------------------------------------------


SIGN_IN = "https://fichaitor.es/app/"


def test_the_restored_email_for_the_profile_has_the_exact_texts():
    mail = restored_email(ADDRESS, "ficha", SIGN_IN)
    assert mail.to == ADDRESS and mail.subject == "Has restaurado Fichaitor"
    assert mail.text == plain(
        "RESTAURADA",
        "Hemos cancelado la desconexión. No se ha borrado nada.",
        "Fichaitor vuelve a leer tus fichas de Google y a avisarte cuando haya algo que decidir.",
        "Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.",
    )
    assert ">No se ha borrado nada.</div>" in mail.html
    assert ">Restaurada<span" in mail.html
    assert "Hemos cancelado la desconexión. No se ha borrado nada." in mail.html
    assert SIGN_IN not in mail.html  # no button: the session is still open
    assert "${" not in mail.html


def test_the_restored_email_for_the_account_has_the_exact_texts_and_a_sign_in_button():
    mail = restored_email(ADDRESS, "cuenta", SIGN_IN)
    assert mail.subject == "Has restaurado tu cuenta de Fichaitor"
    assert mail.text == plain(
        "RESTAURADA",
        "Hemos cancelado la baja de tu cuenta. No se ha borrado nada.",
        "Al confirmar la baja cerramos tu sesión en todos los dispositivos. Entra de nuevo en"
        " Fichaitor para seguir donde lo dejaste.",
        f"Entrar en Fichaitor: {SIGN_IN}",
        "Te llega este correo porque se ha cancelado una baja de tu cuenta de Fichaitor.",
    )
    assert ">Entra de nuevo para seguir donde lo dejaste.</div>" in mail.html
    assert f'<a href="{SIGN_IN}" style="display:inline-block;' in mail.html
    assert "Entrar en Fichaitor &rarr;" in mail.html
    order = [
        mail.html.index("Hemos cancelado la baja"),
        mail.html.index("Al confirmar la baja"),
        mail.html.index("Entrar en Fichaitor &rarr;"),
    ]
    assert order == sorted(order)


def test_the_sign_in_link_is_made_from_the_public_url_of_the_configuration_only():
    assert app_url("https://fichaitor.es") == SIGN_IN
    assert app_url("https://fichaitor.es/") == SIGN_IN
    payload = {
        "template": "offboarding_restored",
        "to": ADDRESS,
        "scope": "cuenta",
        "url": "https://evil.example/app/",
        "host": "evil.example",
    }
    mail = build_email(payload, "https://fichaitor.es/")
    assert "evil" not in mail.text + mail.html
    assert f"Entrar en Fichaitor: {SIGN_IN}\n" in mail.text


# --- the same HTML as the preview of brand/correo --------------------------------------------


def test_the_html_is_byte_for_byte_the_one_of_the_brand_preview(monkeypatch):
    # vista.py is only read: run without __main__, it writes nothing.
    vista = runpy.run_path(str(ROOT / "brand" / "correo" / "vista.py"), run_name="vista")
    monkeypatch.setattr(offboarding_mail, "spanish_date", lambda _: vista["FECHA"])
    restore = vista["ENLACE"]
    sign_in = "https://fichaitor.es/app/"
    ours = {
        "1-codigo": code_email(ADDRESS, "ficha", "482913", 10),
        "2-baja-ficha": scheduled_email(ADDRESS, "ficha", DELETE_AT, restore),
        "2-baja-cuenta": scheduled_email(ADDRESS, "cuenta", DELETE_AT, restore),
        "3-restaurada-ficha": restored_email(ADDRESS, "ficha", sign_in),
        "3-restaurada-cuenta": restored_email(ADDRESS, "cuenta", sign_in),
    }
    for name, mail in ours.items():
        assert mail.html == vista["CORREOS"][name], name
    # Email 4 is never sent; every other preview is covered.
    assert set(vista["CORREOS"]) - set(ours) == {"4-completada-ficha", "4-completada-cuenta"}


# --- escaping and the link ----------------------------------------------------------------


def test_every_value_is_escaped_in_the_html_and_left_alone_in_the_plain_text():
    nasty = 'https://fichaitor.es/app#restaurar=a"><script>alert(1)</script>&b'
    mail = scheduled_email(ADDRESS, "ficha", DELETE_AT, nasty)
    assert "<script>" not in mail.html and 'a">' not in mail.html
    assert 'href="https://fichaitor.es/app#restaurar=a&quot;&gt;&lt;script&gt;' in mail.html
    assert "&amp;b" in mail.html
    assert f"Restaurar: {nasty}" in mail.text  # plain text is not escaped

    weird = code_email(ADDRESS, "ficha", "<b>1</b>", 10)
    assert "&lt;b&gt;1&lt;/b&gt;" in weird.html and "<b>1</b>" not in weird.html
    assert "    <b>1</b>" in weird.text


def test_the_link_is_absolute_and_built_only_from_the_public_url_of_the_configuration():
    assert restore_url("https://fichaitor.es", "tok") == "https://fichaitor.es/app#restaurar=tok"
    payload = {
        "template": "offboarding_scheduled",
        "to": ADDRESS,
        "scope": "ficha",
        "delete_at": DELETE_AT.isoformat(),
        "token": "tok",
        # Whatever else a payload carries never makes the link.
        "host": "evil.example",
        "url": "https://evil.example/app#restaurar=tok",
    }
    mail = build_email(payload, "https://fichaitor.es/")
    assert "evil" not in mail.text and "evil" not in mail.html
    assert "Restaurar: https://fichaitor.es/app#restaurar=tok\n" in mail.text


@pytest.mark.parametrize(
    "base", ["fichaitor.es", "/app", "ftp://fichaitor.es", "https://", "https://f.es/?x=1#y"]
)
def test_a_public_url_that_is_not_absolute_is_refused_at_start(base):
    with pytest.raises(ValidationError):
        MailSettings(_env_file=None, public_base_url=base)


def test_a_public_url_with_a_path_or_the_default_is_accepted():
    assert MailSettings(_env_file=None).public_base_url == "http://localhost:8080"
    settings = MailSettings(_env_file=None, public_base_url="https://fichaitor.es/")
    assert settings.public_base_url == "https://fichaitor.es/"


# --- the job ---------------------------------------------------------------------------


def test_the_job_builds_the_restored_email_and_refuses_a_bad_payload():
    mail = build_email(
        {"template": "offboarding_restored", "to": ADDRESS, "scope": "cuenta"}, "https://f.es"
    )
    assert mail.subject == "Has restaurado tu cuenta de Fichaitor" and mail.to == ADDRESS
    assert "Entrar en Fichaitor: https://f.es/app/\n" in mail.text
    for bad in (
        {"template": "offboarding_restored", "scope": "cuenta"},
        {"template": "offboarding_restored", "to": ADDRESS, "scope": "otra"},
        {"template": "offboarding_completed", "to": ADDRESS, "scope": "ficha"},
    ):
        with pytest.raises(PermanentJobError):
            build_email(bad, "https://f.es")


# --- headers ---------------------------------------------------------------------------


def test_the_headers_come_from_the_configuration_with_reply_to_support_by_default():
    settings = MailSettings(_env_file=None, mail_from="avisos@fichaitor.es")
    assert settings.mail_from_name == "Fichaitor"
    assert settings.mail_reply_to == "soporte@fichaitor.es"
    message = build_message(
        code_email(ADDRESS, "ficha", CODE, 10),
        sender=settings.mail_from,
        sender_name=settings.mail_from_name,
        reply_to=settings.mail_reply_to,
    )
    assert message["From"] == "Fichaitor <avisos@fichaitor.es>"
    assert message["Reply-To"] == "soporte@fichaitor.es"
    assert message["Subject"] == "Tu código de confirmación de Fichaitor"
    assert message.get_content_type() == "multipart/alternative"
    parts = list(message.iter_parts())
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    assert CODE in parts[0].get_content() and CODE in parts[1].get_content()
    assert "Fichaitor<span" in parts[1].get_content()


def test_the_worker_hands_the_reply_to_of_the_configuration_to_the_mailer():
    from app.worker.__main__ import build_mailer

    settings = MailSettings(
        _env_file=None, smtp_host="smtp.example.test", mail_from="avisos@fichaitor.es"
    )
    mailer = build_mailer(settings)  # built, never used: nothing is sent
    assert mailer._reply_to == "soporte@fichaitor.es"
    assert mailer._sender == "avisos@fichaitor.es" and mailer._sender_name == "Fichaitor"
