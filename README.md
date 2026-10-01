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

`vtodo-notion` e `notion-backup` restano **commentati in `docker-compose.yml`**: il comando sopra non li avvia.

## Preparazione sul PC

Esegui i comandi dalla radice della cartella. Servono Python 3, accesso a Internet e un browser.

1. Esegui `bash setup/pc_auth.sh`. Al primo avvio crea `.env` dall'esempio e si ferma.
2. Compila `.env` con URL, utente e password CalDAV, più ID, secret e redirect URI dell'app Spotify e `TODOIST_API_TOKEN`. Il token Todoist è in **Settings → Integrations → Developer → API token**: inseriscilo solo nel file locale. Non occorre un'app OAuth per Todoist; il primo backup verifica realmente il token e registra l'identità dell'account. L'URL CalDAV deve essere raggiungibile **dal container sul NAS**. Per questo Synology, Compose mappa `synologyds224.tail234659.ts.net` verso l'host Docker, lasciando invariato il nome HTTPS usato dal certificato. Se cambi NAS o hostname, imposta `CALDAV_HOST_ALIAS` in `.env` con il nome usato da `CALDAV_URL`.
3. Metti il JSON OAuth Google di tipo **Desktop app** in **`setup/google/client_secret.json`** (nome esatto). Se il file scaricato ha un nome più lungo, rinominalo quando lo copi. Questo è l'unico file da cui Calendar, Contacts e l'eventuale Gmail ricavano ID e secret del client Google; non inserire credenziali Google in `.env`. Il progetto deve avere abilitate le API Calendar e People per i servizi attivi.
4. Prepara `settings/calendar-pairings.json` partendo da `settings/calendar-pairings.example.json`. Per ogni calendario da sincronizzare, indica il nome presente su **entrambi** i servizi e gli ID delle rispettive collezioni. Sostituisci tutti i valori `CHANGE_ME`. Vengono sincronizzati solo i calendari elencati qui. Lo script verifica i nomi e aggiorna gli ID prima di ogni sync; se un nome manca o è ambiguo, blocca le scritture.
5. Registra `SPOTIFY_REDIRECT_URI` nell'app Spotify con lo stesso valore scritto in `.env`. Per un redirect HTTPS locale servono anche `spotify-backup/cert.pem` e `spotify-backup/key.pem`.
6. Riesegui `bash setup/pc_auth.sh`. Lo script usa un ambiente Python esterno alla cartella e chiede i consensi Google per Calendar e Contacts quando i token mancano o appartengono a un client diverso. Chiede il consenso Spotify se manca la cache, poi controlla i file portabili. Il JSON Google fornisce il client OAuth, ma i consensi nel browser sono comunque necessari per ottenere i token dei singoli servizi. Per rinnovare consensi già presenti usa `bash setup/pc_auth.sh --force`; il token precedente viene conservato come `.backup.*` dopo il nuovo consenso riuscito.

Per ripetere solo il controllo locale, esegui `python3 setup/check-portable.py`. Verifica configurazione, formato, scope e client associato ai token; il primo sync/backup sul NAS ne verifica anche l'uso reale.

Il servizio `notion-backup` è disattivato. Se un giorno lo abiliti e vuoi anche la ricerca delle email di esportazione Notion, lo stesso JSON alimenta Gmail: esegui `bash setup/pc_auth.sh --include-gmail` e `python3 setup/check-portable.py --include-gmail`. Questo consenso aggiuntivo non viene chiesto per l'avvio attuale.

## Trasferimento e avvio sul NAS

Copia **l'intera directory**, inclusi `.env`, `settings/calendar-pairings.json`, `setup/google/client_secret.json`, `vdirsyncer/state/`, `spotify-backup/state/` `todoist-backup/state/`, `todoist-backup/backup/`, `todoist-backup/logs/` e i backup esistenti. I percorsi Compose sono relativi alla directory della repo. Da quella directory sul NAS:

```bash
docker compose up -d --build
```

I container ripetono autonomamente le operazioni. Se stai spostando un'istanza già attiva, fermala sul vecchio host prima dell'avvio sul NAS, così i due host non eseguono lo stesso sync contemporaneamente.

## Aggiornamenti via Git sul NAS

Dopo la prima copia completa, aggiorna il codice dal branch `main` nella cartella del NAS:

```bash
cd /volume1/docker/syncer
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

I container CalDAV, Contacts, Spotify e vdirsyncer possono risultare `healthy` anche se il primo backup fallisce: i loro healthcheck controllano le dipendenze Python. Verifica nei log `Backup complete!` per quei backup e una conclusione senza errori per il sync. Todoist controlla invece i dati persistenti: un errore fatale, un archivio incoerente o 12 ore senza avanzamento lo rendono non healthy; i limiti di copertura restano visibili anche quando il servizio funziona. Spotify salva il profilo, la libreria e i metadati delle playlist seguite; l'API permette di leggere i brani solo delle playlist possedute o collaborative. Il backup registra `playlist_items_unavailable` e `check-health.sh` mostra un avviso quando mancano brani per questo limite. CalDAV, Contacts e Spotify pubblicano `backup/current` solo dopo aver completato il nuovo snapshot. Il collegamento è relativo e portabile. Un errore Spotify 429 interrompe il ciclo e conserva il backup precedente.

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
| Notion, disattivato | `notion-backup/backup/current/json/` e `current/zip_exports/` | `notion-backup/state/` | `notion-backup/logs/` |
| VTODO–Notion, disattivato | Non produce un backup | `vtodo-notion/state/` | `vtodo-notion/logs/` |

I token Google condivisi restano in **`vdirsyncer/state/token/`**: Calendar li monta in scrittura, Contacts e l'eventuale Notion in sola lettura. Il client Google unico resta in `setup/google/client_secret.json`. Non cancellare token, cache Spotify o stato di vdirsyncer per ripulire i backup.

`backup/current` è un collegamento relativo allo snapshot completo corrente per CalDAV, Contacts e Spotify. CalDAV esporta un nuovo insieme completo di ICS; Contacts crea un insieme completo di VCF e usa hardlink per i contatti invariati; Spotify scrive un nuovo JSON completo. Nessun servizio sposta i file del backup corrente durante la raccolta dei nuovi dati. Un errore di raccolta lascia il precedente backup corrente disponibile.

### Conservazione degli snapshot

Per questi tre servizi il valore iniziale è **14 snapshot completi in totale**, incluso quello corrente. Il limite è un numero di esecuzioni riuscite, non un numero di giorni. Con i valori iniziali corrisponde a circa 56 ore per CalDAV/Spotify e 14 giorni per Contacts.

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
cd /volume1/docker/syncer
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
