import os
import time

from .config import (
    ARTISTS_FOLDER_ID,
    DRIVE_TARGET_FOLDER,
    LISTS_FOLDER,
    LOCAL_FOLDER,
    PARENT_DRIVE_ID,
    RUN_LOG_NAME,
)
from .drive_auth import get_drive
from .drive_lists import RETRY_DELAYS, with_retries
from .runlog import public


class GDriveUploader:
    def __init__(self, drive=None):
        print("Inicializando Uploader a Google Drive...")
        self.drive = drive or get_drive()
        self.pdfs = 0
        self.state_files = 0
        self.errors = 0

    @staticmethod
    def escape_query_string(text):
        return text.replace("'", "\\'")

    def find_or_create_folder(self, folder_name, parent_id):
        safe_name = self.escape_query_string(folder_name)
        query = (
            f"title='{safe_name}' and '{parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        # Si la búsqueda falla del todo se relanza el error: crear la carpeta
        # sin saber si existe la duplicaría.
        file_list = with_retries(
            lambda: self.drive.ListFile({"q": query}).GetList(),
            f"Error buscando carpeta '{folder_name}'",
        )

        if file_list:
            print(f"   Carpeta Drive existente: {folder_name}")
            return file_list[0]["id"]

        print(f"   Creando nueva carpeta en Drive: {folder_name}")
        folder = self.drive.CreateFile({
            "title": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [{"id": parent_id}],
        })
        folder.Upload()
        return folder["id"]

    def _exists(self, title, parent_id):
        safe = self.escape_query_string(title)
        try:
            return bool(self.drive.ListFile(
                {"q": f"title='{safe}' and '{parent_id}' in parents and trashed=false"}
            ).GetList())
        except Exception:
            return False

    def _upload_file(self, local_path, title, parent_id, existing_id=None):
        """Sube un fichero con reintentos. True si acaba en Drive.

        Drive falla de vez en cuando con un 500 o una respuesta vacía. Antes de
        repetir una creación se comprueba si el fichero llegó pese al error,
        para no dejarlo duplicado.
        """
        for delay in (*RETRY_DELAYS, None):
            try:
                meta = {"id": existing_id} if existing_id else {"title": title, "parents": [{"id": parent_id}]}
                gfile = self.drive.CreateFile(meta)
                gfile.SetContentFile(local_path)
                gfile.Upload()
                return True
            except Exception as e:
                print(f"      Error subiendo {title}: {type(e).__name__}: {e}")
                if delay is None:
                    return False
                time.sleep(delay)
                if not existing_id and self._exists(title, parent_id):
                    print("      (había llegado a Drive pese al error)")
                    return True
        return False

    @staticmethod
    def _count_pdfs(local_path):
        return sum(fn.endswith(".pdf") for _, _, files in os.walk(local_path) for fn in files)

    def upload_recursive(self, local_path, current_parent_drive_id):
        drive_items = {}
        try:
            file_list = with_retries(
                lambda: self.drive.ListFile(
                    {"q": f"'{current_parent_drive_id}' in parents and trashed=false"}
                ).GetList(),
                "Error listando contenidos de carpeta",
            )
            for item in file_list:
                drive_items[item["title"]] = item
        except Exception as e:
            # Los PDFs de esta carpeta se quedan sin subir: se cuentan como
            # error (antes se saltaban en silencio) y reconcile.py los
            # reencola en el siguiente run.
            n = self._count_pdfs(local_path)
            print(f"   Error listando contenidos de carpeta: {e} ({n} PDFs sin subir)")
            self.errors += n
            return

        for item_name in os.listdir(local_path):
            local_item_path = os.path.join(local_path, item_name)

            if os.path.isdir(local_item_path):
                print(f"\nProcesando directorio: {item_name}")
                if (item_name in drive_items
                        and drive_items[item_name]["mimeType"] == "application/vnd.google-apps.folder"):
                    drive_folder_id = drive_items[item_name]["id"]
                else:
                    try:
                        drive_folder_id = self.find_or_create_folder(item_name, current_parent_drive_id)
                    except Exception as e:
                        n = self._count_pdfs(local_item_path)
                        print(f"   No se pudo abrir/crear la carpeta en Drive: {e} ({n} PDFs sin subir)")
                        self.errors += n
                        continue
                self.upload_recursive(local_item_path, drive_folder_id)
            else:
                if not item_name.endswith(".pdf"):
                    continue

                # Capítulos PDF son inmutables: si ya existen en Drive con el
                # mismo nombre, son el mismo contenido. Saltar evita re-subir
                # cientos de MB cada run.
                if (item_name in drive_items
                        and drive_items[item_name]["mimeType"] != "application/vnd.google-apps.folder"):
                    continue

                print(f"   Subiendo nuevo: {item_name}")
                if self._upload_file(local_item_path, item_name, current_parent_drive_id):
                    self.pdfs += 1
                else:
                    self.errors += 1

    def _resolve_main_folder(self):
        if ARTISTS_FOLDER_ID:
            print(f"Usando ARTISTS_FOLDER_ID: {ARTISTS_FOLDER_ID}")
            return ARTISTS_FOLDER_ID
        return self.find_or_create_folder(DRIVE_TARGET_FOLDER, PARENT_DRIVE_ID)

    def run(self):
        main_drive_folder_id = self._resolve_main_folder()

        if os.path.exists(LOCAL_FOLDER):
            print(f"Sincronizando PDFs nuevos de '{LOCAL_FOLDER}' a Drive...")
            self.upload_recursive(LOCAL_FOLDER, main_drive_folder_id)
        else:
            # Normal cuando el run no descargó capítulos nuevos.
            print(f"No hay carpeta local '{LOCAL_FOLDER}' (sin capítulos nuevos este run).")

        if os.path.exists(LISTS_FOLDER):
            print(f"\nSincronizando archivos de configuración e histórico ({LISTS_FOLDER})...")
            # Sin el listado no se sabe qué ficheros existen ya: subirlos a
            # ciegas los duplicaría. Se relanza y el paso falla; el estado de
            # Drive queda como estaba y el siguiente run repite el trabajo.
            lists_drive_folder_id = self.find_or_create_folder(LISTS_FOLDER, main_drive_folder_id)
            file_list = with_retries(
                lambda: self.drive.ListFile(
                    {"q": f"'{lists_drive_folder_id}' in parents and trashed=false"}
                ).GetList(),
                "Error listando 'lists' en Drive",
            )
            drive_lists_items = {item["title"]: item for item in file_list}

            for item_name in os.listdir(LISTS_FOLDER):
                file_path = os.path.join(LISTS_FOLDER, item_name)
                if item_name == RUN_LOG_NAME:
                    continue  # lo gestiona save_log.py
                if os.path.isfile(file_path):
                    existing = drive_lists_items.get(item_name)
                    print(f"   {'Actualizando' if existing else 'Subiendo nuevo'}: {item_name}")
                    if self._upload_file(file_path, item_name, lists_drive_folder_id,
                                         existing_id=existing["id"] if existing else None):
                        self.state_files += 1
                    else:
                        self.errors += 1
        else:
            public(f"\nNo se encontró la carpeta local '{LISTS_FOLDER}'. Se omitirá su respaldo.")

        public(
            f"Subido a Drive: {self.pdfs} PDFs, {self.state_files} ficheros de estado, "
            f"{self.errors} errores."
        )
