"""Materializa archivos de estado desde env-vars base64.

Las cookies y artists.txt se inyectan vía secrets (FORUM_COOKIES_B64 y
ARTISTS_TXT_B64). El secret es la FUENTE DE VERDAD: cuando está presente,
sobrescribe siempre lo que haya en disco.

Esto importa porque el paso de download baja `session.cookies` de Drive
ANTES de que corra el bootstrap. Si solo materializáramos "cuando no existe",
las cookies viejas de Drive ganarían y el secret recién actualizado nunca se
usaría (bug que dejaba el scrape con cookies muertas → 100% de fallos).

Llamado desde los entry points de scrape/write antes de instanciar el bot.
"""
import base64
import os

from .config import ARTISTS_FILE, COOKIES_FILE, LIST_DIR


def _materialize(env_var: str, dest_path: str, label: str, overwrite: bool = True) -> bool:
    """Decodifica el contenido base64 de *env_var* y lo escribe en *dest_path*.

    Si *env_var* no está definida, no toca nada (deja lo que haya bajado de
    Drive) y devuelve False. Si está definida y *overwrite* es True (default),
    sobrescribe el destino aunque ya exista — el secret manda sobre Drive.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return False  # sin secret: respetamos lo que haya (p.ej. lo de Drive)
    existed = os.path.exists(dest_path)
    if existed and not overwrite:
        return False
    try:
        data = base64.b64decode(raw)
    except Exception as e:
        print(f"{env_var} no decodifica como base64: {e}")
        return False
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(data)
    action = "sobrescrito" if existed else "materializado"
    print(f"{label} {action} desde {env_var} ({len(data)} bytes) en {dest_path}")
    return True


def bootstrap_state():
    """Materializa cookies y artists.txt desde secrets (el secret manda sobre Drive)."""
    os.makedirs(LIST_DIR, exist_ok=True)
    _materialize("FORUM_COOKIES_B64", COOKIES_FILE, "Cookies")
    _materialize("ARTISTS_TXT_B64", ARTISTS_FILE, "artists.txt")
