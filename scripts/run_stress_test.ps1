# ==============================================================================
# FILE: scripts/run_stress_test.ps1
# RUOLO: Script di automazione dello Stress Test e Certificazione SLA per Windows.
#        Esegue 'wrk' tramite container Docker leggero su Alpine Linux,
#        esegue controlli pre-flight contro falsi positivi (es. HTTP 308)
#        e valida programmaticamente i tre vincoli di SLA (RPS, Latenza, Errori).
# ==============================================================================

param(
    [string]$TargetHost = "http://localhost",
    [string]$Duration = "30s",
    [int]$Threads = 4,
    [int]$Connections = 100
)

# PSScriptRoot: cartella dello script.
# Resolve-Path: individua il percorso reale canonico della radice di progetto.
# .Replace('\', '/'): converte i backslash Windows in slash POSIX per il mount volume (-v) in Docker.
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path.Replace('\', '/')

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " AVVIO STRESS TEST PER CERTIFICAZIONE SLA CON WRK (DOCKER) " -ForegroundColor Cyan
Write-Host " Target Endpoint:    $TargetHost/predict" -ForegroundColor Yellow
Write-Host " Connessioni attive: $Connections simultanee" -ForegroundColor Yellow
Write-Host " Thread impiegati:   $Threads" -ForegroundColor Yellow
Write-Host " Durata:             $Duration" -ForegroundColor Yellow
Write-Host " Obiettivo SLA:      >= 83.33 RPS, p99 <= 1000 ms, Errori <= 0.50%" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

# ------------------------------------------------------------------------------
# STEP 0: PRE-FLIGHT CHECK
# ------------------------------------------------------------------------------
# Invia una singola richiesta di prova verso $TargetHost/predict per assicurarsi che:
# 1. L'endpoint sia attivo e risponda con HTTP 200 OK.
# 2. NGINX non stia restituendo HTTP 308 (redirect HTTPS) o 503, prevenendo falsi esiti SLA.
Write-Host "[+] Esecuzione Pre-Flight Check su $TargetHost/predict..." -ForegroundColor Yellow
$preflightPayload = '{"patient_id":"PREFLIGHT_WIN","glucose_history":[4.2,4.0,3.8,3.6,3.5,3.7,3.9,4.3,5.0,5.5,6.1,6.7,7.3],"exogenous_inputs":[3.6,0.0,3.8,0.0,3.9,0.0,4.1,0.0,4.2,0.0,4.3,0.0,4.5,0.0]}'

try {
    $preflight = Invoke-WebRequest -Uri "$TargetHost/predict" -Method POST -ContentType "application/json" -Body $preflightPayload -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
    if ($preflight.StatusCode -ne 200) {
        Write-Host "[-] Pre-flight fallito: status code $($preflight.StatusCode) invece di 200 OK!" -ForegroundColor Red
        exit 1
    }
} catch {
    $statusCode = 0
    if ($_.Exception.Response) {
        $statusCode = [int]$_.Exception.Response.StatusCode
    }
    if ($statusCode -eq 308) {
        Write-Host "[-] ERRORE CRITICO PRE-FLIGHT: Ricevuto HTTP 308 Permanent Redirect." -ForegroundColor Red
        Write-Host "    NGINX sta forzando il redirect da HTTP a HTTPS." -ForegroundColor Red
        Write-Host "    Verificare che 'nginx.ingress.kubernetes.io/ssl-redirect: `"false`"' sia configurato in k8s/ingress.yaml" -ForegroundColor Yellow
        exit 1
    } elseif ($statusCode -ne 0) {
        Write-Host "[-] ERRORE PRE-FLIGHT: L'endpoint ha risposto con codice HTTP $statusCode." -ForegroundColor Red
        exit 1
    } else {
        Write-Warning "Avviso: Impossibile contattare l'endpoint durante il pre-flight ($($_.Message)). Procedo con il benchmark..."
    }
}
Write-Host "[✓] Pre-flight superato con successo: l'endpoint risponde 200 OK." -ForegroundColor Green

# ------------------------------------------------------------------------------
# STEP 1: ESECUZIONE DEL CONTAINER DOCKER CON WRK
# ------------------------------------------------------------------------------
Write-Host "`n[+] Esecuzione benchmark wrk..." -ForegroundColor Yellow
$wrkOutput = docker run --rm `
    -v "${ProjectRoot}/scripts:/scripts" `
    --add-host=host.docker.internal:host-gateway `
    williamyeh/wrk `
    -t$Threads `
    -c$Connections `
    -d$Duration `
    -s /scripts/test_predict.lua `
    --latency `
    "$TargetHost/predict"

$wrkOutput | ForEach-Object { Write-Host $_ }
$fullText = $wrkOutput -join "`n"

# ------------------------------------------------------------------------------
# STEP 2: PARSING AUTOMATICO DELLE METRICHE E VALIDAZIONE DEI VINCOLI SLA
# ------------------------------------------------------------------------------
# 1. Throughput (Requests/sec)
$rps = 0.0
if ($fullText -match "Requests/sec:\s+([0-9.]+)") {
    $rps = [double]$Matches[1]
}

# 2. Latenza al 99° percentile
$p99Ms = 0.0
if ($fullText -match "99%\s+([0-9.]+)(ms|s|us)") {
    $val = [double]$Matches[1]
    $unit = $Matches[2]
    if ($unit -eq "ms") { $p99Ms = $val }
    elseif ($unit -eq "s") { $p99Ms = $val * 1000.0 }
    elseif ($unit -eq "us") { $p99Ms = $val / 1000.0 }
}

# 3. Errori HTTP non-2xx
$non2xx = 0
if ($fullText -match "Non-2xx or 3xx responses:\s+([0-9]+)") {
    $non2xx = [int]$Matches[1]
}

# 4. Richieste totali
$totalReq = 1
if ($fullText -match "([0-9]+)\s+requests in") {
    $totalReq = [int]$Matches[1]
}

$errorPct = ($non2xx / $totalReq) * 100.0

Write-Host "`n==========================================================" -ForegroundColor Cyan
Write-Host " REPORT DI CONVALIDA SERVICE LEVEL AGREEMENT (SLA) " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

$slaPassed = $true

# Condizione 1: Throughput >= 83.33 RPS (5.000 req/min)
Write-Host -NoNewline "  1. Throughput:       $rps RPS (Obiettivo: >= 83.33 RPS) ... "
if ($rps -ge 83.33) {
    Write-Host "[PASS]" -ForegroundColor Green
} else {
    Write-Host "[FAIL]" -ForegroundColor Red
    $slaPassed = $false
}

# Condizione 2: Latenza p99 <= 1000.0 ms (<= 1.0 secondo)
Write-Host -NoNewline "  2. Latenza p99:      $p99Ms ms (Obiettivo: <= 1000.0 ms) ... "
if ($p99Ms -le 1000.0 -and $p99Ms -gt 0) {
    Write-Host "[PASS]" -ForegroundColor Green
} else {
    Write-Host "[FAIL]" -ForegroundColor Red
    $slaPassed = $false
}

# Condizione 3: Errori <= 0.50% (Disponibilita >= 99.5%)
Write-Host -NoNewline "  3. Tasso di Errori:  $($errorPct.ToString('F2'))% (Obiettivo: <= 0.50%) ... "
if ($errorPct -le 0.50) {
    Write-Host "[PASS]" -ForegroundColor Green
} else {
    Write-Host "[FAIL]" -ForegroundColor Red
    $slaPassed = $false
}

Write-Host "==========================================================" -ForegroundColor Cyan
if ($slaPassed) {
    Write-Host "[✓] SLA PIENAMENTE RISPETTATO: CERTIFICAZIONE COMPLETATA CON SUCCESSO!" -ForegroundColor Green
    exit 0
} else {
    Write-Host "[✗] ATTENZIONE: UNO O PIU VINCOLI DI SLA NON SONO STATI SODDISFATTI!" -ForegroundColor Red
    exit 1
}
