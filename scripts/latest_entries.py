"""Genera lists/ultimas_entradas.txt: los últimos capítulos de cada autor por fecha.

Arriba va un resumen de actividad (autores ordenados por su última entrada,
el que lleva más tiempo sin publicar primero) y debajo el detalle por autor.

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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (
    ARTISTS_INDEX_FILE,
    FAIL_STREAKS_FILE,
    LISTS_FOLDER,
    LOST_CHAPTERS_FILE,
    PENDING_ATTEMPTS_FILE,
    PENDING_CHAPTERS_FILE,
)
from src.drive_auth import get_drive
from src.drive_lists import FOLDER_MIME, artists_root_id, write_text
from src.health import FAIL_STREAK_WARN, expiry_warnings, load_json
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


def status_sections(today):
    """AVISOS (caducidades) y PROBLEMAS (autores que fallan, capítulos atascados).

    Lee los ficheros de lists/ que hay en local: en listmaker.yml son los de
    este mismo run; en latest-entries.yml, los que baja download.py.
    """
    lines = ["", "AVISOS"]
    for urgent, text in expiry_warnings():
        lines.append(("  !! " if urgent else "  ok ") + text)

    lines += ["", "PROBLEMAS"]
    n_before = len(lines)
    url_to_name = {u: n for n, u in load_json(ARTISTS_INDEX_FILE, {}).items()}

    streaks = load_json(FAIL_STREAKS_FILE, {})
    bad = sorted((s["count"], u, s) for u, s in streaks.items() if s.get("count", 0) >= FAIL_STREAK_WARN)
    if bad:
        lines.append(f"  Autores que fallan {FAIL_STREAK_WARN}+ ejecuciones seguidas:")
        for count, url, s in reversed(bad):
            who = url_to_name.get(url) or url.rstrip("/").split("/")[-1]
            lines.append(f"    {who}: {count} seguidas desde {s.get('since', '?')} ({s.get('reason', '')})")

    pending = load_json(PENDING_CHAPTERS_FILE, {})
    attempts = load_json(PENDING_ATTEMPTS_FILE, {})
    stuck = [(a, t, c) for a, ths in pending.items() for t, cs in ths.items() for c in cs]
    if stuck:
        lines.append(f"  Capítulos pendientes de descargar: {len(stuck)}")
        for a, t, c in stuck[:20]:
            n = attempts.get(f"{a}|{t}|{c}", 0)
            lines.append(f"    {a} / {t} / {c}" + (f"  ({n} intentos fallidos)" if n else ""))
        if len(stuck) > 20:
            lines.append(f"    ... y {len(stuck) - 20} más")

    cutoff = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    lost = [x for x in load_json(LOST_CHAPTERS_FILE, []) if x.get("date", "") >= cutoff]
    if lost:
        lines.append(f"  Capítulos dados por perdidos (últimos 30 días): {len(lost)}")
        for x in lost[-20:]:
            lines.append(f"    {x.get('date', '')[:10]}  {x.get('artist')} / {x.get('thread')} / "
                         f"{x.get('chapter')} — {x.get('reason')}")

    if len(lines) == n_before:
        lines.append("  Ninguno.")
    return lines


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

    today = datetime.now(ZoneInfo("Europe/Madrid"))
    now = today.strftime("%Y-%m-%d %H:%M")
    lines = [
        "=" * 60,
        f"Últimas {args.per_author} entradas por autor — {now}",
        "(más reciente primero; fecha de publicación en el foro)",
        "=" * 60,
    ]
    lines += status_sections(today)

    # Resumen de actividad: autores por su última entrada, el más parado arriba.
    # Sirve para ver de un vistazo quién lleva tiempo sin publicar.
    last = {a: max(e)[0] for a, e in entries.items() if e}
    lines += ["", "ACTIVIDAD (autor por su última entrada, el más antiguo primero)"]
    for author in sorted(last, key=lambda a: last[a]):
        days = (today.date() - datetime.strptime(last[author][:10], "%Y-%m-%d").date()).days
        lines.append(f"  {last[author][:10]}  hace {days:>4} días  {author}")
    for author in sorted((a for a in entries if a not in last), key=str.lower):
        lines.append(f"  (sin fecha)                  {author}")
    lines += ["", "=" * 60, "DETALLE POR AUTOR", "=" * 60]
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
