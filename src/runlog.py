"""Log privado: con PRIVATE_LOG=1, todo lo que se imprime va a un fichero.

El repo es público y los logs de GitHub Actions también: cualquiera puede leer
lo que sale por consola. Con PRIVATE_LOG=1 (lo ponen los workflows) stdout y
stderr — también los de procesos hijos como Chrome — se redirigen a
logs/run_log.txt, y scripts/save_log.py lo guarda en Drive (lists/run_log.txt).

A la consola solo llega lo que se emite con public(): contadores, tipos de
página y tipos de error. Nunca nombres de autores, hilos o capítulos.

Sin PRIVATE_LOG (ejecución local) todo sigue saliendo por consola como siempre.
"""
import os
import re
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

PRIVATE = os.environ.get("PRIVATE_LOG") == "1"
LOG_DIR = "logs"
LOG_PATH = os.path.join(LOG_DIR, "run_log.txt")

_console = None  # copia de la consola real, solo con PRIVATE


def public(msg: str) -> None:
    """Escribe en la consola pública (y en el log privado, para tener contexto)."""
    print(msg, flush=True)
    if _console is not None:
        _console.write(msg + "\n")
        _console.flush()


def redact(text: str, limit: int = 200) -> str:
    """Quita de un mensaje de error lo que pueda llevar nombres: texto entre
    comillas y URLs. Lo usamos para los errores que llegan a la consola."""
    text = re.sub(r"https?://\S+", "<url>", str(text))
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", text)
    text = " ".join(text.split())
    return text[:limit]


def _excepthook(exc_type, exc, tb):
    traceback.print_exception(exc_type, exc, tb)
    public(f"ERROR no controlado: {exc_type.__name__}: {redact(exc)} (detalle en run_log.txt)")


def install() -> None:
    global _console
    if not PRIVATE or _console is not None:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # Guardamos la consola real antes de redirigir los descriptores 1 y 2.
    _console = os.fdopen(os.dup(1), "w", encoding="utf-8", errors="replace")
    log = open(LOG_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    # A nivel de descriptor: así también cae al fichero lo que escriban
    # chromedriver/Chrome u otros procesos hijos.
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdout = log
    sys.stderr = log
    sys.excepthook = _excepthook
    now = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n----- {os.path.basename(sys.argv[0])} ({now}) -----")
