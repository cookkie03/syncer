# Syncer

Prepara credenziali e autorizzazioni su un PC con browser, copia **tutta la cartella** sul NAS e avvia i servizi con un comando:

```bash
docker compose up -d --build
```

La directory è anche l'archivio persistente: contiene token, stato dei sync, backup e log. Non copiare soltanto i file tracciati da Git. Il NAS deve poter raggiungere CalDAV, Google, Spotify e Todoist.

| Servizio attivo | Operazione | Frequenza iniziale |
| --- | --- | --- |
| `caldav-backup` | Esporta eventi e task CalDAV in ICS con snapshot | 4 ore |
| `vdirsyncer` | Sincronizza eventi fra CalDAV e Google Calendar | 60 minuti |
| `google-contacts-backup` | Salva i contatti Google in VCF con snapshot | 24 ore |
| `spotify-backup` | Salva profilo, playlist e libreria Spotify | 4 ore |
| `todoist-backup` | Backup incrementale di Todoist, cronologia e allegati accessibili | Avvio e ogni 4 ore, Europe/Amsterdam |
| `notion-backup` (profilo opzionale `notion`) | API ufficiali: pagine Markdown, database CSV, allegati e JSON | Avvio e ogni 4 ore; una versione |

`vtodo-notion` resta commentato; `notion-backup` è disponibile solo con il profilo Compose `notion`: il comando sopra non lo avvia. Vedi [backup Notion via API ufficiali](#backup-notion-via-api-ufficiali).

## Preparazione sul PC

Esegui i comandi dalla radice della cartella. Servono Python 3, accesso a Internet e un browser.

1. Esegui `bash setup/pc_auth.sh`. Al primo avvio crea `.env` dall'esempio e si ferma.
2. Compila `.env` con URL, utente e password CalDAV, più ID, secret e redirect URI dell'app Spotify e `TODOIST_API_TOKEN`. Il token Todoist è in **Settings → Integrations → Developer → API token**: inseriscilo solo nel file locale. Non occorre un'app OAuth per Todoist; il primo backup verifica realmente il token e registra l'identità dell'account. L'URL CalDAV deve essere raggiungibile **dal container sul NAS**. Se occorre mappare il nome del server verso l’host Docker, imposta `CALDAV_HOST_ALIAS` nel `.env` con il nome usato da `CALDAV_URL`. Il valore predefinito pubblico è un hostname di esempio non risolvibile; configura il tuo hostname localmente prima di ricreare i servizi CalDAV.
3. Metti il JSON OAuth Google di tipo **Desktop app** in **`setup/google/client_secret.json`** (nome esatto). Se il file scaricato ha un nome più lungo, rinominalo quando lo copi. Questo è l'unico file da cui Calendar, Contacts e l'eventuale Gmail ricavano ID e secret del client Google; non inserire credenziali Google in `.env`. Il progetto deve avere abilitate le API Calendar e People per i servizi attivi.
4. Prepara `settings/calendar-pairings.json` partendo da `settings/calendar-pairings.example.json`. Per ogni calendario da sincronizzare, indica il nome presente su **entrambi** i servizi e gli ID delle rispettive collezioni. Sostituisci tutti i valori `CHANGE_ME`. Vengono sincronizzati solo i calendari elencati qui. Lo script verifica i nomi e aggiorna gli ID prima di ogni sync; se un nome manca o è ambiguo, blocca le scritture.
5. Registra `SPOTIFY_REDIRECT_URI` nell'app Spotify con lo stesso valore scritto in `.env`. Per un redirect HTTPS locale servono anche `spotify-backup/cert.pem` e `spotify-backup/key.pem`.
6. Riesegui `bash setup/pc_auth.sh`. Lo script usa un ambiente Python esterno alla cartella e chiede i consensi Google per Calendar e Contacts quando i token mancano o appartengono a un client diverso. Chiede il consenso Spotify se manca la cache, poi controlla i file portabili. Il JSON Google fornisce il client OAuth, ma i consensi nel browser sono comunque necessari per ottenere i token dei singoli servizi. Per rinnovare consensi già presenti usa `bash setup/pc_auth.sh --force`; il token precedente viene conservato come `.backup.*` dopo il nuovo consenso riuscito.

Per ripetere solo il controllo locale, esegui `python3 setup/check-portable.py`. Verifica configurazione, formato, scope e client associato ai token; il primo sync/backup sul NAS ne verifica anche l'uso reale.

Il servizio `notion-backup` ora usa soltanto le API ufficiali Notion e il PAT nel medesimo `.env`; Gmail e Telegram non servono per il backup. I vecchi script di ricezione ZIP restano come riferimento e non vengono avviati. Vedi [configurazione e test Notion](#backup-notion-via-api-ufficiali).

## Trasferimento e avvio sul NAS

Copia **l'intera directory**, inclusi `.env`, `settings/calendar-pairings.json`, `setup/google/client_secret.json`, `vdirsyncer/state/`, `spotify-backup/state/` `todoist-backup/state/`, `todoist-backup/backup/`, `todoist-backup/logs/` e i backup esistenti. I percorsi Compose sono relativi alla directory della repo. Da quella directory sul NAS:

```bash
docker compose up -d --build
```

I container ripetono autonomamente le operazioni. Se stai spostando un'istanza già attiva, fermala sul vecchio host prima dell'avvio sul NAS, così i due host non eseguono lo stesso sync contemporaneamente.

## Aggiornamenti via Git sul NAS

Dopo la prima copia completa, aggiorna il codice dal branch `main` nella cartella del NAS:

```bash
cd /path/to/syncer
git pull --ff-only origin main
docker compose up -d --build
```

Git trasferisce solo i file tracciati. `.env`, il client e i token OAuth, gli abbinamenti dei calendari, lo stato e i backup sono ignorati e restano nella cartella del NAS. Quando cambi uno di questi file sul PC, copialo separatamente sul NAS; `git pull` non lo aggiorna. Se `git pull` segnala modifiche locali o rifiuta l'avanzamento, controlla `git status` prima di intervenire: non eliminare la cartella né usare `git clean` o `git reset --hard` sui dati del NAS.

## Controllo e interventi

```bash
bash check-health.sh
docker compose logs --tail=100 vdirsyncer
docker compose exec vdirsyncer /app/sync-notify.sh
```

`check-health.sh` controlla che siano avviati solo i cinque servizi previsti, che risultino healthy e che i risultati recenti esistano. Per Todoist mostra anche copertura, cronologia e avanzamento persistente. Il comando `sync-notify.sh` esegue subito il controllo degli abbinamenti e il sync. Il sync di Calendar è bidirezionale; in caso di conflitto prevale CalDAV.

Contacts, Spotify e vdirsyncer possono risultare `healthy` anche se il primo ciclo fallisce: i loro healthcheck controllano le dipendenze Python. CalDAV verifica invece inventario e checksum dello snapshot corrente, età massima di 12 ore e risultato dell’ultimo tentativo: un backup fallito lo rende non healthy. Verifica nei log `Backup complete!` per quei backup e una conclusione senza errori per il sync. Todoist controlla invece i dati persistenti: un errore fatale, un archivio incoerente o 12 ore senza avanzamento lo rendono non healthy; i limiti di copertura restano visibili anche quando il servizio funziona. Spotify salva il profilo, la libreria e i metadati delle playlist seguite; l'API permette di leggere i brani solo delle playlist possedute o collaborative. Il backup registra `playlist_items_unavailable` e `check-health.sh` mostra un avviso quando mancano brani per questo limite. CalDAV, Contacts e Spotify pubblicano `backup/current` solo dopo aver completato il nuovo snapshot. Il collegamento è relativo e portabile. Un errore Spotify 429 interrompe il ciclo e conserva il backup precedente.

Se l'autorizzazione Google non è più valida, sostituisci se necessario `setup/google/client_secret.json`, esegui sul PC `bash setup/pc_auth.sh --force`, ricopia il JSON e i nuovi token sul NAS e ricrea i container interessati con `docker compose up -d --force-recreate vdirsyncer google-contacts-backup`.

`docker compose down` arresta i container; i dati rimangono nelle directory del progetto.

## Struttura comune dei servizi

Ogni servizio conserva il codice nella propria directory e usa questi percorsi:

```text
nome-servizio/
  Dockerfile, entrypoint.sh, codice e test
  backup/
    current/       ultimo backup completo
    snapshots/     snapshot completi precedenti e quello corrente
  state/           token, cache e stato necessario al prossimo ciclo
  logs/            log correnti e precedenti
```

Le directory vengono create quando servono. I servizi di sola sincronizzazione non producono `backup/`: conservano il proprio avanzamento in `state/`. Tutto ciò che è dentro `backup/`, `state/` e `logs/` resta locale, è ignorato da Git e va incluso nella copia della cartella verso il NAS.

| Servizio | Backup corrente | Stato persistente | Log |
| --- | --- | --- | --- |
| CalDAV | `caldav-backup/backup/current/`: ICS e `manifest.json` | Nessuno separato | `caldav-backup/logs/` |
| Google Contacts | `google-contacts-backup/backup/current/`: `contacts/*.vcf`, `all_contacts.vcf`, `manifest.json` | Eventuali file precedenti conservati dalla migrazione | `google-contacts-backup/logs/` |
| Spotify | `spotify-backup/backup/current/spotify_backup_current.json` | `spotify-backup/state/.cache` contiene OAuth; `playlist_track_cache.json` conserva i brani già ottenuti | `spotify-backup/logs/` |
| Todoist | `todoist-backup/backup/current/`: SQLite, JSON, CSV e checksum; oggetti condivisi in `backup/objects/` | `todoist-backup/state/`: SQLite e checkpoint | `todoist-backup/logs/` |
| vdirsyncer | Non produce un backup | `vdirsyncer/state/config/`, `state/status/`, `state/token/` | `vdirsyncer/logs/` |
| Notion, profilo opzionale `notion` | `notion-backup/backup/current/` (Markdown, CSV, allegati, JSON) | inventario in `manifest.json`, esito in `backup/last-run.json` | `notion-backup/logs/` |
| VTODO–Notion, disattivato | Non produce un backup | `vtodo-notion/state/` | `vtodo-notion/logs/` |

I token Google condivisi restano in **`vdirsyncer/state/token/`**: Calendar li monta in scrittura, Contacts in sola lettura; il backup Notion usa soltanto il PAT nel `.env`. Il client Google unico resta in `setup/google/client_secret.json`. Non cancellare token, cache Spotify o stato di vdirsyncer per ripulire i backup.

`backup/current` è un collegamento relativo allo snapshot completo corrente per CalDAV, Contacts e Spotify. CalDAV esporta un nuovo insieme completo di ICS; Contacts crea un insieme completo di VCF e usa hardlink per i contatti invariati; Spotify scrive un nuovo JSON completo. Nessun servizio sposta i file del backup corrente durante la raccolta dei nuovi dati. Un errore di raccolta lascia il precedente backup corrente disponibile.

### Verificare CalDAV

Il servizio CalDAV usa inizialmente la rete del NAS (`network_mode: host`), senza forzare il dominio del NAS verso il gateway Docker. La prova sul NAS del 10 ottobre 2026 ha completato il backup con questa rete, mentre la rete bridge riceveva HTTP 401 con le stesse credenziali. Questa impostazione riguarda soltanto `caldav-backup`. Per un ambiente diverso puoi impostare `CALDAV_BACKUP_NETWORK_MODE=bridge` e un `CALDAV_URL` raggiungibile da quel container. La prova singola equivalente sul NAS è `docker compose -f docker-compose.caldav-host-test.yml -p syncer-caldav-test run --rm -T caldav-backup`.

Il backup usa richieste con timeout di 60 secondi. Un errore 502 o una connessione interrotta viene ritentato fino a cinque volte, con pause progressive; dopo un ciclo fallito riprova entro cinque minuti. Un 401/403 sospende automaticamente le richieste del servizio fino al riavvio del container: richiede la verifica di endpoint, credenziali, permessi e blocchi degli IP in DSM. Dopo aver corretto l’accesso, ricrea o riavvia il container. Lo stato dell’ultimo tentativo è in `caldav-backup/backup/status.json`, senza risposte HTML o credenziali. Un lock impedisce backup concorrenti.

La raccolta avviene in una directory temporanea: errori di lettura, nomi di file duplicati o raccolte precedenti mancanti impediscono la pubblicazione. Lo snapshot contiene gli ICS, `manifest.json` con SHA-256 e il marcatore `COMPLETE`. Prima della pubblicazione vengono riletti tutti i file e confrontati i checksum. Le ricorrenze e i fusi orari presenti nei dati esportati sono conservati. La verifica controlla i file salvati; un ripristino su un altro server deve essere verificato separatamente.

Dal terminale SSH, dentro la cartella del progetto:

```bash
docker compose exec -T caldav-backup python < setup/diagnose-caldav.py
docker compose up -d --build --no-deps caldav-backup
docker compose logs --tail=80 caldav-backup
docker compose exec -T caldav-backup python /app/backup.py --verify
```

La diagnosi effettua soltanto PROPFIND e non modifica calendari o promemoria. Per provare la `.env` attuale anche quando il container esistente contiene vecchie variabili, usa `docker compose run --rm --no-deps -T --entrypoint python caldav-backup < setup/diagnose-caldav.py`. Un 401 su entrambi i percorsi non identifica da solo quale credenziale o impostazione NAS sia errata.

Questi snapshot stanno sullo stesso NAS dei dati originali: includi la cartella in una copia su un altro dispositivo o fuori sede per proteggerla anche da un guasto o dalla perdita del NAS.

### Conservazione degli snapshot

CalDAV conserva inizialmente **180 snapshot completi**, incluso quello corrente: circa 30 giorni con un backup ogni quattro ore. Spotify e Contacts conservano **14 snapshot**, circa 56 ore e 14 giorni rispettivamente. Il limite conta le esecuzioni riuscite, non i giorni; eventuali valori nella `.env` prevalgono sui valori iniziali.

Per conservare soltanto il backup corrente, aggiungi a `.env`:

```dotenv
CALDAV_BACKUP_RETENTION=1
GOOGLE_CONTACTS_SNAPSHOT_RETENTION=1
SPOTIFY_SNAPSHOT_RETENTION=1
```

Poi ricrea i servizi con `docker compose up -d --build`. La pulizia viene effettuata **dopo la pubblicazione riuscita** del prossimo backup: rimuove gli snapshot datati eccedenti e conserva sempre quello puntato da `current`. Gli archivi `legacy-*` preservati dalla migrazione sono esclusi da questa pulizia: possono contenere dati unici e vanno verificati prima di eliminarli. I servizi Notion disattivati mantengono i loro flussi esistenti e non acquisiscono una nuova politica di snapshot.

### Migrare una cartella già utilizzata

Questo passaggio serve una volta per le vecchie installazioni. Sul PC `bash setup/pc_auth.sh` esegue la migrazione prima dei controlli OAuth; sul NAS, dopo l'unione di questo branch in `main`, esegui i comandi **una riga alla volta**:

```bash
cd /path/to/syncer
docker compose stop
git pull --ff-only origin main
python3 setup/migrate-storage.py
python3 setup/migrate-storage.py --apply
docker compose up -d --build
bash check-health.sh
```

Il primo comando di migrazione mostra soltanto gli spostamenti. `--apply` controlla che i container siano fermi e che nessuna destinazione esista già, quindi rinomina file e directory senza modificarne il contenuto. In caso di conflitto si ferma prima degli spostamenti; un'interruzione durante gli spostamenti può essere ripresa con lo stesso comando. Non servono nuovi consensi Google o Spotify.

La migrazione sposta `latest` in `current`, i token/config/stato di vdirsyncer dentro `state/`, la cache Spotify dentro `state/` e i suoi backup dentro `backup/`. Gli ICS CalDAV rimasti nella radice e le vecchie copie Spotify `latest` vengono conservati in `backup/snapshots/legacy-*`. I metadati Contacts `latest.json` diventano `state/legacy-latest.json`; il backup corrente viene letto direttamente tramite `current/manifest.json`. Il controllo non legge né stampa i contenuti dei token. Le cartelle di Todoist sono escluse dalla migrazione.

Una nuova installazione preparata con questi percorsi richiede soltanto la copia completa e `docker compose up -d --build`.

`setup/` contiene la preparazione sul PC e la migrazione; `settings/` contiene esempi e parametri operativi; `backup_storage.py` applica la stessa pubblicazione e conservazione ai tre esportatori. `check-health.sh` verifica i manifest correnti e `vdirsyncer/state/status/last-success`.

## Backup Todoist

Il servizio usa la [Todoist API v1](https://developer.todoist.com/api/v1/) esclusivamente in lettura. Il primo Sync richiede tutte le risorse, poi ogni run riutilizza il token incrementale; dati e token vengono salvati nella stessa transazione. Una lettura successiva al primo Sync recupera eventuali aggiornamenti più recenti dello snapshot iniziale.

Il backup conserva payload API (escludendo credenziali), versioni osservate, cancellazioni, progetti, task, sezioni, etichette, filtri, promemoria, impostazioni, commenti, collaboratori e risorse workspace accessibili. Integra il Sync con statistiche, attività, archivio dei task completati, progetti archiviati e altri inventari disponibili. Le cronologie delle completazioni e dell'attività partono dalla data di creazione dell'account e procedono per finestre di massimo 90 giorni. Ogni run recupera le nuove completazioni e attività con 48 ore di sovrapposizione; una riconciliazione quotidiana controlla anche i contenuti già raccolti. Le pagine salvate rimangono disponibili dopo un'interruzione: alla ripresa viene riletta la finestra incompleta senza duplicare i record, senza conservare cursori di pagina scaduti. Le singole occorrenze dei task ricorrenti sono conservate nell'attività quando Todoist le espone; l'archivio dei task completati non necessariamente elenca ogni occorrenza.

Gli allegati accessibili e gli archivi automatici già prodotti da Todoist vengono scaricati in `backup/objects/`, identificati tramite SHA-256 e condivisi quando i byte sono identici. I download falliti si riprovano separatamente; i checksum dei file vengono ricontrollati quotidianamente. Non c'è cancellazione automatica né limite di conservazione. I dati eliminati da Todoist restano nelle versioni precedenti del backup. Un cambio di account interrompe il servizio: usa una directory dati separata per un account diverso.

La copertura dipende dall'API, dal piano e dai permessi. Le route legacy per sezioni archiviate e contenuto completo dei progetti, citate nella guida di migrazione, sono tentate senza assumere che esistano: quando non sono disponibili vengono usate le letture documentate e registrate le lacune. Il dettaglio di un task già completato non è garantito dall'endpoint dei task attivi; la sua copia proviene dall'archivio delle completazioni. L'attività non è una copia di ogni revisione e può avere conservazione limitata. Gli archivi automatici di Todoist possono richiedere un piano a pagamento o MFA; il servizio non memorizza OTP né crea archivi remoti. Le modifiche intermedie tra due polling e i contenuti già inaccessibili prima del primo backup non sono garantiti.

Comandi sul NAS, dalla cartella Syncer:

```bash
docker compose up -d --build todoist-backup
docker compose exec -T todoist-backup python /app/backup.py backup
docker compose exec -T todoist-backup python /app/backup.py status
docker compose exec -T todoist-backup python /app/backup.py verify
docker compose exec -T todoist-backup python /app/backup.py export
docker compose exec -T todoist-backup tail -n 100 /logs/todoist-backup.log
```

`status` distingue `complete`, `partial`, `failed` e `backfill_in_progress`, mostra i risultati recenti e le lacune per risorsa. Il cron usa `0 */4 * * *` in **Europe/Amsterdam**; `TODOIST_BACKUP_SCHEDULE` permette di cambiarlo. Le frequenze seguono l'ora locale anche nei cambi di ora legale. Ogni run ha un limite iniziale di 600 richieste, 30 minuti e 20 finestre storiche (`TODOIST_MAX_REQUESTS`, `TODOIST_MAX_RUN_SECONDS`, `TODOIST_HISTORY_WINDOWS`); le raccolte incomplete riprendono nel run successivo. Un lock impedisce backup concorrenti. Non copiare SQLite mentre il servizio sta scrivendo: arresta Todoist prima del trasferimento della cartella.

`verify` controlla SQLite, riferimenti, payload e checksum di tutti i file. `export` crea una nuova directory sotto `backup/exports/` con `account.json`, `tasks.csv` e un marcatore `COMPLETE`. Il JSON include record, versioni, risposte originali senza credenziali, copertura e riferimenti ai file; il CSV contiene task attivi e copie storiche. Gli allegati rimangono in `backup/objects/`: l'esportazione li referenzia, quindi conserva anche quella directory. Il JSON evita le perdite di dettaglio inevitabili nel CSV. Il ripristino in Todoist non è implementato.

Per provare il servizio sul PC senza Docker:

```bash
python3 -m pip install -r todoist-backup/requirements.txt
python3 setup/run-todoist.py backup
python3 setup/run-todoist.py status
python3 setup/run-todoist.py verify
python3 setup/run-todoist.py export
```

Il launcher legge solo i parametri Todoist dalla `.env` locale senza stamparli. Il token non deve essere passato nella riga di comando. I test offline si eseguono con `python3 -m unittest discover -s todoist-backup -v`; la verifica reale richiede il token, e la verifica sul NAS richiede l'avvio dei container.

Todoist segue la convenzione delle sottocartelle: `state/` contiene SQLite e checkpoint, `backup/snapshots/` conserva snapshot immutabili senza scadenza, `backup/current` è un link relativo allo snapshot più recente e `logs/` contiene i log. Ogni snapshot include SQLite consistente, JSON, CSV e checksum; la copertura indica eventuali limiti API. La pubblicazione è atomica: un errore lascia intatto il precedente `current`. La prima esecuzione copia e verifica i vecchi dati da `data/`, senza eliminarli; conflitti interrompono la migrazione. Copia anche `data/` finché vuoi conservarne la copia originale.

Per ripetere il test Docker completo su un PC con Docker attivo: `python3 setup/check-todoist-docker.py`. Usa solo il servizio Todoist, cartelle isolate e la pianificazione ogni minuto durante il test. Controlla avvio, riavvio, esecuzione programmata, salute, verifica ed esportazione; poi rimuove il container di prova e conserva le evidenze sotto `todoist-backup/state/docker-test-*/`. Non prova il funzionamento sul NAS.

### Verifiche Todoist precedenti

La PR Todoist ha verificato API reale, backup completo e incrementale, esportazioni, integrità SQLite e checksum, oltre al ciclo Docker Linux arm64 (avvio, riavvio e pianificazione). Restano da verificare NAS e Linux amd64. Le API hanno segnalato limiti sulle sezioni archiviate (400) e una finestra di attività storica (403): vengono riportati come copertura parziale. Gli allegati Docker sono stati verificati con dati sintetici. Le evidenze locali restano in `todoist-backup/state/docker-test-*/`.

## Backup Notion via API ufficiali

This service uses official Notion HTTP APIs only, with no browser process or session cookies. It updates the existing notion-backup service; legacy Gmail/Telegram ZIP scripts are retained for reference but never launched. Native automatic workspace ZIP export is Enterprise-only; on Free/Plus this is a reconstructed backup of API-accessible content.

### Configuration

Use the project's single `.env`. `NOTION_PAT` is preferred. Create it in the Notion Developer portal, select the target workspace and **Notion API** capability only. PAT expiry is at most one year; renew it locally before expiry. A legacy internal `NOTION_TOKEN` is accepted only when PAT is empty, and needs every root and original database shared with its integration.

`NOTION_ROOTS` is empty by default. Optional roots such as `page:YOUR_PAGE_ID,database:YOUR_DATABASE_ID` supplement whole-accessible-workspace discovery. It does not restrict the backup to that database. Add more known roots comma-separated (`page:ID,database:ID`) to verify coverage; IDs are stable object IDs, not browser sessions. Search alone has no exhaustive guarantee, so the exporter also recursively visits page children, databases, data sources and all database rows.

Defaults: every 14,400 seconds (4 hours), retention **1** (keep only the newest completed version), health age 43,200 seconds, at most 20,000 API requests per run, and 512 MiB per attachment. Change these in `.env`; `NOTION_BACKUP_RETENTION=0` disables pruning. A new failed run makes health fail even while the previous backup remains available. The interval is measured from the start of each run and runs never overlap.

### Resource limits for a 2 GiB NAS

The Notion container defaults to `NOTION_MEMORY_LIMIT=256m` in the single project `.env`. The RAM-plus-swap limit equals the RAM limit, so Notion cannot use swap. CPU quotas are omitted because this Synology kernel rejects NanoCPUs/CFS limits. No extra dependency is needed. The memory limit uses Docker’s native container setting, configurable through this project’s `.env`. These caps apply to Notion only; other services keep their existing settings. Docker stdout logs rotate at 5 MiB with two files, alongside the application's existing rotating file log.

The exporter stores heavy blocks and Markdown one page at a time in a temporary `.notion-crawl-*` folder on the backup disk, and downloads/hashes attachments in 64 KiB chunks. Normal completion or handled errors remove the temporary crawl folder; an abrupt kill can leave a temporary folder for inspection. Page metadata and row identifiers still occupy RAM; database query results are processed progressively rather than collected upfront. The 1,255-page real-data serialization test on Mac dropped from about 462 MiB peak RSS to 124 MiB; a simulated 256 MiB attachment used about 35 MiB peak RSS. All 5,051 output file hashes and sizes matched the previous verified export. This is a Mac measurement, not a measurement under NAS cgroups.

`NOTION_API_DELAY_SECONDS=0.75` spaces API requests; lowering request rate slows work, whereas the RAM cap is a hard limit. If the container exceeds it Docker may kill the process; an unfinished run never replaces the completed `current` backup. The first export may take around two hours or more on NAS, so the Docker health start period is three hours. Until the first completed export, `--health` correctly reports no recent completed backup. The four-hour interval is still measured from each run's start, and runs never overlap.

Changing these settings or the script requires rebuilding/recreating the service; this interrupts an ongoing export and starts it again:

```sh
docker compose --profile notion up -d --build --force-recreate notion-backup
docker stats --no-stream syncer-notion-backup-1
docker inspect --format 'RAM={{.HostConfig.Memory}} RAMplusSwap={{.HostConfig.MemorySwap}} OOMKilled={{.State.OOMKilled}}' syncer-notion-backup-1
```

Expected limits: RAM and RAM-plus-swap both `268435456` bytes. Check the actual NAS runtime settings and memory usage after applying, rather than assuming Compose defaults have reached an already-running container. [Docker resource configuration](https://docs.docker.com/reference/compose-file/services/#mem_limit).

### Local Mac test

Install Python 3.10+ and requests if missing (`python3 -m pip install -r notion-backup/requirements.txt`, in your chosen virtual environment). No Docker is required.

```sh
python3 -m unittest discover -s notion-backup -p 'test_*.py' -v
python3 setup/run-notion.py --check-access
python3 setup/run-notion.py
python3 setup/run-notion.py --health
python3 setup/verify-notion-backup.py
```

The local runner loads only `NOTION_*` keys from `.env`, honors explicit process environment overrides, and defaults to **`/private/tmp/syncer-notion-test/backup`**, leaving production NAS backups alone. Pass `--backup-dir` to choose another test destination. Access check must succeed on all configured roots before running a real backup. An integration token may be valid yet have no access to the intended workspace.

### Output and publication

`notion-backup/backup/current` is a relative symlink to the newest dated snapshot. Each snapshot contains:

- `index.md`: main pages first, followed by an alphabetical directory with readable parent paths. Open this file in a Markdown viewer to navigate offline.
- `workspace/`: title plus full object-ID directories preserving accessible parent hierarchy; each page has `page.md`, each database/data source has `index.md` and each data source has `rows.csv`.
- `assets/`: downloaded Notion-hosted attachments named by SHA-256, with local references in Markdown.
- `json/`: original metadata, source Markdown before link rewriting, recursively collected block JSON and complete queried rows, including property values fetched through pagination.
- `manifest.json`: hashes/sizes, object inventory, warnings, missing IDs and reuse counts; `COMPLETE` publication marker.

Unchanged files reuse hardlinks (copy fallback). Reused files are never written in place. All work occurs in a new staging folder; network/download/pagination/truncation errors leave `current` untouched. Only a successful snapshot updates the pointer, then retention prunes old dated snapshots. Legacy archives are never pruned.

Resources in Notion Trash (including descendants of trashed databases/pages) are excluded from new snapshots. Confirmed deletions are accepted automatically and recorded in the manifest; older retained snapshots are unchanged. A resource deleted during traversal is skipped only when a fresh metadata read confirms its Trash status. Disappeared objects can also mean lost permissions: default `NOTION_ALLOW_MISSING=0` refuses to replace the previous inventory for unexplained disappearances or ambiguous 404 responses. After investigating, set it to 1 for a deliberate run if those removals are expected, then restore 0. This does not modify Notion.

### Coverage limitations

This is not the native workspace ZIP and cannot prove access to private pages the credential cannot see. API Markdown uses Notion's enhanced syntax, which some generic Markdown readers will not render fully. Raw blocks are retained for unsupported Markdown blocks, with manifest warnings; truncated Markdown subtrees are fetched separately. Inaccessible or unresolved content refuses publication. Known unsupported blocks (including buttons) remain explicit placeholders with raw API JSON and manifest warnings; button actions are unavailable through the API. External embeds/URLs remain links, not downloaded copies of arbitrary external services. Expired signed file URLs are refreshed through their owning API objects after HTTP 403. API JSON retains hosted file metadata including temporary signed URLs; offline files reside in `assets/`. CSV compound properties are JSON strings, and property pagination is followed. Legacy Markdown export links are resolved when their original ID uniquely identifies a backed-up object. Original orphan relative links remain unchanged and are listed in `unresolved_source_links` with source Markdown evidence; the offline verifier reports them separately from broken generated links. Root-relative Notion links to objects outside the backup remain external URLs. Native view layouts, permission policies, page history and trash are not backed up by this exporter. Notion may change during the crawl; there is no server-side point-in-time transaction.

### NAS deployment (after a verified local backup)

The service has opt-in Compose profile `notion`, so routine startup of existing services does not enable it. Use the same `.env` on NAS. Run commands from the Syncer folder:

```sh
mkdir -p notion-backup/backup notion-backup/logs
docker compose --profile notion config --quiet
docker compose --profile notion up -d --build notion-backup
docker compose --profile notion logs --tail=60 notion-backup
COMPOSE_PROFILES=notion bash check-health.sh
```

Only the Notion service is started/rebuilt. File logs rotate under `notion-backup/logs`; health verifies a fresh completed backup, not just a successful import. Mac script tests do not establish NAS image/network/mount behavior; verify those together after deployment. No merge or deployment is implied by local success.

Fonti ufficiali: [Personal Access Token](https://developers.notion.com/guides/get-started/personal-access-tokens), [Markdown API](https://developers.notion.com/reference/retrieve-page-markdown), [limiti della ricerca](https://developers.notion.com/reference/search-optimizations-and-limitations), [export del workspace](https://www.notion.com/help/export-your-content).
