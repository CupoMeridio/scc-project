<#
.SYNOPSIS
    Gestore intelligente degli ambienti virtuali Python per Windows (PowerShell).

.DESCRIPTION
    Consente di configurare e alternare istantaneamente 4 profili hardware diversi
    su macchine Windows con Docker Desktop o GPU dedicate:
      1) cpu-minimal : Solo runtime FastAPI, ONNX Runtime e test (zero PyTorch, ~100MB)
      2) cpu-train   : Standard di progetto (PyTorch CPU ~350MB, allena in 2s) [CONSIGLIATO]
      3) gpu-cuda    : PyTorch completo con supporto NVIDIA CUDA (~2.5GB)
      4) gpu-rocm    : rilevamento AMD; al momento installa PyTorch CPU su Windows
      5) auto        : Rilevamento automatico hardware GPU
#>

[CmdletBinding()]
param (
    [Parameter(Position = 0)]
    [ValidateSet("1", "2", "3", "4", "5", "cpu-minimal", "cpu-train", "gpu-cuda", "cuda", "gpu-rocm", "rocm", "auto", "")]
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvsDir = Join-Path $ProjectRoot ".venvs"
$VenvLink = Join-Path $ProjectRoot "venv"

# Ogni profilo ha un ambiente separato; il percorso stabile "venv" viene
# reindirizzato al profilo selezionato al termine dello script.
if (-not (Test-Path $VenvsDir)) {
    New-Item -ItemType Directory -Path $VenvsDir | Out-Null
}

function Detect-Hardware {
    # La GPU è rilevata tramite il nome esposto da Windows. La scelta del
    # profilo dipende poi dal backend configurato più sotto, non solo dalla GPU.
    Write-Host "[*] Scansione schede video in corso..." -ForegroundColor Cyan
    $gpus = Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name
    foreach ($gpu in $gpus) {
        if ($gpu -match "NVIDIA") {
            Write-Host "[+] Rilevata GPU NVIDIA: $gpu" -ForegroundColor Green
            return "gpu-cuda"
        }
        elseif ($gpu -match "AMD" -or $gpu -match "Radeon") {
            Write-Host "[+] Rilevata GPU AMD: $gpu" -ForegroundColor Green
            return "gpu-rocm"
        }
    }
    Write-Host "[!] Nessuna GPU dedicata rilevata. Fallback su CPU." -ForegroundColor Yellow
    return "cpu-train"
}

if ([string]::IsNullOrWhiteSpace($Profile)) {
    Write-Host "=====================================================================" -ForegroundColor Cyan
    Write-Host "  SCC Project - Gestore Ambienti Virtuali (Windows)                  " -ForegroundColor Cyan
    Write-Host "=====================================================================" -ForegroundColor Cyan
    Write-Host "Selezione profilo per l'ambiente:"
    Write-Host "  1) cpu-minimal : Solo runtime API e test (FastAPI + ONNX Runtime, ~100MB)"
    Write-Host "  2) cpu-train   : Standard progetto (PyTorch CPU ~350MB, allena in 2s) [CONSIGLIATO]"
    Write-Host "  3) gpu-cuda    : PyTorch completo con supporto NVIDIA CUDA (~2.5GB)"
    Write-Host "  4) gpu-rocm    : AMD rilevata; PyTorch CPU (ROCm non abilitato su Windows)"
    Write-Host "  5) auto        : Rilevamento automatico hardware GPU"
    Write-Host "=====================================================================" -ForegroundColor Cyan
    # Premere Invio senza scelta equivale al profilo cpu-train.
    $inputChoice = Read-Host "Opzione [1-5] (default: 2)"
    if ([string]::IsNullOrWhiteSpace($inputChoice)) { $inputChoice = "2" }
    $Profile = $inputChoice
}

switch ($Profile) {
    # Accetta sia i numeri del menu sia i nomi dei profili per l'uso da script.
    { $_ -in "1", "cpu-minimal" } { $SelectedProfile = "cpu-minimal" }
    { $_ -in "2", "cpu-train" }   { $SelectedProfile = "cpu-train" }
    { $_ -in "3", "gpu-cuda", "cuda" } { $SelectedProfile = "gpu-cuda" }
    { $_ -in "4", "gpu-rocm", "rocm" } { $SelectedProfile = "gpu-rocm" }
    { $_ -in "5", "auto" } { $SelectedProfile = Detect-Hardware }
    default { $SelectedProfile = "cpu-train" }
}

$TargetVenv = Join-Path $VenvsDir $SelectedProfile
$TargetPython = Join-Path $TargetVenv "Scripts\python.exe"
$TargetPip = Join-Path $TargetVenv "Scripts\pip.exe"

Write-Host "`n[*] Profilo selezionato: [ $SelectedProfile ]" -ForegroundColor Green
Write-Host "[*] Cartella ambiente  : $TargetVenv" -ForegroundColor Gray

if (-not (Test-Path $TargetPython)) {
    # Crea e popola l'ambiente solo se non esiste già. Gli ambienti presenti
    # vengono riutilizzati senza aggiornare automaticamente i pacchetti.
    Write-Host "[+] Creazione nuovo virtual environment in $TargetVenv..." -ForegroundColor Cyan
    python -m venv $TargetVenv

    Write-Host "[+] Aggiornamento pip..." -ForegroundColor Cyan
    & $TargetPip install --upgrade pip setuptools wheel

    Write-Host "[+] Installazione dipendenze base..." -ForegroundColor Cyan
    & $TargetPip install -r (Join-Path $ProjectRoot "requirements.txt") -r (Join-Path $ProjectRoot "requirements-test.txt")

    switch ($SelectedProfile) {
        "cpu-minimal" {
            # Il fallback ONNX minimale richiede il pacchetto onnx per costruire
            # e salvare il grafo quando PyTorch non è installato.
            & $TargetPip install onnx
        }
        "cpu-train" {
            Write-Host "[+] Installazione PyTorch CPU..." -ForegroundColor Cyan
            & $TargetPip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
            & $TargetPip install -r (Join-Path $ProjectRoot "requirements-train.txt")
        }
        "gpu-cuda" {
            # Usa le wheel PyTorch compilate per CUDA 12.4, invece delle wheel
            # CPU, per poter usare una GPU NVIDIA compatibile.
            Write-Host "[+] Installazione PyTorch CUDA 12.4..." -ForegroundColor Cyan
            & $TargetPip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
            & $TargetPip install -r (Join-Path $ProjectRoot "requirements-train.txt")
        }
        "gpu-rocm" {
            # Questo profilo installa attualmente PyTorch CPU: il nome identifica
            # l'hardware AMD, ma non abilita accelerazione ROCm su Windows.
            Write-Host "[+] Installazione dipendenze AMD/CPU..." -ForegroundColor Cyan
            & $TargetPip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
            & $TargetPip install -r (Join-Path $ProjectRoot "requirements-train.txt")
        }
    }
} else {
    Write-Host "[✓] L'ambiente '$SelectedProfile' esiste gia su disco." -ForegroundColor Green
}

# -----------------------------------------------------------------------------
# CREAZIONE DEL DIRECTORY JUNCTION UNIFICATO 'venv'
# -----------------------------------------------------------------------------
# COS'È UN JUNCTION NTFS:
# Un Directory Junction è una funzionalità nativa del filesystem NTFS (reparse point)
# che crea un puntatore trasparente da 'venv' verso la cartella reale del profilo in '.venvs/<profilo>'.
# A differenza dei symlink convenzionali di Windows, i Junction non richiedono privilegi
# amministrativi elevati (Developer Mode o Run as Administrator), garantendo massima portabilità.
if (Test-Path $VenvLink) {
    cmd /c rmdir "$VenvLink" 2>$null
    if (Test-Path $VenvLink) { Remove-Item -Recurse -Force $VenvLink }
}

New-Item -ItemType Junction -Path $VenvLink -Target $TargetVenv | Out-Null
Write-Host "[✓] Collegamento 'venv' aggiornato verso: .venvs\$SelectedProfile" -ForegroundColor Green
Write-Host "`nPer attivare l'ambiente nella sessione PowerShell corrente, eseguire:"
Write-Host "    .\venv\Scripts\Activate.ps1" -ForegroundColor Yellow
