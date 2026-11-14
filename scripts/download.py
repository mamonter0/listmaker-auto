"""Entry point: descarga estado previo (history, cookies, Artists/) desde Drive."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.downloader import GDriveDownloader


def main():
    downloader = GDriveDownloader()
    try:
        if not downloader.run():
            sys.exit(1)
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
        sys.exit(130)


if __name__ == "__main__":
    main()
