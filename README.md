# Syncer

Backup e sincronizzazione Docker per Synology, Linux, macOS e Windows.

Servizi attivi:

| Servizio | Funzione | Frequenza |
|---|---|---|
| `caldav-backup` | Backup completo CalDAV in ICS, eventi e task | ogni 4 ore |
| `vdirsyncer` | CalDAV <-> Google Calendar | ogni 60 minuti |
| `google-contacts-backup` | Google Contacts in VCF incrementali | ogni 24 ore |
| `spotify-backup` | Backup di profilo, playlist e libreria Spotify | ogni 4 ore |

Gli altri servizi restano nel file Compose come blocchi commentati e non vengono avviati.

## Quick start

Eseguire tutti i comandi dalla directory del progetto:

```bash
cd syncer
cp config/.env.example .env
```

Compilare `.env` con almeno:

- `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`
- `GOOGLE_DEVICE_CLIENT_ID`, `GOOGLE_DEVICE_CLIENT_SECRET`
- credenziali Spotify

I percorsi dei dati sono relativi al progetto. Copiare la directory `syncer/` trasferisce config, token, stato, backup e log.

Autorizzare Google Calendar e Google Contacts:

```bash
bash auth/regenerate-token.sh
```

Il comando mostra URL e codice. Aprire l'URL su un dispositivo con browser e completare entrambe le autorizzazioni. I token vengono salvati in `vdirsyncer/token/`.

Avviare:

```bash
docker compose up -d --build
```

## Controlli

Controllo automatico:

```bash
bash scripts/check-syncer.sh
```

Controlli manuali:

```bash
docker compose ps
docker compose logs --tail=100 caldav-backup
docker compose logs --tail=100 vdirsyncer
docker compose logs --tail=100 google-contacts-backup
docker compose logs --tail=100 spotify-backup
```

I quattro container devono risultare `healthy`. I log persistenti sono tutti qui:

```text
logs/caldav-backup/caldav-backup.log
logs/vdirsyncer/vdirsyncer.log
logs/google-contacts-backup/google-contacts-backup.log
logs/spotify-backup/spotify-backup.log
```

Verificare i file prodotti:

```bash
find caldav-backup/backup/latest -maxdepth 1 -type f -print
cat caldav-backup/backup/latest/manifest.json
cat google-contacts-backup/backup/latest.json
find spotify-backup/data/backup -maxdepth 2 -type f -print
```

## Discover e matching

Ogni giorno alle `02:00` UTC `vdirsyncer`:

1. esegue discover su CalDAV e Google;
2. abbina le collezioni con lo stesso nome;
3. aggiorna `config/calendar-map.json`;
4. rigenera la configurazione persistente;
5. esegue nuovamente discover.

Il sync normale parte ogni 60 minuti. Per eseguire subito il matching:

```bash
docker compose exec vdirsyncer /app/discover-match.sh
```

Per un backup CalDAV manuale:

```bash
docker compose run --rm caldav-backup
docker compose run --rm caldav-backup --discover
```

## OAuth: errore `deleted_client`

Se un log contiene:

```text
deleted_client: The OAuth client was deleted
```

il token non puo essere riparato. Il client Google Cloud usato per crearlo e stato cancellato. Rigenerare entrambi i token:

```bash
bash auth/regenerate-token.sh
docker compose up -d --force-recreate vdirsyncer google-contacts-backup
```

Lo script sposta prima i vecchi token in file `.backup.*` dentro `vdirsyncer/token/`.

## Struttura persistente

```text
syncer/
├── .env
├── caldav-backup/backup/
├── google-contacts-backup/backup/
├── spotify-backup/data/
├── vdirsyncer/config/
├── vdirsyncer/status/
├── vdirsyncer/token/
└── logs/
```

`caldav-backup` conserva snapshot atomici in `backup/snapshots/` e una copia consistente in `backup/latest/`. Un errore non sovrascrive l'ultimo backup valido.

## Synology

1. Copiare l'intera directory in `/volume1/docker/syncer`.
2. Installare Container Manager.
3. Creare `.env` sul NAS senza committarlo.
4. Eseguire `bash auth/regenerate-token.sh` da un PC con browser, oppure completare il device flow da un altro dispositivo.
5. Avviare con `docker compose up -d --build` via SSH.

Non usare percorsi assoluti nel Compose: tutti i mount del progetto sono relativi alla directory `syncer/`.

## Arresto e aggiornamento

```bash
docker compose down
docker compose up -d --build
```

I dati restano nella directory del progetto anche dopo `docker compose down`.
