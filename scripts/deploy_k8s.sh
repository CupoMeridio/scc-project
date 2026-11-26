#!/usr/bin/env bash
# FILE: scripts/deploy_k8s.sh
# RUOLO: Pipeline automatizzata per deploy HA su KinD (CloudNativePG, Redis Queue/Worker, Alembic, NetworkPolicy).

set -euo pipefail

CLUSTER_NAME="scc-cluster"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Avvio deployment dell'infrastruttura Secure Cloud Computing (tag: ${IMAGE_TAG})..."
cd "$PROJECT_ROOT"

# I manifest usano :1.0.0 come tag di default. Questa funzione li allinea al
# tag effettivamente buildato/caricato, evitando version skew tra i nodi.
apply_tagged() {
  sed "s/:1\.0\.0/:${IMAGE_TAG}/g" "$1" | kubectl apply -f -
}

for cmd in docker kubectl kind openssl; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "[-] Errore: il comando '$cmd' non è presente nel PATH. Eseguire prima: make setup"
        exit 1
    fi
done

echo "[+] Step 1: Compilazione immagini Docker multi-stage non-root (tag ${IMAGE_TAG})..."
# Una sola immagine del piano dati (Dockerfile.saas) è condivisa da SaaS API, Ingestion
# Worker e Job di migrazione/seed: gli ultimi due ne differiscono solo per il comando (P7).
docker build -t "diabetes-api:${IMAGE_TAG}" -f docker/Dockerfile .
docker build -t "diabetes-saas-api:${IMAGE_TAG}" -f docker/Dockerfile.saas .
docker build -t "diabetes-frontend:${IMAGE_TAG}" -f docker/Dockerfile.frontend .

if ! kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "[+] Step 2: Creazione cluster KinD (1 Master + 2 Workers)..."
    kind create cluster --name "$CLUSTER_NAME" --config k8s/kind-config.yaml
else
    echo "[+] Il cluster KinD '$CLUSTER_NAME' è già attivo."
fi

echo "[+] Step 3: Caricamento delle immagini pinnate nei nodi KinD..."
kind load docker-image "diabetes-api:${IMAGE_TAG}" --name "$CLUSTER_NAME"
kind load docker-image "diabetes-saas-api:${IMAGE_TAG}" --name "$CLUSTER_NAME"
kind load docker-image "diabetes-frontend:${IMAGE_TAG}" --name "$CLUSTER_NAME"

echo "[+] Step 4: Installazione NGINX Ingress Controller per KinD (vendorizzato, controller-v1.15.1)..."
# D3: manifest vendorizzato e riproducibile, non scaricato da Internet durante il deploy.
# Fonte: kubernetes/ingress-nginx controller-v1.15.1 (deploy/static/provider/kind/deploy.yaml).
# Controller pinnato per digest: ingress-nginx/controller:v1.15.1@sha256:594ceea7...
# sha256 del manifest: 2a3ae008c8786431115502644e77ab398fdebfb721a5d1195ed3089cde3299df
kubectl apply -f k8s/vendor/ingress-nginx-controller-v1.15.1.yaml
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s

# allow-snippet-annotations=false (CVE-2021-25742) e server-tokens=false sono requisiti
# di sicurezza, non opzionali: nessun || true, il deploy FALLISCE se non vengono applicati.
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge \
  -p '{"data":{"allow-snippet-annotations":"false","server-tokens":"false"}}'
SNIPPETS=$(kubectl get cm ingress-nginx-controller -n ingress-nginx -o jsonpath='{.data.allow-snippet-annotations}')
if [ "$SNIPPETS" != "false" ]; then
  echo "[-] allow-snippet-annotations atteso 'false', trovato '${SNIPPETS:-vuoto}'. Deploy interrotto." >&2
  exit 1
fi
echo "[✓] allow-snippet-annotations=false confermato sulla ConfigMap del controller."

if ! kubectl get secret diabetes-tls-cert >/dev/null 2>&1; then
  echo "[+] Generazione certificato TLS autofirmato per localhost con SAN..."
  TMP_KEY=$(mktemp)
  TMP_CRT=$(mktemp)
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
    -keyout "$TMP_KEY" -out "$TMP_CRT" \
    -subj "/CN=localhost/O=DiabetesEarlyWarning" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -addext "basicConstraints=CA:FALSE" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
  kubectl create secret tls diabetes-tls-cert --cert="$TMP_CRT" --key="$TMP_KEY"
  rm -f "$TMP_KEY" "$TMP_CRT"
  echo "[✓] Secret TLS 'diabetes-tls-cert' creato."
else
  echo "[✓] Secret TLS 'diabetes-tls-cert' già presente."
fi

# ------------------------------------------------------------------------------
# Segreti: saas-pg-auth contiene le credenziali del proprietario del DB (saas_migrator).
# CNPG le usa durante initdb e le preserva tra restart. Se il cluster esiste già,
# la password viene recuperata dal Secret per non disallinearla dal ruolo creato da
# CNPG (re-deploy idempotente sullo stesso cluster). Su un cluster nuovo il Secret
# non esiste e viene generato.
# saas-secrets viene sempre ricreato: il progetto è in sviluppo, nessuna retrocompatibilità.
# I DATABASE_URL usano sslmode=require: CloudNativePG espone TLS sul servizio -rw (P12).
# ------------------------------------------------------------------------------
secret_key() { kubectl get secret "$1" -o jsonpath="{.data.$2}" 2>/dev/null || true; }

# Recupera la password di saas_migrator dal secret esistente, o genera una nuova
if kubectl get secret saas-pg-auth >/dev/null 2>&1; then
  PG_PASS=$(secret_key saas-pg-auth password | base64 -d)
  echo "[✓] Secret 'saas-pg-auth' già presente: password di saas_migrator recuperata."
else
  PG_PASS=$(openssl rand -hex 16)
  kubectl create secret generic saas-pg-auth \
    --type=kubernetes.io/basic-auth \
    --from-literal=username=saas_migrator \
    --from-literal=password="$PG_PASS"
  echo "[✓] Secret 'saas-pg-auth' creato (owner: saas_migrator)."
fi

# Password del ruolo applicativo saas_app (creato dalla migrazione)
APP_PASS=$(openssl rand -hex 16)

echo "[+] Creazione segreti applicativi ('saas-secrets')..."
REDIS_PASS=$(openssl rand -hex 16)
JWT_KEY=$(openssl rand -hex 32)

kubectl delete secret saas-secrets --ignore-not-found >/dev/null 2>&1 || true
kubectl create secret generic saas-secrets \
  --from-literal=POSTGRES_USER=saas_migrator \
  --from-literal=POSTGRES_PASSWORD="$PG_PASS" \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_app:${APP_PASS}@diabetes-pg-rw:5432/diabetes_saas?sslmode=require" \
  --from-literal=MIGRATOR_DATABASE_URL="postgresql+psycopg://saas_migrator:${PG_PASS}@diabetes-pg-rw:5432/diabetes_saas?sslmode=require" \
  --from-literal=SAAS_APP_PASSWORD="$APP_PASS" \
  --from-literal=REDIS_PASSWORD="$REDIS_PASS" \
  --from-literal=REDIS_URL="redis://:${REDIS_PASS}@redis-service:6379/0" \
  --from-literal=JWT_SECRET_KEY="$JWT_KEY"
echo "[✓] Secret 'saas-secrets' creato (DATABASE_URL→saas_app, MIGRATOR_DATABASE_URL→saas_migrator)."

for key in POSTGRES_USER POSTGRES_PASSWORD DATABASE_URL MIGRATOR_DATABASE_URL SAAS_APP_PASSWORD REDIS_PASSWORD REDIS_URL JWT_SECRET_KEY; do
  if [ -z "$(secret_key saas-secrets "$key")" ]; then
    echo "[-] Chiave '$key' mancante in saas-secrets." >&2
    exit 1
  fi
done

echo "[+] Step 5: Installazione CloudNativePG Operator (v1.24.2) e PostgreSQL HA (Primary + Standby)..."
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.2.yaml
kubectl rollout status deployment/cnpg-controller-manager -n cnpg-system --timeout=180s

kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/postgres-ha.yaml
kubectl apply -f k8s/pg-backup-cronjob.yaml
echo "[+] Attesa che il cluster PostgreSQL HA 'diabetes-pg' (2 istanze) sia pronto..."
kubectl wait --for=condition=Ready cluster/diabetes-pg --timeout=300s

echo "[+] Step 6: Avvio coda asincrona Redis e migrazioni Alembic (nessun dato demo: usare make seed-demo)..."
kubectl apply -f k8s/redis.yaml
# saas-secrets è appena stato ricreato con una nuova REDIS_PASSWORD:
# riavviamo anche Redis per riallineare la password in RAM tra API, Worker e broker.
kubectl rollout restart deployment/diabetes-redis
kubectl rollout status deployment/diabetes-redis --timeout=120s

kubectl delete job diabetes-saas-seed --ignore-not-found >/dev/null 2>&1 || true
apply_tagged k8s/seed-job.yaml
kubectl wait --for=condition=complete job/diabetes-saas-seed --timeout=180s

echo "[+] Step 7: Applicazione microservizi, Ingestion Worker, PDB e Ingress HTTPS..."
apply_tagged k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ml-headless-service.yaml
kubectl apply -f k8s/pdb.yaml
apply_tagged k8s/saas-deployment.yaml
kubectl apply -f k8s/saas-service.yaml
kubectl apply -f k8s/saas-pdb.yaml
apply_tagged k8s/ingestion-worker-deployment.yaml
kubectl apply -f k8s/worker-pdb.yaml
apply_tagged k8s/frontend-deployment.yaml
kubectl apply -f k8s/frontend-service.yaml
kubectl apply -f k8s/frontend-pdb.yaml
kubectl apply -f k8s/ingress.yaml

kubectl rollout restart deployment/diabetes-forecasting-api
kubectl rollout restart deployment/diabetes-saas-api
kubectl rollout restart deployment/diabetes-ingestion-worker
kubectl rollout restart deployment/diabetes-frontend

kubectl rollout status deployment/diabetes-forecasting-api --timeout=120s
kubectl rollout status deployment/diabetes-saas-api --timeout=180s
kubectl rollout status deployment/diabetes-ingestion-worker --timeout=120s
kubectl rollout status deployment/diabetes-frontend --timeout=120s

echo "[✓] Deployment HA completato con successo!"
kubectl get nodes -o wide
kubectl get cluster diabetes-pg
kubectl get pods -o wide
