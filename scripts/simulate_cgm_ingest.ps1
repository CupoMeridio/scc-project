# ==============================================================================
# FILE: scripts/simulate_cgm_ingest.ps1
# RUOLO: Controparte Windows del target 'make simulate-cgm-ingest'.
#        Legge la chiave del dispositivo dal Secret Kubernetes 'device-keys'
#        (creato dal Job di seed, N1), apre il port-forward verso il servizio
#        SaaS e invia i campioni a /api/cgm/ingest.
#        L'endpoint e' solo interno (D2): non e' raggiungibile tramite l'Ingress.
# ==============================================================================

param(
    [string]$Patient = "591",
    [int]$Steps = 10,
    [double]$Delay = 0.3
)

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = if (Test-Path (Join-Path $ProjectRoot "venv/bin/python3")) {
    Join-Path $ProjectRoot "venv/bin/python3"
} elseif (Test-Path (Join-Path $ProjectRoot "venv/Scripts/python.exe")) {
    Join-Path $ProjectRoot "venv/Scripts/python.exe"
} else {
    "python"
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " INGESTIONE CGM VERSO IL CLUSTER (paziente $Patient) " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Lettura della chiave del dispositivo dal Secret 'device-keys' (una voce per paziente).
$keyB64 = kubectl get secret device-keys -o "jsonpath={.data.patient_$Patient}" 2>$null
if ([string]::IsNullOrWhiteSpace($keyB64)) {
    Write-Host "[-] Chiave del paziente $Patient assente nel Secret device-keys." -ForegroundColor Red
    Write-Host "    Esegui prima 'make seed-demo' (o il Job di seed) e verifica con: kubectl get secret device-keys" -ForegroundColor Yellow
    exit 1
}
$deviceKey = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($keyB64))

# 2. Port-forward verso il servizio SaaS (endpoint interno: 8080 -> 80).
Write-Host "[+] Apertura port-forward verso svc/diabetes-saas-service 8080:80..." -ForegroundColor Yellow
$portForward = Start-Process -FilePath "kubectl" `
    -ArgumentList "port-forward", "svc/diabetes-saas-service", "8080:80" `
    -PassThru -NoNewWindow
Start-Sleep -Seconds 2

try {
    # 3. Invio dei campioni: l'endpoint /predict viene rimappato su /api/cgm/ingest.
    & $Python (Join-Path $ProjectRoot "scripts/simulate_cgm_patient.py") `
        --ingest --endpoint "http://127.0.0.1:8080/predict" `
        --patient $Patient --steps $Steps --delay $Delay --device-key $deviceKey
    $exitCode = $LASTEXITCODE
} finally {
    if ($portForward -and -not $portForward.HasExited) {
        Stop-Process -Id $portForward.Id -Force
    }
}

exit $exitCode
