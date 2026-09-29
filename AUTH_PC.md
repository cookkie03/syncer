# Autenticazione portabile

## Google Calendar e Contacts

I due servizi attivi richiedono token OAuth distinti. Il percorso consigliato
usa un client Google Cloud di tipo **Desktop app** e un browser su Mac/PC.
Il device flow in `auth/authorize-device.py` rimane disponibile per diagnosi,
ma la documentazione Google attuale non elenca gli scope Calendar e Contacts
tra quelli consentiti per quel flow: non farne il percorso principale.

1. Abilitare Google Calendar API e People API nel progetto Google Cloud.
2. Mettere il JSON del client Desktop in `auth/client_secret*.json` (un solo file)
   oppure passarlo con `--client-json`. Il file contiene una credenziale: non
   committarlo e non inviarlo in chat.
3. Verificare che `GOOGLE_DEVICE_CLIENT_ID` e `GOOGLE_DEVICE_CLIENT_SECRET`
   in `.env` corrispondano al client JSON. Questi nomi sono storici; Compose li
   passa a vdirsyncer come `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET`.
   Il client presente in questa copia ha restituito `deleted_client` nelle
   richieste di device code del 29 settembre 2026: va creato un nuovo client
   Desktop prima di chiedere nuovi token. Conservare il vecchio JSON solo come
   archivio, fuori dal pattern `auth/client_secret*.json`.
4. Sul computer con browser, dalla radice del progetto:

   ```bash
   python3 -m venv /tmp/syncer-auth-venv
   /tmp/syncer-auth-venv/bin/python -m pip install google-auth-oauthlib
   PYTHON_BIN=/tmp/syncer-auth-venv/bin/python bash auth/regenerate-token.sh
   ```

Il comando apre il consenso per Calendar e poi Contacts. Scrive
`vdirsyncer/token/google.json` e `vdirsyncer/token/google_contacts.json`.
Se esiste un token precedente, lo conserva in `.backup.<timestamp>` solo dopo
aver ricevuto un nuovo refresh token. Si possono copiare i due file attivi
e `.env` sul NAS insieme al progetto. Il refresh avviene poi senza browser,
finché Google non revoca o fa scadere il refresh token.

Sul NAS usare `docker compose up -d --build`; dopo aver copiato nuovi token
su uno stack già avviato, eseguire:

```bash
docker compose up -d --force-recreate vdirsyncer google-contacts-backup
```

La cartella `.venv` non è portabile tra macOS, Debian e Synology. Installare
la dipendenza Python sull'host che esegue l'autorizzazione; i container hanno
le proprie dipendenze.

## Spotify

Il backup usa `spotify-backup/data/.cache`, che contiene un refresh token
riutilizzabile dopo la copia del progetto. Se il token non è più valido,
eseguire `python3 spotify-backup/auth_helper.py` su Mac/PC con browser,
usando lo stesso client Spotify e una redirect URI registrata. Copiare poi
`spotify-backup/data/.cache` sul NAS. Non avviare due istanze del backup con
la stessa cache durante la migrazione.

## CalDAV

CalDAV usa `CALDAV_URL`, `CALDAV_USERNAME` e `CALDAV_PASSWORD` in `.env`; non
richiede OAuth. Dopo la copia, la raggiungibilità del server dal NAS e i
permessi dell'account vanno verificati con una richiesta di sola lettura.

## Verifica

Controllare i file attivi, non soltanto le copie `.bak`:

```bash
test -s vdirsyncer/token/google.json
test -s vdirsyncer/token/google_contacts.json
test -s spotify-backup/data/.cache
```

La presenza dei file non prova che i token siano ancora accettati. Verificare
il primo backup riuscito e il primo discover nei log del NAS.
