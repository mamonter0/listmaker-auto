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
        try:
            file_list = self.drive.ListFile({"q": query}).GetList()
        except Exception as e:
            print(f"   Error buscando carpeta '{folder_name}': {e}")
            file_list = []

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

    def upload_recursive(self, local_path, current_parent_drive_id):
        drive_items = {}
        try:
            file_list = self.drive.ListFile(
                {"q": f"'{current_parent_drive_id}' in parents and trashed=false"}
            ).GetList()
            for item in file_list:
                drive_items[item["title"]] = item
        except Exception as e:
            print(f"   Error listando contenidos de carpeta: {e}")
            return

        for item_name in os.listdir(local_path):
            local_item_path = os.path.join(local_path, item_name)

            if os.path.isdir(local_item_path):
                print(f"\nProcesando directorio: {item_name}")
                if (item_name in drive_items
                        and drive_items[item_name]["mimeType"] == "application/vnd.google-apps.folder"):
                    drive_folder_id = drive_items[item_name]["id"]
                else:
                    drive_folder_id = self.find_or_create_folder(item_name, current_parent_drive_id)
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
                try:
                    gfile = self.drive.CreateFile({
                        "title": item_name,
                        "parents": [{"id": current_parent_drive_id}],
                    })
                    gfile.SetContentFile(local_item_path)
                    gfile.Upload()
                    self.pdfs += 1
                except Exception as e:
                    print(f"      Error subiendo archivo: {e}")
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
            lists_drive_folder_id = self.find_or_create_folder(LISTS_FOLDER, main_drive_folder_id)

            drive_lists_items = {}
            try:
                file_list = self.drive.ListFile(
                    {"q": f"'{lists_drive_folder_id}' in parents and trashed=false"}
                ).GetList()
                for item in file_list:
                    drive_lists_items[item["title"]] = item
            except Exception as e:
                print(f"   Error listando contenidos de 'lists' en Drive: {e}")

            for item_name in os.listdir(LISTS_FOLDER):
                file_path = os.path.join(LISTS_FOLDER, item_name)
                if item_name == RUN_LOG_NAME:
                    continue  # lo gestiona save_log.py
                if os.path.isfile(file_path):
                    try:
                        if item_name in drive_lists_items:
                            print(f"   Actualizando: {item_name} en Drive")
                            gfile = self.drive.CreateFile({"id": drive_lists_items[item_name]["id"]})
                        else:
                            print(f"   Subiendo nuevo: {item_name} a Drive")
                            gfile = self.drive.CreateFile({
                                "title": item_name,
                                "parents": [{"id": lists_drive_folder_id}],
                            })
                        gfile.SetContentFile(file_path)
                        gfile.Upload()
                        self.state_files += 1
                    except Exception as e:
                        print(f"   Error subiendo {item_name}: {e}")
                        self.errors += 1
        else:
            public(f"\nNo se encontró la carpeta local '{LISTS_FOLDER}'. Se omitirá su respaldo.")

        public(
            f"Subido a Drive: {self.pdfs} PDFs, {self.state_files} ficheros de estado, "
            f"{self.errors} errores."
        )
