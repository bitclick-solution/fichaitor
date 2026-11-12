"""The brand templates of the emails, read once when the module is imported.

`base.html` and `bloques.html` are copies of brand/correo/, where the interface session
keeps them (guide in brand/correo/LEEME.md). They live here so that they travel inside
the `app` package, which is what the Docker image copies; tests/test_offboarding_mail.py
fails as soon as a copy differs from its original. To change a template, change it in
brand/correo/ and copy it here.

They are filled with string.Template.substitute: a missing value is an error, never an
email with a "${hole}" in it.
"""

import re
from importlib.resources import files
from string import Template

_HERE = files(__name__)
_BLOCK = re.compile(r"== (\w+) ==\n(.*?)(?=\n== |\Z)", flags=re.S)


def _read(name: str) -> str:
    # Universal newlines: the same text whatever line endings the checkout has.
    return _HERE.joinpath(name).read_text(encoding="utf-8").replace("\r\n", "\n")


BASE = Template(_read("base.html"))
"""The frame: ${asunto}, ${preencabezado}, ${titular}, ${contenido} and ${motivo}."""

BLOCKS: dict[str, Template] = {
    name: Template(body.strip()) for name, body in _BLOCK.findall(_read("bloques.html"))
}
"""The pieces of the body: parrafo, codigo, boton and aviso."""
