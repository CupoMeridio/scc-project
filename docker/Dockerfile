# FILE: docker/Dockerfile
# RUOLO: Istruzioni di compilazione per l'immagine container Docker dell'applicazione.

# ------------------------------------------------------------------------------
# STADIO 1: BUILDER (Ambiente di compilazione e scaricamento dipendenze)
# ------------------------------------------------------------------------------
# FROM: Definisce l'immagine di partenza fissata per digest (pinning immutabile per riproducibilità).
# - python:3.14.10-slim: Versione ufficiale minimale basata su Debian 13 (trixie) slim.
#   Rispetto all'immagine standard 'python:3.14', 'slim' pesa molto meno
#   e non contiene compilatori C, documentazione o utility inutilizzate, abbattendo le CVE rilevate da Docker Scout.
#   Il digest è quello dell'indice multi-arch (output di `docker buildx imagetools inspect`,
#   archiviato in docs/audit_reports/base_images_digests.txt): il tag da solo può essere ri-puntato.
# - AS builder: Assegna un nome simbolico a questo stadio intermedio. I file generati qui
#   potranno essere copiati nel secondo stadio; tutto il resto verrà automaticamente eliminato!
FROM python:3.14.10-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b AS builder

# WORKDIR /build: Imposta la cartella di lavoro corrente all'interno del container.
# Se la cartella /build non esiste, Docker la crea automaticamente.
WORKDIR /build

# COPY requirements.lock .: Copia il lock (versione + hash) prima del codice sorgente
# per massimizzare il riutilizzo della cache di Docker durante i successivi build.
COPY requirements.lock .

# RUN pip install ...:
# - --no-cache-dir: Ordina a pip di non salvare i file .whl/.tar.gz scaricati nella cache locale (/root/.cache/pip),
#   risparmiando spazio su disco.
# - --require-hashes: Installa un pacchetto solo se il suo sha256 coincide con quello fissato
#   nel lock, a difesa dal supply-chain tampering. Nessun `pip install --upgrade pip`.
# - --user: Installa le librerie nella cartella dell'utente (/root/.local/lib/python3.14/site-packages)
#   invece che nella directory globale di sistema (/usr/local). Questo renderà facilissimo copiarle nello stadio successivo!
# - -r requirements.lock: Legge l'elenco dei pacchetti pinnati dal file.
RUN pip install --no-cache-dir --require-hashes --user -r requirements.lock

# ------------------------------------------------------------------------------
# STADIO 2: RUNNER (Ambiente di produzione finale: minimale, sicuro e non-root)
# ------------------------------------------------------------------------------
# Inizializzazione da un'immagine pulita, fissata per lo stesso digest dello stadio di build.
FROM python:3.14.10-slim@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b AS runner

# ENV: Imposta variabili d'ambiente permanenti valide per tutti i processi del container.
# - PYTHONDONTWRITEBYTECODE=1: Impedisce all'interprete Python di generare file compilati .pyc sul disco.
#   Questo velocizza l'avvio ed è ideale per container con filesystem in sola lettura (read-only).
# - PYTHONUNBUFFERED=1: Disabilita il buffer di stdout e stderr di Python.
#   I messaggi di log (print, logger.info) vengono inviati IMMEDIATAMENTE alla console senza attendere il buffer,
#   garantendo che Kubernetes veda i log in tempo reale.
# - PATH: Aggiunge la cartella dei binari dell'utente non privilegiato (/home/appuser/.local/bin) al percorso di sistema,
#   così il comando 'uvicorn' può essere lanciato direttamente.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/appuser/.local/bin:$PATH" \
    PORT=8000 \
    HOST=0.0.0.0 \
    MODEL_PATH="model/model.onnx" \
    METADATA_PATH="model/model_metadata.json"

# Imposta la cartella di lavoro principale dell'applicazione
WORKDIR /app

# CREAZIONE DELL'UTENTE NON-ROOT (MISURA CHIAVE DI HARDENING):
# Di default i container girano come 'root' (UID 0). Se un attaccante compromettesse l'applicazione,
# avrebbe privilegi di amministratore e potrebbe evadere dal container (Container Escape).
# - addgroup --system --gid 10001 appgroup: Crea un gruppo di sistema con ID numerico esplicito 10001.
# - adduser --system --uid 10001 ... appuser: Crea un utente di sistema privo di password e senza privilegi di sudo,
#   assegnandogli la home directory /home/appuser.
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup --home /home/appuser appuser

# COPY --from=builder ...: Preleva file generati nello stadio 'builder'!
# Copia la cartella /root/.local (dove pip ha installato FastAPI e ONNX Runtime) dentro /home/appuser/.local.
# --chown=appuser:appgroup: Assegna immediatamente la proprietà dei file all'utente non-root.
COPY --from=builder --chown=appuser:appgroup /root/.local /home/appuser/.local

# Copia il codice sorgente dell'API e solo gli artefatti necessari per l'inferenza runtime
# (vengono esclusi script di training e pesi grezzi PyTorch .pt, riducendo le dimensioni dell'immagine)
COPY --chown=appuser:appgroup common/ /app/common/
COPY --chown=appuser:appgroup app/ /app/app/
COPY --chown=appuser:appgroup model/model.onnx* /app/model/
COPY --chown=appuser:appgroup model/model_metadata.json /app/model/

# chown -R: Assicura ricorsivamente che tutte le cartelle appartengano all'utente non privilegiato
RUN chown -R appuser:appgroup /app /home/appuser

# USER: Passa dall'utente 'root' all'utente 'appuser' (UID 10001).
# Da questa riga in poi, nessun comando potrà mai essere eseguito come amministratore!
USER 10001:10001

# EXPOSE 8000: Istruzione documentale per indicare che il container ascolta sulla porta TCP 8000.
# Non apre realmente la porta sull'host, ma serve a Docker e Kubernetes per tracciarla.
EXPOSE 8000

# HEALTHCHECK: Meccanismo interno di Docker per monitorare la salute del container.
# - --interval=30s: Intervallo di verifica ogni 30 secondi.
# - --timeout=3s: Considera il controllo fallito se l'app non risponde entro 3 secondi.
# - --start-period=5s: Concede 5 secondi all'avvio prima di iniziare i controlli (Cold start).
# - --retries=3: Se fallisce 3 volte di fila, il container viene marchiato come "unhealthy".
# - CMD python3 -c ...: Esegue un micro-script Python che invoca l'endpoint locale /health.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# CMD: Comando predefinito eseguito quando il container viene avviato.
# L'uso della forma 'exec' all'interno della shell assicura che uvicorn diventi il processo PID 1
# e riceva direttamente i segnali del kernel OS (SIGTERM/SIGINT) per un graceful shutdown in Kubernetes,
# supportando al contempo le variabili d'ambiente HOST e PORT.
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000} --workers 1"]
