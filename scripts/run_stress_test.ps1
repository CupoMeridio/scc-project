# ==============================================================================
# FILE: scripts/run_stress_test.ps1
# RUOLO: Script di automazione dello Stress Test e Certificazione SLA per Windows.
#        In modalita' 'internal' (default) esegue il pre-flight via port-forward
#        e lancia `wrk` in-cluster (pod con label app=diabetes-saas-api) contro
#        svc/diabetes-forecasting-service per bilanciare su tutte e 3 le repliche ML.
#        Salva l'output grezzo in docs/audit_reports/wrk_<data>.txt (Decisione D8).
# ==============================================================================

param(
    [string]$TargetHost = "internal",
    [string]$Duration = "30s",
    [int]$Threads = 4,
    [int]$Connections = 100
)

$ErrorActionPreference = "Stop"
$ProjectRootPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectRoot = $ProjectRootPath.Replace('\', '/')
$LuaScript = Join-Path $PSScriptRoot "test_predict.lua"
$ReportDir = Join-Path $ProjectRootPath "docs/audit_reports"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$ReportTs = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmssZ")
$ReportFile = Join-Path $ReportDir "wrk_$ReportTs.txt"

$portForward = $null
$inCluster = ($TargetHost -eq "internal")
$cmName = "wrk-lua-bench"
$podName = "wrk-bench"
$wrkUrl = if ($inCluster) { "https://diabetes-forecasting-service/predict" } else { "$TargetHost/predict" }
$preflightUrl = if ($inCluster) { "https://127.0.0.1:18000/predict" } else { "$TargetHost/predict" }

try {
    if ($inCluster) {
        Write-Host "[+] Pre-flight tramite port-forward temporaneo su svc/diabetes-forecasting-service (18000 -> 443 TLS)..." -ForegroundColor Yellow
        $portForward = Start-Process -FilePath "kubectl" `
            -ArgumentList "port-forward", "svc/diabetes-forecasting-service", "18000:443" `
            -PassThru -NoNewWindow
        Start-Sleep -Seconds 2
    }

    Write-Host "==========================================================" -ForegroundColor Cyan
    Write-Host " AVVIO STRESS TEST PER CERTIFICAZIONE SLA CON WRK " -ForegroundColor Cyan
    Write-Host " Target Endpoint:    $wrkUrl" -ForegroundColor Yellow
    Write-Host " Connessioni attive: $Connections simultanee" -ForegroundColor Yellow
    Write-Host " Thread impiegati:   $Threads" -ForegroundColor Yellow
    Write-Host " Durata:             $Duration" -ForegroundColor Yellow
    Write-Host " Obiettivo SLA:      >= 83.33 RPS, p99 <= 1000 ms, Errori <= 0.50%" -ForegroundColor Yellow
    Write-Host "==========================================================" -ForegroundColor Cyan

    Write-Host "[+] Esecuzione Pre-Flight Check su $preflightUrl..." -ForegroundColor Yellow
    $preflightPayload = '{"patient_id":"PREFLIGHT_WIN","glucose_history":[4.2,4.0,3.8,3.6,3.5,3.7,3.9,4.3,5.0,5.5,6.1,6.7,7.3],"exogenous_inputs":[3.6,0.0,3.8,0.0,3.9,0.0,4.1,0.0,4.2,0.0,4.3,0.0,4.5,0.0]}'
    try {
        $preflight = Invoke-WebRequest -Uri $preflightUrl -Method POST -ContentType "application/json" -Body $preflightPayload -MaximumRedirection 0 -UseBasicParsing -ErrorAction Stop
        if ($preflight.StatusCode -ne 200) {
            Write-Host "[-] Pre-flight fallito: status code $($preflight.StatusCode) invece di 200 OK!" -ForegroundColor Red
            exit 1
        }
    } catch {
        $statusCode = 0
        if ($_.Exception.Response) { $statusCode = [int]$_.Exception.Response.StatusCode }
        if ($statusCode -eq 308 -or $statusCode -eq 404) {
            Write-Host "[-] ERRORE PRE-FLIGHT (HTTP $statusCode): /predict non e' esposto sull'Ingress." -ForegroundColor Red
            Write-Host "    Usare il target interno predefinito: .\scripts\run_stress_test.ps1 -TargetHost internal" -ForegroundColor Yellow
            exit 1
        } else {
            Write-Host "[-] ERRORE PRE-FLIGHT ($statusCode): $($_.Exception.Message)" -ForegroundColor Red
            exit 1
        }
    } finally {
        if ($portForward -and -not $portForward.HasExited) {
            Stop-Process -Id $portForward.Id -Force
            $portForward = $null
        }
    }
    Write-Host "[✓] Pre-flight superato con successo: l'endpoint risponde 200 OK." -ForegroundColor Green

    $header = @(
        "=== wrk Stress Test Report (D8) ===",
        "Date (UTC): $((Get-Date).ToUniversalTime().ToString('o'))",
        "Command: wrk -t$Threads -c$Connections -d$Duration -s test_predict.lua --latency $wrkUrl",
        "--- ML Deployment & Pods ---"
    )
    $header += (kubectl get deployment diabetes-forecasting-api -o wide 2>&1)
    $header += (kubectl get pods -l app=diabetes-forecasting-api -o wide 2>&1)
    $header += "--- Raw wrk Output ---"
    $header | Set-Content -Path $ReportFile -Encoding UTF8

    Write-Host "`n[+] Esecuzione benchmark wrk..." -ForegroundColor Yellow
    if ($inCluster) {
        kubectl create configmap $cmName "--from-file=test_predict.lua=$LuaScript" --dry-run=client -o yaml | kubectl apply -f - | Out-Null
        kubectl delete pod $podName --ignore-not-found | Out-Null
        $overrides = "{`"spec`":{`"containers`":[{`"name`":`"$podName`",`"image`":`"williamyeh/wrk`",`"args`":[`"-t$Threads`",`"-c$Connections`",`"-d$Duration`",`"-s`",`"/scripts/test_predict.lua`",`"--latency`",`"$wrkUrl`"],`"volumeMounts`":[{`"name`":`"lua`",`"mountPath`":`"/scripts`"}]}],`"volumes`":[{`"name`":`"lua`",`"configMap`":{`"name`":`"$cmName`"}}]}}"
        $wrkOutput = kubectl run $podName --rm -i --restart=Never --image=williamyeh/wrk --labels="app=diabetes-clinical-api" "--overrides=$overrides"
    } else {
        $wrkOutput = docker run --rm `
            -v "${ProjectRoot}/scripts:/scripts" `
            --add-host=host.docker.internal:host-gateway `
            williamyeh/wrk `
            -t$Threads -c$Connections -d$Duration -s /scripts/test_predict.lua --latency $wrkUrl
    }

    $wrkOutput | ForEach-Object { Write-Host $_ }
    $wrkOutput | Add-Content -Path $ReportFile -Encoding UTF8
    Write-Host "[✓] Output grezzo wrk salvato in $ReportFile" -ForegroundColor Green

    $fullText = $wrkOutput -join "`n"
    $rps = 0.0
    if ($fullText -match "Requests/sec:\s+([0-9.]+)") { $rps = [double]$Matches[1] }

    $p99Ms = 0.0
    if ($fullText -match "(?m)^\s*99%\s+([0-9.]+)(ms|s|us)") {
        $val = [double]$Matches[1]
        $unit = $Matches[2]
        if ($unit -eq "ms") { $p99Ms = $val }
        elseif ($unit -eq "s") { $p99Ms = $val * 1000.0 }
        elseif ($unit -eq "us") { $p99Ms = $val / 1000.0 }
    }

    $non2xx = 0
    if ($fullText -match "Non-2xx or 3xx responses:\s+([0-9]+)") { $non2xx = [int]$Matches[1] }

    $totalReq = 1
    if ($fullText -match "([0-9]+)\s+requests in") { $totalReq = [int]$Matches[1] }

    $errorPct = ($non2xx / $totalReq) * 100.0

    Write-Host "`n==========================================================" -ForegroundColor Cyan
    Write-Host " REPORT DI CONVALIDA SERVICE LEVEL AGREEMENT (SLA) " -ForegroundColor Cyan
    Write-Host "==========================================================" -ForegroundColor Cyan

    $slaPassed = $true
    Write-Host -NoNewline "  1. Throughput:       $rps RPS (Obiettivo: >= 83.33 RPS) ... "
    if ($rps -ge 83.33) { Write-Host "[PASS]" -ForegroundColor Green } else { Write-Host "[FAIL]" -ForegroundColor Red; $slaPassed = $false }

    Write-Host -NoNewline "  2. Latenza p99:      $p99Ms ms (Obiettivo: <= 1000.0 ms) ... "
    if ($p99Ms -le 1000.0 -and $p99Ms -gt 0) { Write-Host "[PASS]" -ForegroundColor Green } else { Write-Host "[FAIL]" -ForegroundColor Red; $slaPassed = $false }

    Write-Host -NoNewline "  3. Tasso di Errori:  $($errorPct.ToString('F2'))% (Obiettivo: <= 0.50%) ... "
    if ($errorPct -le 0.50) { Write-Host "[PASS]" -ForegroundColor Green } else { Write-Host "[FAIL]" -ForegroundColor Red; $slaPassed = $false }

    Write-Host "==========================================================" -ForegroundColor Cyan
    if ($slaPassed) {
        Write-Host "[✓] SLA PIENAMENTE RISPETTATO!" -ForegroundColor Green
        exit 0
    } else {
        Write-Host "[✗] UNO O PIU VINCOLI DI SLA NON SODDISFATTI!" -ForegroundColor Red
        exit 1
    }
} finally {
    if ($portForward -and -not $portForward.HasExited) {
        Stop-Process -Id $portForward.Id -Force
    }
    if ($inCluster) {
        kubectl delete pod $podName --ignore-not-found | Out-Null
        kubectl delete configmap $cmName --ignore-not-found | Out-Null
    }
}
