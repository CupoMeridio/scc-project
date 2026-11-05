# FILE: scripts/setup_host.ps1
# RUOLO NEL PROGETTO: Setup automatico dei prerequisiti su ambienti Windows (PowerShell).
#
# Verifica e installa tramite winget:
# 1. KinD (Kubernetes in Docker)
# 2. Kubectl (CLI di controllo Kubernetes)
# 3. Verifica Docker Desktop (Docker Scout è già incluso in Docker Desktop)
# 4. Configura wrk per l'esecuzione trasparente via container Docker

$ErrorActionPreference = "Stop"

Write-Host "Verifica dell'ambiente Windows per Secure Cloud Computing..." -ForegroundColor Cyan

# 1. Verifica Docker Desktop
Write-Host "[+] Step 1: Controllo di Docker Desktop..." -ForegroundColor Yellow
if (Get-Command docker -ErrorAction SilentlyContinue) {
    try {
        docker info | Out-Null
        Write-Host "    [✓] Docker Desktop è installato e attivo." -ForegroundColor Green
    } catch {
        Write-Host "    [!] Docker è installato ma il demone non risponde. Verificare che Docker Desktop sia avviato." -ForegroundColor Red
    }
} else {
    Write-Host "    [-] Docker Desktop non trovato. Installare Docker Desktop da: https://www.docker.com/products/docker-desktop/" -ForegroundColor Red
}

# 2. Controllo / Installazione di kubectl
Write-Host "[+] Step 2: Controllo di kubectl..." -ForegroundColor Yellow
if (Get-Command kubectl -ErrorAction SilentlyContinue) {
    Write-Host "    [✓] kubectl è già installato." -ForegroundColor Green
} else {
    Write-Host "    [+] Installazione di kubectl tramite Windows Package Manager (winget)..." -ForegroundColor Yellow
    # PARAMETRI WINGET:
    # - --id Kubernetes.kubectl: seleziona l'identificativo univoco del pacchetto nel catalogo ufficiale.
    # - -e (--exact): impone la corrispondenza esatta dell'identificativo, evitando pacchetti simili.
    # - --accept-source-agreements / --accept-package-agreements: accettano automaticamente le licenze
    #   permettendo l'installazione non interattiva (silent mode) senza prompt di conferma.
    winget install --id Kubernetes.kubectl -e --accept-source-agreements --accept-package-agreements
}

# 3. Controllo / Installazione di kind
Write-Host "[+] Step 3: Controllo di KinD..." -ForegroundColor Yellow
if (Get-Command kind -ErrorAction SilentlyContinue) {
    Write-Host "    [✓] KinD è già installato." -ForegroundColor Green
} else {
    Write-Host "    [+] Installazione di KinD tramite Windows Package Manager (winget)..." -ForegroundColor Yellow
    winget install --id Kubernetes.kind -e --accept-source-agreements --accept-package-agreements
}

# 4. Verifica di Docker Scout (incluso nativamente in Docker Desktop)
Write-Host "[+] Step 4: Controllo del plugin Docker Scout..." -ForegroundColor Yellow
try {
    docker scout version | Out-Null
    Write-Host "    [✓] Docker Scout è disponibile e pronto all'uso." -ForegroundColor Green
} catch {
    Write-Host "    [!] Verificare l'abilitazione di Docker Scout nelle impostazioni di Docker Desktop." -ForegroundColor Yellow
}

# 5. Spiegazione wrk per Windows
Write-Host "[+] Step 5: Configurazione di wrk per lo stress test..." -ForegroundColor Yellow
Write-Host "    [✓] Su Windows, wrk verrà eseguito automaticamente tramite un micro-container Docker" -ForegroundColor Green
Write-Host "        senza necessità di compilazione locale del binario." -ForegroundColor Green

Write-Host "`n[✓] Setup completato. Riavviare la sessione PowerShell in caso di nuova installazione di kubectl o kind." -ForegroundColor Cyan
