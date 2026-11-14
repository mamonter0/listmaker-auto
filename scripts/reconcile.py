"""Autorreparación: reencola los capítulos que salieron en un delta y no tienen PDF en Drive.

Un capítulo entra en history.json en cuanto el scraper lo ve, y a partir de ahí
ningún delta vuelve a pedirlo. Si después no llega a Drive (la subida falla, el
writer no arranca, el scrape aborta tras guardar...) se quedaba perdido sin
aviso. Este paso compara TODOS los deltas (deltas.jsonl) con los PDFs que hay
de verdad en Drive y devuelve a pending_chapters.json lo que falte; el writer
lo descarga en este mismo run.

No toca el foro: lee lists/ (ya bajado por download.py) y lista Drive.

USO:
    python scripts/reconcile.py            # reencola
    python scripts/reconcile.py --dry-run  # solo cuenta, no escribe nada
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import bootstrap_state
from src.config import (
    ARTISTS_FILE,
    ARTISTS_INDEX_FILE,
    DELTA_JSONL_FILE,
    LOST_CHAPTERS_FILE,
    PENDING_CHAPTERS_FILE,
    RECONCILE_MAX,
    RECONCILE_STATUS_FILE,
    parse_artists_file,
)
from src.drive_auth import get_drive
from src.drive_lists import pdf_paths
from src.health import load_json
from src.runlog import public

DATE_FIRST = re.compile(r"^\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2})?_(.+)$")
DATE_LAST = re.compile(r"^(.+)_\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2})?$")


def sanitize(name):
    # El mismo saneado que Writer.sanitize_filename (no se importa de ahí para
    # no cargar Selenium en un paso que no abre navegador).
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def delta_chapters(path):
    """(autor, hilo, capítulo) de todo lo que algún delta marcó como nuevo."""
    seen = {}
    if not os.path.exists(path):
        return seen
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            for section in ("artists_added", "threads_added", "chapters_added"):
                for artist, threads in entry.get(section, {}).items():
                    for thread, chapters in threads.items():
                        for chapter in chapters:
                            seen.setdefault((artist, thread, chapter), entry.get("timestamp", "?"))
    return seen


def saved_titles(paths):
    """{(autor, hilo): títulos de capítulo con PDF}, con los nombres ya saneados.

    Un PDF puede llamarse FECHA_Título, Título_FECHA (formato antiguo) o
    Título a secas; se apuntan las tres lecturas. Las categorías de un hilo
    son subcarpetas y cuentan para el mismo hilo.
    """
    have = {}
    for path, title in paths:
        stem = title[:-4] if title.lower().endswith(".pdf") else title
        titles = have.setdefault((path[0], path[1]), set())
        titles.add(stem)
        for rx in (DATE_FIRST, DATE_LAST):
            m = rx.match(stem)
            if m:
                titles.add(m.group(1))
    return have


def find_unsaved(deltas, have, pending, lost, active_artists):
    """Capítulos de los deltas sin PDF, que no están ya en cola ni dados por perdidos."""
    lost_keys = {(x.get("artist"), x.get("thread"), x.get("chapter")) for x in lost}
    lost_threads = {(x.get("artist"), x.get("thread")) for x in lost if x.get("chapter") == "__ALL__"}
    out = []
    for (artist, thread, chapter), when in deltas.items():
        if artist not in active_artists:
            continue  # autor quitado de la lista o read_only: nadie lo va a descargar
        if sanitize(chapter) in have.get((sanitize(artist), sanitize(thread)), ()):
            continue
        queued = pending.get(artist, {}).get(thread, [])
        if chapter in queued or "__ALL__" in queued:
            continue
        if (artist, thread, chapter) in lost_keys or (artist, thread) in lost_threads:
            continue
        out.append((when, artist, thread, chapter))
    return sorted(out)


def active_artist_names():
    """Nombres visibles de los autores que siguen en la lista y se descargan."""
    urls = {url for url, read_only in parse_artists_file(ARTISTS_FILE) if not read_only}
    return {name for name, url in load_json(ARTISTS_INDEX_FILE, {}).items() if url in urls}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bootstrap_state()  # artists.txt del secret, igual que scrape/write
    deltas = delta_chapters(DELTA_JSONL_FILE)
    pending = load_json(PENDING_CHAPTERS_FILE, {})
    lost = load_json(LOST_CHAPTERS_FILE, [])
    have = saved_titles(pdf_paths(get_drive()))

    missing = find_unsaved(deltas, have, pending, lost, active_artist_names())
    for when, artist, thread, chapter in missing:
        print(f"   Sin PDF en Drive (delta del {when}): {artist} / {thread} / {chapter}")

    blocked = len(missing) > RECONCILE_MAX
    if args.dry_run:
        public(f"Reconciliación (simulacro): {len(deltas)} capítulos en deltas, {len(missing)} sin PDF en Drive.")
        return

    if blocked:
        public(
            f"Reconciliación: {len(missing)} capítulos de deltas sin PDF en Drive. Son más de "
            f"{RECONCILE_MAX}: NO se reencolan (¿fallo al listar Drive?). Revisar a mano."
        )
    else:
        for _, artist, thread, chapter in missing:
            pending.setdefault(artist, {}).setdefault(thread, []).append(chapter)
        if missing:
            with open(PENDING_CHAPTERS_FILE, "w", encoding="utf-8") as f:
                json.dump(pending, f, indent=2, ensure_ascii=False)
        public(
            f"Reconciliación: {len(deltas)} capítulos en deltas, "
            f"{len(missing)} sin PDF en Drive" + (" → reencolados." if missing else ".")
        )

    with open(RECONCILE_STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "date": datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M"),
            "missing": len(missing),
            "blocked": blocked,
        }, f, indent=2)

    # Para el job `avisos`: que mande el email sin marcar el job `run` como
    # fallido (un `run` fallido haría que el cron de respaldo repitiera el scrapeo).
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"blocked={'true' if blocked else 'false'}\n")


if __name__ == "__main__":
    main()
