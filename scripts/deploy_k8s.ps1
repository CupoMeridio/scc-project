# FILE: scripts/deploy_k8s.ps1
# RUOLO NEL PROGETTO: Pipeline di rilascio automatizzata e idempotente per Windows (PowerShell).

# ------------------------------------------------------------------------------
# DIRETTIVA DI SICUREZZA POWERSHELL (Strict Error Handling)
# ------------------------------------------------------------------------------
# Impone a PowerShell di interrompere immediatamente l'esecuzione dello script
# qualora un cmdlet o un comando esterno generi un errore (equivalente di 'set -e' in Bash).
$ErrorActionPreference = "Stop"

$ClusterName = "scc-cluster"

# PSScriptRoot: variabile automatica che contiene il percorso della cartella in cui risiede questo script.
# Join-Path e Resolve-Path: costruiscono e risolvono in modo robusto il percorso canonico della radice di progetto.
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
# Set-Location: sposta la cartella di lavoro corrente (equivalente del comando 'cd').
Set-Location $ProjectRoot

Write-Host "Avvio deployment dell'infrastruttura su Windows (Docker Desktop + KinD)..." -ForegroundColor Cyan

# ------------------------------------------------------------------------------
# STEP 0: VERIFICA DEI TOOL PREREQUISITI
# ------------------------------------------------------------------------------
# Itera sui comandi fondamentali: docker, kubectl, kind.
# Get-Command con -ErrorAction SilentlyContinue verifica la presenza nel PATH senza sollevare eccezioni bloccanti.
foreach ($cmd in @("docker", "kubectl", "kind")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "Comando '$cmd' non trovato nel PATH. Eseguire prima: .\scripts\setup_host.ps1"
    }
}

# ------------------------------------------------------------------------------
# STEP 1: COMPILAZIONE DELL'IMMAGINE DOCKER (Passo C della traccia)
# ------------------------------------------------------------------------------
# docker build:
# - -t diabetes-api:latest: tag identificativo dell'immagine prodotta.
# - -f docker/Dockerfile: percorso del descrittore di compilazione multi-stage.
# - .: build context locale contenente il codice sorgente e gli artefatti.
Write-Host "[+] Step 1: Compilazione dell'immagine Docker (diabetes-api:latest)..." -ForegroundColor Yellow
docker build -t diabetes-api:latest -f docker/Dockerfile .

# ------------------------------------------------------------------------------
# STEP 2: CREAZIONE DEL CLUSTER KIND MULTI-NODO (Passo F della traccia)
# ------------------------------------------------------------------------------
# Interroga i cluster KinD attivi. L'operatore -notcontains controlla se 'scc-cluster' è presente.
$existingClusters = kind get clusters 2>$null
if ($existingClusters -notcontains $ClusterName) {
    Write-Host "[+] Step 2: Creazione cluster KinD (1 Master + 2 Workers)..." -ForegroundColor Yellow
    kind create cluster --name $ClusterName --config k8s/kind-config.yaml
} else {
    Write-Host "[+] Il cluster KinD '$ClusterName' e gia attivo." -ForegroundColor Green
}

# ------------------------------------------------------------------------------
# STEP 3: CARICAMENTO DELL'IMMAGINE LOCALE NEI NODI KIND
# ------------------------------------------------------------------------------
# I nodi KinD operano come container isolati con un proprio demone containerd.
# 'kind load docker-image' trasferisce i layer compilati localmente direttamente dentro i nodi,
# evitando la pubblicazione su un registry pubblico esterno.
Write-Host "[+] Step 3: Caricamento dell'immagine nei nodi KinD..." -ForegroundColor Yellow
kind load docker-image diabetes-api:latest --name $ClusterName

# ------------------------------------------------------------------------------
# STEP 4: INSTALLAZIONE DI NGINX INGRESS CONTROLLER
# ------------------------------------------------------------------------------
Write-Host "[+] Step 4: Installazione di NGINX Ingress Controller..." -ForegroundColor Yellow
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

# Attesa del completamento dell'avvio del pod controller prima di procedere con i manifest applicativi.
# Il carattere backtick (`) rappresenta il carattere di continuazione riga in PowerShell.
Write-Host "[+] Attesa che NGINX Ingress Controller sia operativo..." -ForegroundColor Yellow
kubectl wait --namespace ingress-nginx `
  --for=condition=ready pod `
  --selector=app.kubernetes.io/component=controller `
  --timeout=180s

# Abilitazione delle annotazioni snippet di sicurezza in NGINX Ingress (CVE-2021-25742 mitigation)
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p '{"data":{"allow-snippet-annotations":"true"}}' 2>$null

# ------------------------------------------------------------------------------
# STEP 5: APPLICAZIONE DEI MANIFEST APPLICATIVI (Passo H della traccia)
# ------------------------------------------------------------------------------
# k8s/deployment.yaml: definisce 3 repliche del pod, risorse garantite e sonde.
# k8s/service.yaml: definisce il bilanciatore di carico interno (ClusterIP).
# k8s/pdb.yaml: garantisce un minimo di 2 repliche costantemente disponibili (SLA 99.5%).
# k8s/ingress.yaml: stabilisce le regole di routing e perimetrazione di sicurezza di NGINX.
Write-Host "[+] Step 5: Applicazione di Deployment, Service, PDB e Ingress..." -ForegroundColor Yellow
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/pdb.yaml
kubectl apply -f k8s/ingress.yaml

# Idempotenza: riavvio controllato progressivo per caricare la nuova immagine compilata
Write-Host "[+] Forzatura riavvio controllato (Rollout Restart idempotente)..." -ForegroundColor Yellow
kubectl rollout restart deployment/diabetes-forecasting-api
kubectl rollout status deployment/diabetes-forecasting-api --timeout=120s

# ------------------------------------------------------------------------------
# STEP 6: VISUALIZZAZIONE DELLO STATO FINALE
# ------------------------------------------------------------------------------
Write-Host "`n[✓] Deployment completato con successo!" -ForegroundColor Green
Write-Host "`n--- Nodi del Cluster ---" -ForegroundColor Cyan
kubectl get nodes -o wide
Write-Host "`n--- Pod Attivi ---" -ForegroundColor Cyan
kubectl get pods -o wide

Write-Host "`nPer testare gli endpoint in PowerShell:" -ForegroundColor Yellow
Write-Host "  Invoke-RestMethod -Uri http://localhost/health"
Write-Host "  Invoke-RestMethod -Uri http://localhost/predict -Method POST -ContentType 'application/json' -Body '{\`"features\`": [[110,0,0],[112,0,0],[115,0,0],[120,0,0],[130,50,0],[150,0,0],[170,0,0],[165,0,0],[155,0,0],[145,0,0],[135,0,0],[130,0,0]]}'"
