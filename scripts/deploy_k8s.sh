#!/usr/bin/env bash
# FILE: scripts/deploy_k8s.sh
# RUOLO: Pipeline automatizzata e idempotente per il rilascio completo dell'infrastruttura su Kubernetes.

set -euo pipefail # Interruzione immediata in caso di errori o variabili non definite

CLUSTER_NAME="scc-cluster"
# Ricava il percorso assoluto della cartella principale del progetto
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Avvio deployment dell'infrastruttura Secure Cloud Computing..."

# Posizionamento nella radice del progetto per consentire a Docker e kubectl di individuare i file relativi
cd "$PROJECT_ROOT"

# ------------------------------------------------------------------------------
# STEP 0: CONTROLLO DEI TOOL PREREQUISITI
# ------------------------------------------------------------------------------
# Itera sui tre comandi fondamentali: docker, kubectl, kind.
# command -v restituisce 0 se il comando è trovato nel PATH di sistema, altrimenti fallisce.
for cmd in docker kubectl kind; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "[-] Errore: il comando '$cmd' non e presente nel PATH. Eseguire prima: make setup"
        exit 1
    fi
done

# ------------------------------------------------------------------------------
# STEP 1: COMPILAZIONE DELL'IMMAGINE DOCKER (Passo C della traccia)
# ------------------------------------------------------------------------------
# docker build:
# - -t diabetes-api:latest -> Assegna il nome (tag) 'diabetes-api' con versione 'latest' all'immagine prodotta.
# - -f docker/Dockerfile -> Specifica il percorso del file di istruzioni Dockerfile.
# - . (punto finale) -> Il "build context": invia a Docker tutti i file della cartella corrente necessari per la build.
echo "[+] Step 1: Compilazione immagine Docker (diabetes-api:latest)..."
docker build -t diabetes-api:latest -f docker/Dockerfile .

# ------------------------------------------------------------------------------
# STEP 2: CREAZIONE DEL CLUSTER KIND MULTI-NODO (Passo F della traccia)
# ------------------------------------------------------------------------------
# kind get clusters: Elenca tutti i cluster KinD attivi sul computer.
# Se 'scc-cluster' non compare nell'elenco, lo crea; altrimenti salta questo passaggio senza errori.
if ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "[+] Step 2: Creazione cluster KinD (1 Master + 2 Workers)..."
    # kind create cluster:
    # - --name scc-cluster -> Assegna un nome al cluster.
    # - --config k8s/kind-config.yaml -> Legge la topologia dei 3 nodi e la mappatura delle porte 80 e 443.
    kind create cluster --name "$CLUSTER_NAME" --config k8s/kind-config.yaml
else
    echo "[+] Il cluster KinD '$CLUSTER_NAME' e gia attivo."
fi

# ------------------------------------------------------------------------------
# STEP 3: CARICAMENTO DELL'IMMAGINE LOCALE DENTRO KIND
# ------------------------------------------------------------------------------
# KinD viene eseguito all'interno di container Docker isolati. I nodi worker non hanno accesso diretto alle immagini dell'host locale.
# 'kind load docker-image' trasferisce i layer dell'immagine locale 'diabetes-api:latest'
# direttamente all'interno del motore containerd dei nodi del cluster, evitando di doverla pubblicare su Docker Hub online.
echo "[+] Step 3: Caricamento dell'immagine locale nei nodi di calcolo KinD..."
kind load docker-image diabetes-api:latest --name "$CLUSTER_NAME"

# ------------------------------------------------------------------------------
# STEP 4: INSTALLAZIONE DI NGINX INGRESS CONTROLLER
# ------------------------------------------------------------------------------
# kubectl apply -f <URL>: Scarica e applica il manifest ufficiale del controller NGINX ottimizzato per KinD.
echo "[+] Step 4: Installazione di NGINX Ingress Controller per KinD..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# kubectl wait: Mette in pausa l'esecuzione dello script finché una condizione non si avvera:
# - --namespace ingress-nginx: Cerca nel namespace dove risiede NGINX.
# - --for=condition=ready pod: Attende che lo stato del pod diventi 'Ready' (pronto a servire).
# - --selector app.kubernetes.io/component=controller: Filtra solo il pod del controller.
# - --timeout=180s: Attende al massimo 3 minuti (180 secondi); se scade, interrompe lo script con errore.
echo "[+] Attesa che NGINX Ingress sia pronto..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s

# ABILITAZIONE DELLE ANNOTAZIONI SNIPPET IN NGINX:
# Per motivi di sicurezza (CVE-2021-25742), le versioni recenti di NGINX Ingress disabilitano di default
# le annotazioni 'server-snippet' e 'configuration-snippet'.
# Questo comando aggiorna la ConfigMap di NGINX abilitandole, permettendo l'uso del
# Rate Limiting e degli Header di sicurezza personalizzati specificati in k8s/ingress.yaml.
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p '{"data":{"allow-snippet-annotations":"true"}}' 2>/dev/null || true

# ------------------------------------------------------------------------------
# STEP 5: APPLICAZIONE DEI MANIFEST APPLICATIVI (Passo H della traccia)
# ------------------------------------------------------------------------------
# kubectl apply: Applica lo stato desiderato dichiarato nei file YAML:
# - k8s/deployment.yaml: Crea le 3 repliche del pod con limiti di risorse e probe.
# - k8s/service.yaml: Crea il bilanciatore di carico interno (ClusterIP).
# - k8s/ingress.yaml: Crea le regole di routing e sicurezza di NGINX verso il Service.
echo "[+] Step 5: Applicazione di Deployment, Service, PDB e Ingress..."
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/pdb.yaml
kubectl apply -f k8s/ingress.yaml

# IDEMPOTENZA (kubectl rollout restart):
# In caso di modifiche al codice e ricompilazione dell'immagine con tag 'latest', Kubernetes non riavvierebbe i pod
# perché il file deployment.yaml non è cambiato.
# 'rollout restart' forza Kubernetes a spegnere gradualmente le vecchie repliche e avviarne di nuove
# caricando l'immagine aggiornata (senza disservizio!).
echo "[+] Forzatura riavvio controllato repliche (Rollout Restart idempotente)..."
kubectl rollout restart deployment/diabetes-forecasting-api

# Attende che tutte e 3 le repliche abbiano superato con successo la readiness probe (/ready)
kubectl rollout status deployment/diabetes-forecasting-api --timeout=120s

# ------------------------------------------------------------------------------
# STEP 6: VERIFICA FINALE DELLO STATO DEL CLUSTER
# ------------------------------------------------------------------------------
echo "[✓] Deployment completato con successo!"
echo ""
echo "--- Nodi del Cluster ---"
# kubectl get nodes -o wide: Mostra i 3 nodi (1 master + 2 workers), la versione del kernel e l'IP interno
kubectl get nodes -o wide
echo ""
echo "--- Pod Attivi ---"
# kubectl get pods -o wide: Mostra i 3 pod attivi, su quale nodo worker risiedono e il loro stato di salute (Running)
kubectl get pods -o wide
