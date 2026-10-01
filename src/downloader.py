import os

from .config import (
    ARTISTS_FOLDER_ID,
    DRIVE_TARGET_FOLDER,
    LISTS_FOLDER,
    LOCAL_FOLDER,
    PARENT_DRIVE_ID,
    RUN_LOG_NAME,
)
from .drive_auth import get_drive
from .runlog import public


class GDriveDownloader:
    def __init__(self, drive=None):
        print("Inicializando Downloader desde Google Drive...")
        self.drive = drive or get_drive()
        self.downloaded = 0
        self.errors = 0

    def find_folder_in_drive(self, folder_name, parent_id="root"):
        safe_name = folder_name.replace("'", "\\'")
        query = (
            f"title='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        try:
            file_list = self.drive.ListFile({"q": query}).GetList()
            if file_list:
                return file_list[0]["id"]
        except Exception as e:
            print(f"   Error buscando carpeta '{folder_name}' en Drive: {e}")
        return None

    def download_recursive(self, drive_folder_id, local_path, skip_folder_names=None, force_overwrite=False):
        if skip_folder_names is None:
            skip_folder_names = []

        os.makedirs(local_path, exist_ok=True)

        try:
            file_list = self.drive.ListFile(
                {"q": f"'{drive_folder_id}' in parents and trashed=false"}
            ).GetList()
        except Exception as e:
            print(f"   Error listando contenidos de Drive: {e}")
            self.errors += 1
            return

        for item in file_list:
            item_name = item["title"]
            item_path = os.path.join(local_path, item_name)
            if item_name == RUN_LOG_NAME:
                continue  # lo gestiona save_log.py

            if item["mimeType"] == "application/vnd.google-apps.folder":
                if item_name in skip_folder_names:
                    continue
                print(f"\nRevisando carpeta Drive: {item_name}")
                self.download_recursive(item["id"], item_path, force_overwrite=force_overwrite)
            else:
                if force_overwrite or not os.path.exists(item_path):
                    if force_overwrite and os.path.exists(item_path):
                        print(f"   Actualizando local: {item_name}")
                    else:
                        print(f"   Descargando nuevo: {item_name}")
                    try:
                        item.GetContentFile(item_path)
                        self.downloaded += 1
                    except Exception as e:
                        print(f"      Error descargando archivo: {e}")
                        self.errors += 1

    def run(self):
        """Sincroniza solo `lists/` desde Drive — el estado runtime que cada
        run necesita (cookies, history.json, deltas.jsonl, artists_index.json).

        Antes bajábamos también todo `Artists/` para que el writer pudiera
        saltarse PDFs ya descargados via os.path.exists. Pero los capítulos
        son inmutables y deltas.jsonl es la fuente canónica de qué es nuevo,
        así que el local cache es redundante. Bajar 410 MB cada run no aportaba
        nada y disparaba rate limits de la Drive API.
        """
        print("Iniciando recuperación de estado desde Google Drive...")
        if ARTISTS_FOLDER_ID:
            main_drive_folder_id = ARTISTS_FOLDER_ID
            print(f"Usando ARTISTS_FOLDER_ID: {main_drive_folder_id}")
        else:
            main_drive_folder_id = self.find_folder_in_drive(DRIVE_TARGET_FOLDER, PARENT_DRIVE_ID)
        if not main_drive_folder_id:
            public(f"Error: No se encontró la carpeta '{DRIVE_TARGET_FOLDER}' en Google Drive.")
            return

        lists_drive_folder_id = self.find_folder_in_drive(LISTS_FOLDER, main_drive_folder_id)
        if lists_drive_folder_id:
            print(f"\nRestaurando archivos de configuración e histórico en '{LISTS_FOLDER}'...")
            self.download_recursive(lists_drive_folder_id, LISTS_FOLDER, force_overwrite=True)
        else:
            public(f"\nNo se encontró la carpeta '{LISTS_FOLDER}' respaldada en Drive.")

        public(f"Estado descargado de Drive: {self.downloaded} ficheros, {self.errors} errores.")
