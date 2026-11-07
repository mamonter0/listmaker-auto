"""Caducidad de credenciales y problemas acumulados, para avisar antes de que fallen.

- Token de Drive: con la app OAuth en modo Testing, el refresh token caduca a
  los 7 días. GitHub no deja leer cuándo se actualizó un secret, así que
  renew_drive_token.py guarda la fecha en la variable de repo
  GDRIVE_TOKEN_ISSUED (llega como env). Si la app se publica y el token deja de
  caducar, pon esa variable a "never".
- Cookies del foro: la fecha va dentro del propio pickle (FORUM_COOKIES_B64).
  Manda la cookie de "confiar en este dispositivo" (xf_tfa_trust, ~45 días);
  la de sesión recordada (xf_user) dura un año.
- Token de GitHub (PAT) del disparo externo (cron-job.org): vive fuera de
  GitHub, así que su fecha se apunta a mano en la variable de repo
  CRONJOB_PAT_EXPIRES (AAAA-MM-DD).

Nada de aquí toca el foro ni Drive.
"""
import base64
import json
import os
import pickle
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DRIVE_TOKEN_DAYS = 7
DRIVE_WARN_DAYS = 2     # avisar cuando queden <= 2 días
COOKIES_WARN_DAYS = 7   # avisar cuando queden <= 7 días
PAT_WARN_DAYS = 14      # avisar cuando queden <= 14 días
COOKIE_NAMES = ("xf_tfa_trust", "xf_user")
FAIL_STREAK_WARN = 3    # autor que falla N runs seguidos -> aparece en PROBLEMAS
MADRID = ZoneInfo("Europe/Madrid")


def _days_left(expires, now):
    return (expires - now).total_seconds() / 86400


def drive_token_expiry():
    """(caducidad UTC | None, detalle). None = no se sabe o no caduca."""
    raw = (os.environ.get("GDRIVE_TOKEN_ISSUED") or "").strip()
    if raw.lower() == "never":
        return None, "no caduca (app publicada)"
    if not raw:
        return None, "fecha de renovación desconocida (falta la variable GDRIVE_TOKEN_ISSUED)"
    try:
        issued = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"GDRIVE_TOKEN_ISSUED no es una fecha válida ({raw!r})"
    return issued + timedelta(days=DRIVE_TOKEN_DAYS), f"renovado {issued.astimezone(MADRID):%Y-%m-%d %H:%M}"


def cookies_expiry():
    """(caducidad UTC | None, detalle) de la cookie que antes caduque."""
    raw = os.environ.get("FORUM_COOKIES_B64")
    if not raw:
        return None, "no hay FORUM_COOKIES_B64 en el entorno"
    try:
        cookies = pickle.loads(base64.b64decode(raw))
    except Exception as e:
        return None, f"no se pudieron leer ({type(e).__name__})"
    found = {c.get("name"): c.get("expiry") for c in cookies if c.get("name") in COOKIE_NAMES}
    dated = {n: e for n, e in found.items() if e}
    if not dated:
        return None, "sin fecha de caducidad en las cookies de sesión"
    name, exp = min(dated.items(), key=lambda kv: kv[1])
    return datetime.fromtimestamp(exp, timezone.utc), f"manda {name}"


def pat_expiry():
    """(caducidad UTC | None, detalle) del PAT que usa cron-job.org."""
    raw = (os.environ.get("CRONJOB_PAT_EXPIRES") or "").strip()
    if not raw:
        return None, "fecha desconocida (falta la variable CRONJOB_PAT_EXPIRES)"
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, f"CRONJOB_PAT_EXPIRES no es una fecha válida ({raw!r})"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires, "lo usa cron-job.org para lanzar la pipeline"


# (nombre, función de caducidad, avisar con <= N días, cómo renovar)
CREDENTIALS = [
    ("Token de Drive", drive_token_expiry, DRIVE_WARN_DAYS,
     "renew_drive_token.py en listmaker-tools (documentación §10.1)"),
    ("Cookies del foro", cookies_expiry, COOKIES_WARN_DAYS,
     "login limpio con cookiesExtractor y subir FORUM_COOKIES_B64 (documentación §10.2)"),
    ("Token de GitHub de cron-job.org", pat_expiry, PAT_WARN_DAYS,
     "nuevo PAT, ponerlo en los 2 jobs de cron-job.org y actualizar CRONJOB_PAT_EXPIRES (documentación §10.4)"),
]


def credential_status(now=None):
    """[{name, expires, left (días) | None, detail, warn_days, urgent, renew}]."""
    now = now or datetime.now(timezone.utc)
    out = []
    for name, expiry, warn_days, renew in CREDENTIALS:
        expires, detail = expiry()
        left = _days_left(expires, now) if expires else None
        out.append({
            "name": name, "expires": expires, "left": left, "detail": detail,
            "warn_days": warn_days, "renew": renew,
            "urgent": left is not None and left <= warn_days,
        })
    return out


def expiry_warnings(now=None):
    """[(urgente: bool, texto)] de una línea por credencial."""
    out = []
    for c in credential_status(now):
        if c["expires"] is None:
            text = f"{c['name']}: {c['detail']}."
        else:
            text = (f"{c['name']}: caduca {c['expires'].astimezone(MADRID):%Y-%m-%d %H:%M} (Madrid) "
                    f"({c['detail']}; quedan {c['left']:.1f} días).")
        out.append((c["urgent"], text))
    return out


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default
