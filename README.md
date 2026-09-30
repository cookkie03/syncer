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

Copia **l'intera directory**, inclusi `.env`, `settings/calendar-pairings.json`, `setup/google/client_secret.json`, `vdirsyncer/token/`, `spotify-backup/data/` e i backup esistenti. I percorsi Compose sono relativi alla directory della repo. Da quella directory sul NAS:

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

I container possono risultare `healthy` anche se il primo backup fallisce: i loro healthcheck controllano le dipendenze Python. Verifica nei log `Backup complete!` per i backup e una conclusione senza errori per il sync. Spotify salva il profilo, la libreria e i metadati delle playlist seguite; l'API permette di leggere i brani solo delle playlist possedute o collaborative. Il backup registra `playlist_items_unavailable` e `check-health.sh` mostra un avviso quando mancano brani per questo limite. Contacts conserva una vecchia directory `backup/latest` come `backup/legacy-latest-*` prima di crearne il collegamento aggiornato.

Se l'autorizzazione Google non è più valida, sostituisci se necessario `setup/google/client_secret.json`, esegui sul PC `bash setup/pc_auth.sh --force`, ricopia il JSON e i nuovi token sul NAS e ricrea i container interessati con `docker compose up -d --force-recreate vdirsyncer google-contacts-backup`.

`docker compose down` arresta i container; i dati rimangono nelle directory del progetto.

## Dove si trova cosa

| Percorso | Motivo |
| --- | --- |
| `setup/` | Preparazione e OAuth sul PC. `setup/google/client_secret.json` è il client Google unico montato in sola lettura nei container che usano Google. |
| `settings/` | Esempi e parametri operativi. `calendar-pairings.json` è la mappa effettiva; `service-options.yaml` serve a Contacts e ai servizi Notion disattivati. |
| `caldav-backup/`, `vdirsyncer/`, `google-contacts-backup/`, `spotify-backup/` | Codice del singolo servizio insieme ai suoi dati persistenti. Ogni servizio tiene i log nella propria `logs/`. |
| `vtodo-notion/`, `notion-backup/` | Codice conservato per i servizi attualmente disattivati. |
| `check-health.sh` | Controllo facoltativo sul NAS. |

I risultati principali sono `caldav-backup/backup/latest/`, `google-contacts-backup/backup/latest.json`, `spotify-backup/data/backup/spotify_backup_current.json` e `vdirsyncer/status/last-success`. I segreti, i backup e i log sono ignorati da Git, ma devono essere inclusi nella copia della cartella verso il NAS e protetti come dati personali.
