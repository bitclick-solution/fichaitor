"""Actions on Google confirmed by the owner: the closed list, their states and what the
owner is told about each outcome. No I/O.

The model proposes and the code executes: an action only exists because an owner
confirmed it from a session, and only the worker's executor carries it out.
"""

import re

REPLY_REVIEW = "reply_review"  # the action type and the kind of the job that runs it
SET_SPECIAL_HOURS = "set_special_hours"  # special hours of one day (a key date)
ACTION_TYPES = (REPLY_REVIEW, SET_SPECIAL_HOURS)

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
LIVE_STATES = (PENDING, RUNNING)

# Why an action failed. A closed list: nothing Google says is ever stored or shown.
ERROR_RECONNECT = "reconnect"  # the owner's grant is gone (401, invalid_grant)
ERROR_REJECTED = "rejected"  # Google refused the request (4xx)
ERROR_REVIEW_GONE = "review_gone"  # the review no longer exists (404)
ERROR_UNAVAILABLE = "unavailable"  # Google kept failing (5xx, 429, network)
ERROR_INTERNAL = "internal"  # our side: the profile left the connection, bad token
ERROR_SUSPENDED = "suspended"  # the account has a scheduled leave: nothing is sent
# Special hours only.
ERROR_DATE_PASSED = "date_passed"  # its day had passed before it could be applied
ERROR_NO_REGULAR_HOURS = "no_regular_hours"  # "horario normal" without regular hours

# Audit trail.
EVENT_CONFIRMED = "reply_confirmed"
EVENT_SENT = "reply_sent"
EVENT_FAILED = "reply_failed"
EVENT_HOURS_CONFIRMED = "special_hours_confirmed"
EVENT_HOURS_SENT = "special_hours_sent"
EVENT_HOURS_FAILED = "special_hours_failed"
# action type: (confirmed, sent, failed)
AUDIT_EVENTS = {
    REPLY_REVIEW: (EVENT_CONFIRMED, EVENT_SENT, EVENT_FAILED),
    SET_SPECIAL_HOURS: (EVENT_HOURS_CONFIRMED, EVENT_HOURS_SENT, EVENT_HOURS_FAILED),
}
REASON_ALREADY_APPLIED = "already_applied"  # a retry found the reply already in Google

ACTION_ID = re.compile(r"act_[0-9a-f]{8,32}")
REVIEW_ID = re.compile(r"rev_[0-9a-f]{8,32}")
# Google's reviewId goes into the URL path of the write: only this shape is accepted.
GBP_REVIEW_ID = re.compile(r"[A-Za-z0-9_-]{1,255}")

STATUS_LABELS = {PENDING: "pendiente", RUNNING: "en_curso", DONE: "hecha", FAILED: "fallida"}

MSG_PENDING = "Tu respuesta está en cola para enviarse a Google."
MSG_RUNNING = "Estamos enviando tu respuesta a Google."
# "Enviada", never "publicada": Google reviews the reply and may take a while to show it.
MSG_DONE = "Enviada a Google. Puede tardar unos minutos en verse en tu ficha."
MSG_RECONNECT = "Tu acceso a Google ha caducado; vuelve a conectar."
_FAILED_MESSAGES = {
    ERROR_RECONNECT: MSG_RECONNECT,
    ERROR_REJECTED: "Google no ha aceptado la respuesta. Revisa el texto y vuelve a enviarla.",
    ERROR_REVIEW_GONE: "Esa reseña ya no está en Google. No hay nada que responder.",
    ERROR_SUSPENDED: "No se ha enviado: tienes una baja programada.",
    ERROR_UNAVAILABLE: (
        "No hemos podido enviar tu respuesta a Google. Prueba otra vez en unos minutos."
    ),
}
MSG_FAILED = "No hemos podido enviar tu respuesta. Prueba otra vez en unos minutos."

# The same, for a change of the hours of one day.
MSG_HOURS_PENDING = "Tu cambio de horario está en cola para enviarse a Google."
MSG_HOURS_RUNNING = "Estamos enviando tu cambio de horario a Google."
MSG_HOURS_DONE = "Enviado a Google. Puede tardar unos minutos en verse en tu ficha."
MSG_HOURS_FAILED = "No hemos podido enviar el horario a Google. No se ha cambiado nada en tu ficha."
_HOURS_FAILED_MESSAGES = {
    ERROR_RECONNECT: MSG_RECONNECT,
    ERROR_REJECTED: "Google no ha aceptado ese horario. Tu ficha sigue como estaba.",
    ERROR_SUSPENDED: "No se ha enviado: tienes una baja programada.",
    ERROR_DATE_PASSED: (
        "Ese día ya ha pasado y no se ha podido aplicar a tiempo. No se ha cambiado nada."
    ),
    ERROR_NO_REGULAR_HOURS: (
        "Tu ficha no tiene horario normal en Google, así que no hay nada que copiar. "
        "Elige cerrado u otro horario."
    ),
    ERROR_UNAVAILABLE: (
        "No hemos podido enviar el horario a Google. Prueba otra vez en unos minutos."
    ),
}


def status_label(state: str) -> str:
    return STATUS_LABELS.get(state, STATUS_LABELS[PENDING])


def status_message(
    state: str, error_code: str | None = None, action_type: str = REPLY_REVIEW
) -> str:
    """What the owner reads about an action, in Spanish and ready to show."""
    if action_type == SET_SPECIAL_HOURS:
        if state == DONE:
            return MSG_HOURS_DONE
        if state == FAILED:
            return _HOURS_FAILED_MESSAGES.get(error_code or "", MSG_HOURS_FAILED)
        return MSG_HOURS_RUNNING if state == RUNNING else MSG_HOURS_PENDING
    if state == DONE:
        return MSG_DONE
    if state == FAILED:
        return _FAILED_MESSAGES.get(error_code or "", MSG_FAILED)
    return MSG_RUNNING if state == RUNNING else MSG_PENDING
