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
Write-Host "[+] Step 4: Installazione di NGINX Ingress Controller (v1.12.0)..." -ForegroundColor Yellow
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.0/deploy/static/provider/kind/deploy.yaml

# Attesa del completamento dell'avvio del pod controller prima di procedere con i manifest applicativi.
# Il carattere backtick (`) rappresenta il carattere di continuazione riga in PowerShell.
Write-Host "[+] Attesa che NGINX Ingress Controller sia operativo..." -ForegroundColor Yellow
kubectl wait --namespace ingress-nginx `
  --for=condition=ready pod `
  --selector=app.kubernetes.io/component=controller `
  --timeout=180s

# AUTORIZZAZIONE ESPLICITA DEGLI SNIPPET NGINX (TRADE-OFF ARCHITETTURALE):
# A seguito della CVE-2021-25742, Ingress-NGINX disabilita di default 'server-snippet' e
# 'configuration-snippet' classificandoli 'Critical'. Nel nostro ambiente di collaudo monoutente,
# autorizziamo esplicitamente gli snippet nella ConfigMap (annotations-risk-level: Critical)
# per applicare il filtro sui metodi HTTP non ammessi (405) e gli header difensivi OWASP.
Write-Host "[+] Configurazione ConfigMap NGINX Ingress (autorizzazione snippet, annotations-risk-level: Critical)..." -ForegroundColor Yellow
$patchJson = '{"data":{"allow-snippet-annotations":"true","annotations-risk-level":"Critical"}}'
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type merge -p $patchJson

# ------------------------------------------------------------------------------
# STEP 5: CREAZIONE SEGRETO TLS E APPLICAZIONE MANIFEST APPLICATIVI (Passo H)
# ------------------------------------------------------------------------------
# MOTIVAZIONE DI SICUREZZA (HIPAA & GDPR Art. 32):
# I certificati e le chiavi private NON devono mai essere committati in chiaro nel repository git.
# Questo passaggio genera dinamicamente un certificato X.509 autofirmato valido per 'localhost'
# con Subject Alternative Name (SAN: DNS:localhost, IP:127.0.0.1) ed Extended Key Usage (serverAuth).
$tlsSecretExists = kubectl get secret diabetes-tls-cert 2>$null
if (-not $tlsSecretExists) {
    Write-Host "[+] Generazione certificato TLS autofirmato sicuro per localhost con SAN..." -ForegroundColor Yellow
    $tempKey = [System.IO.Path]::GetTempFileName()
    $tempCrt = [System.IO.Path]::GetTempFileName()
    try {
        # Ricerca robusta dell'eseguibile OpenSSL nel PATH o nelle cartelle applicative standard di Windows
        $opensslCmd = $null
        $found = Get-Command openssl -ErrorAction SilentlyContinue
        if ($found) {
            $opensslCmd = $found.Source
        } else {
            $candidatePaths = @(
                "C:\Program Files\Git\usr\bin\openssl.exe",
                "C:\cygwin64\bin\openssl.exe",
                "C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
                "C:\ProgramData\chocolatey\bin\openssl.exe"
            )
            foreach ($cand in $candidatePaths) {
                if (Test-Path $cand) {
                    $opensslCmd = $cand
                    break
                }
            }
        }

        if ($opensslCmd) {
            & $opensslCmd req -x509 -newkey rsa:2048 -nodes -days 365 `
                -keyout $tempKey -out $tempCrt `
                -subj "/CN=localhost/O=DiabetesEarlyWarning" `
                -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" `
                -addext "basicConstraints=CA:FALSE" `
                -addext "extendedKeyUsage=serverAuth" 2>$null
            kubectl create secret tls diabetes-tls-cert --cert=$tempCrt --key=$tempKey
            Write-Host "[✓] Secret TLS 'diabetes-tls-cert' creato con successo tramite OpenSSL ($opensslCmd)." -ForegroundColor Green
        } else {
            Write-Host "[-] ERRORE BLOCCANTE: Impossibile generare il Secret TLS 'diabetes-tls-cert'." -ForegroundColor Red
            Write-Host "    OpenSSL non e stato rilevato nel sistema. Per completare il deployment:" -ForegroundColor Yellow
            Write-Host "    1. Installare Git for Windows (include OpenSSL in 'C:\Program Files\Git\usr\bin'), oppure"
            Write-Host "    2. Eseguire: winget install ShiningLight.OpenSSL"
            exit 1
        }
    } finally {
        Remove-Item -Path $tempKey, $tempCrt -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[✓] Secret TLS 'diabetes-tls-cert' gia presente nel cluster." -ForegroundColor Green
}

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
Write-Host "  Invoke-RestMethod -Uri http://localhost/predict -Method POST -ContentType 'application/json' -Body '{\`"patient_id\`":\`"PATIENT_DEMO\`",\`"glucose_history\`":[4.2,4.0,3.8,3.6,3.5,3.7,3.9,4.3,5.0,5.5,6.1,6.7,7.3],\`"exogenous_inputs\`":[3.6,0.0,3.8,0.0,3.9,0.0,4.1,0.0,4.2,0.0,4.3,0.0,4.5,0.0]}'"
