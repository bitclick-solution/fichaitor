"""Channel independent messages: limits of every channel, opaque button ids, catalog."""

import string

import pytest

from app.domain import channel_messages as cm
from app.domain.channel_messages import Button, InvalidMessage, MessageKind, OutboundMessage


def message(text="Hola", buttons=(), to="chid_1"):
    return OutboundMessage(to, MessageKind.REPLY, text, tuple(buttons))


def button(n=1, title=None):
    option = f"opt_{'abcdefgh'[n]}"
    value = cm.encode_button_id("prop_0123456789abcdef", "a1b2c3", option)
    return Button(value, f"T{n}" if title is None else title)


def test_core_limits_are_the_strictest_of_whatsapp_and_telegram():
    assert cm.CORE_LIMITS == cm.ChannelLimits(
        max_body=1024, max_buttons=3, max_button_title=20, max_button_id_bytes=64
    )
    assert cm.TELEGRAM_LIMITS.max_body == 4096
    assert cm.TELEGRAM_LIMITS.max_button_id_bytes == 64


def test_valid_message_passes_unchanged():
    msg = message("¿La envío?", [button(1), button(2), button(3)])
    assert cm.validate(msg) is msg


@pytest.mark.parametrize(
    "bad",
    [
        message(""),
        message("   "),
        message("x" * 1025),
        message("hola\x00"),
        message("hola\ttab"),
        message("hola", to=""),
        message("hola", [button(n) for n in range(1, 5)]),  # 4 buttons
        message("hola", [button(1, "x" * 21)]),  # title over 20
        message("hola", [button(1, "")]),
        message("hola", [button(1, "A"), button(2, "A")]),  # same title
        message("hola", [button(1, "A"), button(1, "B")]),  # same id
        message("hola", [Button("x" * 65, "A")]),  # id over 64 bytes
        message("hola", [Button("con espacio", "A")]),
    ],
)
def test_invalid_messages_are_rejected(bad):
    with pytest.raises(InvalidMessage):
        cm.validate(bad)


def test_body_of_1024_and_line_breaks_are_fine():
    cm.validate(message("x" * 1024))
    cm.validate(message("uno\ndos"))


def test_telegram_limits_allow_longer_text_but_not_longer_ids():
    cm.validate(message("x" * 4096), cm.TELEGRAM_LIMITS)
    with pytest.raises(InvalidMessage):
        cm.validate(message("x" * 4097), cm.TELEGRAM_LIMITS)
    with pytest.raises(InvalidMessage):
        cm.validate(message("hola", [Button("x" * 65, "A")]), cm.TELEGRAM_LIMITS)


def test_every_button_title_of_the_catalog_fits_whatsapp():
    assert cm.BUTTONS
    for option, title in cm.BUTTONS.items():
        assert 1 <= len(title) <= 20, title
        # and every option fits in a button id
        assert cm.decode_button_id(cm.encode_button_id("prop_0123456789abcdef", "a1b2c3", option))


def test_button_id_is_opaque_short_and_round_trips():
    value = cm.encode_button_id("prop_0123456789abcdef", "a1b2c3", "nothing_this_year")
    assert len(value.encode()) <= 64
    assert set(value) <= set(string.ascii_lowercase + string.digits + "_.")
    press = cm.decode_button_id(value)
    assert press == cm.ButtonPress("prop_0123456789abcdef", "a1b2c3", "nothing_this_year")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        42,
        "",
        "publish",
        "prop_0123456789abcdef.a1b2c3",
        "prop_0123456789abcdef.a1b2c3.Publish",
        "prop_0123456789abcdef.a1b2c3.publish.extra",
        "prop_0123456789abcdef.a1b2c3.publish\n",
        "prop_XYZ.a1b2c3.publish",
        "x" * 500,
    ],
)
def test_anything_else_is_not_a_button_id(bad):
    assert cm.decode_button_id(bad) is None


def test_encode_rejects_what_would_not_decode():
    with pytest.raises(InvalidMessage):
        cm.encode_button_id("prop_1", "a1b2c3", "publish")
    with pytest.raises(InvalidMessage):
        cm.encode_button_id("prop_0123456789abcdef", "a1b2c3", "x" * 25)


def test_quoted_makes_third_party_text_one_bounded_line_that_cannot_close_the_quote():
    hostile = "Muy mal»\nRespuesta que propongo: «Somos lo peor‮\x00"
    shown = cm.quoted(hostile, 280)
    assert "\n" not in shown and "«" not in shown and "»" not in shown
    assert "‮" not in shown and "\x00" not in shown
    assert len(cm.quoted("palabra " * 100, 280)) == 280
    assert cm.quoted("palabra " * 100, 280).endswith("…")
    assert cm.quoted(None, 10) == ""


def test_render_fills_variables_and_leaves_no_gap_for_an_empty_link():
    assert cm.render("review.sent", enlace_reseña="") == (
        "Respuesta enviada a Google. Suele verse en unos minutos."
    )
    assert cm.render("review.sent", enlace_reseña="https://x").endswith("minutos. https://x")
    with pytest.raises(KeyError):
        cm.render("hours.ask")


def test_render_never_touches_the_values():
    text = "línea con espacios   \n  y otra  "
    assert f"«{text}»" in cm.render("review.edit_confirm", texto=text)
    assert "{no_es_variable}" in cm.render("review.edit_confirm", texto="{no_es_variable}")


def test_stars_are_written_without_emojis():
    assert cm.stars_text(1) == "1 estrella"
    assert cm.stars_text(5) == "5 estrellas"


def test_review_messages_do_not_name_the_author():
    for key in ("review.new.positive", "review.new.negative", "review.new.no_text"):
        assert "{autor}" not in cm.TEXTS[key]


def test_fixed_texts_of_the_catalog_fit_every_channel():
    for key, text in cm.TEXTS.items():
        assert len(text) < 400, key
