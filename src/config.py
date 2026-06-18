import os

BASE_URL = "https://forum.questionablequesting.com/"

LIST_DIR = "lists"
COOKIES_FILE = os.path.join(LIST_DIR, "qq_verified_session.cookies")
ARTISTS_FILE = os.path.join(LIST_DIR, "artists.txt")
HISTORY_FILE = os.path.join(LIST_DIR, "history.json")
DELTA_FILE = os.path.join(LIST_DIR, "deltas.txt")
DELTA_JSONL_FILE = os.path.join(LIST_DIR, "deltas.jsonl")
OUTPUT_FILE = os.path.join(LIST_DIR, "final_list.txt")
FAILED_FILE = os.path.join(LIST_DIR, "failed_artists.json")
ARTISTS_INDEX_FILE = os.path.join(LIST_DIR, "artists_index.json")
SEEN_REMOVALS_FILE = os.path.join(LIST_DIR, "seen_removals.json")
PENDING_CHAPTERS_FILE = os.path.join(LIST_DIR, "pending_chapters.json")

LOCAL_FOLDER = "Artists"
DRIVE_TARGET_FOLDER = "Artists"
PARENT_DRIVE_ID = "root"
LISTS_FOLDER = "lists"

# ID directo de la carpeta `Artists` en Drive. Imprescindible cuando se usa
# Service Account, porque el SA no ve la carpeta bajo su 'root' (está en
# "Shared with me"). Si no se setea, caemos al lookup por nombre desde 'root'
# (modo legacy con auth de usuario normal).
ARTISTS_FOLDER_ID = os.environ.get("ARTISTS_FOLDER_ID", "").strip() or None

# Safety net contra paginación infinita
MAX_PAGES_PER_LOOP = 300
# Backoff escalado ante rate limit detectado.
# Scraper: corto (185s total) — preferimos abortar el artista y seguir.
# Writer: largo (~55min total) — los capítulos rate-limited no son recuperables
# si el writer aborta antes de tiempo (history.json ya tiene el cambio del scrape).
RATE_LIMIT_BACKOFF = [5, 15, 45, 120]
WRITER_RATE_LIMIT_BACKOFF = [30, 120, 300, 900, 1800]

# Flag opcional al final de una línea de artists.txt. Un artista read_only se
# scrapea y se registra en history.json/final_list.txt, pero sus capítulos
# NUNCA se descargan: se funden en el histórico como si ya estuvieran guardados.
READ_ONLY_FLAG = "read_only"


def parse_artists_file(path):
    """Lee artists.txt y devuelve lista de (url, read_only: bool).

    Formato por línea: la URL, opcionalmente seguida de flags separados por
    espacios. Hoy el único flag es `read_only`. Líneas vacías se ignoran.
    """
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            url = parts[0]
            flags = parts[1:]
            entries.append((url, READ_ONLY_FLAG in flags))
    return entries
