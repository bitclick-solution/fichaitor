"""The WhatsApp templates to submit for approval (app/domain/whatsapp_templates.py) and
how a message becomes one of them. No I/O."""

import re

import pytest

from app.application.conversation_service import HOLIDAY_ASK, REVIEW_ASK, STEPS
from app.domain import channel_messages as cm
from app.domain import message_allowance as allowance
from app.domain import whatsapp_templates as t

SHOW = "prop_0123456789abcdef.a1b2c3.show"
IDS = tuple(f"prop_0123456789abcdef.a1b2c3.{o}" for o in ("publish", "edit", "no_reply"))
HOURS_IDS = tuple(f"prop_0123456789abcdef.a1b2c3.{o}" for o in STEPS[HOLIDAY_ASK].options)
SALES_WORDS = ("oferta", "descuento", "promoción", "aprovecha", "gratis", "no te lo pierdas",
               "mejora tu plan", "pro+")  # fmt: skip


@pytest.mark.parametrize("template", t.TEMPLATES, ids=lambda x: x.name)
def test_each_template_meets_the_rules_of_meta_and_of_the_script(template):
    assert re.fullmatch(r"[a-z0-9_]{1,512}", template.name)
    assert template.category == t.UTILITY
    numbers = sorted({int(n) for n in re.findall(r"\{\{(\d+)\}\}", template.body)})
    assert numbers == list(range(1, len(numbers) + 1))  # positional, from 1, none missing
    assert len(template.example) == template.placeholders
    assert len(template.render(template.example)) <= t.MAX_BODY
    # Never starts nor ends with a variable, and never only variables (3.2, rule 5).
    assert not template.body.startswith("{{") and not template.body.rstrip(".?").endswith("}}")
    assert template.fixed_length > 40
    assert "ficha de {{" in template.body or "La ficha de {{" in template.body  # names it
    lowered = template.body.lower()
    assert not any(word in lowered for word in SALES_WORDS)
    assert len(template.buttons) <= 3 and len(set(template.buttons)) == len(template.buttons)
    assert all(len(b) <= cm.CORE_LIMITS.max_button_title for b in template.buttons)


def test_the_templates_with_the_buttons_of_a_step_have_them_in_the_same_order():
    review = tuple(cm.button_title(o) for o in STEPS[REVIEW_ASK].options)
    hours = tuple(cm.button_title(o) for o in STEPS[HOLIDAY_ASK].options)
    assert t.REVIEW.buttons == t.REVIEW_NO_TEXT.buttons == review
    assert t.HOLIDAY.buttons == t.SPECIAL_DAY.buttons == hours


def test_the_names_are_unique():
    names = [template.name for template in t.TEMPLATES]
    assert len(names) == len(set(names))


def review_values(review="El arroz, espectacular.", reply="Muchas gracias por tu reseña."):
    return {"estrellas": "5 estrellas", "resena": review, "respuesta": reply}


def test_a_review_shows_its_reply_whole_and_its_buttons():
    fallback = t.for_proposal("review.new.positive", "Bar Pepe", review_values(), IDS, SHOW)
    assert fallback.name == "resena_nueva_v1"
    assert fallback.params == (
        "Bar Pepe",
        "5 estrellas",
        "El arroz, espectacular.",
        "Muchas gracias por tu reseña.",
    )
    assert fallback.button_ids == IDS
    assert allowance.units(cm.OutboundMessage("x", cm.MessageKind.PROPOSAL, "x",
                                              template_fallback=fallback)) == 1  # fmt: skip


def test_a_review_without_text_has_its_own_template():
    fallback = t.for_proposal("review.new.no_text", "Bar", review_values(review=""), IDS, SHOW)
    assert fallback.name == "resena_sin_texto_v1"
    assert fallback.params == ("Bar", "5 estrellas", "Muchas gracias por tu reseña.")


def test_values_never_carry_line_breaks_and_the_excerpt_gives_way_to_the_reply():
    reply = "Gracias.\nTe esperamos " + "pronto " * 95  # about 700 characters, never cut
    review = "Muy bien\n\n" + "bla " * 300
    fallback = t.for_proposal("review.new.negative", "Bar", review_values(review, reply), IDS, SHOW)
    assert fallback.name == "resena_nueva_v1"
    assert all("\n" not in p and "\t" not in p and "     " not in p for p in fallback.params)
    assert fallback.params[3] == cm.one_line(reply, 10_000)  # whole
    assert fallback.params[2].endswith("…")
    assert len(t.REVIEW.render(fallback.params)) <= t.MAX_BODY


def test_a_reply_that_leaves_no_room_for_the_review_goes_as_the_generic_notice():
    reply = "palabra " * 120  # 960 characters
    fallback = t.for_proposal("review.new.positive", "Bar", review_values(reply=reply), IDS, SHOW)
    assert fallback.name == "aviso_pendiente_v1"
    assert fallback.params == ("Bar",) and fallback.button_ids == (SHOW,)


def test_holidays_and_special_days():
    values = {"fecha_larga": "martes 8 de diciembre", "nombre": "Inmaculada Concepción"}
    holiday = t.for_proposal("holiday.ask", "Bar", values, HOURS_IDS, SHOW)
    assert holiday.name == "festivo_v1"
    assert holiday.params == ("martes 8 de diciembre", "Inmaculada Concepción", "Bar")
    assert holiday.button_ids == HOURS_IDS
    eve = t.for_proposal("eve.ask", "Bar", {**values, "nombre": "Nochebuena"}, HOURS_IDS, SHOW)
    assert eve.name == "dia_especial_v1"


@pytest.mark.parametrize("key", ["keydate.ask", "review.new.write", "queue.resume", "otra"])
def test_anything_without_a_template_is_the_generic_notice(key):
    fallback = t.for_proposal(key, "Bar", {}, IDS, SHOW)
    assert fallback.name == "aviso_pendiente_v1" and fallback.button_ids == (SHOW,)


def test_buttons_that_do_not_match_the_template_give_the_notice():
    fallback = t.for_proposal("review.new.positive", "Bar", review_values(), IDS[:2], SHOW)
    assert fallback.name == "aviso_pendiente_v1"


def test_reminder_and_result():
    reminder = t.reminder("Bar", "la reseña de 5 estrellas", "lunes 16 de noviembre", SHOW)
    assert reminder.name == "recordatorio_v1" and reminder.button_ids == (SHOW,)
    result = t.result("Bar", "Respuesta enviada a Google.\nSuele verse en unos minutos.")
    assert result.name == "resultado_cambio_v1" and result.button_ids == ()
    assert result.params[1] == "Respuesta enviada a Google. Suele verse en unos minutos."


def test_an_empty_value_is_never_sent():
    assert t.notice("", SHOW).params == ("-",)
    with pytest.raises(ValueError):
        t.fill(t.NOTICE, ("",), (SHOW,))
    with pytest.raises(ValueError):
        t.fill(t.NOTICE, ("a", "b"), (SHOW,))


def test_a_marketing_template_would_cost_four_messages():
    fallback = cm.TemplateFallback("x", category="marketing")
    message = cm.OutboundMessage("x", cm.MessageKind.PROPOSAL, "x", template_fallback=fallback)
    assert allowance.units(message) == 4
    assert allowance.units(cm.OutboundMessage("x", cm.MessageKind.REPLY, "x")) == 1


def test_the_document_for_the_owner_says_exactly_what_the_code_sends():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "whatsapp-plantillas.md").read_text(
        encoding="utf-8"
    )
    for template in t.TEMPLATES:
        assert f"### `{template.name}`" in doc
        assert f"```text\n{template.body}\n```" in doc
        assert all(f"`{button}`" in doc for button in template.buttons)
