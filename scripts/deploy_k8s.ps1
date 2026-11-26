# ==============================================================================
# FILE: scripts/deploy_k8s.ps1
# RUOLO: Pipeline PowerShell allineata a scripts/deploy_k8s.sh (ML, SaaS HA,
#        CloudNativePG, Redis Queue/Worker, Alembic, NetworkPolicy, Ingress HTTPS).
# ==============================================================================

param(
    [string]$ImageTag = "1.0.0"
)

$ErrorActionPreference = "Stop"
$ClusterName = "scc-cluster"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

function Apply-TaggedManifest {
    param([string]$Path)
    (Get-Content -Raw $Path) -replace ":1\.0\.0", ":$ImageTag" | kubectl apply -f -
}

function New-RandomHex {
    param([int]$Bytes = 16)
    $buf = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    return ($buf | ForEach-Object { $_.ToString("x2") }) -join ""
}

Write-Host "Avvio deployment dell'infrastruttura Secure Cloud Computing (tag: $ImageTag)..." -ForegroundColor Cyan

foreach ($cmd in @("docker", "kubectl", "kind")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "Comando '$cmd' non trovato nel PATH. Eseguire prima: .\scripts\setup_host.ps1"
    }
}

Write-Host "[+] Step 1: Compilazione immagini Docker multi-stage non-root (tag $ImageTag)..." -ForegroundColor Yellow
docker build -t "diabetes-api:$ImageTag" -f docker/Dockerfile .
docker build -t "diabetes-saas-api:$ImageTag" -f docker/Dockerfile.saas .
docker build -t "diabetes-frontend:$ImageTag" -f docker/Dockerfile.frontend .

$existingClusters = kind get clusters 2>$null
if ($existingClusters -notcontains $ClusterName) {
    Write-Host "[+] Step 2: Creazione cluster KinD (1 Master + 2 Workers)..." -ForegroundColor Yellow
    kind create cluster --name $ClusterName --config k8s/kind-config.yaml
} else {
    Write-Host "[+] Il cluster KinD '$ClusterName' e' gia' attivo." -ForegroundColor Green
}

Write-Host "[+] Step 3: Caricamento delle immagini pinnate nei nodi KinD..." -ForegroundColor Yellow
kind load docker-image "diabetes-api:$ImageTag" --name $ClusterName
kind load docker-image "diabetes-saas-api:$ImageTag" --name $ClusterName
kind load docker-image "diabetes-frontend:$ImageTag" --name $ClusterName

Write-Host "[+] Step 4: Installazione NGINX Ingress Controller (vendorizzato, v1.15.1)..." -ForegroundColor Yellow
kubectl apply -f k8s/vendor/ingress-nginx-controller-v1.15.1.yaml
kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=180s

$patchJson = '{"data":{"allow-snippet-annotations":"false","server-tokens":"false"}}'
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p $patchJson
$snippets = kubectl get cm ingress-nginx-controller -n ingress-nginx -o jsonpath='{.data.allow-snippet-annotations}'
if ($snippets -ne "false") {
    Write-Error "allow-snippet-annotations atteso 'false', trovato '$snippets'. Deploy interrotto."
    exit 1
}
Write-Host "[ok] allow-snippet-annotations=false confermato sulla ConfigMap del controller." -ForegroundColor Green

$tlsSecretExists = kubectl get secret diabetes-tls-cert 2>$null
if (-not $tlsSecretExists) {
    Write-Host "[+] Generazione certificato TLS autofirmato per localhost con SAN..." -ForegroundColor Yellow
    $tempKey = [System.IO.Path]::GetTempFileName()
    $tempCrt = [System.IO.Path]::GetTempFileName()
    try {
        $opensslCmd = $null
        $found = Get-Command openssl -ErrorAction SilentlyContinue
        if ($found) { $opensslCmd = $found.Source }
        else {
            foreach ($cand in @("C:\Program Files\Git\usr\bin\openssl.exe", "C:\Program Files\OpenSSL-Win64\bin\openssl.exe")) {
                if (Test-Path $cand) { $opensslCmd = $cand; break }
            }
        }
        if (-not $opensslCmd) {
            Write-Error "OpenSSL non trovato. Installare Git for Windows o winget install ShiningLight.OpenSSL."
            exit 1
        }
        & $opensslCmd req -x509 -newkey rsa:2048 -nodes -days 365 `
            -keyout $tempKey -out $tempCrt `
            -subj "/CN=localhost/O=DiabetesEarlyWarning" `
            -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" `
            -addext "basicConstraints=CA:FALSE" `
            -addext "extendedKeyUsage=serverAuth" 2>$null
        kubectl create secret tls diabetes-tls-cert --cert=$tempCrt --key=$tempKey
    } finally {
        Remove-Item -Path $tempKey, $tempCrt -Force -ErrorAction SilentlyContinue
    }
}

$pgAuthExists = kubectl get secret saas-pg-auth 2>$null
if ($pgAuthExists) {
    $pgB64 = kubectl get secret saas-pg-auth -o jsonpath='{.data.password}'
    $pgPass = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($pgB64))
} else {
    $pgPass = New-RandomHex -Bytes 16
    kubectl create secret generic saas-pg-auth `
        --type=kubernetes.io/basic-auth `
        --from-literal=username=saas_migrator `
        --from-literal=password="$pgPass"
}

$appPass = New-RandomHex -Bytes 16
$redisPass = New-RandomHex -Bytes 16
$jwtKey = New-RandomHex -Bytes 32

kubectl delete secret saas-secrets --ignore-not-found | Out-Null
kubectl create secret generic saas-secrets `
    --from-literal=POSTGRES_USER=saas_migrator `
    --from-literal=POSTGRES_PASSWORD="$pgPass" `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_app:${appPass}@diabetes-pg-rw:5432/diabetes_saas?sslmode=require" `
    --from-literal=MIGRATOR_DATABASE_URL="postgresql+psycopg://saas_migrator:${pgPass}@diabetes-pg-rw:5432/diabetes_saas?sslmode=require" `
    --from-literal=SAAS_APP_PASSWORD="$appPass" `
    --from-literal=REDIS_PASSWORD="$redisPass" `
    --from-literal=REDIS_URL="redis://:${redisPass}@redis-service:6379/0" `
    --from-literal=JWT_SECRET_KEY="$jwtKey"

Write-Host "[+] Step 5: Installazione CloudNativePG Operator e PostgreSQL HA..." -ForegroundColor Yellow
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.2.yaml
kubectl rollout status deployment/cnpg-controller-manager -n cnpg-system --timeout=180s

kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/postgres-ha.yaml
kubectl apply -f k8s/pg-backup-cronjob.yaml
kubectl wait --for=condition=Ready cluster/diabetes-pg --timeout=300s

Write-Host "[+] Step 6: Avvio coda Redis e migrazioni Alembic..." -ForegroundColor Yellow
kubectl apply -f k8s/redis.yaml
kubectl rollout restart deployment/diabetes-redis
kubectl rollout status deployment/diabetes-redis --timeout=120s
kubectl delete job diabetes-saas-seed --ignore-not-found | Out-Null
Apply-TaggedManifest k8s/seed-job.yaml
kubectl wait --for=condition=complete job/diabetes-saas-seed --timeout=180s

Write-Host "[+] Step 7: Applicazione microservizi, Worker, PDB e Ingress HTTPS..." -ForegroundColor Yellow
Apply-TaggedManifest k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ml-headless-service.yaml
kubectl apply -f k8s/pdb.yaml
Apply-TaggedManifest k8s/saas-deployment.yaml
kubectl apply -f k8s/saas-service.yaml
kubectl apply -f k8s/saas-pdb.yaml
Apply-TaggedManifest k8s/ingestion-worker-deployment.yaml
kubectl apply -f k8s/worker-pdb.yaml
Apply-TaggedManifest k8s/frontend-deployment.yaml
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

Write-Host "`n[ok] Deployment HA completato! Portale HTTPS: https://localhost" -ForegroundColor Green
kubectl get nodes -o wide
kubectl get cluster diabetes-pg
kubectl get pods -o wide
