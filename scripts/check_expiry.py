"""Falla (exit 1) si alguna credencial (token de Drive, cookies del foro, token de
cron-job.org) está a punto de caducar.

Corre como job aparte ("avisos") en listmaker.yml: al fallar, el run sale en
rojo y GitHub manda el email de "run failed", aunque la pipeline haya ido bien.
Solo lee secrets/variables del entorno: no toca el foro ni Drive.
"""
import os
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
    # Lo deja el paso de reconciliación del job `run` cuando faltan en Drive
    # demasiados capítulos como para reencolarlos solos.
    blocked = os.environ.get("RECONCILE_BLOCKED") == "true"
    if blocked:
        public("AVISO: faltan en Drive muchos capítulos de deltas y no se han reencolado (documentación, §6.1).")
    if urgent or blocked:
        sys.exit(1)


if __name__ == "__main__":
    main()
