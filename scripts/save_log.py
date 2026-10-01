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

from src.config import (
    ARTISTS_FOLDER_ID,
    DRIVE_TARGET_FOLDER,
    LISTS_FOLDER,
    PARENT_DRIVE_ID,
    RUN_LOG_NAME,
)
from src.drive_auth import get_drive
from src.runlog import LOG_PATH, public

MAX_BYTES = 5_000_000  # ~los últimos 30-50 runs
RUN_MARKER = "\n######## RUN "


def _find(drive, title, parent_id, folder=False):
    safe = title.replace("'", "\\'")
    q = f"title='{safe}' and '{parent_id}' in parents and trashed=false"
    if folder:
        q += " and mimeType='application/vnd.google-apps.folder'"
    items = drive.ListFile({"q": q}).GetList()
    return items[0] if items else None


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
    root_id = ARTISTS_FOLDER_ID or _find(drive, DRIVE_TARGET_FOLDER, PARENT_DRIVE_ID, folder=True)["id"]
    lists = _find(drive, LISTS_FOLDER, root_id, folder=True)
    if lists is None:
        public(f"No existe '{LISTS_FOLDER}' en Drive; no se guarda el log.")
        return

    existing = _find(drive, RUN_LOG_NAME, lists["id"])
    previous = existing.GetContentString(encoding="utf-8") if existing else ""

    content = previous + header + current
    if len(content.encode("utf-8")) > MAX_BYTES:
        tail = content[-MAX_BYTES:]
        cut = tail.find(RUN_MARKER)
        content = tail[cut:] if cut != -1 else tail

    gfile = drive.CreateFile({"id": existing["id"]}) if existing else drive.CreateFile({
        "title": RUN_LOG_NAME,
        "parents": [{"id": lists["id"]}],
    })
    gfile.SetContentString(content, encoding="utf-8")
    gfile.Upload()
    public(f"Log privado guardado en Drive ({LISTS_FOLDER}/{RUN_LOG_NAME}, {len(current) // 1024} KB este run).")


if __name__ == "__main__":
    main()
