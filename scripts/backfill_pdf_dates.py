"""Recupera del foro la fecha de los PDFs que se guardaron sin ella.

Los PDFs descargados antes de que el Writer extrajera la fecha se llaman
`Titulo.pdf` a secas. Este script vuelve al foro, localiza cada capitulo en el
indice de threadmarks de su hilo, lee la fecha del post y renombra el fichero
en Drive a `FECHA_Titulo.pdf`.

Optimizacion: el indice de threadmarks ya trae la fecha de cada entrada, asi
que basta UNA carga de pagina por hilo. Solo si el indice no la trae se visita
el post concreto (--visit-missing).

USO:
    python scripts/backfill_pdf_dates.py                  # dry-run
    python scripts/backfill_pdf_dates.py --apply          # renombra en Drive
    python scripts/backfill_pdf_dates.py --limit 20       # prueba con pocos hilos
"""
import argparse
import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from src.bootstrap import bootstrap_state
from src.config import (
    ARTISTS_FILE,
    ARTISTS_FOLDER_ID,
    ARTISTS_INDEX_FILE,
    DRIVE_TARGET_FOLDER,
    LISTS_FOLDER,
    MAX_PAGES_PER_LOOP,
    PARENT_DRIVE_ID,
    parse_artists_file,
)
from src.drive_auth import get_drive
from src.writer import Writer

FOLDER_MIME = "application/vnd.google-apps.folder"
# Nombres que YA llevan fecha (delante o detras): no hay nada que recuperar.
HAS_DATE = re.compile(r"(^\d{4}-\d{2}-\d{2}(_\d{2}-\d{2})?_)|(_\d{4}-\d{2}-\d{2}(_\d{2}-\d{2})?$)")


# ---------------------------------------------------------------- Drive walk
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


def collect_undated(drive, folder_id, artist=None, thread=None, out=None):
    """Devuelve [{id, stem, artist, thread}] de los PDFs sin fecha en el nombre."""
    if out is None:
        out = []
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
                collect_undated(drive, item["id"], title, None, out)      # nivel artista
            elif thread is None:
                collect_undated(drive, item["id"], artist, title, out)    # nivel hilo
            else:
                collect_undated(drive, item["id"], artist, thread, out)   # subcategoria
            continue
        if not title.lower().endswith(".pdf"):
            continue
        stem = title[:-4]
        if HAS_DATE.search(stem):
            continue
        out.append({"id": item["id"], "stem": stem, "artist": artist, "thread": thread})
    return out


# ------------------------------------------------------------ forum scraping
def scrape_threadmark_dates(w, thread_base_url):
    """{titulo_capitulo: 'YYYY-MM-DD_HH-MM'} leyendo el indice del hilo.

    El indice de XenForo ya incluye un <time datetime> por fila, asi que no hace
    falta abrir cada capitulo.
    """
    dates = {}
    to_visit = [thread_base_url + "threadmarks"]
    visited = set()

    while to_visit:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not w._safe_get(url):
            continue

        try:
            w.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".block-body")))
            cat_urls = w.driver.execute_script(
                "return Array.from(document.querySelectorAll(\"a[href*='threadmark_category=']\"))"
                ".map(a => a.href.split('#')[0]);"
            )
            for c in cat_urls or []:
                if c and c not in visited and c not in to_visit:
                    to_visit.append(c)
        except Exception:
            pass

        pages = 0
        seen_pages = set()
        while True:
            pages += 1
            if pages > MAX_PAGES_PER_LOOP:
                break
            try:
                cur = w.driver.current_url
            except Exception:
                cur = None
            if cur and cur in seen_pages:
                break
            if cur:
                seen_pages.add(cur)

            try:
                rows = w.driver.execute_script("""
                    let out = [];
                    let els = document.querySelectorAll(
                        "div.structItem--threadmark .structItem-title a, li.threadmarkItem a");
                    els.forEach(el => {
                        let row = el.closest('.structItem, li.threadmarkItem, tr');
                        let t = row ? row.querySelector('time[datetime]') : null;
                        out.push({
                            title: el.innerText.trim(),
                            date: t ? (t.getAttribute('datetime') || '') : ''
                        });
                    });
                    return out;
                """) or []
            except WebDriverException:
                break

            for r in rows:
                if r["title"] and r["date"] and r["title"] not in dates:
                    dates[r["title"]] = iso_to_stamp(r["date"])

            try:
                nxt = w.driver.find_element(By.CSS_SELECTOR, "a.pageNav-jump--next")
                nurl = nxt.get_attribute("href")
                if not nurl or nurl in seen_pages or not w._safe_get(nurl):
                    break
            except Exception:
                break
    return dates


def iso_to_stamp(raw):
    """'2024-03-15T10:23:45+0000' -> '2024-03-15_10-23'."""
    try:
        d, rest = raw.split("T", 1)
        t = rest.split("+")[0].split("Z")[0].split(".")[0].split(":")
        return f"{d}_{t[0]}-{t[1] if len(t) > 1 else '00'}"
    except Exception:
        return raw.split("T")[0]


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="renombra de verdad (por defecto dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="procesar solo N hilos (prueba)")
    ap.add_argument("--visit-missing", action="store_true",
                    help="si el indice no da fecha, abrir el post del capitulo")
    args = ap.parse_args()

    if not args.apply:
        print("=== DRY-RUN: no se modifica nada. Usa --apply para ejecutar. ===\n")

    bootstrap_state()
    drive = get_drive()

    print("Escaneando Drive en busca de PDFs sin fecha...")
    undated = collect_undated(drive, find_root(drive))
    print(f"PDFs sin fecha: {len(undated)}")
    if not undated:
        return

    # Agrupar por (artista, hilo): un scrape de indice por hilo, no por fichero.
    groups = {}
    for f in undated:
        groups.setdefault((f["artist"], f["thread"]), []).append(f)
    print(f"Hilos afectados: {len(groups)}\n")

    artists_index = {}
    if os.path.exists(ARTISTS_INDEX_FILE):
        with open(ARTISTS_INDEX_FILE, "r", encoding="utf-8") as fh:
            artists_index = json.load(fh)
    else:
        print(f"Aviso: falta {ARTISTS_INDEX_FILE}, tiro solo de artists.txt")

    # Fallback: artists.txt. El indice solo tiene los autores vistos en runs
    # recientes; los PDFs viejos pueden ser de autores que no estan ahi.
    artist_urls = [u for u, _ in parse_artists_file(ARTISTS_FILE)]

    w = Writer()
    # carpeta_saneada -> url de perfil
    by_folder = {w.sanitize_filename(name): url for name, url in artists_index.items()}
    stats = {"renamed": 0, "no_thread": 0, "no_match": 0, "no_date": 0, "errors": 0}

    try:
        if not w.load_cookies():
            sys.exit("No se pudieron cargar las cookies.")

        thread_cache = {}   # artista -> {titulo_saneado: url}
        processed = 0

        for (artist, thread), files in sorted(groups.items()):
            if args.limit and processed >= args.limit:
                print(f"\n(--limit {args.limit} alcanzado, paro)")
                break
            processed += 1
            print(f"\n[{processed}/{len(groups)}] {artist} / {thread}  ({len(files)} PDFs)")

            # 1) indice exacto  2) fallback por substring contra artists.txt
            profile = by_folder.get(artist) or w.resolve_artist_url(
                artist, artists_index, artist_urls
            )
            if not profile:
                print("   sin URL de perfil (ni en el indice ni en artists.txt) — salto")
                stats["no_thread"] += len(files)
                continue

            if artist not in thread_cache:
                thread_cache[artist] = {
                    w.sanitize_filename(t): u
                    for t, u in w.find_thread_urls_for_artist(profile).items()
                }
            threads_found = thread_cache[artist]
            thread_url = threads_found.get(thread)
            if not thread_url:
                # El titulo pudo cambiar desde que se descargo (tags editados,
                # puntuacion...). Buscamos el mas parecido antes de rendirnos.
                near = difflib.get_close_matches(thread, list(threads_found), n=1, cutoff=0.80)
                if near:
                    thread_url = threads_found[near[0]]
                    print(f"   titulo cambiado, uso el mas parecido: '{near[0]}'")
            if not thread_url:
                print("   hilo no encontrado en el perfil — salto")
                stats["no_thread"] += len(files)
                continue

            dates = scrape_threadmark_dates(w, thread_url)
            by_stem = {w.sanitize_filename(t): d for t, d in dates.items()}
            print(f"   indice: {len(by_stem)} capitulos con fecha")

            for f in files:
                stamp = by_stem.get(f["stem"])
                if not stamp and args.visit_missing:
                    chap_map = w.get_all_chapter_urls(thread_url)
                    url = next((v["url"] for k, v in chap_map.items()
                                if w.sanitize_filename(k) == f["stem"]), None)
                    if url and w._safe_get(url):
                        time.sleep(1.5)
                        stamp = w.get_post_datetime() or None
                if not stamp:
                    print(f"   ?  sin fecha: {f['stem']}")
                    stats["no_match" if not by_stem else "no_date"] += 1
                    continue

                new_title = f"{stamp}_{f['stem']}.pdf"
                print(f"   -> {f['stem']}.pdf\n      {new_title}")
                if args.apply:
                    try:
                        gf = drive.CreateFile({"id": f["id"]})
                        gf["title"] = new_title
                        gf.Upload()   # solo metadatos
                        stats["renamed"] += 1
                    except Exception as e:
                        print(f"      !! fallo renombrando: {e}")
                        stats["errors"] += 1
                else:
                    stats["renamed"] += 1
    finally:
        w.close()

    print("\n" + "=" * 55)
    print(f"{'Renombrados' if args.apply else 'Renombrables'} : {stats['renamed']}")
    print(f"Hilo no localizado    : {stats['no_thread']}")
    print(f"Capitulo no localizado: {stats['no_match']}")
    print(f"Sin fecha en el indice: {stats['no_date']}")
    print(f"Errores               : {stats['errors']}")
    print("=" * 55)


if __name__ == "__main__":
    main()
