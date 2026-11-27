# ==============================================================================
# FILE: scripts/deploy_k8s.ps1
# RUOLO: Pipeline PowerShell allineata a scripts/deploy_k8s.sh (deploy HA per zone).
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

Write-Host "[+] Step 1: Compilazione delle 4 immagini Docker multi-stage non-root (tag $ImageTag)..." -ForegroundColor Yellow
docker build -t "diabetes-api:$ImageTag" -f docker/Dockerfile .
docker build -t "diabetes-saas-api:$ImageTag" -f docker/Dockerfile.saas .
docker build -t "diabetes-frontend:$ImageTag" -f docker/Dockerfile.frontend .
docker build -t "diabetes-trainer:$ImageTag" -f docker/Dockerfile.trainer .

$existingClusters = kind get clusters 2>$null
if ($existingClusters -contains $ClusterName) {
    Write-Warning "Il deploy e' distruttivo: il cluster '$ClusterName' esistente e tutti i suoi dati, PVC e Secret saranno eliminati."
    kind delete cluster --name $ClusterName
}
Write-Host "[+] Creazione cluster KinD pulito (1 Master + 2 Workers)..." -ForegroundColor Yellow
kind create cluster --name $ClusterName --config k8s/kind-config.yaml
$kindNodes = kind get nodes --name $ClusterName
if ($kindNodes) {
    docker update --restart=no $kindNodes | Out-Null
}

Write-Host "[+] Step 3: Caricamento immagini nei nodi KinD..." -ForegroundColor Yellow
kind load docker-image "diabetes-api:$ImageTag" --name $ClusterName
kind load docker-image "diabetes-saas-api:$ImageTag" --name $ClusterName
kind load docker-image "diabetes-frontend:$ImageTag" --name $ClusterName
kind load docker-image "diabetes-trainer:$ImageTag" --name $ClusterName

Write-Host "[+] Step 4: Verifica checksum vendor Ingress NGINX v1.15.1 + patch KinD (V7)..." -ForegroundColor Yellow
$expectedHash = (Get-Content -Raw "k8s/vendor/ingress-nginx-controller-v1.15.1.yaml.sha256").Trim().Split()[0].ToLower()
$actualHash = (Get-FileHash -Algorithm SHA256 "k8s/vendor/ingress-nginx-controller-v1.15.1.yaml").Hash.ToLower()
if ($actualHash -ne $expectedHash) {
    Write-Error "Checksum vendor Ingress NGINX non valido ($actualHash != $expectedHash)."
    exit 1
}
$patchedIngress = Join-Path ([System.IO.Path]::GetTempPath()) "ingress-nginx-patched.yaml"
if (Get-Command patch -ErrorAction SilentlyContinue) {
    cmd /c "patch -s -o `"$patchedIngress`" k8s\vendor\ingress-nginx-controller-v1.15.1.yaml < k8s\vendor\kind-ingress-ready.patch"
    kubectl apply -f $patchedIngress
    Remove-Item -Path $patchedIngress -Force -ErrorAction SilentlyContinue
} else {
    Write-Warning "Comando 'patch' assente su PATH Windows: uso k8s/vendor/ingress-nginx-controller-v1.15.1.yaml con patch manuale controller."
    kubectl apply -f k8s/vendor/ingress-nginx-controller-v1.15.1.yaml
}
kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=180s

$patchJson = '{"data":{"allow-snippet-annotations":"false","server-tokens":"false"}}'
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p $patchJson
$snippets = kubectl get cm ingress-nginx-controller -n ingress-nginx -o jsonpath='{.data.allow-snippet-annotations}'
if ($snippets -ne "false") {
    Write-Error "allow-snippet-annotations atteso 'false', trovato '$snippets'. Deploy interrotto."
    exit 1
}

kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' 2>$null | Out-Null

$opensslCmd = $null
$foundOpenSsl = Get-Command openssl -ErrorAction SilentlyContinue
if ($foundOpenSsl) { $opensslCmd = $foundOpenSsl.Source }
else {
    foreach ($cand in @("C:\Program Files\Git\usr\bin\openssl.exe", "C:\Program Files\OpenSSL-Win64\bin\openssl.exe")) {
        if (Test-Path $cand) { $opensslCmd = $cand; break }
    }
}
if (-not $opensslCmd) {
    Write-Error "OpenSSL non trovato. Installare Git for Windows o winget install ShiningLight.OpenSSL."
    exit 1
}

Write-Host "[+] Creazione nuovo certificato edge TLS..." -ForegroundColor Yellow
$tempKey = [System.IO.Path]::GetTempFileName()
$tempCrt = [System.IO.Path]::GetTempFileName()
try {
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

$tmpTlsDir = Join-Path ([System.IO.Path]::GetTempPath()) ("scc-tls-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmpTlsDir | Out-Null
try {
    $caKey = Join-Path $tmpTlsDir "ca.key"
    $caCrt = Join-Path $tmpTlsDir "ca.crt"
    & $opensslCmd req -x509 -newkey rsa:2048 -nodes -days 365 `
        -keyout $caKey -out $caCrt `
        -subj "/CN=scc-internal-ca/O=DiabetesInternal" `
        -addext "basicConstraints=critical,CA:TRUE" `
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>$null
    kubectl create configmap internal-ca "--from-file=ca.crt=$caCrt" --dry-run=client -o yaml | kubectl apply -f - | Out-Null

    function Issue-InternalCert {
        param([string]$SecretName, [string]$Cn, [string]$San)
        $srvKey = Join-Path $tmpTlsDir "$SecretName.key"
        $srvCsr = Join-Path $tmpTlsDir "$SecretName.csr"
        $srvCrt = Join-Path $tmpTlsDir "$SecretName.crt"
        $extFile = Join-Path $tmpTlsDir "$SecretName.ext"
        "subjectAltName=$San`nextendedKeyUsage=serverAuth`nbasicConstraints=CA:FALSE" | Set-Content -Path $extFile -Encoding ASCII
        & $opensslCmd req -newkey rsa:2048 -nodes -keyout $srvKey -out $srvCsr -subj "/CN=$Cn/O=DiabetesInternal" 2>$null
        & $opensslCmd x509 -req -in $srvCsr -CA $caCrt -CAkey $caKey -CAcreateserial -out $srvCrt -days 365 -extfile $extFile 2>$null
        kubectl delete secret $SecretName --ignore-not-found | Out-Null
        kubectl create secret tls $SecretName --cert=$srvCrt --key=$srvKey | Out-Null
    }
    Issue-InternalCert "iam-internal-tls" "diabetes-iam-service" "DNS:diabetes-iam-service,DNS:diabetes-iam-service.default.svc,DNS:localhost,IP:127.0.0.1"
    Issue-InternalCert "clinical-internal-tls" "diabetes-clinical-service" "DNS:diabetes-clinical-service,DNS:diabetes-clinical-service.default.svc,DNS:localhost,IP:127.0.0.1"
    Issue-InternalCert "manager-internal-tls" "diabetes-manager-service" "DNS:diabetes-manager-service,DNS:diabetes-manager-service.default.svc,DNS:localhost,IP:127.0.0.1"
    Issue-InternalCert "ingest-internal-tls" "diabetes-ingest-service" "DNS:diabetes-ingest-service,DNS:diabetes-ingest-service.default.svc,DNS:localhost,IP:127.0.0.1"
    Issue-InternalCert "ml-internal-tls" "diabetes-forecasting-service" "DNS:diabetes-forecasting-service,DNS:diabetes-forecasting-service.default.svc,DNS:diabetes-forecasting-headless,DNS:diabetes-forecasting-headless.default.svc,DNS:localhost,IP:127.0.0.1"
    Issue-InternalCert "model-repo-internal-tls" "diabetes-model-repo" "DNS:diabetes-model-repo,DNS:diabetes-model-repo.default.svc,DNS:localhost,IP:127.0.0.1"
    Issue-InternalCert "redis-internal-tls" "redis-service" "DNS:redis-service,DNS:redis-service.default.svc,DNS:localhost,IP:127.0.0.1"
} finally {
    Remove-Item -Path $tmpTlsDir -Recurse -Force -ErrorAction SilentlyContinue
}

$pgPass = New-RandomHex -Bytes 16
kubectl create secret generic saas-pg-auth `
    --type=kubernetes.io/basic-auth `
    --from-literal=username=saas_migrator `
    --from-literal=password="$pgPass"

$iamPw = New-RandomHex -Bytes 16
$clinicalPw = New-RandomHex -Bytes 16
$managerPw = New-RandomHex -Bytes 16
$ingestPw = New-RandomHex -Bytes 16
$workerPw = New-RandomHex -Bytes 16
$trainerPw = New-RandomHex -Bytes 16
$trainSalt = New-RandomHex -Bytes 16
$redisPass = New-RandomHex -Bytes 16
$redisRevPass = New-RandomHex -Bytes 16
$provisionToken = New-RandomHex -Bytes 32
$modelRepoToken = New-RandomHex -Bytes 32

$pyExe = if (Test-Path "venv/bin/python3") { "venv/bin/python3" } elseif (Test-Path "venv/Scripts/python.exe") { "venv/Scripts/python.exe" } else { "python" }
$keysJson = & $pyExe -c "import json; from cryptography.hazmat.primitives import serialization; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from saas_app.backup_crypto import generate_backup_keypair; ed=lambda: (lambda k: (k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(), k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()))(Ed25519PrivateKey.generate()); jp,ju=ed(); mp,mu=ed(); bp,bu=generate_backup_keypair(); print(json.dumps({'jp':jp,'ju':ju,'mp':mp,'mu':mu,'bp':bp,'bu':bu}))" | ConvertFrom-Json

$bkDir = Join-Path $env:USERPROFILE ".config/scc-cluster"
New-Item -ItemType Directory -Force -Path $bkDir | Out-Null
$keysJson.bp | Set-Content -Path (Join-Path $bkDir "backup_private.b64") -Encoding ASCII

foreach ($s in @("pg-backup-key", "saas-secrets", "redis-secrets", "migrator-secrets", "iam-secrets", "clinical-secrets", "manager-secrets", "ingest-secrets", "worker-secrets", "training-secrets", "pg-role-iam", "pg-role-clinical", "pg-role-manager", "pg-role-ingest", "pg-role-worker", "pg-role-trainer")) {
    kubectl delete secret $s --ignore-not-found | Out-Null
}
foreach ($pair in @(
    @("pg-role-iam", "saas_iam", $iamPw),
    @("pg-role-clinical", "saas_clinical", $clinicalPw),
    @("pg-role-manager", "saas_manager", $managerPw),
    @("pg-role-ingest", "saas_ingest", $ingestPw),
    @("pg-role-worker", "saas_worker", $workerPw),
    @("pg-role-trainer", "saas_trainer", $trainerPw)
)) {
    kubectl create secret generic $pair[0] --type=kubernetes.io/basic-auth --from-literal=username=$($pair[1]) --from-literal=password=$($pair[2]) | Out-Null
    kubectl label secret $pair[0] cnpg.io/reload=true --overwrite | Out-Null
}

kubectl create configmap pg-backup-public "--from-literal=BACKUP_PUBLIC_KEY_B64=$($keysJson.bu)" --dry-run=client -o yaml | kubectl apply -f - | Out-Null
kubectl create configmap iam-jwt-public "--from-literal=JWT_PUBLIC_KEY_PEM=$($keysJson.ju)" --dry-run=client -o yaml | kubectl apply -f - | Out-Null
kubectl create configmap model-signing-public "--from-literal=MODEL_SIGNING_PUBLIC_KEY_PEM=$($keysJson.mu)" --dry-run=client -o yaml | kubectl apply -f - | Out-Null

$pgSsl = "sslmode=verify-full&sslrootcert=/etc/pg-ca/ca.crt"
kubectl create secret generic redis-secrets --from-literal=REDIS_PASSWORD="$redisPass" --from-literal=REDIS_REVOCATION_PASSWORD="$redisRevPass" | Out-Null
kubectl create secret generic migrator-secrets `
    --from-literal=POSTGRES_PASSWORD="$pgPass" `
    --from-literal=MIGRATOR_DATABASE_URL="postgresql+psycopg://saas_migrator:${pgPass}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=TRAINING_PSEUDO_SALT="$trainSalt" | Out-Null

kubectl create secret generic iam-secrets `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_iam:${iamPw}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=JWT_PRIVATE_KEY_PEM="$($keysJson.jp)" `
    --from-literal=IAM_PROVISION_TOKEN="$provisionToken" `
    --from-literal=REDIS_URL="rediss://:${redisPass}@redis-service:6379/0" | Out-Null

kubectl create secret generic clinical-secrets `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_clinical:${clinicalPw}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=IAM_PROVISION_TOKEN="$provisionToken" `
    --from-literal=REDIS_REVOCATION_URL="rediss://revocation_ro:${redisRevPass}@redis-service:6379/0" | Out-Null

kubectl create secret generic manager-secrets `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_manager:${managerPw}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=REDIS_REVOCATION_URL="rediss://revocation_ro:${redisRevPass}@redis-service:6379/0" | Out-Null

kubectl create secret generic ingest-secrets `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_ingest:${ingestPw}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=REDIS_URL="rediss://:${redisPass}@redis-service:6379/0" | Out-Null

kubectl create secret generic worker-secrets `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_worker:${workerPw}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=REDIS_URL="rediss://:${redisPass}@redis-service:6379/0" | Out-Null

kubectl create secret generic training-secrets `
    --from-literal=DATABASE_URL="postgresql+psycopg://saas_trainer:${trainerPw}@diabetes-pg-rw:5432/diabetes_saas?${pgSsl}" `
    --from-literal=MODEL_REPO_UPLOAD_TOKEN="$modelRepoToken" `
    --from-literal=MODEL_SIGNING_PRIVATE_KEY_PEM="$($keysJson.mp)" | Out-Null

Write-Host "[+] Step 5: CloudNativePG Operator, PostgreSQL HA, NetworkPolicy..." -ForegroundColor Yellow
kubectl apply --server-side -f https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.24/releases/cnpg-1.24.2.yaml
kubectl rollout status deployment/cnpg-controller-manager -n cnpg-system --timeout=180s
kubectl apply -f k8s/networkpolicy.yaml
kubectl apply -f k8s/postgres-ha.yaml
Apply-TaggedManifest k8s/pg-backup-cronjob.yaml
kubectl wait --for=condition=Ready cluster/diabetes-pg --timeout=600s
$expectedRv = kubectl get secret pg-role-trainer -o jsonpath='{.metadata.resourceVersion}'
kubectl annotate cluster diabetes-pg ("scc.cluster/roles-sync=" + (Get-Date).ToUniversalTime().ToString("yyyyMMddHHmmss")) --overwrite | Out-Null
for ($i = 0; $i -lt 30; $i++) {
    $currentRv = kubectl get cluster diabetes-pg -o jsonpath='{.status.managedRolesStatus.passwordStatus.saas_trainer.resourceVersion}' 2>$null
    if ($currentRv -eq $expectedRv) { break }
    Start-Sleep -Seconds 1
}

Write-Host "[+] Step 6: Redis, Model Repository, Alembic (6 ruoli + viste)..." -ForegroundColor Yellow
kubectl apply -f k8s/redis.yaml
kubectl rollout restart deployment/diabetes-redis
kubectl rollout status deployment/diabetes-redis --timeout=120s
Apply-TaggedManifest k8s/model-repo.yaml
kubectl rollout restart deployment/diabetes-model-repo
kubectl rollout status deployment/diabetes-model-repo --timeout=120s
kubectl delete job diabetes-saas-seed --ignore-not-found | Out-Null
Apply-TaggedManifest k8s/seed-job.yaml
kubectl wait --for=condition=complete job/diabetes-saas-seed --timeout=180s

Write-Host "[+] Step 7: Deploy microservizi per zona..." -ForegroundColor Yellow
Apply-TaggedManifest k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ml-headless-service.yaml
kubectl apply -f k8s/pdb.yaml
kubectl apply -f k8s/hpa.yaml
Apply-TaggedManifest k8s/iam-deployment.yaml
Apply-TaggedManifest k8s/clinical-deployment.yaml
Apply-TaggedManifest k8s/manager-deployment.yaml
Apply-TaggedManifest k8s/ingest-deployment.yaml
Apply-TaggedManifest k8s/ingestion-worker-deployment.yaml
kubectl apply -f k8s/worker-pdb.yaml
Apply-TaggedManifest k8s/frontend-deployment.yaml
kubectl apply -f k8s/frontend-service.yaml
kubectl apply -f k8s/frontend-pdb.yaml
Apply-TaggedManifest k8s/training-cronjob.yaml
kubectl apply -f k8s/ingress.yaml

foreach ($dep in @("diabetes-forecasting-api", "diabetes-iam-api", "diabetes-clinical-api", "diabetes-manager-api", "diabetes-ingest-api", "diabetes-ingestion-worker", "diabetes-frontend")) {
    kubectl rollout restart "deployment/$dep"
}
foreach ($dep in @("diabetes-forecasting-api", "diabetes-iam-api", "diabetes-clinical-api", "diabetes-manager-api", "diabetes-ingest-api", "diabetes-ingestion-worker", "diabetes-frontend")) {
    kubectl rollout status "deployment/$dep" --timeout=180s
}

Write-Host "`n[ok] Deployment HA per zone completato! Portale HTTPS: https://localhost" -ForegroundColor Green
kubectl get hpa
kubectl get pods -o wide
