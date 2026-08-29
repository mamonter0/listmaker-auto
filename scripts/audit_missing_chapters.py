"""Compara el indice de threadmarks del foro contra lo que hay en Drive.

El pipeline solo descarga lo que aparece en el delta. Un capitulo perdido por
un corte, un rate-limit o un bug queda marcado como conocido en history.json y
no vuelve a salir nunca. Este script detecta esos huecos yendo a la fuente:
lista TODOS los threadmarks de cada hilo de cada autor y los cruza con los PDFs
que hay realmente en Drive.

Solo lee: no descarga ni modifica nada. Deja el resultado en
`lists/missing_chapters.json` para que el recuperador lo consuma despues.

USO:
    python scripts/audit_missing_chapters.py
    python scripts/audit_missing_chapters.py --limit 5     # primeros N autores
    python scripts/audit_missing_chapters.py --artist koss # un autor suelto
"""
import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import bootstrap_state
from src.config import (
    ARTISTS_FILE,
    ARTISTS_FOLDER_ID,
    ARTISTS_INDEX_FILE,
    DRIVE_TARGET_FOLDER,
    LIST_DIR,
    LISTS_FOLDER,
    PARENT_DRIVE_ID,
    parse_artists_file,
)
from src.drive_auth import get_drive
from src.writer import Writer

FOLDER_MIME = "application/vnd.google-apps.folder"
DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d{2}-\d{2})?_")
DATE_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2}(_\d{2}-\d{2})?$")
OUT_FILE = os.path.join(LIST_DIR, "missing_chapters.json")


def strip_date(stem):
    """Quita la fecha (delante o detras) para quedarnos con el titulo."""
    s = DATE_PREFIX.sub("", stem)
    return DATE_SUFFIX.sub("", s)


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


def index_drive(drive, folder_id, artist=None, thread=None, out=None):
    """{(carpeta_artista, carpeta_hilo): set(titulos sin fecha)} de lo que hay en Drive."""
    if out is None:
        out = {}
    try:
        items = drive.ListFile({"q": f"'{folder_id}' in parents and trashed=false"}).GetList()
    except Exception as e:
        print(f"  !! error listando Drive: {e}")
        return out

    for item in items:
        title = item["title"]
        if item["mimeType"] == FOLDER_MIME:
            if title == LISTS_FOLDER:
                continue
            if artist is None:
                index_drive(drive, item["id"], title, None, out)
            elif thread is None:
                index_drive(drive, item["id"], artist, title, out)
            else:
                index_drive(drive, item["id"], artist, thread, out)  # subcategoria
            continue
        if not title.lower().endswith(".pdf"):
            continue
        out.setdefault((artist, thread), set()).add(strip_date(title[:-4]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="solo los primeros N autores")
    ap.add_argument("--artist", default="", help="filtrar por subcadena del perfil")
    args = ap.parse_args()

    bootstrap_state()
    drive = get_drive()

    print("Indexando lo que hay en Drive...")
    have = index_drive(drive, find_root(drive))
    total_have = sum(len(v) for v in have.values())
    print(f"  {total_have} PDFs en {len(have)} hilos\n")

    entries = parse_artists_file(ARTISTS_FILE)
    if args.artist:
        entries = [e for e in entries if args.artist.lower() in e[0].lower()]
    if args.limit:
        entries = entries[: args.limit]
    print(f"Auditando {len(entries)} autores...\n")

    artists_index = {}
    if os.path.exists(ARTISTS_INDEX_FILE):
        with open(ARTISTS_INDEX_FILE, "r", encoding="utf-8") as fh:
            artists_index = json.load(fh)
    url_to_name = {u: n for n, u in artists_index.items()}

    w = Writer()
    missing = {}
    stats = {"threads": 0, "chapters": 0, "missing": 0, "no_threads": 0}

    try:
        if not w.load_cookies():
            sys.exit("No se pudieron cargar las cookies.")

        for i, (profile_url, read_only) in enumerate(entries, 1):
            if read_only:
                continue
            name = url_to_name.get(profile_url) or profile_url.rstrip("/").split("/")[-1]
            folder = w.sanitize_filename(name)
            print(f"[{i}/{len(entries)}] {name}")

            threads = w.find_thread_urls_for_artist(profile_url)
            if not threads:
                print("   sin hilos (perfil caido o sin resultados)")
                stats["no_threads"] += 1
                continue

            # Carpetas de Drive de este autor (para casar titulos que cambiaron)
            drive_folders = [t for (a, t) in have if a == folder]
            print(f"   carpeta Drive '{folder}': {len(drive_folders)} hilos | foro: {len(threads)} hilos")
            if not drive_folders:
                # Nada bajo ese nombre: puede que la carpeta se llame distinto.
                todas = sorted({a for (a, _t) in have})
                cerca = difflib.get_close_matches(folder, todas, n=3, cutoff=0.6)
                print(f"   !! sin carpeta en Drive para '{folder}'. Parecidas: {cerca}")

            for th_title, th_url in threads.items():
                stats["threads"] += 1
                th_folder = w.sanitize_filename(th_title)
                stem_set = have.get((folder, th_folder))
                if stem_set is None:
                    near = difflib.get_close_matches(th_folder, drive_folders, n=1, cutoff=0.80)
                    if near:
                        print(f"   ~ '{th_folder}' -> carpeta '{near[0]}'")
                        stem_set = have.get((folder, near[0]), set())
                    else:
                        # Sin carpeta: o nunca se bajo, o el nombre no casa.
                        cand = difflib.get_close_matches(th_folder, drive_folders, n=2, cutoff=0.5)
                        print(f"   ?? sin carpeta para '{th_folder}'"
                              + (f" (parecidas: {cand})" if cand else " (ninguna parecida)"))
                        stem_set = set()

                chapters = w.get_all_chapter_urls(th_url)
                stats["chapters"] += len(chapters)
                gaps = [
                    {"chapter": c, "url": info["url"], "category": info["category"]}
                    for c, info in chapters.items()
                    if w.sanitize_filename(c) not in stem_set
                ]
                if gaps:
                    stats["missing"] += len(gaps)
                    print(f"   {th_title}: faltan {len(gaps)} de {len(chapters)}")
                    missing.setdefault(name, {})[th_title] = {
                        "profile_url": profile_url,
                        "thread_url": th_url,
                        "chapters": gaps,
                    }
    finally:
        w.close()

    os.makedirs(LIST_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(missing, fh, indent=2, ensure_ascii=False)

    print("\n" + "=" * 55)
    print(f"Hilos revisados       : {stats['threads']}")
    print(f"Capitulos en el foro  : {stats['chapters']}")
    print(f"PDFs en Drive         : {total_have}")
    print(f"FALTAN                : {stats['missing']}")
    print(f"Autores sin hilos     : {stats['no_threads']}")
    print(f"Detalle en            : {OUT_FILE}")
    print("=" * 55)


if __name__ == "__main__":
    main()
