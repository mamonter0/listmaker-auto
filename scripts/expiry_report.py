"""Genera lists/caducidades.txt: cuánto le queda exactamente a cada credencial.

Se regenera en cada run (y tras renew_drive_token.py, que lanza
latest-entries.yml). El tiempo restante se cuenta desde el momento del run.
Solo lee secrets/variables del entorno y escribe un fichero en Drive: no toca
el foro.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LISTS_FOLDER
from src.drive_auth import get_drive
from src.drive_lists import write_text
from src.health import MADRID, credential_status
from src.runlog import public

OUT_NAME = "caducidades.txt"


def human(days):
    """'6 días 23 h 49 min', o 'CADUCADO hace ...' si ya pasó."""
    mins = int(round(abs(days) * 1440))
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    text = f"{d} días {h} h {m} min" if d else f"{h} h {m} min"
    return f"CADUCADO hace {text}" if days < 0 else text


def main():
    now = datetime.now(timezone.utc)
    creds = credential_status(now)
    # La que antes caduca, arriba; las de fecha desconocida, al final.
    creds.sort(key=lambda c: (c["left"] is None, c["left"] or 0))

    lines = [
        "=" * 60,
        f"Caducidad de credenciales — {now.astimezone(MADRID):%Y-%m-%d %H:%M} (hora de Madrid)",
        "(el tiempo restante se cuenta desde esa hora)",
        "=" * 60,
    ]
    for c in creds:
        mark = "!!" if c["urgent"] or (c["left"] is not None and c["left"] < 0) else "ok"
        lines += ["", f"{mark} {c['name']}"]
        if c["expires"] is None:
            lines.append(f"   Estado:   {c['detail']}")
        else:
            lines += [
                f"   Caduca:   {c['expires'].astimezone(MADRID):%Y-%m-%d %H:%M} (Madrid)",
                f"   Quedan:   {human(c['left'])}",
                f"   Detalle:  {c['detail']}",
                f"   Aviso:    email cuando queden {c['warn_days']} días o menos",
            ]
        lines.append(f"   Renovar:  {c['renew']}")
    content = "\n".join(lines) + "\n"

    if not write_text(get_drive(), OUT_NAME, content):
        public(f"No existe '{LISTS_FOLDER}' en Drive; no se genera {OUT_NAME}.")
        sys.exit(1)
    public(f"{OUT_NAME} actualizado ({len(creds)} credenciales).")


if __name__ == "__main__":
    main()
