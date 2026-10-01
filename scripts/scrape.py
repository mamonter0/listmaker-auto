"""Entry point: scraping de perfiles del foro. Detecta cambios y los escribe a deltas.jsonl."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bootstrap import bootstrap_state
from src.runlog import public, redact
from src.scraper import ListMaker


def main():
    bootstrap_state()
    bot = ListMaker()
    exit_code = 0
    try:
        if not bot.load_cookies():
            sys.exit(2)
        bot.process_artists()
        bot.save_and_compare_history()
    except KeyboardInterrupt:
        print("\nDetenido.")
        bot.save_and_compare_history()
        exit_code = 130
    except RuntimeError as e:
        # RuntimeError = abort intencionado: rate limit persistente,
        # tasa de fallos >50%, etc. Guardamos lo que tengamos y salimos != 0.
        public(f"\nAbortado: {redact(e, 300)}")
        bot.save_and_compare_history()
        exit_code = 3
    finally:
        bot.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
