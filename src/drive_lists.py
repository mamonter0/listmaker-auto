"""Acceso directo a la carpeta Artists/ y a Artists/lists/ en Drive.

Para los pasos que leen o escriben un fichero suelto de lists/ sin pasar por
download.py/upload.py (save_log.py, latest_entries.py).
"""
from .config import ARTISTS_FOLDER_ID, DRIVE_TARGET_FOLDER, LISTS_FOLDER, PARENT_DRIVE_ID

FOLDER_MIME = "application/vnd.google-apps.folder"


def find(drive, title, parent_id, folder=False):
    safe = title.replace("'", "\\'")
    q = f"title='{safe}' and '{parent_id}' in parents and trashed=false"
    if folder:
        q += f" and mimeType='{FOLDER_MIME}'"
    items = drive.ListFile({"q": q}).GetList()
    return items[0] if items else None


def artists_root_id(drive):
    if ARTISTS_FOLDER_ID:
        return ARTISTS_FOLDER_ID
    root = find(drive, DRIVE_TARGET_FOLDER, PARENT_DRIVE_ID, folder=True)
    if root is None:
        raise RuntimeError(f"No encuentro la carpeta '{DRIVE_TARGET_FOLDER}' en Drive.")
    return root["id"]


def lists_folder_id(drive):
    lists = find(drive, LISTS_FOLDER, artists_root_id(drive), folder=True)
    return lists["id"] if lists else None


def read_text(drive, name):
    """Contenido de lists/<name> en Drive, o "" si no existe."""
    lists_id = lists_folder_id(drive)
    item = find(drive, name, lists_id) if lists_id else None
    return item.GetContentString(encoding="utf-8") if item else ""


def write_text(drive, name, content):
    """Crea o sobrescribe lists/<name> en Drive. False si no existe lists/."""
    lists_id = lists_folder_id(drive)
    if lists_id is None:
        return False
    item = find(drive, name, lists_id)
    gfile = drive.CreateFile({"id": item["id"]}) if item else drive.CreateFile({
        "title": name,
        "parents": [{"id": lists_id}],
    })
    gfile.SetContentString(content, encoding="utf-8")
    gfile.Upload()
    return True
