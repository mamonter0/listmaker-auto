"""Recupera por tandas los capitulos que faltan en Drive.

Pensado para correr desatendido en un cron: cada ejecucion se come un trozo de
la cola, guarda el avance en Drive y se para.

Estado en `lists/recovery_queue.json` (lo sincronizan download.py / upload.py):
    {
      "artists":       ["fakeking", ...],   # filtros con los que se construyo
      "built_at":      "2026-08-30 12:00:00",
      "pending":       [ {artist, thread, chapter, url, category, attempts}, ... ],
      "failed":        [ ... ],             # dados por perdidos tras MAX_ATTEMPTS
      "done":          123,                 # contador acumulado
      "verifications": 1,                   # veces que se re-audito al vaciarse
      "completed_at":  "2026-09-18 22:30"   # solo cuando Drive confirma que no falta nada
    }

Como sabe que ha terminado: NO basta con que la cola se vacie. La cola solo sabe
lo que ella misma tacho, y tacha un capitulo en cuanto el PDF existe en el disco
del runner; la subida a Drive es un paso posterior que puede fallar. Por eso,
cuando la cola se queda vacia, la siguiente ejecucion vuelve a auditar el foro
contra Drive. Si sigue faltando algo, lo reencola. Solo cuando la auditoria da
cero se marca `completed_at`, y a partir de ahi cada ejecucion sale al instante
(el workflow ademas desactiva su propio cron).

USO:
    python scripts/recover_missing.py --artists "fakeking,infonticus" --batch 200
    python scripts/recover_missing.py --batch 200          # sigue la cola existente
    python scripts/recover_missing.py --rebuild --artists "..."   # empieza de cero
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from selenium.common.exceptions import WebDriverException

from src.bootstrap import bootstrap_state
from src.config import (
    ARTISTS_FILE,
    ARTISTS_INDEX_FILE,
    LIST_DIR,
    LOCAL_FOLDER,
    parse_artists_file,
)
from src.writer import Writer

QUEUE_FILE = os.path.join(LIST_DIR, "recovery_queue.json")

# Un capitulo que falla estas veces (en noches distintas) se da por perdido.
MAX_ATTEMPTS = 3
# Re-auditorias maximas al vaciarse la cola. Evita un bucle eterno si algo se
# descarga bien pero nunca llega a Drive.
MAX_VERIFICATIONS = 3


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def set_output(key, value):
    """Pasa un valor a los pasos siguientes del workflow (solo en Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return None
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            q = json.load(f)
        return q if isinstance(q, dict) and "pending" in q else None
    except Exception as e:
        print(f"{QUEUE_FILE} ilegible ({e}); se reconstruira.")
        return None


def save_queue(q):
    os.makedirs(LIST_DIR, exist_ok=True)
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(q, f, indent=2, ensure_ascii=False)
    os.replace(tmp, QUEUE_FILE)  # atomico: nunca dejar la cola a medio escribir


def register_failure(queue, pending, item):
    """Cuenta un intento fallido. Devuelve 1 si el capitulo se da por perdido.

    Si aun le quedan intentos lo manda al FINAL de la cola: cada tanda coge la
    cabeza, y un capitulo roto que se quedara delante bloquearia a los demas.
    """
    item["attempts"] = item.get("attempts", 0) + 1
    pending.remove(item)
    if item["attempts"] >= MAX_ATTEMPTS:
        queue.setdefault("failed", []).append(item)
        print(f"   {MAX_ATTEMPTS} intentos fallidos: se da por perdido")
        return 1
    pending.append(item)
    return 0


def build_queue(w, drive, artist_filters):
    """Audita el foro contra Drive y devuelve la cola de capitulos que faltan."""
    # Import diferido: solo hace falta al construir o verificar, no al consumir.
    from scripts.audit_missing_chapters import find_root, index_drive  # noqa: E402
    import difflib

    print("Indexando Drive...")
    have = index_drive(drive, find_root(drive))
    print(f"  {sum(len(v) for v in have.values())} PDFs en {len(have)} hilos")

    entries = parse_artists_file(ARTISTS_FILE)
    wanted = [a.strip().lower() for a in artist_filters if a.strip()]
    if not wanted:
        sys.exit("build_queue necesita al menos un filtro de artista.")
    entries = [e for e in entries if any(a in e[0].lower() for a in wanted)]
    if not entries:
        sys.exit(f"Ningun perfil casa con {wanted}")
    print(f"Auditando {len(entries)} autores...\n")

    # El nombre visible del autor ("Fakeking") es el que da nombre a la carpeta.
    # El slug de la URL ("fakeking") NO sirve: difiere en mayusculas y no casaria.
    artists_index = {}
    if os.path.exists(ARTISTS_INDEX_FILE):
        with open(ARTISTS_INDEX_FILE, "r", encoding="utf-8") as fh:
            artists_index = json.load(fh)
    url_to_name = {u: n for n, u in artists_index.items()}

    pending = []
    for i, (profile_url, read_only) in enumerate(entries, 1):
        if read_only:
            continue
        name = url_to_name.get(profile_url)
        if not name:
            print(f"[{i}/{len(entries)}] {profile_url} — sin nombre en artists_index.json, salto")
            continue
        threads = w.find_thread_urls_for_artist(profile_url)
        if not threads:
            print(f"[{i}/{len(entries)}] {name} — sin hilos, salto")
            continue
        folder = w.sanitize_filename(name)
        drive_folders = [t for (a, t) in have if a == folder]
        print(f"[{i}/{len(entries)}] {name}: {len(threads)} hilos en el foro, "
              f"{len(drive_folders)} en Drive")

        for th_title, th_url in threads.items():
            th_folder = w.sanitize_filename(th_title)
            stems = have.get((folder, th_folder))
            if stems is None:
                near = difflib.get_close_matches(th_folder, drive_folders, n=1, cutoff=0.80)
                stems = have.get((folder, near[0]), set()) if near else set()
            chapters = w.get_all_chapter_urls(th_url)
            gaps = [
                {
                    "artist": name,
                    "thread": th_title,
                    "chapter": c,
                    "url": info["url"],
                    "category": info["category"],
                }
                for c, info in chapters.items()
                if w.sanitize_filename(c) not in stems
            ]
            if gaps:
                print(f"     {th_title}: faltan {len(gaps)} de {len(chapters)}")
                pending.extend(gaps)

    return {
        "artists": wanted,
        "built_at": now(),
        "pending": pending,
        "failed": [],
        "done": 0,
        "verifications": 0,
    }


def verify(w, queue):
    """Re-audita Drive. Devuelve lo que sigue faltando, sin los ya perdidos."""
    from src.drive_auth import get_drive
    fresh = build_queue(w, get_drive(), queue.get("artists", []))
    lost = {it["url"] for it in queue.get("failed", [])}
    return [it for it in fresh["pending"] if it["url"] not in lost]


def download_one(w, item, pause_min, pause_max):
    """Descarga un capitulo. True si quedo el PDF en disco."""
    safe_art = w.sanitize_filename(item["artist"])
    safe_th = w.sanitize_filename(item["thread"])
    safe_ch = w.sanitize_filename(item["chapter"])
    cat = (item.get("category") or "").strip()

    save_dir = os.path.join(LOCAL_FOLDER, safe_art, safe_th)
    if cat and cat.lower() not in ("threadmarks", ""):
        save_dir = os.path.join(save_dir, w.sanitize_filename(cat))
    os.makedirs(save_dir, exist_ok=True)

    if w._chapter_already_saved(save_dir, safe_ch):
        return True

    if not w._safe_get(item["url"]):
        return False
    time.sleep(pause_min + random.uniform(0, pause_max - pause_min))

    post_dt = w.get_post_datetime()
    prefix = f"{post_dt}_" if post_dt else ""
    path = os.path.join(save_dir, f"{prefix}{safe_ch}.pdf")
    w.isolate_and_print(path)
    if not os.path.exists(path):
        return False

    try:
        w.driver.get("about:blank")
    except WebDriverException:
        if not w._driver_alive():
            w._recover_driver()
    time.sleep(pause_min + random.uniform(0, pause_max - pause_min))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artists", default="", help="subcadenas separadas por coma (solo al construir)")
    ap.add_argument("--batch", type=int, default=200, help="capitulos por ejecucion")
    ap.add_argument("--rebuild", action="store_true", help="reconstruir la cola desde cero")
    ap.add_argument("--pause-min", type=float, default=3.0, help="pausa minima entre paginas (s)")
    ap.add_argument("--pause-max", type=float, default=6.0, help="pausa maxima entre paginas (s)")
    args = ap.parse_args()

    bootstrap_state()
    queue = None if args.rebuild else load_queue()

    # Terminada y verificada: salir antes de arrancar Chrome o tocar el foro.
    if queue is not None and queue.get("completed_at"):
        print(f"Recuperacion terminada y verificada el {queue['completed_at']}. Nada que hacer.")
        set_output("completed", "true")
        return

    ok = fail = lost = 0
    w = Writer()
    try:
        if not w.load_cookies():
            sys.exit("No se pudieron cargar las cookies.")

        if queue is None:
            from src.drive_auth import get_drive
            filters = args.artists.split(",") if args.artists else []
            if not filters:
                sys.exit("No hay cola y no se paso --artists para construirla.")
            queue = build_queue(w, get_drive(), filters)
            save_queue(queue)
            print(f"\nCola construida: {len(queue['pending'])} capitulos pendientes.")

        pending = queue.get("pending", [])

        if not pending:
            # La cola dice que esta todo, pero solo sabe lo que ella misma tacho.
            # Ahora los PDFs de la tanda anterior ya estan subidos: comprobamos Drive.
            print("Cola vacia. Verificando contra Drive que no falta nada...")
            still = verify(w, queue)
            queue["verifications"] = queue.get("verifications", 0) + 1

            if not still:
                queue["completed_at"] = now()
                save_queue(queue)
                set_output("completed", "true")
                print(f"Verificado: todo esta en Drive "
                      f"({len(queue.get('failed', []))} dados por perdidos). Terminado.")
                return

            if queue["verifications"] >= MAX_VERIFICATIONS:
                # Se descargan pero no aparecen en Drive tras varias vueltas:
                # algo sistematico, no insistimos para siempre.
                for it in still:
                    it["reason"] = "no llega a Drive tras varias verificaciones"
                queue.setdefault("failed", []).extend(still)
                queue["completed_at"] = now()
                save_queue(queue)
                set_output("completed", "true")
                print(f"{len(still)} siguen sin aparecer tras {MAX_VERIFICATIONS} "
                      f"verificaciones; pasan a 'failed'. Terminado.")
                return

            print(f"La verificacion encontro {len(still)} que siguen faltando; se reencolan.")
            queue["pending"] = pending = still
            save_queue(queue)

        batch = pending[: args.batch]
        print(f"\nPendientes: {len(pending)} | esta tanda: {len(batch)}\n")

        try:
            for n, item in enumerate(batch, 1):
                print(f"[{n}/{len(batch)}] {item['artist']} / {item['thread']} / {item['chapter']}")
                try:
                    if download_one(w, item, args.pause_min, args.pause_max):
                        ok += 1
                        pending.remove(item)
                    else:
                        fail += 1
                        lost += register_failure(queue, pending, item)
                except RuntimeError as e:
                    # Rate limit persistente: paramos la tanda sin castigar al capitulo.
                    print(f"   Abortando tanda: {e}")
                    break
                except WebDriverException as e:
                    print(f"   Error de driver: {type(e).__name__}")
                    fail += 1
                    lost += register_failure(queue, pending, item)
                    if not w._driver_alive() and not w._recover_driver():
                        break
        finally:
            queue["pending"] = pending
            queue["done"] = queue.get("done", 0) + ok
            save_queue(queue)
    finally:
        w.close()

    print("\n" + "=" * 55)
    print(f"Descargados en esta tanda : {ok}")
    print(f"Fallidos (se reintentan)  : {fail - lost}")
    print(f"Dados por perdidos        : {lost} (total {len(queue.get('failed', []))})")
    print(f"Quedan pendientes         : {len(queue.get('pending', []))}")
    print(f"Acumulado total           : {queue.get('done', 0)}")
    if not queue.get("pending"):
        print("Cola vacia: la proxima ejecucion verificara contra Drive.")
    print("=" * 55)


if __name__ == "__main__":
    main()
