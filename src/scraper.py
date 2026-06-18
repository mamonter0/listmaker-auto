import json
import os
import pickle
import re
import time
from datetime import datetime

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .config import (
    ARTISTS_FILE,
    ARTISTS_INDEX_FILE,
    BASE_URL,
    COOKIES_FILE,
    DELTA_FILE,
    DELTA_JSONL_FILE,
    FAILED_FILE,
    HISTORY_FILE,
    LIST_DIR,
    MAX_PAGES_PER_LOOP,
    OUTPUT_FILE,
    RATE_LIMIT_BACKOFF,
    SEEN_REMOVALS_FILE,
    parse_artists_file,
)


class QQListMaker:
    def __init__(self):
        print("Inicializando navegador (Chrome Headless)...")
        self.driver = None
        self.wait = None
        self._rl_attempts = 0
        self._build_driver()
        self.scraped_data = {}
        self.failed_artists = []
        self.author_url_map = {}
        self.read_only_urls = set()      # URLs marcadas read_only en artists.txt
        self.read_only_authors = set()   # nombres de autor resueltos como read_only

    def _build_driver(self):
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 15)

    def _driver_alive(self):
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    def _recover_driver(self):
        print("   Driver muerto. Reiniciando Chrome y recargando sesión...")
        try:
            self.driver.quit()
        except Exception:
            pass
        self._build_driver()
        ok = self.load_cookies()
        if not ok:
            print("   No se pudieron recargar cookies tras el reinicio.")
        return ok

    def _check_rate_limit(self):
        try:
            title = (self.driver.title or "").lower()
            body = self.driver.find_element(By.TAG_NAME, "body").text[:1500].lower()
        except Exception:
            return False
        # NB: no incluimos "429" como signal — sus 3 dígitos aparecen como
        # subcadena en muchos sitios benignos del HTML (IDs de thread, números
        # de página, conteos). Antes había thread-36429 que disparaba falso
        # positivo permanente. Los signals textuales de abajo son específicos
        # y cubren el caso real: cuando el foro responde con página de bloqueo,
        # incluye texto humano-legible explicando el rate limit.
        signals = (
            "too many requests",
            "rate limit",
            "you are being rate limited",
            "you have been temporarily blocked",
            "slow down",
        )
        return any(s in title or s in body for s in signals)

    def _handle_rate_limit(self):
        self._rl_attempts += 1
        if self._rl_attempts > len(RATE_LIMIT_BACKOFF):
            print("   Rate limit persistente — abortando.")
            raise RuntimeError("Rate limit persistente")
        wait = RATE_LIMIT_BACKOFF[self._rl_attempts - 1]
        print(f"   Posible rate limit — esperando {wait}s (intento {self._rl_attempts}).")
        time.sleep(wait)

    def _reset_backoff(self):
        self._rl_attempts = 0

    def _safe_get(self, url, retries=3):
        for _ in range(retries):
            try:
                self.driver.get(url)
            except WebDriverException as e:
                print(f"   Error de driver al abrir {url}: {type(e).__name__}")
                return False
            if self._check_rate_limit():
                self._handle_rate_limit()
                continue
            self._reset_backoff()
            return True
        return False

    def load_cookies(self):
        if not os.path.exists(COOKIES_FILE):
            print(f"Error: No se encuentra {COOKIES_FILE}")
            return False
        print("Cargando cookies...")
        self.driver.get(BASE_URL)
        time.sleep(2)
        try:
            with open(COOKIES_FILE, "rb") as f:
                cookies = pickle.load(f)
                for cookie in cookies:
                    if "expiry" in cookie:
                        del cookie["expiry"]
                    self.driver.add_cookie(cookie)
            self.driver.refresh()
            time.sleep(2)
            print("Cookies cargadas.")
            return True
        except Exception as e:
            print(f"Error cookies: {e}")
            return False

    def safe_click(self, element):
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element
            )
            time.sleep(0.5)
            element.click()
        except Exception:
            try:
                self.driver.execute_script("arguments[0].click();", element)
            except Exception:
                pass

    def get_clean_thread_url(self, raw_url):
        url = raw_url.split("?")[0].split("#")[0]
        if "/post-" in url:
            url = url.split("/post-")[0]
        if "unread" in url:
            url = url.split("/unread")[0]
        url = re.sub(r"/page-\d+", "", url)
        if not url.endswith("/"):
            url += "/"
        return url

    def collect_threads_from_search(self):
        unique_threads = {}
        pages_visited = set()
        pages_count = 0
        MAX_ROW_RETRIES = 2
        RETRY_WAIT = 3

        while True:
            pages_count += 1
            if pages_count > MAX_PAGES_PER_LOOP:
                print(f"   Excedido límite de {MAX_PAGES_PER_LOOP} páginas — corto búsqueda.")
                break

            try:
                current_url = self.driver.current_url
            except Exception:
                current_url = None
            if current_url and current_url in pages_visited:
                print("   Página de búsqueda ya visitada — corto para evitar loop.")
                break
            if current_url:
                pages_visited.add(current_url)

            rows_ok = False
            for attempt in range(1, MAX_ROW_RETRIES + 1):
                try:
                    self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h3.contentRow-title")))
                    rows = self.driver.find_elements(By.CSS_SELECTOR, "h3.contentRow-title a")

                    print(f"      ... Escaneando página de búsqueda ({len(rows)} enlaces)")
                    for row in rows:
                        try:
                            title = row.text.strip()
                            raw_url = row.get_attribute("href")
                        except StaleElementReferenceException:
                            continue
                        if not title:
                            continue
                        if not raw_url or "threads/" not in raw_url:
                            continue
                        clean_url = self.get_clean_thread_url(raw_url)
                        if clean_url not in unique_threads:
                            unique_threads[clean_url] = title
                    rows_ok = True
                    break

                except TimeoutException:
                    if attempt < MAX_ROW_RETRIES:
                        time.sleep(RETRY_WAIT)
                        continue
                    print("      Timeout esperando resultados de búsqueda.")
                except (StaleElementReferenceException, WebDriverException) as e:
                    if attempt < MAX_ROW_RETRIES:
                        print(f"      Error transitorio ({type(e).__name__}), reintentando...")
                        time.sleep(RETRY_WAIT)
                        if not self._driver_alive():
                            self._recover_driver()
                            return unique_threads
                        continue
                    print(f"   Error leyendo filas tras {MAX_ROW_RETRIES} intentos: {type(e).__name__}")

            if not rows_ok and not self._driver_alive():
                self._recover_driver()
                return unique_threads

            try:
                next_btn = self.driver.find_element(By.CSS_SELECTOR, "a.pageNav-jump--next")
                self.safe_click(next_btn)
                time.sleep(3)
            except NoSuchElementException:
                break
            except WebDriverException as e:
                print(f"   Error paginando: {type(e).__name__}")
                break
        return unique_threads

    def extract_threadmarks_direct(self, base_tm_url):
        all_tmarks = []
        categories_to_visit = [base_tm_url]
        categories_visited = set()

        while categories_to_visit:
            current_category_url = categories_to_visit.pop(0)
            if current_category_url in categories_visited:
                continue
            categories_visited.add(current_category_url)

            if not self._safe_get(current_category_url):
                print("      No se pudo abrir categoría (network/rate-limit).")
                continue

            try:
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".block-body")))
                category_tabs = self.driver.find_elements(By.CSS_SELECTOR, "a.tabs-tab")
                for tab in category_tabs:
                    try:
                        cat_url = tab.get_attribute("href")
                    except StaleElementReferenceException:
                        continue
                    if not cat_url:
                        continue
                    cat_url = cat_url.split("#")[0]
                    if (
                        "threadmarks" in cat_url
                        and cat_url not in categories_visited
                        and cat_url not in categories_to_visit
                    ):
                        categories_to_visit.append(cat_url)
            except TimeoutException:
                pass
            except WebDriverException:
                pass

            tm_pages_visited = set()
            tm_pages_count = 0
            while True:
                tm_pages_count += 1
                if tm_pages_count > MAX_PAGES_PER_LOOP:
                    print(f"      Excedido límite de {MAX_PAGES_PER_LOOP} páginas de TMs — corto.")
                    break

                try:
                    cur = self.driver.current_url
                except Exception:
                    cur = None
                if cur and cur in tm_pages_visited:
                    break
                if cur:
                    tm_pages_visited.add(cur)

                try:
                    self.wait.until(EC.presence_of_element_located((
                        By.CSS_SELECTOR,
                        "div.structItem--threadmark .structItem-title a, li.threadmarkItem a",
                    )))
                    items = self.driver.find_elements(
                        By.CSS_SELECTOR, "div.structItem--threadmark .structItem-title a"
                    )
                    if not items:
                        items = self.driver.find_elements(By.CSS_SELECTOR, "li.threadmarkItem a")

                    for item in items:
                        try:
                            text = item.text.strip()
                        except StaleElementReferenceException:
                            continue
                        if text and text not in all_tmarks:
                            all_tmarks.append(text)

                    try:
                        next_tm_btn = self.driver.find_element(By.CSS_SELECTOR, "a.pageNav-jump--next")
                        next_url = next_tm_btn.get_attribute("href")
                        if not next_url or next_url in tm_pages_visited:
                            break
                        if not self._safe_get(next_url):
                            break
                    except NoSuchElementException:
                        break

                except TimeoutException:
                    break
                except WebDriverException as e:
                    print(f"      Error leyendo lista de TMs: {type(e).__name__}")
                    break

        return all_tmarks

    def _register_failure(self, url, reason, detail="", author_name=None):
        entry = {"url": url, "reason": reason, "detail": str(detail)[:300]}
        if author_name:
            entry["author"] = author_name
        self.failed_artists.append(entry)

    def process_artists(self):
        if not os.path.exists(ARTISTS_FILE):
            print("No existe el archivo de artistas.")
            return

        os.makedirs(LIST_DIR, exist_ok=True)

        entries = parse_artists_file(ARTISTS_FILE)
        urls = [url for url, _ in entries]
        self.read_only_urls = {url for url, ro in entries if ro}

        ro_count = len(self.read_only_urls)
        print(f"Procesando {len(urls)} artistas ({ro_count} read_only)...")

        for url in urls:
            print("=" * 60)

            if not self._driver_alive():
                self._recover_driver()

            author_name = None
            try:
                if not self._safe_get(url):
                    print(f"Fallo de red o rate limit abriendo {url}")
                    self._register_failure(url, "network_or_ratelimit")
                    if not self._driver_alive():
                        self._recover_driver()
                    continue

                try:
                    username_el = self.wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "h1 .username"))
                    )
                    author_name = username_el.text.strip()
                except TimeoutException:
                    print(f"Perfil no cargó (posible baneo / cuenta eliminada): {url}")
                    self._register_failure(url, "profile_timeout")
                    continue
                except WebDriverException as e:
                    print(f"Error de driver cargando perfil {url}: {type(e).__name__}")
                    self._register_failure(url, "profile_driver_error", e)
                    if not self._driver_alive():
                        self._recover_driver()
                    continue

                print(f"Autor: {author_name}")
                self.scraped_data[author_name] = {}
                self.author_url_map[author_name] = url
                if url in self.read_only_urls:
                    self.read_only_authors.add(author_name)
                    print("   (read_only — se registrará sin descargar)")

                try:
                    postings_tab = self.wait.until(EC.element_to_be_clickable((By.ID, "recent-content")))
                    self.safe_click(postings_tab)
                    time.sleep(2)
                except TimeoutException:
                    print("   Fallo al clicar Postings (timeout).")
                    self._register_failure(url, "postings_timeout", author_name=author_name)
                    continue
                except WebDriverException as e:
                    print(f"   Fallo al clicar Postings: {type(e).__name__}")
                    self._register_failure(url, "postings_driver_error", e, author_name=author_name)
                    continue

                try:
                    find_threads_btn = self.wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='content=thread']"))
                    )
                    self.safe_click(find_threads_btn)
                    self.wait.until(EC.url_contains("search"))
                except TimeoutException:
                    print("   Fallo al clicar Find Threads (timeout).")
                    self._register_failure(url, "find_threads_timeout", author_name=author_name)
                    continue
                except WebDriverException as e:
                    print(f"   Fallo al clicar Find Threads: {type(e).__name__}")
                    self._register_failure(url, "find_threads_driver_error", e, author_name=author_name)
                    continue

                if self._check_rate_limit():
                    self._handle_rate_limit()

                print("   Buscando threads...")
                threads_dict = self.collect_threads_from_search()
                print(f"   Threads encontrados: {len(threads_dict)}")

                if not threads_dict:
                    self._register_failure(url, "zero_threads", author_name=author_name)

                count = 1
                for thread_url, thread_title in threads_dict.items():
                    print(f"      [{count}/{len(threads_dict)}] {thread_title}")
                    self.scraped_data[author_name][thread_title] = []

                    try:
                        tm_url = thread_url + "threadmarks"
                        if not self._safe_get(tm_url):
                            print("      No se pudo abrir threadmarks (network/rate-limit).")
                            count += 1
                            continue

                        if "threadmarks" in self.driver.current_url:
                            tmarks = self.extract_threadmarks_direct(self.driver.current_url)
                            if tmarks:
                                print(f"         {len(tmarks)} capítulos extraídos.")
                                self.scraped_data[author_name][thread_title] = tmarks
                            else:
                                print("         Lista vacía.")
                        else:
                            print("         Sin índice (posiblemente pocos capítulos).")
                        time.sleep(1)

                    except WebDriverException as e:
                        print(f"      Error hilo (driver): {type(e).__name__}")
                        if not self._driver_alive():
                            self._recover_driver()
                            break
                    except RuntimeError:
                        raise
                    except Exception as e:
                        print(f"      Error hilo: {type(e).__name__}: {e}")

                    count += 1

            except KeyboardInterrupt:
                raise
            except RuntimeError as e:
                print(f"\nAbortando run: {e}")
                raise
            except Exception as e:
                print(f"   Error autor ({author_name or url}): {type(e).__name__}: {e}")
                self._register_failure(url, "unexpected_error", e, author_name=author_name)
                if not self._driver_alive():
                    self._recover_driver()

        # Sanity check de fin de run: si la mayoría de artistas falló es muy
        # probable que las cookies hayan caducado, el foro haya cambiado el HTML,
        # o nos hayan bloqueado la IP. Abortamos con error para que el workflow
        # falle y GitHub mande email — evita la trampa silenciosa de un job
        # "verde" que en realidad lleva semanas sin scrapear nada.
        total = len(urls)
        failed = len(self.failed_artists)
        if total >= 5 and failed / total > 0.5:
            raise RuntimeError(
                f"Tasa de fallos {failed}/{total} ({failed/total:.0%}) > 50%. "
                "Posibles causas: cookies caducadas, foro cambió, IP bloqueada."
            )

    def save_failed_report(self):
        if not self.failed_artists:
            return
        try:
            os.makedirs(LIST_DIR, exist_ok=True)
            with open(FAILED_FILE, "w", encoding="utf-8") as f:
                json.dump(self.failed_artists, f, indent=2, ensure_ascii=False)
            print(f"{len(self.failed_artists)} artistas con incidencias en: {FAILED_FILE}")
        except Exception as e:
            print(f"No se pudo guardar el reporte de fallos: {e}")

    def _load_seen_removals(self):
        """Devuelve los sets de removals ya reportados en runs anteriores.

        Sirve para no re-reportar las mismas eliminaciones cada día
        (las eliminaciones se mantienen en history.json por diseño,
        así que una eliminación detectada hoy seguiría detectándose
        mañana sin este filtro).
        """
        empty = {"threads_removed": set(), "chapters_removed": set()}
        if not os.path.exists(SEEN_REMOVALS_FILE):
            return empty
        try:
            with open(SEEN_REMOVALS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "threads_removed": set(data.get("threads_removed", [])),
                "chapters_removed": set(data.get("chapters_removed", [])),
            }
        except Exception as e:
            print(f"{SEEN_REMOVALS_FILE} corrupto, reseteando: {e}")
            return empty

    def _save_seen_removals(self, seen):
        try:
            with open(SEEN_REMOVALS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "threads_removed": sorted(seen["threads_removed"]),
                    "chapters_removed": sorted(seen["chapters_removed"]),
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"No se pudo guardar {SEEN_REMOVALS_FILE}: {e}")

    def save_artists_index(self):
        if not self.author_url_map:
            return
        existing = {}
        if os.path.exists(ARTISTS_INDEX_FILE):
            try:
                with open(ARTISTS_INDEX_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception as e:
                print(f"{ARTISTS_INDEX_FILE} corrupto, se reescribirá. Detalle: {e}")
                existing = {}
        existing.update(self.author_url_map)
        try:
            with open(ARTISTS_INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            print(f"Índice de artistas: {ARTISTS_INDEX_FILE} ({len(existing)} entradas)")
        except Exception as e:
            print(f"No se pudo guardar el índice de artistas: {e}")

    def save_and_compare_history(self):
        print("\n" + "=" * 60)
        print("GUARDANDO DATOS Y DELTAS EN EL HISTÓRICO...")

        old_data = {}
        first_run = False

        if not os.path.exists(HISTORY_FILE):
            print("No se encontró history.json. Se asume primera ejecución.")
            first_run = True
        else:
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                print(f"Historial cargado correctamente. ({len(old_data)} artistas en memoria).")
            except Exception as e:
                print(f"ERROR CRÍTICO: history.json corrupto: {e}")
                print("Abortando guardado para no borrar el historial.")
                return

        deltas = []
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        deltas.append(f"Reporte de Cambios - {timestamp}")

        structured = {
            "timestamp": timestamp,
            "first_run": first_run,
            "artists_added": {},
            "threads_added": {},
            "chapters_added": {},
            "threads_removed": {},
            "chapters_removed": {},
        }

        if first_run:
            deltas.append("PRIMERA EJECUCIÓN")

        final_history = old_data.copy()

        for artist, threads in self.scraped_data.items():
            if artist not in final_history:
                final_history[artist] = {}

            # read_only: fundir en history como "ya guardado", sin emitir delta
            # ni reportar nada. Así final_list.txt queda completo pero el writer
            # nunca ve estos capítulos en deltas.jsonl → no se descargan.
            if artist in self.read_only_authors:
                for th_title, chapters in threads.items():
                    if th_title not in final_history[artist]:
                        final_history[artist][th_title] = list(chapters)
                    else:
                        existing = final_history[artist][th_title]
                        existing_set = set(existing)
                        for ch in chapters:
                            if ch not in existing_set:
                                existing.append(ch)
                continue

            if artist not in old_data:
                deltas.append(f"\n[+] NUEVO ARTISTA: {artist}")
                structured["artists_added"][artist] = {}
                for th_title, chapters in threads.items():
                    deltas.append(f"    + Nuevo Thread: {th_title} ({len(chapters)} caps)")
                    final_history[artist][th_title] = chapters.copy()
                    structured["artists_added"][artist][th_title] = list(chapters)
                continue

            for thread_title, chapters in threads.items():
                if thread_title not in old_data[artist]:
                    deltas.append(f"\n[+] NUEVO THREAD ({artist}): {thread_title}")
                    deltas.append(f"    + {len(chapters)} capítulos añadidos.")
                    final_history[artist][thread_title] = chapters.copy()
                    structured["threads_added"].setdefault(artist, {})[thread_title] = list(chapters)
                else:
                    old_chapters = final_history[artist][thread_title]
                    old_chapters_set = set(old_chapters)
                    new_chapters = [ch for ch in chapters if ch not in old_chapters_set]

                    if new_chapters:
                        deltas.append(f"\n[+] ACTUALIZACIÓN ({artist} - {thread_title}):")
                        for new_ch in new_chapters:
                            deltas.append(f"    > Nuevo Capítulo: {new_ch}")
                            old_chapters.append(new_ch)
                        structured["chapters_added"].setdefault(artist, {})[thread_title] = list(new_chapters)

        # Eliminaciones — solo se reportan la PRIMERA vez. Como history.json
        # nunca se purga, sin este filtro las mismas eliminaciones se
        # repetirían en cada run para siempre.
        seen = self._load_seen_removals()

        # Si un thread/cap aparece de nuevo en el scrape, lo quitamos del set:
        # si vuelve a desaparecer en el futuro queremos volver a reportarlo.
        for artist, threads in self.scraped_data.items():
            for thread_title, chapters in threads.items():
                seen["threads_removed"].discard(f"{artist}|{thread_title}")
                for ch in chapters:
                    seen["chapters_removed"].discard(f"{artist}|{thread_title}|{ch}")

        for artist, old_threads in old_data.items():
            if artist not in self.scraped_data:
                continue
            if artist in self.read_only_authors:
                continue  # read_only: silencioso también para eliminaciones
            new_threads = self.scraped_data[artist]

            # Threads desaparecidos
            new_thread_removals = []
            for th in old_threads:
                if th in new_threads:
                    continue
                key = f"{artist}|{th}"
                if key in seen["threads_removed"]:
                    continue  # ya reportado en run anterior
                new_thread_removals.append(th)
                seen["threads_removed"].add(key)

            for th in new_thread_removals:
                deltas.append(f"\n[-] THREAD ELIMINADO ({artist}): {th}")
                deltas.append("    (se mantiene en history.json, solo reporte)")
                structured["threads_removed"].setdefault(artist, []).append(th)

            # Capítulos desaparecidos dentro de threads que siguen existiendo
            for thread_title, old_chapters in old_threads.items():
                if thread_title not in new_threads:
                    continue
                new_chapters = new_threads[thread_title]
                if not new_chapters:
                    continue  # scrape vacío → probable fallo, no asumir eliminación
                new_set = set(new_chapters)

                new_chapter_removals = []
                for ch in old_chapters:
                    if ch in new_set:
                        continue
                    key = f"{artist}|{thread_title}|{ch}"
                    if key in seen["chapters_removed"]:
                        continue
                    new_chapter_removals.append(ch)
                    seen["chapters_removed"].add(key)

                if new_chapter_removals:
                    deltas.append(f"\n[-] CAPÍTULOS ELIMINADOS ({artist} - {thread_title}):")
                    for ch in new_chapter_removals:
                        deltas.append(f"    < Eliminado: {ch}")
                    deltas.append("    (se mantienen en history.json, solo reporte)")
                    structured["chapters_removed"].setdefault(artist, {})[thread_title] = list(new_chapter_removals)

        self._save_seen_removals(seen)

        with open(DELTA_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 60 + "\n")
            if len(deltas) > 1 or first_run:
                f.write("\n".join(deltas))
                f.write("\n")
                print(f"Nuevos deltas añadidos al histórico: {DELTA_FILE}")
            else:
                f.write(f"Reporte de Cambios - {timestamp}\nSin cambios detectados.\n")
                print("Sin cambios.")

        try:
            with open(DELTA_JSONL_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(structured, ensure_ascii=False) + "\n")
            print(f"Delta estructurado: {DELTA_JSONL_FILE}")
        except Exception as e:
            print(f"No se pudo escribir delta estructurado: {e}")

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            for artist, threads in final_history.items():
                f.write(f"{artist}\n")
                for thread_title, chapters in threads.items():
                    f.write(f"    {thread_title}\n")
                    for chapter in chapters:
                        f.write(f"        {chapter}\n")
                f.write("\n")
        print(f"Lista Final: {OUTPUT_FILE}")

        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(final_history, f, indent=4, ensure_ascii=False)
        print(f"Histórico JSON: {HISTORY_FILE}")

        self.save_failed_report()
        self.save_artists_index()

    def close(self):
        print("\nCerrando.")
        try:
            self.driver.quit()
        except Exception:
            pass
