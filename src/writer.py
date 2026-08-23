import base64
import json
import os
import pickle
import random
import re
import time

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
    LOCAL_FOLDER,
    MAX_PAGES_PER_LOOP,
    PENDING_CHAPTERS_FILE,
    WRITER_RATE_LIMIT_BACKOFF,
    parse_artists_file,
)


class Writer:
    def __init__(self):
        print("Inicializando Writer (PDF Generator Headless)...")
        self.driver = None
        self.wait = None
        self._rl_attempts = 0
        self._build_driver()

    def _build_driver(self):
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument("--enable-print-browser")
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 20)

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
        # NB: no incluimos "429" como signal — esa subcadena aparece en muchos
        # sitios benignos del HTML (IDs de thread, números de página, conteos).
        # Antes había thread-36429 que disparaba falso positivo permanente.
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
        if self._rl_attempts > len(WRITER_RATE_LIMIT_BACKOFF):
            print("   Rate limit persistente — abortando.")
            raise RuntimeError("Rate limit persistente")
        wait = WRITER_RATE_LIMIT_BACKOFF[self._rl_attempts - 1]
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
                if not self._driver_alive():
                    if not self._recover_driver():
                        return False
                    continue
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
        self.driver.get(BASE_URL)
        time.sleep(1)
        try:
            with open(COOKIES_FILE, "rb") as f:
                cookies = pickle.load(f)
                for cookie in cookies:
                    if "expiry" in cookie:
                        del cookie["expiry"]
                    self.driver.add_cookie(cookie)
            self.driver.refresh()
            time.sleep(2)
            return True
        except Exception as e:
            print(f"Error cargando cookies: {e}")
            return False

    def js_click(self, element):
        self.driver.execute_script("arguments[0].click();", element)

    @staticmethod
    def sanitize_filename(name):
        return re.sub(r'[\\/*?:"<>|]', "", name).strip()

    @staticmethod
    def _chapter_already_saved(dirpath, safe_ch):
        """True si ya existe un PDF de ese capitulo, en formato nuevo o viejo.

        Formato nuevo:  2024-03-15_10-23_Titulo.pdf   (fecha delante)
        Formato viejo:  Titulo_2024-03-15_10-23.pdf   (fecha detras)
        Sin fecha:      Titulo.pdf

        Comparamos el titulo completo, no por prefijo: el `startswith` de antes
        daba falso positivo entre "Chapter 1" y "Chapter 10_2024-...", y se
        saltaba capitulos que nunca llegaron a descargarse.
        """
        if not os.path.isdir(dirpath):
            return False
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}(?:_\d{2}-\d{2})?_(?P<name>.+)$")
        for fn in os.listdir(dirpath):
            if not fn.lower().endswith(".pdf"):
                continue
            stem = fn[:-4]
            m = date_re.match(stem)
            if m and m.group("name") == safe_ch:
                return True  # formato nuevo
            if stem == safe_ch:
                return True  # sin fecha
            if stem.startswith(safe_ch + "_"):
                return True  # formato viejo
        return False

    def get_post_datetime(self):
        try:
            js = """
                var target = null;
                if (window.location.hash) {
                    var id = window.location.hash.substring(1);
                    var anchor = document.getElementById(id);
                    if (anchor) target = anchor.closest('.message, article');
                }
                if (!target) {
                    var posts = document.querySelectorAll('.message, article');
                    for (var i = 0; i < posts.length; i++) {
                        if (posts[i].querySelector('.bbWrapper')) {
                            target = posts[i]; break;
                        }
                    }
                }
                if (!target) return '';
                var t = target.querySelector('time[datetime]');
                if (!t) return '';
                return t.getAttribute('datetime') || '';
            """
            raw = self.driver.execute_script(js)
            if not raw:
                return ""
            try:
                date_part, rest = raw.split("T", 1)
                time_part = rest.split("+")[0].split("Z")[0].split(".")[0]
                hm = time_part.split(":")
                hour = hm[0] if len(hm) > 0 else "00"
                minute = hm[1] if len(hm) > 1 else "00"
                return f"{date_part}_{hour}-{minute}"
            except Exception:
                return raw.split("T")[0]
        except Exception as e:
            print(f"      No se pudo extraer fecha/hora del post: {e}")
            return ""

    def _load_pending(self):
        """Carga capítulos pendientes de runs anteriores que no se descargaron.
        Formato: {artist: {thread: [chapter_names]}}. Nunca contiene "__ALL__"
        (lo expandimos a nombres concretos antes de persistir).
        """
        if not os.path.exists(PENDING_CHAPTERS_FILE):
            return {}
        try:
            with open(PENDING_CHAPTERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"{PENDING_CHAPTERS_FILE} corrupto, reseteando: {e}")
            return {}

    def _save_pending(self, pending):
        """Persiste pending. Limpia entradas vacías antes de guardar."""
        clean = {}
        for artist, threads in pending.items():
            clean_threads = {t: list(c) for t, c in threads.items() if c}
            if clean_threads:
                clean[artist] = clean_threads
        try:
            with open(PENDING_CHAPTERS_FILE, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2, ensure_ascii=False)
            if clean:
                count = sum(len(c) for t in clean.values() for c in t.values())
                print(f"{count} capítulos pendientes guardados en {PENDING_CHAPTERS_FILE}")
            else:
                print("Sin capítulos pendientes (todos los intentos exitosos).")
        except Exception as e:
            print(f"No se pudo guardar pending: {e}")

    def _merge_pending_into_queue(self, pending, queue):
        """Combina pendientes (prioritarios) con la queue del último delta."""
        merged = {}
        for artist, threads in pending.items():
            merged[artist] = {t: list(c) for t, c in threads.items()}
        for artist, threads in queue.items():
            merged.setdefault(artist, {})
            for thread, chapters in threads.items():
                existing = merged[artist].get(thread, [])
                if "__ALL__" in chapters or "__ALL__" in existing:
                    merged[artist][thread] = ["__ALL__"]
                else:
                    merged[artist][thread] = list(dict.fromkeys(existing + list(chapters)))
        return merged

    def _mark_done(self, pending, artist, thread, chapter):
        """Quita un capítulo del pending tras descarga exitosa."""
        if artist in pending and thread in pending[artist]:
            if chapter in pending[artist][thread]:
                pending[artist][thread].remove(chapter)

    def parse_deltas_jsonl(self):
        """Lee la última línea del deltas.jsonl. Devuelve None si no hay JSONL utilizable."""
        if not os.path.exists(DELTA_JSONL_FILE):
            return None

        try:
            with open(DELTA_JSONL_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except Exception as e:
            print(f"No se pudo leer {DELTA_JSONL_FILE}: {e}")
            return None

        if not lines:
            return None

        try:
            entry = json.loads(lines[-1])
        except json.JSONDecodeError as e:
            print(f"Última línea de {DELTA_JSONL_FILE} corrupta: {e}")
            return None

        print(f"Delta estructurado leído (timestamp: {entry.get('timestamp', '?')}).")

        queue = {}

        for artist, threads in entry.get("artists_added", {}).items():
            queue.setdefault(artist, {})
            for th_title in threads.keys():
                queue[artist][th_title] = ["__ALL__"]

        for artist, threads in entry.get("threads_added", {}).items():
            queue.setdefault(artist, {})
            for th_title in threads.keys():
                queue[artist][th_title] = ["__ALL__"]

        for artist, threads in entry.get("chapters_added", {}).items():
            queue.setdefault(artist, {})
            for th_title, chapters in threads.items():
                if queue[artist].get(th_title) == ["__ALL__"]:
                    continue
                queue[artist][th_title] = list(chapters)

        return queue

    def parse_deltas(self):
        """Parser legacy del deltas.txt (fallback)."""
        if not os.path.exists(DELTA_FILE):
            print("No hay archivo de deltas.")
            return {}

        queue = {}
        current_artist = None
        current_thread = None

        print("Extrayendo SOLO la última ejecución del histórico (legacy txt)...")
        try:
            with open(DELTA_FILE, "r", encoding="utf-8") as f:
                content = f.read()

            runs = content.split("Reporte de Cambios -")
            if len(runs) <= 1:
                return {}

            last_run = runs[-1]

            for line in last_run.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "Sin cambios detectados" in line:
                    return {}

                m_artist = re.match(r"\[\+\] NUEVO ARTISTA: (.*)", line)
                if m_artist:
                    current_artist = m_artist.group(1)
                    if current_artist not in queue:
                        queue[current_artist] = {}
                    continue

                m_update = re.match(r"\[\+\] ACTUALIZACIÓN \((.*?) - (.*)\):", line)
                if m_update:
                    current_artist = m_update.group(1)
                    current_thread = m_update.group(2)
                    if current_artist not in queue:
                        queue[current_artist] = {}
                    if current_thread not in queue[current_artist]:
                        queue[current_artist][current_thread] = []
                    continue

                m_new_thread_ex = re.match(r"\[\+\] NUEVO THREAD \((.*)\): (.*)", line)
                if m_new_thread_ex:
                    current_artist = m_new_thread_ex.group(1)
                    current_thread = m_new_thread_ex.group(2)
                    if current_artist not in queue:
                        queue[current_artist] = {}
                    if current_thread not in queue[current_artist]:
                        queue[current_artist][current_thread] = []
                    queue[current_artist][current_thread].append("__ALL__")
                    continue

                m_thread_sub = re.match(r"\+ Nuevo Thread: (.*) \(", line)
                if m_thread_sub and current_artist:
                    current_thread = m_thread_sub.group(1)
                    if current_thread not in queue[current_artist]:
                        queue[current_artist][current_thread] = []
                    queue[current_artist][current_thread].append("__ALL__")
                    continue

                m_chap = re.match(r"> Nuevo Capítulo: (.*)", line)
                if m_chap and current_artist and current_thread:
                    chap_name = m_chap.group(1)
                    if "__ALL__" not in queue[current_artist][current_thread]:
                        queue[current_artist][current_thread].append(chap_name)

        except Exception as e:
            print(f"Error leyendo deltas: {e}")
            return {}

        return queue

    def load_artists_index(self):
        if not os.path.exists(ARTISTS_INDEX_FILE):
            print(f"No existe {ARTISTS_INDEX_FILE}. Caeré en matching por substring.")
            return {}
        try:
            with open(ARTISTS_INDEX_FILE, "r", encoding="utf-8") as f:
                idx = json.load(f)
            print(f"Índice de artistas cargado ({len(idx)} entradas).")
            return idx
        except Exception as e:
            print(f"{ARTISTS_INDEX_FILE} corrupto: {e}. Caeré en matching por substring.")
            return {}

    def resolve_artist_url(self, artist_name, artists_index, artist_urls_list):
        if artist_name in artists_index:
            return artists_index[artist_name]

        name_clean = artist_name.lower()
        name_hyphen = name_clean.replace(" ", "-")
        name_underscore = name_clean.replace(" ", "_")
        name_nospace = name_clean.replace(" ", "")

        return next(
            (u for u in artist_urls_list if
             name_clean in u.lower()
             or name_hyphen in u.lower()
             or name_underscore in u.lower()
             or name_nospace in u.lower()),
            None,
        )

    def find_thread_urls_for_artist(self, artist_url):
        thread_map = {}
        try:
            if not self._safe_get(artist_url):
                return thread_map

            postings_tab = self.wait.until(EC.presence_of_element_located((By.ID, "recent-content")))
            self.js_click(postings_tab)
            time.sleep(2)

            find_btn = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='content=thread']"))
            )
            self.js_click(find_btn)
            self.wait.until(EC.url_contains("search"))

            pages_visited = set()
            pages_count = 0
            while True:
                pages_count += 1
                if pages_count > MAX_PAGES_PER_LOOP:
                    print(f"      Excedido límite de {MAX_PAGES_PER_LOOP} páginas — corto búsqueda.")
                    break

                try:
                    cur = self.driver.current_url
                except Exception:
                    cur = None
                if cur and cur in pages_visited:
                    print("      Página de búsqueda ya visitada — corto para evitar loop.")
                    break
                if cur:
                    pages_visited.add(cur)

                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h3.contentRow-title")))
                rows = self.driver.find_elements(By.CSS_SELECTOR, "h3.contentRow-title a")

                for row in rows:
                    try:
                        href = row.get_attribute("href")
                        if href and "threads/" in href:
                            title = row.text.strip()
                            url = href.split("/post-")[0]
                            if not url.endswith("/"):
                                url += "/"
                            if title and title not in thread_map:
                                thread_map[title] = url
                    except StaleElementReferenceException:
                        continue

                try:
                    next_btn = self.driver.find_element(By.CSS_SELECTOR, "a.pageNav-jump--next")
                    self.js_click(next_btn)
                    print("      ... Buscando hilos en siguiente página...")
                    time.sleep(2)
                except NoSuchElementException:
                    break

        except Exception as e:
            print(f"   Error buscando hilos de {artist_url}: {e}")
        return thread_map

    def get_all_chapter_urls(self, thread_base_url):
        chapter_map = {}
        categories_to_visit = [thread_base_url + "threadmarks"]
        categories_visited = set()

        while categories_to_visit:
            current_category_url = categories_to_visit.pop(0)
            if current_category_url in categories_visited:
                continue

            categories_visited.add(current_category_url)
            if not self._safe_get(current_category_url):
                continue

            current_category_name = "Threadmarks"
            try:
                self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".block-body")))

                try:
                    active_tab = self.driver.find_element(By.CSS_SELECTOR, "a.tabs-tab.is-active")
                    current_category_name = active_tab.text.strip()
                except NoSuchElementException:
                    pass

                print(f"      ... Explorando sección: {current_category_name}")

                js_categories = """
                    return Array.from(document.querySelectorAll("a[href*='threadmark_category=']"))
                                .map(a => a.href.split('#')[0]);
                """
                cat_urls = self.driver.execute_script(js_categories)
                for url in cat_urls:
                    if url and url not in categories_visited and url not in categories_to_visit:
                        categories_to_visit.append(url)
            except Exception:
                pass

            tm_pages_visited = set()
            tm_pages_count = 0
            while True:
                tm_pages_count += 1
                if tm_pages_count > MAX_PAGES_PER_LOOP:
                    print(f"      Excedido límite de {MAX_PAGES_PER_LOOP} páginas — corto categoría.")
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

                    js_chapters = """
                        let chaps = [];
                        let elements = document.querySelectorAll("div.structItem--threadmark .structItem-title a, li.threadmarkItem a");
                        elements.forEach(el => chaps.push({title: el.innerText.trim(), url: el.href}));
                        return chaps;
                    """
                    chapters_data = self.driver.execute_script(js_chapters)

                    for chap in chapters_data:
                        if chap["title"]:
                            chapter_map[chap["title"]] = {
                                "url": chap["url"],
                                "category": current_category_name,
                            }

                    try:
                        next_btn = self.driver.find_element(By.CSS_SELECTOR, "a.pageNav-jump--next")
                        next_url = next_btn.get_attribute("href")
                        if not next_url or next_url in tm_pages_visited:
                            break
                        if not self._safe_get(next_url):
                            break
                    except NoSuchElementException:
                        break

                except TimeoutException:
                    break
                except Exception as e:
                    print(f"      Error extrayendo capítulos: {e}")
                    break

        return chapter_map

    def isolate_and_print(self, save_path):
        try:
            js_isolate = r"""
                var target = null;

                if(window.location.hash) {
                    var id = window.location.hash.substring(1);
                    var anchor = document.getElementById(id);
                    if(anchor) {
                         target = anchor.closest('.message, article');
                    }
                }

                if(!target) {
                    var all_posts = document.querySelectorAll('.message, article, .message-inner');
                    for(var i=0; i<all_posts.length; i++) {
                        if(all_posts[i].querySelector('.bbWrapper')) {
                            target = all_posts[i];
                            break;
                        }
                    }
                }

                if(target) {
                    try {
                        var toRemove = target.querySelectorAll('.message-footer, .message-signature, .message-attribution-opposite, .actionBar');
                        toRemove.forEach(function(el) {
                            if(el && el.parentNode) {
                                el.parentNode.removeChild(el);
                            }
                        });
                    } catch(err) {}

                    var spoilers = target.querySelectorAll('.bbCodeSpoiler-content');
                    spoilers.forEach(function(sp) {
                        sp.style.display = 'block';
                        sp.style.height = 'auto';
                        sp.classList.add('is-active');
                    });

                    var lazyImages = target.querySelectorAll('img[data-src]');
                    lazyImages.forEach(function(img) {
                        var realSrc = img.getAttribute('data-src');
                        if(realSrc) {
                            img.src = realSrc;
                            img.classList.remove('lazyload');
                        }
                    });

                    var content = target.querySelector('.bbWrapper');
                    var contentHTML = content ? content.innerHTML : "Error extracting content";

                    document.body.innerHTML = `
                        <style>
                            .bbCodeSpoiler-content,
                            .bbCodeSpoiler-content *,
                            .bbCodeBlock--spoiler,
                            .bbCodeBlock--spoiler * {
                                background: #ffffff !important;
                                background-color: #ffffff !important;
                                color: #000000 !important;
                                text-shadow: none !important;
                                filter: none !important;
                                box-shadow: none !important;
                            }
                            .bbCodeSpoiler-content {
                                display: block !important;
                                height: auto !important;
                                visibility: visible !important;
                                opacity: 1 !important;
                                padding: 15px;
                                border-left: 4px solid #ccc;
                                margin-top: 5px;
                            }
                            .bbCodeSpoiler-button {
                                display: block !important;
                                font-weight: bold;
                                background: #eeeeee !important;
                                border: 1px solid #ccc;
                                padding: 8px;
                                color: #333 !important;
                                width: 100%;
                                text-align: left;
                                box-sizing: border-box;
                            }
                            .bbCodeBlock--spoiler { margin-bottom: 15px; }
                            img { max-width: 100% !important; height: auto !important; }
                        </style>
                        <div style="
                            font-family: 'Georgia', 'Times New Roman', serif, 'Noto Sans Symbols', 'Noto Sans Symbols 2', 'Symbola', 'DejaVu Sans';
                            font-size: 14pt;
                            line-height: 1.6;
                            padding: 40px;
                            max-width: 800px;
                            margin: 0 auto;
                            color: #000;
                        ">
                            ${contentHTML}
                        </div>
                    `;

                    document.body.style.backgroundColor = "white";
                    document.documentElement.style.backgroundColor = "white";
                    document.body.style.overflow = "auto";
                    return true;
                }
                return false;
            """

            success = self.driver.execute_script(js_isolate)

            if success:
                time.sleep(1.0)
                print_options = {
                    "landscape": False,
                    "displayHeaderFooter": False,
                    "printBackground": True,
                    "preferCSSPageSize": True,
                }
                result = self.driver.execute_cdp_cmd("Page.printToPDF", print_options)
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(result["data"]))
                print(f"      PDF Guardado: {os.path.basename(save_path)}")
            else:
                print("      No se pudo aislar el post.")

        except Exception as e:
            print(f"      Error generando PDF: {e}")

    def run(self):
        if not self.load_cookies():
            return

        # Estrategia de queue:
        # 1. Cargamos pending_chapters.json (capítulos que fallaron en runs previos).
        # 2. Cargamos el último delta (capítulos nuevos detectados por scrape hoy).
        # 3. Mergeamos: pending tienen prioridad de retry, fresh se añaden detrás.
        # 4. Mientras procesamos, mantenemos `pending` mutable en memoria; cada
        #    descarga exitosa se descuenta. Lo no descargado al final se persiste
        #    para que el siguiente run lo reintente.
        pending_prev = self._load_pending()

        fresh_queue = self.parse_deltas_jsonl()
        if fresh_queue is None:
            print("Usando parser legacy de deltas.txt (no hay deltas.jsonl utilizable).")
            fresh_queue = self.parse_deltas()

        queue = self._merge_pending_into_queue(pending_prev, fresh_queue or {})

        if not queue:
            self._save_pending({})  # asegura archivo limpio
            print("Nada nuevo que descargar.")
            return

        if pending_prev:
            cnt = sum(len(c) for t in pending_prev.values() for c in t.values())
            print(f"Reintentando {cnt} capítulos pendientes de runs anteriores.")

        # `pending` es la copia mutable: empieza igual que la queue, se reduce
        # al ir descargando con éxito, y al final se persiste con lo que quede.
        pending = self._merge_pending_into_queue({}, queue)

        # URLs limpias (sin el flag read_only) para el fallback de matching.
        # Los artistas read_only nunca llegan a la queue de todos modos.
        artist_urls_list = [url for url, _ in parse_artists_file(ARTISTS_FILE)]
        artists_index = self.load_artists_index()

        try:
            for artist_name, threads_data in queue.items():
                print(f"\nProcesando: {artist_name}")

                if not self._driver_alive():
                    if not self._recover_driver():
                        print("   No pude recuperar el driver — abortando.")
                        return

                my_url = self.resolve_artist_url(artist_name, artists_index, artist_urls_list)

                if not my_url:
                    print(f"   No encontré la URL perfil para {artist_name}")
                    # Lo dejamos en pending por si más adelante el índice se actualiza.
                    continue

                known_threads = self.find_thread_urls_for_artist(my_url)

                for thread_title, chapters in threads_data.items():
                    if thread_title not in known_threads:
                        print(f"   Hilo no encontrado en perfil: '{thread_title}'")
                        continue

                    thread_base_url = known_threads[thread_title]
                    print(f"   Thread: {thread_title}")

                    safe_art = self.sanitize_filename(artist_name)
                    safe_th = self.sanitize_filename(thread_title)

                    base_save_dir = os.path.join(LOCAL_FOLDER, safe_art, safe_th)
                    os.makedirs(base_save_dir, exist_ok=True)

                    print("      ... Escaneando índice de capítulos...")
                    chapter_map = self.get_all_chapter_urls(thread_base_url)

                    if "__ALL__" in chapters:
                        to_download = list(chapter_map.keys())
                        # Expandir __ALL__ a nombres concretos también en pending
                        # (no queremos persistir ["__ALL__"]).
                        if pending.get(artist_name, {}).get(thread_title) == ["__ALL__"]:
                            pending[artist_name][thread_title] = list(to_download)
                    else:
                        to_download = chapters

                    for i, chap_name in enumerate(to_download):
                        if chap_name not in chapter_map:
                            print(f"      Cap no hallado: {chap_name}")
                            # No lo encontramos en el índice — sacarlo del pending,
                            # no tiene sentido reintentar lo inexistente.
                            self._mark_done(pending, artist_name, thread_title, chap_name)
                            continue

                        chap_info = chapter_map[chap_name]
                        chap_url = chap_info["url"]
                        cat_name = chap_info["category"]

                        safe_ch = self.sanitize_filename(chap_name)

                        if cat_name.lower() in ["threadmarks", ""]:
                            final_save_dir = base_save_dir
                        else:
                            safe_cat = self.sanitize_filename(cat_name)
                            final_save_dir = os.path.join(base_save_dir, safe_cat)
                            os.makedirs(final_save_dir, exist_ok=True)

                        if self._chapter_already_saved(final_save_dir, safe_ch):
                            # Ya descargado en este mismo run (raro) o anterior crash.
                            self._mark_done(pending, artist_name, thread_title, chap_name)
                            continue

                        print(f"      [{i+1}/{len(to_download)}] ({cat_name}) Descargando: {chap_name}")

                        try:
                            if not self._safe_get(chap_url):
                                print("      Saltando capítulo (network/rate-limit transitorio).")
                                # Permanece en pending → se reintentará en el próximo run.
                                continue
                            time.sleep(2 + random.uniform(0.5, 1.5))
                            post_dt = self.get_post_datetime()
                            # Fecha DELANTE: asi el orden alfabetico del explorador
                            # (y de Drive) coincide con el orden cronologico. Con la
                            # fecha al final ordenaba por titulo y "Chapter Ten" salia
                            # antes que "Chapter Two".
                            date_prefix = f"{post_dt}_" if post_dt else ""
                            pdf_path = os.path.join(final_save_dir, f"{date_prefix}{safe_ch}.pdf")
                            self.isolate_and_print(pdf_path)
                            # Éxito: descontar de pending.
                            self._mark_done(pending, artist_name, thread_title, chap_name)
                            try:
                                self.driver.get("about:blank")
                            except WebDriverException:
                                if not self._driver_alive():
                                    self._recover_driver()
                            time.sleep(2 + random.uniform(1.0, 2.0))
                        except WebDriverException as e:
                            print(f"      Error driver al descargar capítulo: {type(e).__name__}")
                            # No marcamos done — queda en pending.
                            if not self._driver_alive():
                                if not self._recover_driver():
                                    return
                        except RuntimeError:
                            # Rate limit persistente — pending se guarda en el finally.
                            raise
                        except Exception as e:
                            print(f"      Error al descargar capítulo: {e}")
                            # No marcamos done — queda en pending.
        finally:
            self._save_pending(pending)

    def close(self):
        if self.driver:
            print("\nCerrando.")
            try:
                self.driver.quit()
            except Exception:
                pass
