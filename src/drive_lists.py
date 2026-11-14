"""Acceso directo a la carpeta Artists/ y a Artists/lists/ en Drive.

Para los pasos que leen o escriben un fichero suelto de lists/ sin pasar por
download.py/upload.py (save_log.py, latest_entries.py, reconcile.py), y para
listar de golpe todos los PDFs que hay bajo Artists/.
"""
import time

from .config import ARTISTS_FOLDER_ID, DRIVE_TARGET_FOLDER, LISTS_FOLDER, PARENT_DRIVE_ID

FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_FIELDS = "nextPageToken,items(id,title,parents(id))"
RETRY_DELAYS = (3, 10, 30)  # segundos entre intentos de una llamada a Drive


def with_retries(fn, what):
    """Ejecuta fn(); si falla, reintenta con pausas. Relanza el último error.

    Drive devuelve de vez en cuando un 500 o una respuesta vacía ("Expecting
    value: line 1 column 1"). Sin reintento, un PDF que no subía se perdía
    para siempre: el writer ya lo había dado por hecho.
    """
    for delay in RETRY_DELAYS:
        try:
            return fn()
        except Exception as e:
            print(f"   {what}: {type(e).__name__}: {e} — reintento en {delay}s")
            time.sleep(delay)
    return fn()


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


def list_all(drive, q):
    # Sin maxResults: si se pasa, PyDrive2 GetList() devuelve SOLO la primera
    # página. Sin él usa páginas de 1000 y las recorre todas.
    return with_retries(
        lambda: drive.ListFile({"q": f"{q} and trashed=false", "fields": PDF_FIELDS}).GetList(),
        "Error listando Drive",
    )


def pdf_paths(drive):
    """[(ruta, título)] de cada PDF bajo Artists/, con ruta = [autor, hilo, (categoría...)].

    En vez de recorrer carpeta a carpeta (cientos de llamadas), pide de golpe
    todas las carpetas y todos los PDFs y reconstruye las rutas por `parents`.
    """
    root = artists_root_id(drive)
    folders = {
        f["id"]: (f["title"], f["parents"][0]["id"] if f.get("parents") else None)
        for f in list_all(drive, f"mimeType='{FOLDER_MIME}'")
    }
    out = []
    for pdf in list_all(drive, "mimeType='application/pdf'"):
        if not pdf.get("parents"):
            continue
        # Subir por los padres hasta Artists/
        path, node = [], pdf["parents"][0]["id"]
        while node in folders and node != root and len(path) < 6:
            title, parent = folders[node]
            path.insert(0, title)
            node = parent
        if node != root or len(path) < 2 or path[0] == LISTS_FOLDER:
            continue  # PDF fuera de Artists/ o mal colocado
        out.append((path, pdf["title"]))
    return out
