"""Renombra en Drive los PDFs de `Titulo_FECHA.pdf` a `FECHA_Titulo.pdf`.

Motivo: con la fecha al final, el explorador (y Drive) ordena por titulo, asi que
"Chapter Ten" sale antes que "Chapter Two". Con la fecha delante, el orden
alfabetico coincide con el cronologico.

Es un renombrado de metadatos: NO se re-suben los archivos, solo cambia el titulo.

USO:
    python scripts/rename_pdfs_datefirst.py            # dry-run (no toca nada)
    python scripts/rename_pdfs_datefirst.py --apply    # aplica los cambios

Requiere las mismas credenciales que el resto del pipeline
(GDRIVE_OAUTH_CLIENT_ID / _SECRET / _REFRESH_TOKEN y ARTISTS_FOLDER_ID).
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ARTISTS_FOLDER_ID, DRIVE_TARGET_FOLDER, LISTS_FOLDER, PARENT_DRIVE_ID
from src.drive_auth import get_drive

# Titulo_YYYY-MM-DD[_HH-MM].pdf  ->  captura titulo y fecha
OLD_RE = re.compile(r"^(?P<name>.+)_(?P<date>\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2})?)$")
# Ya empieza por fecha -> no tocar
NEW_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2})?_")

FOLDER_MIME = "application/vnd.google-apps.folder"


def find_root(drive):
    if ARTISTS_FOLDER_ID:
        return ARTISTS_FOLDER_ID
    q = (
        f"title='{DRIVE_TARGET_FOLDER}' and '{PARENT_DRIVE_ID}' in parents "
        f"and mimeType='{FOLDER_MIME}' and trashed=false"
    )
    hits = drive.ListFile({"q": q}).GetList()
    if not hits:
        sys.exit(f"No encuentro la carpeta '{DRIVE_TARGET_FOLDER}' en Drive.")
    return hits[0]["id"]


def walk(drive, folder_id, path, stats, apply_changes):
    """Recorre recursivamente y renombra los PDFs que lo necesiten."""
    try:
        items = drive.ListFile({"q": f"'{folder_id}' in parents and trashed=false"}).GetList()
    except Exception as e:
        print(f"  !! error listando {path}: {e}")
        stats["errors"] += 1
        return

    for item in items:
        title = item["title"]
        if item["mimeType"] == FOLDER_MIME:
            if title == LISTS_FOLDER:
                continue  # el estado no lleva PDFs
            walk(drive, item["id"], f"{path}/{title}", stats, apply_changes)
            continue

        if not title.lower().endswith(".pdf"):
            continue
        stats["pdfs"] += 1
        stem = title[:-4]

        if NEW_RE.match(stem):
            stats["already"] += 1
            continue

        m = OLD_RE.match(stem)
        if not m:
            stats["no_date"] += 1
            print(f"  ?  sin fecha, lo dejo: {path}/{title}")
            continue

        new_title = f"{m.group('date')}_{m.group('name')}.pdf"
        stats["to_rename"] += 1
        print(f"  -> {path}/{title}\n     {new_title}")

        if apply_changes:
            try:
                f = drive.CreateFile({"id": item["id"]})
                f["title"] = new_title
                f.Upload()  # solo metadatos: no re-sube el contenido
                stats["renamed"] += 1
            except Exception as e:
                print(f"     !! fallo renombrando: {e}")
                stats["errors"] += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="aplica los cambios (por defecto dry-run)")
    args = ap.parse_args()

    if not args.apply:
        print("=== DRY-RUN: no se modifica nada. Usa --apply para ejecutar. ===\n")

    drive = get_drive()
    root = find_root(drive)
    stats = {"pdfs": 0, "already": 0, "no_date": 0, "to_rename": 0, "renamed": 0, "errors": 0}

    walk(drive, root, DRIVE_TARGET_FOLDER, stats, args.apply)

    print("\n" + "=" * 55)
    print(f"PDFs encontrados      : {stats['pdfs']}")
    print(f"Ya en formato nuevo   : {stats['already']}")
    print(f"Sin fecha (intactos)  : {stats['no_date']}")
    print(f"A renombrar           : {stats['to_rename']}")
    if args.apply:
        print(f"Renombrados           : {stats['renamed']}")
    print(f"Errores               : {stats['errors']}")
    print("=" * 55)
    if not args.apply and stats["to_rename"]:
        print("\nRevisa la lista y relanza con --apply si te cuadra.")


if __name__ == "__main__":
    main()
