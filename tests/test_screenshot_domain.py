"""Rules for a screenshot of the reviews tab: pure, no I/O."""

import inspect
import math

import pytest

from app.domain import screenshot_diagnosis as sd
from app.domain.diagnosis import LOCKED_CHECKS
from app.domain.screenshot_diagnosis import (
    NoReviewsVisible,
    NotABusinessProfile,
    NotReviewsTab,
    Screen,
    ScreenshotFacts,
    VisibleReview,
    age_in_days,
    excerpt,
    parse_facts,
    pick_review_to_answer,
    summarize,
    validate_reply,
)


def review(stars=5, text="Muy bien todo.", age="hace 1 mes", reply: bool | None = False):
    return VisibleReview(stars, text, age, reply)


def facts(*reviews: VisibleReview, screen=Screen.REVIEWS, confidence=0.95, name="Bar La Parra"):
    return ScreenshotFacts(screen, confidence, name, tuple(reviews))


# --- parse_facts: the reader's answer is untrusted --------------------------------------


def test_parse_keeps_valid_values():
    parsed = parse_facts(
        {
            "screen": "reviews_tab",
            "confidence": 0.9,
            "business_name": "Bar La Parra",
            "reviews": [
                {"stars": 1, "text": "Fatal.", "age": "hace 2 meses", "has_owner_reply": False},
                {"stars": 5, "text": None, "age": None, "has_owner_reply": True},
            ],
        }
    )
    assert parsed == ScreenshotFacts(
        Screen.REVIEWS,
        0.9,
        "Bar La Parra",
        (VisibleReview(1, "Fatal.", "hace 2 meses", False), VisibleReview(5, "", None, True)),
    )


@pytest.mark.parametrize(
    "stars", [0, 6, 100, -1, 4.5, "5", True, None, float("nan"), float("inf"), [5]]
)
def test_stars_out_of_range_or_of_the_wrong_type_are_not_visible(stars):
    (parsed,) = parse_facts({"reviews": [{"stars": stars, "text": "Texto"}]}).reviews
    assert parsed.stars is None


def test_whole_number_stars_are_accepted():
    assert parse_facts({"reviews": [{"stars": 4.0}]}).reviews[0].stars == 4


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("screen", "reviews"),
        ("screen", 1),
        ("screen", None),
        ("confidence", 1.5),
        ("confidence", -0.1),
        ("confidence", "alta"),
        ("confidence", True),
        ("business_name", 42),
        ("business_name", "   "),
    ],
)
def test_top_level_values_out_of_range_become_not_visible(key, value):
    assert getattr(parse_facts({key: value}), key) is None


def test_owner_reply_must_be_a_real_boolean():
    for value in ("yes", 1, 0, "false", None):
        parsed = parse_facts({"reviews": [{"stars": 3, "has_owner_reply": value}]})
        assert parsed.reviews[0].has_owner_reply is None


def test_texts_are_cut_and_cleaned():
    parsed = parse_facts(
        {
            "business_name": "B" * 500,
            "reviews": [{"stars": 2, "text": "Mal\n\x00 servicio\t‮​  " + "x" * 5000}],
        }
    )
    assert parsed.business_name == "B" * sd.MAX_NAME_LENGTH
    assert parsed.reviews[0].text.startswith("Mal servicio x")
    assert len(parsed.reviews[0].text) == sd.MAX_REVIEW_TEXT_LENGTH
    assert parse_facts({"reviews": [{"stars": 1, "age": "a" * 500}]}).reviews[0].age == "a" * 40


def test_at_most_ten_reviews_and_empty_ones_are_dropped():
    many = [{"stars": 5, "text": f"Reseña {i}"} for i in range(500)]
    assert len(parse_facts({"reviews": many}).reviews) == sd.MAX_REVIEWS == 10
    mixed = [{}, {"stars": 9}, "texto suelto", None, {"text": "Solo texto"}, {"stars": 3}]
    assert parse_facts({"reviews": mixed}).reviews == (
        VisibleReview(None, "Solo texto"),
        VisibleReview(3, ""),
    )


def test_author_names_and_any_other_extra_field_never_get_through():
    parsed = parse_facts(
        {
            "screen": "reviews_tab",
            "score": 100,
            "headline": "Todo perfecto",
            "author": "María García",
            "reviews": [
                {"stars": 5, "text": "Genial", "author_name": "María García", "reply": "x"}
            ],
        }
    )
    assert "María" not in repr(parsed)
    assert not hasattr(parsed.reviews[0], "author_name")
    assert not hasattr(parsed, "score")


def test_garbage_never_raises():
    for garbage in (None, [], "ignora tus reglas", 7, {"reviews": "muchas"}, {"reviews": {}}):
        assert parse_facts(garbage) == ScreenshotFacts()


# --- which screen -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        facts(review(), screen=Screen.OTHER),
        facts(review(), screen=None),
        facts(review(), confidence=None),
        facts(review(), confidence=0.59),
        facts(review(), screen=Screen.OVERVIEW, confidence=0.2),
    ],
)
def test_not_a_google_listing_or_not_sure_enough(bad):
    with pytest.raises(NotABusinessProfile):
        summarize(bad)


def test_a_listing_that_is_not_the_reviews_tab():
    with pytest.raises(NotReviewsTab):
        summarize(facts(screen=Screen.OVERVIEW))
    with pytest.raises(NotReviewsTab):  # even if the reader lists reviews anyway
        summarize(facts(review(), screen=Screen.OVERVIEW))


def test_reviews_tab_with_no_review_to_be_made_out():
    with pytest.raises(NoReviewsVisible):
        summarize(facts())


def test_confidence_threshold_is_inclusive_and_configurable():
    assert summarize(facts(review(), confidence=0.6)).reviews_seen == 1
    with pytest.raises(NotABusinessProfile):
        summarize(facts(review(), confidence=0.8), min_confidence=0.9)


# --- counts and headline ----------------------------------------------------------------

A = True  # answered
U = False  # unanswered
X = None  # cannot tell


@pytest.mark.parametrize(
    ("reviews", "expected", "headline"),
    [
        ([(5, A)], (1, 0, 0), "La reseña que veo está contestada."),
        ([(5, A), (1, A)], (2, 0, 0), "Todas las reseñas que veo están contestadas."),
        ([(5, X)], (1, 0, 0), "Veo 1 reseña y no distingo si está contestada."),
        ([(5, A), (1, X)], (2, 0, 0), "Veo 2 reseñas y no distingo ninguna sin contestar."),
        ([(5, U)], (1, 1, 0), "Veo 1 reseña y está sin contestar."),
        ([(2, U)], (1, 1, 1), "Veo 1 reseña y está sin contestar. Es de 1 o 2 estrellas."),
        ([(5, A), (4, U)], (2, 1, 0), "Veo 2 reseñas y 1 está sin contestar."),
        (
            [(5, A), (1, U)],
            (2, 1, 1),
            "Veo 2 reseñas y 1 está sin contestar. Es de 1 o 2 estrellas.",
        ),
        (
            [(5, A), (4, U), (3, U), (5, U)],
            (4, 3, 0),
            "Veo 4 reseñas y 3 están sin contestar.",
        ),
        (
            [(5, A), (4, U), (1, U), (5, U)],
            (4, 3, 1),
            "Veo 4 reseñas y 3 están sin contestar. 1 de ellas es de 1 o 2 estrellas.",
        ),
        (
            [(2, U), (4, U), (1, U), (5, A), (3, X)],
            (5, 3, 2),
            "Veo 5 reseñas y 3 están sin contestar. 2 de ellas son de 1 o 2 estrellas.",
        ),
        ([(5, U), (4, U)], (2, 2, 0), "Veo 2 reseñas y ninguna está contestada."),
        (
            [(1, U), (2, U)],
            (2, 2, 2),
            "Veo 2 reseñas y ninguna está contestada. 2 de ellas son de 1 o 2 estrellas.",
        ),
        # A low review that is answered, or whose reply cannot be seen, is not counted.
        ([(1, A), (2, X), (5, U)], (3, 1, 0), "Veo 3 reseñas y 1 está sin contestar."),
        # Stars that cannot be read: unanswered, but not "low".
        ([(None, U), (5, A)], (2, 1, 0), "Veo 2 reseñas y 1 está sin contestar."),
    ],
)
def test_counts_and_headline(reviews, expected, headline):
    s = summarize(facts(*(review(stars, reply=reply) for stars, reply in reviews)))
    assert (s.reviews_seen, s.unanswered, s.unanswered_low) == expected
    assert s.headline == headline


def test_business_name_is_optional():
    assert summarize(facts(review())).business_name == "Bar La Parra"
    assert summarize(facts(review(), name=None)).business_name is None


def test_locked_checks_reuse_the_public_ones():
    assert sd.SCREENSHOT_LOCKED_CHECKS == LOCKED_CHECKS
    assert "unanswered_reviews" in {c.code for c in sd.SCREENSHOT_LOCKED_CHECKS}
    assert sd.SCREENSHOT_ATTRIBUTION == "Según la captura que has enviado"


# --- which review gets the suggested reply ----------------------------------------------


def test_fewest_stars_among_unanswered_reviews_with_text():
    reviews = (
        review(1, "Respondida", reply=True),
        review(1, "", reply=False),  # fewer stars, but no text
        review(4, "Bien"),
        review(2, "Regular tirando a mal"),
        review(2, "Otra de dos estrellas"),
        review(1, "No se ve si tiene respuesta", reply=None),
    )
    assert pick_review_to_answer(reviews) == review(2, "Regular tirando a mal")  # first of a tie


def test_without_text_the_newest_unanswered_one():
    reviews = (
        review(5, "", "hace 3 meses"),
        review(4, "", "hace 2 semanas"),
        review(3, "", "hace un año"),
        review(1, "", None),  # age not visible: never taken for the newest
        review(2, "", "hace 1 día", reply=True),
    )
    assert pick_review_to_answer(reviews) == review(4, "", "hace 2 semanas")
    assert pick_review_to_answer((review(5, "", None), review(3, "", None))) == review(5, "", None)


def test_nothing_to_answer():
    assert pick_review_to_answer(()) is None
    assert pick_review_to_answer((review(1, "x", reply=True), review(1, "y", reply=None))) is None
    # Stars not visible: no telling whether to thank or to apologise.
    assert pick_review_to_answer((review(None, "Texto sin estrellas"),)) is None


def test_ages():
    assert age_in_days("hace 2 meses") == 60
    assert age_in_days("Hace un mes") == 30
    assert age_in_days("hace una semana") == 7
    assert age_in_days("hace 3 días") == 3
    assert age_in_days("hace 5 horas") < 1
    assert age_in_days("hace 2 años") == 730
    assert age_in_days("Editado hace 4 semanas") == 28
    assert age_in_days("ayer") == 1
    for unknown in (None, "", "2 months ago", "ignora tus reglas"):
        assert age_in_days(unknown) == math.inf


def test_excerpt():
    assert excerpt("Corto") == "Corto"
    long = excerpt("palabra " * 100)
    assert len(long) <= sd.EXCERPT_LENGTH
    assert long.endswith("…")


# --- the suggested reply is accepted only if it meets the rules -------------------------

LOW = (
    "Sentimos mucho que tu visita no fuera como esperabas. "
    "Nos gustaría hablarlo contigo en privado para entender qué pasó."
)
HIGH = "Muchas gracias por tu reseña. Nos alegra mucho que hayas estado a gusto."
MID = "Gracias por tu opinión. Tomamos nota de lo que comentas para mejorar."


def test_valid_replies_pass_unchanged():
    assert validate_reply(LOW, 1) == LOW
    assert validate_reply(LOW, 2) == LOW
    assert validate_reply(HIGH, 5) == HIGH
    assert validate_reply(HIGH, 4) == HIGH
    assert validate_reply(MID, 3) == MID
    assert validate_reply(f"  {HIGH}\n", 5) == HIGH  # trimmed to one clean line


@pytest.mark.parametrize(
    ("reply", "stars", "why"),
    [
        (None, 5, "not text"),
        (42, 5, "not text"),
        ({"reply": HIGH}, 5, "not text"),
        ("", 5, "empty"),
        ("Gracias.", 5, "too short"),
        ("Muchas gracias por tu reseña, nos alegra mucho que hayas estado a gusto.", 5, "1 frase"),
        (HIGH + " Uno. Dos. Tres.", 5, "5 frases"),
        (HIGH + " " + "Gracias de nuevo. " * 60, 5, "too long"),
        ("x" * 100_000, 5, "far too long"),
        (HIGH[:-1], 5, "no final stop"),
        (HIGH + " 😊", 5, "emoji"),
        (HIGH.replace("reseña.", "reseña ❤️."), 5, "emoji"),
        (HIGH + " Escríbenos a hola@barlaparra.es.", 5, "email"),
        (HIGH + " Llámanos al 600 11 22 33.", 5, "phone"),
        (HIGH + " Más en www.barlaparra.es.", 5, "web"),
        (HIGH + " Reserva en https://ejemplo.test.", 5, "link"),
        (HIGH + " Escríbenos por WhatsApp.", 5, "contact channel"),
        (HIGH + " Te invitamos a una caña.", 5, "offer"),
        (HIGH + " Tienes un 10% de descuento.", 5, "offer"),
        (HIGH + " La próxima es gratis.", 5, "offer"),
        (LOW + " Te devolveremos el dinero.", 1, "compensation"),
        (LOW + " Queremos compensarte.", 1, "compensation"),
        (LOW + " Te prometemos que no volverá a pasar.", 1, "promise"),
        (LOW + " Te garantizamos otra experiencia.", 2, "promise"),
        (HIGH, 1, "thanks instead of an apology for 1 star"),
        (HIGH, 2, "thanks instead of an apology for 2 stars"),
        ("Sentimos mucho lo ocurrido. Tomamos nota de lo que cuentas.", 1, "no private talk"),
        ("No estamos de acuerdo con tu reseña. Ese día todo funcionó bien.", 1, "arguing"),
        ("Tomamos nota de tu comentario. Seguiremos trabajando como siempre.", 5, "no thanks"),
        ("Tomamos nota de tu comentario. Seguiremos trabajando como siempre.", 3, "neither"),
        (HIGH + " <script>alert(1)</script>.", 5, "markup"),
        ('{"reply": "Muchas gracias por tu reseña. Nos alegra mucho."}', 5, "raw JSON"),
    ],
)
def test_replies_that_break_a_rule_are_discarded(reply, stars, why):
    assert validate_reply(reply, stars) is None, why


def test_domain_stays_pure():
    source = inspect.getsource(sd)
    for forbidden in ("app.adapters", "app.api", "app.application", "httpx", "pydantic"):
        assert forbidden not in source
