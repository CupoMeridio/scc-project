#!/usr/bin/env bash
# FILE: scripts/setup_host_macos.sh
# RUOLO NEL PROGETTO: Installazione e configurazione automatica dei prerequisiti host su macOS.
#
# STRUMENTI INSTALLATI:
# 1. Python 3: interprete di sistema e modulo venv per la gestione degli ambienti isolati.
# 2. KinD (Kubernetes in Docker): motore per creare ed eseguire cluster Kubernetes multi-nodo locali.
# 3. kubectl: interfaccia da riga di comando per interrogare e controllare il cluster Kubernetes.
# 4. wrk: generatore di carico HTTP multithread ad alte prestazioni per le prove di stress SLA.
# 5. jq: processore JSON da terminale impiegato per il parsing dei dati di telemetria.
# 6. Docker Desktop: motore container con hypervisor nativo e runtime containerd.

# ------------------------------------------------------------------------------
# DIRETTIVE DI SICUREZZA BASH (Strict Mode)
# ------------------------------------------------------------------------------
# - set -e: interrompe immediatamente l'esecuzione se un qualsiasi comando restituisce codice diverso da 0.
# - set -u: tratta qualsiasi tentativo di utilizzare una variabile non inizializzata come un errore bloccante.
# - set -o pipefail: assicura che una pipeline (cmd1 | cmd2) fallisca se un qualsiasi comando intermedio fallisce.
set -euo pipefail

# ------------------------------------------------------------------------------
# VERIFICA DEL SISTEMA OPERATIVO
# ------------------------------------------------------------------------------
# 'uname -s' restituisce il nome del kernel del sistema operativo.
# Nei sistemi operativi Apple (macOS), il kernel restituito è sempre "Darwin".
if [ "$(uname -s)" != "Darwin" ]; then
    echo "[-] Errore: questo script e destinato esclusivamente a sistemi operativi macOS."
    exit 1
fi

# ------------------------------------------------------------------------------
# VERIFICA DEL PACKAGE MANAGER (Homebrew)
# ------------------------------------------------------------------------------
# 'command -v brew': cerca l'eseguibile di Homebrew nel PATH di sistema.
# '> /dev/null 2>&1': ridireziona sia lo standard output (1) che lo standard error (2) a /dev/null,
# sopprimendo l'output a schermo e valutando esclusivamente il codice di uscita ($?).
if ! command -v brew >/dev/null 2>&1; then
    echo "[-] Homebrew non e installato sul sistema host."
    echo "    Per installare Homebrew, eseguire nel terminale:"
    echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi

echo "====================================================================="
echo " SCC Project - Provisioning dei Prerequisiti Host (macOS / Homebrew)  "
echo "====================================================================="

# ------------------------------------------------------------------------------
# STEP 1: INSTALLAZIONE FORMULAE CLI (Strumenti a riga di comando)
# ------------------------------------------------------------------------------
# In Homebrew, una 'formula' rappresenta un pacchetto compilato eseguibile da terminale:
# - python: fornisce l'interprete Python 3 e i moduli pip/venv.
# - kind: binario per il bootstrap di cluster Kubernetes simulati tramite container Docker.
# - kubectl: client ufficiale per l'invio di manifest YAML all'API Server di Kubernetes.
# - wrk: motore in C per benchmarking HTTP ad alta concorrenza con supporto a script Lua.
# - jq: utility efficiente per formattare e filtrare risposte JSON grezze da riga di comando.
echo "[+] Step 1: Installazione formule CLI (Python, KinD, kubectl, wrk, jq)..."
brew install python kind kubectl wrk jq

# ------------------------------------------------------------------------------
# STEP 2: INSTALLAZIONE CASKS (Applicazioni native macOS con interfaccia grafica)
# ------------------------------------------------------------------------------
# In Homebrew, un 'cask' rappresenta un'applicazione macOS completa (.app / .dmg).
# Docker Desktop richiede un cask perché include l'hypervisor nativo per macOS,
# l'interfaccia utente grafica e l'integrazione con il socket UNIX locale (/var/run/docker.sock).
#
# CONTROLLO DI IDEMPOTENZA:
# 'brew list --cask --versions docker': verifica se Docker Desktop è già registrato tra i cask installati.
# In questo modo, esecuzioni successive dello script non tenteranno una reinstallazione ridondante.
echo "[+] Step 2: Verifica e installazione di Docker Desktop..."
if ! brew list --cask --versions docker >/dev/null 2>&1; then
    echo "[+] Installazione di Docker Desktop tramite Homebrew Cask..."
    brew install --cask docker
else
    echo "[✓] Docker Desktop e gia installato sul sistema."
fi

echo ""
echo "====================================================================="
echo "[✓] Configurazione dei prerequisiti macOS completata con successo!"
echo "====================================================================="
echo "ISTRUZIONI DI AVVIO:"
echo "1. Avviare Docker Desktop tramite Spotlight oppure eseguendo da terminale:"
echo "       open -a Docker"
echo "2. Attendere che il motore Docker completi l'inizializzazione."
echo "3. Verificare che il demone sia operativo eseguendo:"
echo "       docker info"
echo "====================================================================="
