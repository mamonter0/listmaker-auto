"""Guarda el log privado del run (logs/run_log.txt) en Drive: Artists/lists/run_log.txt.

Los workflows lo llaman al final con `if: always()`, también cuando algo ha
fallado. Añade el run actual al final del fichero de Drive y recorta por el
principio para que no crezca sin límite. Es independiente del resto del estado:
aunque el paso de download haya fallado, no pisa el historial de logs.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LISTS_FOLDER, RUN_LOG_NAME
from src.drive_auth import get_drive
from src.drive_lists import read_text, write_text
from src.runlog import LOG_PATH, public

MAX_BYTES = 5_000_000  # ~los últimos 30-50 runs
RUN_MARKER = "\n######## RUN "


def main():
    if not os.path.exists(LOG_PATH):
        public("No hay log privado que guardar.")
        return
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        current = f.read()

    now = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M:%S")
    header = (
        f"{RUN_MARKER}{os.environ.get('GITHUB_WORKFLOW', 'local')} "
        f"#{os.environ.get('GITHUB_RUN_NUMBER', '?')} "
        f"(id {os.environ.get('GITHUB_RUN_ID', '?')}) — guardado {now} ########\n"
    )

    drive = get_drive()
    content = read_text(drive, RUN_LOG_NAME) + header + current
    if len(content.encode("utf-8")) > MAX_BYTES:
        tail = content[-MAX_BYTES:]
        cut = tail.find(RUN_MARKER)
        content = tail[cut:] if cut != -1 else tail

    if not write_text(drive, RUN_LOG_NAME, content):
        public(f"No existe '{LISTS_FOLDER}' en Drive; no se guarda el log.")
        return
    public(f"Log privado guardado en Drive ({LISTS_FOLDER}/{RUN_LOG_NAME}, {len(current) // 1024} KB este run).")


if __name__ == "__main__":
    main()
