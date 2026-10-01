# Syncer

Prepara credenziali e autorizzazioni su un PC con browser, copia **tutta la cartella** sul NAS e avvia i servizi con un comando:

```bash
docker compose up -d --build
```

La directory è anche l'archivio persistente: contiene token, stato dei sync, backup e log. Non copiare soltanto i file tracciati da Git. Il NAS deve poter raggiungere CalDAV, Google e Spotify.

| Servizio attivo | Operazione | Frequenza iniziale |
| --- | --- | --- |
| `caldav-backup` | Esporta eventi e task CalDAV in ICS con snapshot | 4 ore |
| `vdirsyncer` | Sincronizza eventi fra CalDAV e Google Calendar | 60 minuti |
| `google-contacts-backup` | Salva i contatti Google in VCF con snapshot | 24 ore |
| `spotify-backup` | Salva profilo, playlist e libreria Spotify | 4 ore |

`vtodo-notion` e `notion-backup` restano **commentati in `docker-compose.yml`**: il comando sopra non li avvia.

## Preparazione sul PC

Esegui i comandi dalla radice della cartella. Servono Python 3, accesso a Internet e un browser.

1. Esegui `bash setup/pc_auth.sh`. Al primo avvio crea `.env` dall'esempio e si ferma.
2. Compila `.env` con URL, utente e password CalDAV, più ID, secret e redirect URI dell'app Spotify. L'URL CalDAV deve essere raggiungibile **dal container sul NAS**. Per questo Synology, Compose mappa `synologyds224.tail234659.ts.net` verso l'host Docker, lasciando invariato il nome HTTPS usato dal certificato. Se cambi NAS o hostname, imposta `CALDAV_HOST_ALIAS` in `.env` con il nome usato da `CALDAV_URL`.
3. Metti il JSON OAuth Google di tipo **Desktop app** in **`setup/google/client_secret.json`** (nome esatto). Se il file scaricato ha un nome più lungo, rinominalo quando lo copi. Questo è l'unico file da cui Calendar, Contacts e l'eventuale Gmail ricavano ID e secret del client Google; non inserire credenziali Google in `.env`. Il progetto deve avere abilitate le API Calendar e People per i servizi attivi.
4. Prepara `settings/calendar-pairings.json` partendo da `settings/calendar-pairings.example.json`. Per ogni calendario da sincronizzare, indica il nome presente su **entrambi** i servizi e gli ID delle rispettive collezioni. Sostituisci tutti i valori `CHANGE_ME`. Vengono sincronizzati solo i calendari elencati qui. Lo script verifica i nomi e aggiorna gli ID prima di ogni sync; se un nome manca o è ambiguo, blocca le scritture.
5. Registra `SPOTIFY_REDIRECT_URI` nell'app Spotify con lo stesso valore scritto in `.env`. Per un redirect HTTPS locale servono anche `spotify-backup/cert.pem` e `spotify-backup/key.pem`.
6. Riesegui `bash setup/pc_auth.sh`. Lo script usa un ambiente Python esterno alla cartella e chiede i consensi Google per Calendar e Contacts quando i token mancano o appartengono a un client diverso. Chiede il consenso Spotify se manca la cache, poi controlla i file portabili. Il JSON Google fornisce il client OAuth, ma i consensi nel browser sono comunque necessari per ottenere i token dei singoli servizi. Per rinnovare consensi già presenti usa `bash setup/pc_auth.sh --force`; il token precedente viene conservato come `.backup.*` dopo il nuovo consenso riuscito.

Per ripetere solo il controllo locale, esegui `python3 setup/check-portable.py`. Verifica configurazione, formato, scope e client associato ai token; il primo sync/backup sul NAS ne verifica anche l'uso reale.

Il servizio `notion-backup` è disattivato. Se un giorno lo abiliti e vuoi anche la ricerca delle email di esportazione Notion, lo stesso JSON alimenta Gmail: esegui `bash setup/pc_auth.sh --include-gmail` e `python3 setup/check-portable.py --include-gmail`. Questo consenso aggiuntivo non viene chiesto per l'avvio attuale.

## Trasferimento e avvio sul NAS

Copia **l'intera directory**, inclusi `.env`, `settings/calendar-pairings.json`, `setup/google/client_secret.json`, `vdirsyncer/state/`, `spotify-backup/state/` e i backup esistenti. I percorsi Compose sono relativi alla directory della repo. Da quella directory sul NAS:

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

`check-health.sh` controlla che siano avviati solo i quattro servizi previsti, che risultino healthy e che i risultati recenti esistano. Il comando `sync-notify.sh` esegue subito il controllo degli abbinamenti e il sync. Il sync di Calendar è bidirezionale; in caso di conflitto prevale CalDAV.

I container possono risultare `healthy` anche se il primo backup fallisce: i loro healthcheck controllano le dipendenze Python. Verifica nei log `Backup complete!` per i backup e una conclusione senza errori per il sync. Spotify salva il profilo, la libreria e i metadati delle playlist seguite; l'API permette di leggere i brani solo delle playlist possedute o collaborative. Il backup registra `playlist_items_unavailable` e `check-health.sh` mostra un avviso quando mancano brani per questo limite. CalDAV, Contacts e Spotify pubblicano `backup/current` solo dopo aver completato il nuovo snapshot. Il collegamento è relativo e portabile: non è una seconda copia del backup.

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
