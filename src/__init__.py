"""Reconfigura stdout/stderr a UTF-8 al importar.

En Windows con cp1252 por defecto, prints con caracteres no-ASCII (títulos de
threads con corazones, kanji, emojis...) lanzan UnicodeEncodeError. En CI Linux
da igual, pero esto hace que correr local en Windows no rompa.

En CI (PRIVATE_LOG=1) además manda toda la salida al log privado; ver runlog.py.
"""
import sys

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        # AttributeError: TextIOWrapper sin reconfigure (Python <3.7 o stream redirigido)
        # ValueError: stream ya cerrado
        pass

from . import runlog  # noqa: E402

runlog.install()
