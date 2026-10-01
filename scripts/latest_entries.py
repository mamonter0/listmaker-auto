"""Genera lists/ultimas_entradas.txt: los últimos capítulos de cada autor por fecha.

Lee la fecha del nombre de cada PDF en Drive (2024-03-15_10-23_Titulo.pdf), así
que no toca el foro. Se regenera entero en cada run: refleja lo que hay en Drive.

En vez de recorrer carpeta a carpeta (cientos de llamadas), pide de golpe todas
las carpetas y todos los PDFs y reconstruye las rutas por sus `parents`.

USO:
    python scripts/latest_entries.py            # 7 por autor
    python scripts/latest_entries.py --per-author 10
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LISTS_FOLDER
from src.drive_auth import get_drive
from src.drive_lists import FOLDER_MIME, artists_root_id, write_text
from src.runlog import public

OUT_NAME = "ultimas_entradas.txt"
FIELDS = "nextPageToken,items(id,title,parents(id))"
DATE_FIRST = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(\d{2})-(\d{2}))?_(.+)$")
DATE_LAST = re.compile(r"^(.+)_(\d{4}-\d{2}-\d{2})(?:_(\d{2})-(\d{2}))?$")


def parse_pdf_name(title):
    """('2024-03-15 10:23', 'Titulo') o None si el nombre no lleva fecha."""
    stem = title[:-4]
    m = DATE_FIRST.match(stem)
    if m:
        day, hh, mm, name = m.groups()
    else:
        m = DATE_LAST.match(stem)
        if not m:
            return None
        name, day, hh, mm = m.groups()
    when = f"{day} {hh}:{mm}" if hh else f"{day}      "
    return when, name


def list_all(drive, q):
    # Sin maxResults: si se pasa, PyDrive2 GetList() devuelve SOLO la primera
    # página. Sin él usa páginas de 1000 y las recorre todas.
    return drive.ListFile({"q": f"{q} and trashed=false", "fields": FIELDS}).GetList()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-author", type=int, default=7)
    args = ap.parse_args()

    drive = get_drive()
    root = artists_root_id(drive)

    folders = {
        f["id"]: (f["title"], f["parents"][0]["id"] if f.get("parents") else None)
        for f in list_all(drive, f"mimeType='{FOLDER_MIME}'")
    }
    pdfs = list_all(drive, "mimeType='application/pdf'")

    authors = sorted(
        (title for title, parent in folders.values() if parent == root and title != LISTS_FOLDER),
        key=str.lower,
    )
    entries = {a: [] for a in authors}
    undated = 0

    for pdf in pdfs:
        if not pdf.get("parents"):
            continue
        # Subir por los padres hasta Artists/: [autor, hilo, (categoría)]
        path, node = [], pdf["parents"][0]["id"]
        while node in folders and node != root and len(path) < 6:
            title, parent = folders[node]
            path.insert(0, title)
            node = parent
        if node != root or len(path) < 2 or path[0] == LISTS_FOLDER:
            continue  # PDF fuera de Artists/ o mal colocado
        parsed = parse_pdf_name(pdf["title"])
        if parsed is None:
            undated += 1
            continue
        when, chapter = parsed
        thread = path[1] + (f" [{'/'.join(path[2:])}]" if len(path) > 2 else "")
        entries.setdefault(path[0], []).append((when, thread, chapter))

    now = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M")
    lines = [
        "=" * 60,
        f"Últimas {args.per_author} entradas por autor — {now}",
        "(más reciente primero; fecha de publicación en el foro)",
        "=" * 60,
    ]
    for author in sorted(entries, key=str.lower):
        latest = sorted(entries[author], reverse=True)[: args.per_author]
        lines.append("")
        lines.append(author)
        if not latest:
            lines.append("  (sin capítulos con fecha)")
        for when, thread, chapter in latest:
            lines.append(f"  {when}  {thread} / {chapter}")
    content = "\n".join(lines) + "\n"

    if not write_text(drive, OUT_NAME, content):
        public(f"No existe '{LISTS_FOLDER}' en Drive; no se genera {OUT_NAME}.")
        sys.exit(1)
    dated = sum(len(v) for v in entries.values())
    public(f"{OUT_NAME}: {len(entries)} autores, {dated} PDFs con fecha ({undated} sin fecha).")


if __name__ == "__main__":
    main()
