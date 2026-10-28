"""Caducidad de credenciales y problemas acumulados, para avisar antes de que fallen.

- Token de Drive: con la app OAuth en modo Testing, el refresh token caduca a
  los 7 días. GitHub no deja leer cuándo se actualizó un secret, así que
  renew_drive_token.py guarda la fecha en la variable de repo
  GDRIVE_TOKEN_ISSUED (llega como env). Si la app se publica y el token deja de
  caducar, pon esa variable a "never".
- Cookies del foro: la fecha va dentro del propio pickle (FORUM_COOKIES_B64).
  Manda la cookie de "confiar en este dispositivo" (xf_tfa_trust, ~45 días);
  la de sesión recordada (xf_user) dura un año.

Nada de aquí toca el foro ni Drive.
"""
import base64
import json
import os
import pickle
from datetime import datetime, timedelta, timezone

DRIVE_TOKEN_DAYS = 7
DRIVE_WARN_DAYS = 2     # avisar cuando queden <= 2 días
COOKIES_WARN_DAYS = 7   # avisar cuando queden <= 7 días
COOKIE_NAMES = ("xf_tfa_trust", "xf_user")
FAIL_STREAK_WARN = 3    # autor que falla N runs seguidos -> aparece en PROBLEMAS


def _days_left(expires, now):
    return (expires - now).total_seconds() / 86400


def drive_token_status(now=None):
    """(dias_restantes | None, texto). None = no se sabe o no caduca."""
    now = now or datetime.now(timezone.utc)
    raw = (os.environ.get("GDRIVE_TOKEN_ISSUED") or "").strip()
    if raw.lower() == "never":
        return None, "Token de Drive: no caduca (app publicada)."
    if not raw:
        return None, "Token de Drive: fecha de renovación desconocida (falta la variable GDRIVE_TOKEN_ISSUED)."
    try:
        issued = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"Token de Drive: GDRIVE_TOKEN_ISSUED no es una fecha válida ({raw!r})."
    expires = issued + timedelta(days=DRIVE_TOKEN_DAYS)
    left = _days_left(expires, now)
    return left, (f"Token de Drive: renovado {issued:%Y-%m-%d}, caduca {expires:%Y-%m-%d %H:%M} UTC "
                  f"(quedan {left:.1f} días).")


def cookies_status(now=None):
    """(dias_restantes | None, texto) según la cookie que antes caduque."""
    now = now or datetime.now(timezone.utc)
    raw = os.environ.get("FORUM_COOKIES_B64")
    if not raw:
        return None, "Cookies del foro: no hay FORUM_COOKIES_B64 en el entorno."
    try:
        cookies = pickle.loads(base64.b64decode(raw))
    except Exception as e:
        return None, f"Cookies del foro: no se pudieron leer ({type(e).__name__})."
    found = {c.get("name"): c.get("expiry") for c in cookies if c.get("name") in COOKIE_NAMES}
    dated = {n: e for n, e in found.items() if e}
    if not dated:
        return None, "Cookies del foro: sin fecha de caducidad en las cookies de sesión."
    name, exp = min(dated.items(), key=lambda kv: kv[1])
    expires = datetime.fromtimestamp(exp, timezone.utc)
    left = _days_left(expires, now)
    return left, f"Cookies del foro: caducan {expires:%Y-%m-%d} ({name}; quedan {left:.1f} días)."


def expiry_warnings(now=None):
    """[(urgente: bool, texto)] para token de Drive y cookies."""
    out = []
    left, text = drive_token_status(now)
    out.append((left is not None and left <= DRIVE_WARN_DAYS, text))
    left, text = cookies_status(now)
    out.append((left is not None and left <= COOKIES_WARN_DAYS, text))
    return out


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default
