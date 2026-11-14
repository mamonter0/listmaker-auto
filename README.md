# listmaker-auto

Pipeline que corre en GitHub Actions dos veces al día: recorre los perfiles de
una lista de autores de un foro, detecta capítulos nuevos, los guarda como PDF
y lo sincroniza todo con Google Drive.

El foro, la lista de autores y las credenciales son secretos: no aparecen en
el repo. La documentación completa de mantenimiento (renovación de
credenciales, diagnóstico, cómo replicarlo) es privada y vive en Drive.

## Cuándo se ejecuta

- **Disparo principal:** un servicio externo llama a la API
  (`workflow_dispatch`) a las **09:00 y 23:00 hora de Madrid**, puntual.
- **Respaldo:** 4 crons de GitHub (2 por estación, porque el cron va en UTC)
  5 minutos después. El job `gate` solo los deja pasar si desde su hora
  programada no hubo un disparo cuyo job `run` terminase bien.

## Jobs de `listmaker.yml`

```
gate     decide si este disparo se ejecuta (estación correcta, sin run reciente)
  │
  ▼
run      download → reconcile → scrape → write → upload → informes → log privado
  │
  ▼
avisos   falla a propósito (email de GitHub) si una credencial caduca pronto
         o si la autorreparación encuentra demasiados huecos
```

Dentro de `run`:

| Paso | Script | Qué hace |
|---|---|---|
| Download | `scripts/download.py` | Baja de Drive el estado (`lists/`); si no lo consigue entero, el run se para |
| Reconcile | `scripts/reconcile.py` | Reencola lo que salió en algún delta y no tiene PDF en Drive (solo lee Drive) |
| Scrape | `scripts/scrape.py` | Recorre los perfiles y genera el delta (`deltas.jsonl`) |
| Write | `scripts/write.py` | Descarga como PDF los capítulos nuevos y los pendientes |
| Upload | `scripts/upload.py` | Sube PDFs y estado a Drive |
| Informes | `scripts/latest_entries.py`, `scripts/expiry_report.py` | Regeneran `ultimas_entradas.txt` y `caducidades.txt` en Drive |
| Log | `scripts/save_log.py` | Añade el log completo del run a `run_log.txt` en Drive |

## Otros workflows

| Workflow | Toca el foro | Para qué |
|---|---|---|
| `latest-entries.yml` | No | Regenerar los informes de Drive sin scrapear (también para probar cambios) |
| `audit-missing.yml` | Sí (solo lectura) | Comparar lo que hay en el foro con lo que hay en Drive |
| `recover-missing.yml` | Sí | Descargar por tandas los capítulos que falten |
| `backfill-dates.yml`, `rename-pdfs.yml` | Sí / No | Migraciones ya aplicadas |

Todos comparten el grupo de concurrencia `listmaker`: nunca corren dos a la vez.

## Estado en Drive (`Artists/lists/`)

`history.json`, `deltas.jsonl`, `deltas.txt` y los pendientes del writer son el
estado del pipeline. Para personas:

- `deltas.txt`: novedades de cada run.
- `ultimas_entradas.txt`: avisos, problemas (autores que fallan, capítulos
  atascados o perdidos), actividad por autor y sus últimas entradas.
- `caducidades.txt`: tiempo exacto que le queda a cada credencial.
- `run_log.txt`: log completo de los últimos runs.

## Privacidad

El repo y sus logs de Actions son públicos. Con `PRIVATE_LOG=1` (todos los
workflows), `src/runlog.py` manda toda la salida a `logs/run_log.txt`, que
acaba en Drive. En la consola de GitHub solo sale lo que pasa por `public()`:
contadores y tipos de error, nunca autores, hilos, capítulos ni URLs. No se
suben artifacts.

## Secrets y variables

| Nombre | Tipo | Qué es |
|---|---|---|
| `FORUM_BASE_URL` | secret | URL base del foro |
| `FORUM_COOKIES_B64` | secret | Cookies de sesión del foro (pickle en base64) |
| `ARTISTS_TXT_B64` | secret | Lista de perfiles, uno por línea (base64) |
| `GDRIVE_OAUTH_CLIENT_ID`, `GDRIVE_OAUTH_CLIENT_SECRET`, `GDRIVE_OAUTH_REFRESH_TOKEN` | secrets | OAuth de usuario para Drive |
| `ARTISTS_FOLDER_ID` | secret | ID de la carpeta `Artists` en Drive |
| `GDRIVE_SA_JSON` | secret | Service Account (respaldo opcional) |
| `GDRIVE_TOKEN_ISSUED` | variable | Fecha de la última renovación del token de Drive |
| `CRONJOB_PAT_EXPIRES` | variable | Caducidad del token que usa el disparo externo |

**Por qué OAuth de usuario y no Service Account:** las SA no tienen cuota de
almacenamiento propia, así que no pueden crear ficheros nuevos en un Drive
personal.

## Estructura

```
src/                módulos (scraper, writer, Drive, logs, caducidades)
scripts/            un entry point por paso o herramienta
.github/workflows/  workflows de GitHub Actions
lists/, logs/       estado y log en runtime (gitignored; viven en Drive)
```
