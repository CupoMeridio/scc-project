# FILE: scripts/run_stress_test.ps1
# RUOLO NEL PROGETTO: Stress test e validazione dei vincoli SLA con wrk per Windows (PowerShell).
#
# Esegue il benchmark 'wrk' all'interno di un container Docker ufficiale leggero, evitando
# la necessità di compilare o installare binari nativi C su macchine Windows.
#
# OBIETTIVI SLA VERIFICATI:
# - Throughput: >= 83.33 RPS (equivalente al target di 5.000 richieste al minuto).
# - Latenza al 99° percentile (p99): <= 1.00 secondo (1000 ms).
# - Tasso di errore (non-2xx): < 0.5% (equivalente alla disponibilità richiesta del 99.5%).

# ------------------------------------------------------------------------------
# DICHIARAZIONE DEI PARAMETRI FORMALI DELLO SCRIPT
# ------------------------------------------------------------------------------
# param(): costrutto nativo di PowerShell per definire argomenti con tipizzazione statica e valori di default.
param(
    # Endpoint bersaglio. 'http://host.docker.internal' è il nome DNS speciale usato da Docker
    # per consentire al container di raggiungere i servizi in ascolto su localhost dell'host Windows.
    [string]$TargetHost = "http://host.docker.internal",
    
    # Durata complessiva del test di carico (default 30 secondi).
    [string]$Duration = "30s",
    
    # Numero di thread CPU allocati per generare le connessioni.
    [int]$Threads = 4,
    
    # Numero di connessioni TCP aperte contemporaneamente verso il gateway NGINX.
    [int]$Connections = 100
)

# Interruzione immediata in caso di errori imprevisti
$ErrorActionPreference = "Stop"

# PSScriptRoot: cartella dello script.
# Resolve-Path: individua il percorso reale canonico della radice di progetto.
# .Replace('\', '/'): converte i backslash Windows in slash POSIX, obbligatori per il montaggio volumi (-v) in Docker.
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.Replace('\', '/')

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " AVVIO STRESS TEST PER CERTIFICAZIONE SLA CON WRK (DOCKER) " -ForegroundColor Cyan
Write-Host " Target Endpoint:    $TargetHost/predict" -ForegroundColor Yellow
Write-Host " Connessioni attive: $Connections simultanee" -ForegroundColor Yellow
Write-Host " Thread impiegati:   $Threads" -ForegroundColor Yellow
Write-Host " Durata:             $Duration" -ForegroundColor Yellow
Write-Host " Obiettivo SLA:      >= 83.33 RPS, p99 <= 1000 ms, Errori < 0.5%" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

# ------------------------------------------------------------------------------
# ESECUZIONE DEL CONTAINER DOCKER CON WRK
# ------------------------------------------------------------------------------
# docker run:
# - --rm: elimina automaticamente il container dallo stato di Docker al termine dell'esecuzione.
# - -v "${ProjectRoot}/scripts:/scripts": monta la cartella locale degli script all'interno del container
#   nel percorso '/scripts', rendendo accessibile lo script Lua di generazione payload (test_predict.lua).
# - --add-host=host.docker.internal:host-gateway: associa il DNS host.docker.internal al gateway di rete dell'host.
# - williamyeh/wrk: immagine minimale basata su Alpine Linux con il binario 'wrk' precompilato.
#
# PARAMETRI WRK:
# - -t: numero di thread worker di generazione traffico.
# - -c: numero di socket TCP aperti contemporaneamente.
# - -d: intervallo di durata temporale.
# - -s: script Lua contenente il generatore di matrici cliniche [12 x 3] ad ogni iterazione.
# - --latency: ordina a wrk di calcolare ed esporre i percentili di risposta (50%, 75%, 90%, 99%).
docker run --rm `
    -v "${ProjectRoot}/scripts:/scripts" `
    --add-host=host.docker.internal:host-gateway `
    williamyeh/wrk `
    -t$Threads `
    -c$Connections `
    -d$Duration `
    -s /scripts/test_predict.lua `
    --latency `
    "$TargetHost/predict"

Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host "[✓] Stress test completato." -ForegroundColor Green
Write-Host "Verificare che i parametri di output soddisfino i vincoli SLA:" -ForegroundColor Yellow
Write-Host "  1. Throughput (Requests/sec) >= 83.33"
Write-Host "  2. Latenza al 99° percentile (p99) <= 1000 ms (1.00 s)"
Write-Host "  3. Errori non-2xx assenti o < 0.5% del totale"
Write-Host "==========================================================" -ForegroundColor Cyan
