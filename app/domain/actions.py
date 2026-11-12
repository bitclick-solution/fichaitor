"""Actions on Google confirmed by the owner: the closed list, their states and what the
owner is told about each outcome. No I/O.

The model proposes and the code executes: an action only exists because an owner
confirmed it from a session, and only the worker's executor carries it out.
"""

import re

REPLY_REVIEW = "reply_review"  # the action type and the kind of the job that runs it

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

# Audit trail.
EVENT_CONFIRMED = "reply_confirmed"
EVENT_SENT = "reply_sent"
EVENT_FAILED = "reply_failed"
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


def status_label(state: str) -> str:
    return STATUS_LABELS.get(state, STATUS_LABELS[PENDING])


def status_message(state: str, error_code: str | None = None) -> str:
    """What the owner reads about an action, in Spanish and ready to show."""
    if state == DONE:
        return MSG_DONE
    if state == FAILED:
        return _FAILED_MESSAGES.get(error_code or "", MSG_FAILED)
    return MSG_RUNNING if state == RUNNING else MSG_PENDING
