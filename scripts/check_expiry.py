"""Falla (exit 1) si el token de Drive o las cookies del foro están a punto de caducar.

Corre como job aparte ("avisos") en listmaker.yml: al fallar, el run sale en
rojo y GitHub manda el email de "run failed", aunque la pipeline haya ido bien.
Solo lee secrets/variables del entorno: no toca el foro ni Drive.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.health import expiry_warnings
from src.runlog import public


def main():
    urgent = False
    for is_urgent, text in expiry_warnings():
        public(("AVISO: " if is_urgent else "OK: ") + text)
        urgent |= is_urgent
    if urgent:
        public("Hay credenciales a punto de caducar: renuévalas (ver documentación, §10).")
        sys.exit(1)


if __name__ == "__main__":
    main()
