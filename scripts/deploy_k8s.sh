#!/usr/bin/env bash
# FILE: scripts/deploy_k8s.sh
# RUOLO: Pipeline automatizzata per deploy HA per zone su KinD con TLS end-to-end (Q2),
#        EdDSA locale + provisioning 2-step (V1), CNPG managed.roles (V8),
#        manifest ML firmato Ed25519 (V4), backup asimmetrico X25519 (V6) e vendor patch (V7).

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-scc-cluster}"
IMAGE_TAG="${IMAGE_TAG:-1.0.0}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY_BIN="${PROJECT_ROOT}/venv/bin/python3"
if [ ! -x "$PY_BIN" ]; then
  PY_BIN="$(command -v python3)"
fi

echo "Avvio deployment dell'infrastruttura Secure Cloud Computing (tag: ${IMAGE_TAG})..."
cd "$PROJECT_ROOT"

apply_tagged() {
  sed "s/:1\.0\.0/:${IMAGE_TAG}/g" "$1" | kubectl apply -f -
}

for cmd in docker kubectl kind openssl patch sha256sum; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "[-] Errore: il comando '$cmd' non è presente nel PATH. Eseguire prima: make setup" >&2
    exit 1
  fi
done

echo "[+] Step 1: Compilazione delle 4 immagini Docker multi-stage non-root (tag ${IMAGE_TAG})..."
docker build -t "diabetes-api:${IMAGE_TAG}" -f docker/Dockerfile .
docker build -t "diabetes-saas-api:${IMAGE_TAG}" -f docker/Dockerfile.saas .
docker build -t "diabetes-frontend:${IMAGE_TAG}" -f docker/Dockerfile.frontend .
docker build -t "diabetes-trainer:${IMAGE_TAG}" -f docker/Dockerfile.trainer .

if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
  echo "[!] Eliminazione del cluster KinD esistente '$CLUSTER_NAME' (reset distruttivo: database, PVC e Secret saranno persi)..."
  kind delete cluster --name "$CLUSTER_NAME"
fi
echo "[+] Creazione cluster KinD pulito (1 Master + 2 Workers)..."
kind create cluster --name "$CLUSTER_NAME" --config k8s/kind-config.yaml
# Evita che i nodi KinD ripartano automaticamente all'avvio del PC di sviluppo (restart policy no).
docker update --restart=no $(kind get nodes --name "$CLUSTER_NAME") >/dev/null

echo "[+] Step 3: Caricamento delle immagini pinnate nei nodi KinD..."
kind load docker-image "diabetes-api:${IMAGE_TAG}" --name "$CLUSTER_NAME"
kind load docker-image "diabetes-saas-api:${IMAGE_TAG}" --name "$CLUSTER_NAME"
kind load docker-image "diabetes-frontend:${IMAGE_TAG}" --name "$CLUSTER_NAME"
kind load docker-image "diabetes-trainer:${IMAGE_TAG}" --name "$CLUSTER_NAME"

echo "[+] Step 4: Verifica checksum vendor Ingress NGINX v1.15.1 + patch KinD (V7)..."
sha256sum -c k8s/vendor/ingress-nginx-controller-v1.15.1.yaml.sha256
TMP_ING=$(mktemp)
patch -s -o "$TMP_ING" k8s/vendor/ingress-nginx-controller-v1.15.1.yaml < k8s/vendor/kind-ingress-ready.patch
kubectl apply -f "$TMP_ING"
rm -f "$TMP_ING"
kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=180s

kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge \
  -p '{"data":{"allow-snippet-annotations":"false","server-tokens":"false"}}'
SNIPPETS=$(kubectl get cm ingress-nginx-controller -n ingress-nginx -o jsonpath='{.data.allow-snippet-annotations}')
if [ "$SNIPPETS" != "false" ]; then
  echo "[-] allow-snippet-annotations atteso 'false', trovato '${SNIPPETS:-vuoto}'." >&2
  exit 1
fi
echo "[✓] allow-snippet-annotations=false confermato sulla ConfigMap del controller."

# metrics-server per Automated Scaling Listener (HPA) su tutte le zone online
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null 2>&1 || true
kubectl rollout status deployment/metrics-server -n kube-system --timeout=120s || true

# Certificato edge TLS per localhost Ingress (cluster nuovo: generato a ogni deploy)
TMP_KEY=$(mktemp); TMP_CRT=$(mktemp)
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout "$TMP_KEY" -out "$TMP_CRT" \
  -subj "/CN=localhost/O=DiabetesEarlyWarning" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  -addext "basicConstraints=CA:FALSE" \
  -addext "extendedKeyUsage=serverAuth" 2>/dev/null
kubectl create secret tls diabetes-tls-cert --cert="$TMP_CRT" --key="$TMP_KEY" >/dev/null
rm -f "$TMP_KEY" "$TMP_CRT"
echo "[✓] Secret edge TLS 'diabetes-tls-cert' creato."

# CA interna + certificati TLS di servizio (Q2: TLS interno tra Ingress, servizi, Redis e ModelRepo)
TMP_TLS_DIR=$(mktemp -d)
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout "${TMP_TLS_DIR}/ca.key" -out "${TMP_TLS_DIR}/ca.crt" \
  -subj "/CN=scc-internal-ca/O=DiabetesInternal" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

kubectl create configmap internal-ca \
  --from-file=ca.crt="${TMP_TLS_DIR}/ca.crt" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

issue_internal_cert() {
  local secret_name="$1"
  local cn="$2"
  local san="$3"
  openssl req -newkey rsa:2048 -nodes \
    -keyout "${TMP_TLS_DIR}/${secret_name}.key" -out "${TMP_TLS_DIR}/${secret_name}.csr" \
    -subj "/CN=${cn}/O=DiabetesInternal" 2>/dev/null
  openssl x509 -req -in "${TMP_TLS_DIR}/${secret_name}.csr" \
    -CA "${TMP_TLS_DIR}/ca.crt" -CAkey "${TMP_TLS_DIR}/ca.key" -CAcreateserial \
    -out "${TMP_TLS_DIR}/${secret_name}.crt" -days 365 \
    -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n" "$san") 2>/dev/null
  kubectl delete secret "$secret_name" --ignore-not-found >/dev/null 2>&1 || true
  kubectl create secret tls "$secret_name" \
    --cert="${TMP_TLS_DIR}/${secret_name}.crt" --key="${TMP_TLS_DIR}/${secret_name}.key" >/dev/null
}

issue_internal_cert "iam-internal-tls" "diabetes-iam-service" "DNS:diabetes-iam-service,DNS:diabetes-iam-service.default.svc,DNS:localhost,IP:127.0.0.1"
issue_internal_cert "clinical-internal-tls" "diabetes-clinical-service" "DNS:diabetes-clinical-service,DNS:diabetes-clinical-service.default.svc,DNS:localhost,IP:127.0.0.1"
issue_internal_cert "manager-internal-tls" "diabetes-manager-service" "DNS:diabetes-manager-service,DNS:diabetes-manager-service.default.svc,DNS:localhost,IP:127.0.0.1"
issue_internal_cert "ingest-internal-tls" "diabetes-ingest-service" "DNS:diabetes-ingest-service,DNS:diabetes-ingest-service.default.svc,DNS:localhost,IP:127.0.0.1"
issue_internal_cert "ml-internal-tls" "diabetes-forecasting-service" "DNS:diabetes-forecasting-service,DNS:diabetes-forecasting-service.default.svc,DNS:diabetes-forecasting-headless,DNS:diabetes-forecasting-headless.default.svc,DNS:localhost,IP:127.0.0.1"
issue_internal_cert "model-repo-internal-tls" "diabetes-model-repo" "DNS:diabetes-model-repo,DNS:diabetes-model-repo.default.svc,DNS:localhost,IP:127.0.0.1"
issue_internal_cert "redis-internal-tls" "redis-service" "DNS:redis-service,DNS:redis-service.default.svc,DNS:localhost,IP:127.0.0.1"
rm -rf "$TMP_TLS_DIR"
echo "[✓] Certificati TLS interni (CA + 7 servizi) creati."

# Segreto DB owner (saas_migrator): sempre nuovo, il cluster viene ricreato da zero
PG_PASS=$(openssl rand -hex 16)
kubectl create secret generic saas-pg-auth \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=saas_migrator \
  --from-literal=password="$PG_PASS" >/dev/null

# 6 password DB + secret CNPG managed.roles (`pg-role-*`, zero CREATEROLE)
IAM_PW=$(openssl rand -hex 16)
CLINICAL_PW=$(openssl rand -hex 16)
MANAGER_PW=$(openssl rand -hex 16)
INGEST_PW=$(openssl rand -hex 16)
WORKER_PW=$(openssl rand -hex 16)
TRAINER_PW=$(openssl rand -hex 16)
TRAIN_SALT=$(openssl rand -hex 16)

REDIS_PASS=$(openssl rand -hex 16)
REDIS_REV_PASS=$(openssl rand -hex 16)
PROVISION_TOKEN=$(openssl rand -hex 32)
MODEL_REPO_TOKEN=$(openssl rand -hex 32)

# Coppie Ed25519 (JWT IAM + firma manifest ML) e X25519 (backup asimmetrico con privata fuori cluster)
read -r JWT_PRIV_B64 JWT_PUB_B64 MOD_PRIV_B64 MOD_PUB_B64 BK_PRIV_B64 BK_PUB_B64 < <("$PY_BIN" - <<'PY'
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from saas_app.backup_crypto import generate_backup_keypair

def ed_pair_b64():
    k = Ed25519PrivateKey.generate()
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(priv).decode("ascii"), base64.b64encode(pub).decode("ascii")

jwt_priv, jwt_pub = ed_pair_b64()
mod_priv, mod_pub = ed_pair_b64()
bk_priv, bk_pub = generate_backup_keypair()
print(jwt_priv, jwt_pub, mod_priv, mod_pub, bk_priv, bk_pub)
PY
)
JWT_PRIV_PEM=$(printf '%s' "$JWT_PRIV_B64" | base64 -d)
JWT_PUB_PEM=$(printf '%s' "$JWT_PUB_B64" | base64 -d)
MOD_PRIV_PEM=$(printf '%s' "$MOD_PRIV_B64" | base64 -d)
MOD_PUB_PEM=$(printf '%s' "$MOD_PUB_B64" | base64 -d)

# La chiave privata dei backup resta RIGOROSAMENTE FUORI DAL CLUSTER (V6)
BK_DIR="${HOME}/.config/scc-cluster"
mkdir -p "$BK_DIR"
chmod 700 "$BK_DIR"
printf '%s\n' "$BK_PRIV_B64" > "${BK_DIR}/backup_private.b64"
chmod 600 "${BK_DIR}/backup_private.b64"

for s in pg-backup-key saas-secrets redis-secrets migrator-secrets iam-secrets clinical-secrets manager-secrets ingest-secrets worker-secrets training-secrets pg-role-iam pg-role-clinical pg-role-manager pg-role-ingest pg-role-worker pg-role-trainer; do
  kubectl delete secret "$s" --ignore-not-found >/dev/null 2>&1 || true
done

create_pg_role_secret() {
  kubectl create secret generic "$1" --type=kubernetes.io/basic-auth \
    --from-literal=username="$2" --from-literal=password="$3" >/dev/null
  kubectl label secret "$1" cnpg.io/reload=true --overwrite >/dev/null
}
create_pg_role_secret pg-role-iam saas_iam "$IAM_PW"
create_pg_role_secret pg-role-clinical saas_clinical "$CLINICAL_PW"
create_pg_role_secret pg-role-manager saas_manager "$MANAGER_PW"
create_pg_role_secret pg-role-ingest saas_ingest "$INGEST_PW"
create_pg_role_secret pg-role-worker saas_worker "$WORKER_PW"
create_pg_role_secret pg-role-trainer saas_trainer "$TRAINER_PW"

kubectl create configmap pg-backup-public \
  --from-literal=BACKUP_PUBLIC_KEY_B64="$BK_PUB_B64" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl create configmap iam-jwt-public \
  --from-literal=JWT_PUBLIC_KEY_PEM="$JWT_PUB_PEM" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl create configmap model-signing-public \
  --from-literal=MODEL_SIGNING_PUBLIC_KEY_PEM="$MOD_PUB_PEM" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl create secret generic redis-secrets \
  --from-literal=REDIS_PASSWORD="$REDIS_PASS" \
  --from-literal=REDIS_REVOCATION_PASSWORD="$REDIS_REV_PASS" >/dev/null

PG_SSL_PARAMS="sslmode=verify-full&sslrootcert=/etc/pg-ca/ca.crt"

kubectl create secret generic migrator-secrets \
  --from-literal=POSTGRES_PASSWORD="$PG_PASS" \
  --from-literal=MIGRATOR_DATABASE_URL="postgresql+psycopg://saas_migrator:${PG_PASS}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=TRAINING_PSEUDO_SALT="$TRAIN_SALT" >/dev/null

kubectl create secret generic iam-secrets \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_iam:${IAM_PW}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=JWT_PRIVATE_KEY_PEM="$JWT_PRIV_PEM" \
  --from-literal=IAM_PROVISION_TOKEN="$PROVISION_TOKEN" \
  --from-literal=REDIS_URL="rediss://:${REDIS_PASS}@redis-service:6379/0" >/dev/null

kubectl create secret generic clinical-secrets \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_clinical:${CLINICAL_PW}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=IAM_PROVISION_TOKEN="$PROVISION_TOKEN" \
  --from-literal=REDIS_REVOCATION_URL="rediss://revocation_ro:${REDIS_REV_PASS}@redis-service:6379/0" >/dev/null

# manager-secrets NON possiede alcuna credenziale IAM (V1)
kubectl create secret generic manager-secrets \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_manager:${MANAGER_PW}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=REDIS_REVOCATION_URL="rediss://revocation_ro:${REDIS_REV_PASS}@redis-service:6379/0" >/dev/null

kubectl create secret generic ingest-secrets \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_ingest:${INGEST_PW}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=REDIS_URL="rediss://:${REDIS_PASS}@redis-service:6379/0" >/dev/null

kubectl create secret generic worker-secrets \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_worker:${WORKER_PW}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=REDIS_URL="rediss://:${REDIS_PASS}@redis-service:6379/0" >/dev/null

kubectl create secret generic training-secrets \
  --from-literal=DATABASE_URL="postgresql+psycopg://saas_trainer:${TRAINER_PW}@diabetes-pg-rw:5432/diabetes_saas?${PG_SSL_PARAMS}" \
  --from-literal=MODEL_REPO_UPLOAD_TOKEN="$MODEL_REPO_TOKEN" \
  --from-literal=MODEL_SIGNING_PRIVATE_KEY_PEM="$MOD_PRIV_PEM" >/dev/null

echo "[✓] Segreti per zona e chiavi Ed25519/X25519 creati (chiave backup privata salvata fuori cluster in ${BK_DIR}/backup_private.b64)."

echo "[+] Step 5: Installazione CloudNativePG Operator (v1.24.2), PostgreSQL HA (managed.roles) e NetworkPolicy..."
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.2.yaml
kubectl rollout status deployment/cnpg-controller-manager -n cnpg-system --timeout=180s

kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/postgres-ha.yaml
apply_tagged k8s/pg-backup-cronjob.yaml
echo "[+] Attesa che il cluster PostgreSQL HA 'diabetes-pg' (2 istanze + managed.roles) sia pronto..."
kubectl wait --for=condition=Ready cluster/diabetes-pg --timeout=600s
EXPECTED_ROLE_RV=$(kubectl get secret pg-role-trainer -o jsonpath='{.metadata.resourceVersion}')
kubectl annotate cluster diabetes-pg "scc.cluster/roles-sync=$(date -u +%Y%m%d%H%M%S)" --overwrite >/dev/null
for _i in $(seq 1 30); do
  CURRENT_ROLE_RV=$(kubectl get cluster diabetes-pg -o jsonpath='{.status.managedRolesStatus.passwordStatus.saas_trainer.resourceVersion}' 2>/dev/null || true)
  if [ "$CURRENT_ROLE_RV" = "$EXPECTED_ROLE_RV" ]; then
    break
  fi
  sleep 1
done
if [ "${CURRENT_ROLE_RV:-}" != "$EXPECTED_ROLE_RV" ]; then
  echo "[-] Timeout sincronizzazione managed.roles CNPG (atteso rv=${EXPECTED_ROLE_RV}, trovato=${CURRENT_ROLE_RV:-vuoto})." >&2
  exit 1
fi
echo "[✓] Tutti i 6 ruoli PostgreSQL riconciliati da CloudNativePG (rv=${EXPECTED_ROLE_RV})."

echo "[+] Step 6: Avvio Redis TLS, Model Repository TLS, firma baseline Ed25519 e migrazioni Alembic..."
kubectl apply -f k8s/redis.yaml
kubectl rollout restart deployment/diabetes-redis
kubectl rollout status deployment/diabetes-redis --timeout=120s

apply_tagged k8s/model-repo.yaml
kubectl rollout restart deployment/diabetes-model-repo
kubectl rollout status deployment/diabetes-model-repo --timeout=120s

# Firma il manifest baseline 1.0.0 con Ed25519 e lo pubblica sul Model Repository
kubectl port-forward svc/diabetes-model-repo 18002:443 >/dev/null 2>&1 &
REPO_PF_PID=$!
sleep 2
"$PY_BIN" - "$MODEL_REPO_TOKEN" "$MOD_PRIV_PEM" <<'PY'
import base64, json, ssl, sys, httpx
from pathlib import Path
from common.model_signing import sign_manifest_ed25519

token, priv_pem = sys.argv[1], sys.argv[2]
meta_path = Path("model/model_metadata.json")
meta = json.loads(meta_path.read_text(encoding="utf-8"))
meta["manifest_signature"] = sign_manifest_ed25519(meta, priv_pem)
payload = {
    "metadata_b64": base64.b64encode(json.dumps(meta, indent=2).encode("utf-8")).decode("ascii"),
    "onnx_b64": base64.b64encode(Path("model/model.onnx").read_bytes()).decode("ascii"),
    "data_b64": base64.b64encode(Path("model/model.onnx.data").read_bytes()).decode("ascii"),
}
r = httpx.put("https://127.0.0.1:18002/models/publish", json=payload, headers={"X-Repo-Token": token}, verify=False, timeout=15.0)
r.raise_for_status()
print("[✓] Baseline modello 1.0.0 firmata Ed25519 e pubblicata nel Model Repository:", r.json())
PY
kill "$REPO_PF_PID" >/dev/null 2>&1 || true

kubectl delete job diabetes-saas-seed --ignore-not-found >/dev/null 2>&1 || true
apply_tagged k8s/seed-job.yaml
kubectl wait --for=condition=complete job/diabetes-saas-seed --timeout=180s

echo "[+] Step 7: Deploy microservizi per zona (ML+HPA, IAM, Clinical, Manager, Ingest, Worker, Frontend, Trainer CronJob, Ingress)..."
apply_tagged k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ml-headless-service.yaml
kubectl apply -f k8s/pdb.yaml

apply_tagged k8s/iam-deployment.yaml
apply_tagged k8s/clinical-deployment.yaml
apply_tagged k8s/manager-deployment.yaml
apply_tagged k8s/ingest-deployment.yaml
apply_tagged k8s/ingestion-worker-deployment.yaml
kubectl apply -f k8s/worker-pdb.yaml

apply_tagged k8s/frontend-deployment.yaml
kubectl apply -f k8s/frontend-service.yaml
kubectl apply -f k8s/frontend-pdb.yaml

kubectl apply -f k8s/hpa.yaml
apply_tagged k8s/training-cronjob.yaml
kubectl apply -f k8s/ingress.yaml

for dep in diabetes-forecasting-api diabetes-iam-api diabetes-clinical-api diabetes-manager-api diabetes-ingest-api diabetes-ingestion-worker diabetes-frontend; do
  kubectl rollout restart "deployment/${dep}"
done

kubectl rollout status deployment/diabetes-forecasting-api --timeout=180s
kubectl rollout status deployment/diabetes-iam-api --timeout=120s
kubectl rollout status deployment/diabetes-clinical-api --timeout=120s
kubectl rollout status deployment/diabetes-manager-api --timeout=120s
kubectl rollout status deployment/diabetes-ingest-api --timeout=120s
kubectl rollout status deployment/diabetes-ingestion-worker --timeout=120s
kubectl rollout status deployment/diabetes-frontend --timeout=120s

echo "[✓] Deployment HA per zone con TLS end-to-end, EdDSA e HPA completato con successo!"
kubectl get nodes -o wide
kubectl get cluster diabetes-pg
kubectl get hpa
kubectl get pods -o wide
