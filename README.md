# cam4web

Una webcam a scatti: un agente Raspberry invia JPEG tramite HTTPS a FastAPI; una pagina pubblica mostra l'ultima immagine e fino a 24 scatti orari. Nessun account utente richiesto. Python 3.10+ (Docker: 3.12).

## Prova locale senza Raspberry

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
cp .env.example .env
# Sostituire il token in .env con un segreto casuale:
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
set -a
source .env
set +a
.venv/bin/python -m server.run
```

Aprire http://localhost:8000. In un secondo terminale, dalla cartella del repository:

```bash
export CAM4WEB_TOKEN='il-token-impostato-in-.env'
.venv/bin/python scripts/demo.py --hours 24 --output /tmp/cam4web-test.jpg
```

Il simulatore genera paesaggi geometrici esplicitamente marcati DEMO e invia 24 immagini con date nell'ultima giornata tramite le API reali. Per aggiornamenti continui con l'agente:

```bash
export CAM4WEB_URL=http://127.0.0.1:8000
export CAM4WEB_CAMERA=casa
export CAM4WEB_INTERVAL=60
.venv/bin/python agent/cam4web_agent.py --image /tmp/cam4web-test.jpg
# Aggiungere --once per un unico invio.
```

La modalità demo non è esposta dal server: richiede il token di caricamento. Lo storico si popola solo nelle ore ricevute; nessun dato viene inventato dal server. Per svuotare le prove, fermare il server ed eliminare la cartella locale `data/`.

## Deploy Railway da GitHub

1. Caricare questi file nel repository GitHub e collegarlo a un nuovo servizio Railway. Il Dockerfile alla radice viene usato da `railway.toml`.
2. Collegare un volume persistente al servizio, mount path `/data`.
3. Impostare `CAMERA_TOKENS` come JSON, ad esempio `{"casa":"SEGRETO-CASUALE-DI-ALMENO-24-CARATTERI"}`. Nell'interfaccia Railway inserire il JSON senza apici esterni. `DATA_DIR=/data` è già definito nell'immagine Docker.
4. Generare il dominio pubblico HTTPS. L'applicazione ascolta su `0.0.0.0:$PORT`; healthcheck `/health`.
5. Provare da locale: `CAM4WEB_TOKEN='segreto' .venv/bin/python scripts/demo.py --url https://nome.up.railway.app --hours 24`.
6. Configurare poi il Raspberry con lo stesso URL, camera e token.

Usare **una replica**: SQLite e il volume sono locali al servizio. Il volume non sostituisce un backup; Railway può avere una breve interruzione durante i deploy con volume. Il filesystem senza volume è effimero. Non vengono creati automaticamente servizio, volume o variabili sull'account Railway.

Riferimenti: [volumi Railway](https://docs.railway.com/volumes), [healthcheck](https://docs.railway.com/deployments/healthchecks), [configurazione](https://docs.railway.com/config-as-code/reference).

## Installazione sul Raspberry Pi 3B

Usare Raspberry Pi OS con stack camera moderno. Verificare prima l'acquisizione:

```bash
sudo apt update
sudo apt install rpicam-apps-lite
rpicam-still --nopreview --timeout 2000 --output /tmp/test.jpg
```

Su installazioni precedenti l'agente riconosce anche `libcamera-still`. Lo stack legacy `raspistill` non è supportato. [Documentazione camera Raspberry Pi](https://www.raspberrypi.com/documentation/computers/camera_software.html).

Copiare il repository in `/opt/cam4web`, quindi:

```bash
sudo useradd --system --no-create-home --groups video,render cam4web
sudo cp /opt/cam4web/agent/agent.env.example /etc/cam4web-agent.env
sudo chmod 600 /etc/cam4web-agent.env
sudo nano /etc/cam4web-agent.env
sudo cp /opt/cam4web/agent/cam4web-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cam4web-agent
sudo journalctl -u cam4web-agent -f
```

Impostare URL HTTPS, token, ID camera e intervallo nell'env. Il file deve essere leggibile da root: systemd passa le variabili al processo. Assicurarsi che script e cartelle in `/opt/cam4web` siano leggibili dall'utente `cam4web`. I gruppi `video` e `render` devono esistere sul sistema. Per applicare modifiche: `sudo systemctl restart cam4web-agent`.

L'agente usa solo la libreria standard Python, acquisisce JPEG 1280×720 di default, non archivia scatti e rimuove il file temporaneo dopo ogni tentativo. In caso di errore riprova con un nuovo scatto, con intervallo crescente fino ad almeno 300 secondi (o l'intervallo configurato, se maggiore). Non recupera gli scatti persi. Camera e permessi hardware restano da verificare sul dispositivo reale.

## Contratto API

Documentazione interattiva `/docs`. Caricamento:

```bash
curl --fail-with-body -X POST "$CAM4WEB_URL/api/v1/cameras/casa/images" \
  -H "Authorization: Bearer $CAM4WEB_TOKEN" \
  -H 'Content-Type: image/jpeg' \
  -H "X-Captured-At: $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --data-binary @/tmp/cam4web-test.jpg
```

- `GET /api/v1/cameras`: ID delle camere pubbliche, senza token.
- `GET /api/v1/cameras/{id}/status`: metadati dell'ultima immagine, oppure `latest: null`.
- `GET /api/v1/cameras/{id}/latest`: JPEG corrente, 404 se assente.
- `GET /api/v1/cameras/{id}/history`: metadati dei campioni orari, dal più recente.
- `GET /api/v1/cameras/{id}/images/{image_id}`: JPEG; `?download=true` forza lo scaricamento (anche su `/latest`).

Letture pubbliche; scritture protette da token per camera. La dipendenza `can_view` concentra la politica di lettura in vista di account futuri; non vengono implementati account o ruoli fittizi. I token si ruotano aggiornando variabili server e agente. Non commetterli nel repository.

Accettati solo JPEG validi, massimo 5 MiB di upload e 20 milioni di pixel, data ISO 8601 con fuso nelle ultime 24 ore e tolleranza futura di 5 minuti. `MAX_IMAGE_BYTES` cambia il limite. Il server ricodifica in JPEG rimuovendo EXIF/GPS: il download non è l'originale byte per byte.

## Conservazione

SQLite contiene JPEG e metadati nella stessa transazione, sul volume. Il primo scatto **ricevuto** per ora UTC diventa il campione di quell'ora. Gli altri aggiornano l'immagine corrente solo se hanno data più recente; upload fuori ordine non riportano indietro la webcam. Si conservano al massimo 24 campioni orari più la corrente, eliminando quanto precede le ultime 24 ore. Gli estremi parziali delle ore sono limitati ai 24 campioni più recenti. Nessun riempimento per ore senza scatti.

Pulizia all'avvio, ogni minuto e durante gli upload; le API escludono immediatamente le immagini scadute. Gli scatti intermedi smettono di essere accessibili quando sostituiti. SQLite riutilizza lo spazio liberato; il file sul volume non necessariamente si riduce dopo le cancellazioni. L'interfaccia visualizza Europe/Rome, inclusi i cambi d'ora; lo stato “nessuno scatto recente” usa una soglia fissa di 3 minuti.

## Verifica

```bash
.venv/bin/python -m pytest -q
node --check server/static/app.js
```

`requirements.lock.txt` registra tutte le versioni usate nella verifica locale (Python 3.10); `requirements.txt` e `requirements-dev.txt` definiscono gli intervalli supportati.

I test verificano token, validazione, limiti, download, isolamento tra camere, scatti concorrenti, campionamento orario, arrivi fuori ordine, scadenza e persistenza alla riapertura. GitHub Actions li esegue su push e pull request.
