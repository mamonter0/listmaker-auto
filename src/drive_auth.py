"""Authentication for Google Drive via PyDrive2.

Two modes, in priority order:

1. **OAuth user credentials** (preferred).
   Uses the user's own quota — required for personal Drive accounts since
   Service Accounts can't *create* new files (only update existing ones).
   Reads ``GDRIVE_OAUTH_CLIENT_ID``, ``GDRIVE_OAUTH_CLIENT_SECRET`` and
   ``GDRIVE_OAUTH_REFRESH_TOKEN`` env-vars. The refresh token is obtained
   once locally with ``scripts/auth_oauth.py`` and stored as a GitHub secret.

2. **Service Account** (fallback).
   Reads ``GDRIVE_SA_JSON`` env-var or a local ``service_account.json``.
   Works for *reading* and *updating* files in shared folders, but fails
   on file creation in personal Drive (no storage quota). Kept here for
   testing/back-compat.
"""
import os

from oauth2client.client import OAuth2Credentials
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive


_TOKEN_URI = "https://oauth2.googleapis.com/token"
_OAUTH_SCOPE = ["https://www.googleapis.com/auth/drive"]


def _get_drive_oauth(client_id: str, client_secret: str, refresh_token: str) -> GoogleDrive:
    """Build a Drive client from a stored OAuth refresh token.

    PyDrive2 will use the refresh token to obtain a fresh access token on
    every API call; nothing else needs to persist between runs.
    """
    creds = OAuth2Credentials(
        access_token=None,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        token_expiry=None,
        token_uri=_TOKEN_URI,
        user_agent=None,
        revoke_uri=None,
    )
    # El client_config va en settings para que PyDrive2 pueda renovar el access
    # token por su cuenta cuando caduque (~1h). Sin esto, cualquier llamada
    # posterior a la primera hora revienta con KeyError 'client_config_file'
    # al intentar releer la config de un fichero que no existe.
    gauth = GoogleAuth(settings={
        "client_config_backend": "settings",
        "client_config": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": _TOKEN_URI,
            "revoke_uri": None,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        },
        "oauth_scope": _OAUTH_SCOPE,
        "save_credentials": False,
    })
    gauth.credentials = creds
    gauth.Refresh()  # mint an access token from the refresh token immediately
    return GoogleDrive(gauth)


def _get_drive_sa(service_account_json_path: str | None) -> GoogleDrive:
    sa_path = service_account_json_path
    if sa_path is None:
        raw_json = os.environ.get("GDRIVE_SA_JSON")
        if raw_json:
            sa_path = "_sa_tmp.json"
            with open(sa_path, "w") as f:
                f.write(raw_json)
        elif os.path.exists("service_account.json"):
            sa_path = "service_account.json"
        else:
            raise RuntimeError(
                "No credentials found. Configure either OAuth env-vars "
                "(GDRIVE_OAUTH_CLIENT_ID, GDRIVE_OAUTH_CLIENT_SECRET, "
                "GDRIVE_OAUTH_REFRESH_TOKEN) or a Service Account "
                "(GDRIVE_SA_JSON env-var or service_account.json file)."
            )

    gauth = GoogleAuth(settings={
        "client_config_backend": "service",
        "service_config": {"client_json_file_path": sa_path},
        "oauth_scope": _OAUTH_SCOPE,
    })
    gauth.ServiceAuth()
    return GoogleDrive(gauth)


def get_drive(service_account_json_path: str | None = None) -> GoogleDrive:
    """Authenticate with Google Drive. OAuth preferred, SA fallback."""
    client_id = os.environ.get("GDRIVE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_OAUTH_REFRESH_TOKEN")

    if client_id and client_secret and refresh_token:
        print("Auth mode: OAuth user credentials")
        drive = _get_drive_oauth(client_id, client_secret, refresh_token)
    else:
        print("Auth mode: Service Account (fallback)")
        drive = _get_drive_sa(service_account_json_path)

    try:
        email = drive.GetAbout().get("user", {}).get("emailAddress", "?")
        print(f"Drive account: {email}")
    except Exception:
        pass
    return drive
