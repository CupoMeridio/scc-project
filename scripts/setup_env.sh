#!/usr/bin/env bash
# =============================================================================
# FILE: scripts/setup_env.sh
# RUOLO NEL PROGETTO: Gestore intelligente degli ambienti virtuali Python (VENV).
#
# Consente di configurare e alternare profili hardware diversi
# per supportare ambienti eterogenei (CPU su Linux/macOS, NVIDIA CUDA e AMD ROCm su Linux):
#   1) cpu-minimal : Solo runtime FastAPI, ONNX Runtime e test (zero PyTorch, ~100MB)
#   2) cpu-train   : Standard di progetto (PyTorch CPU)
#   3) gpu-cuda    : PyTorch con accelerazione NVIDIA CUDA 13.0
#   4) gpu-rocm    : PyTorch con accelerazione AMD ROCm 7.1
#   5) auto        : Rilevamento automatico della GPU tramite lspci / device nodes
# =============================================================================

# set -e: interrompe immediatamente lo script se un comando fallisce
# set -u: tratta variabili non definite come errori
# set -o pipefail: restituisce il codice di errore dell'ultimo comando fallito in una pipeline
set -euo pipefail

HOST_OS="$(uname -s)"
PYTHON_BIN="${PYTHON_BIN:-python3.14}"

# Individua la cartella radice del progetto risalendo di un livello rispetto a scripts/
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENVS_DIR="${PROJECT_ROOT}/.venvs"
SYMLINK_VENV="${PROJECT_ROOT}/venv"

# Crea la directory nascosta .venvs se non esiste ancora
mkdir -p "${VENVS_DIR}"

# -----------------------------------------------------------------------------
# FUNZIONE DI RILEVAMENTO AUTOMATICO HARDWARE (AUTO-DETECT)
# -----------------------------------------------------------------------------
detect_hardware() {
    if [ "${HOST_OS}" = "Darwin" ]; then
        echo "cpu-train"
        return
    fi

    # Controllo 1: Presenza di GPU NVIDIA (scheda video o 3D controller dedicato)
    if command -v lspci >/dev/null 2>&1 && lspci | grep -E "VGA compatible controller|3D controller|Display controller" | grep -i "nvidia" >/dev/null 2>&1; then
        echo "gpu-cuda"
        return
    elif [ -e /dev/nvidia0 ] || [ -e /dev/nvidiactl ]; then
        echo "gpu-cuda"
        return
    fi

    # Controllo 2: Presenza di GPU AMD Radeon (scheda video o 3D controller dedicato)
    if command -v lspci >/dev/null 2>&1 && lspci | grep -E "VGA compatible controller|3D controller|Display controller" | grep -i -E "amd|radeon|ati" >/dev/null 2>&1; then
        echo "gpu-rocm"
        return
    elif [ -e /dev/kfd ]; then
        echo "gpu-rocm"
        return
    fi

    # Fallback: Nessuna GPU dedicata con driver attivi -> Profilo CPU Train
    echo "cpu-train"
}

# -----------------------------------------------------------------------------
# DETERMINAZIONE DEL PROFILO SELEZIONATO
# -----------------------------------------------------------------------------
CHOICE="${1:-}"

if [ -z "${CHOICE}" ]; then
    echo "====================================================================="
    echo "  SCC Project - Gestore Ambienti Virtuali (${HOST_OS})"
    echo "====================================================================="
    if [ "${HOST_OS}" = "Darwin" ]; then
        echo "Su macOS sono disponibili i profili CPU. Selezione profilo:"
    else
        echo "Supporto hardware eterogeneo (CPU, NVIDIA CUDA, AMD ROCm). Selezione profilo:"
    fi
    echo ""
    echo "  1) cpu-minimal : Solo runtime API e test (FastAPI + ONNX Runtime, ~100MB)"
    echo "  2) cpu-train   : Standard progetto (PyTorch CPU, download variabile) [CONSIGLIATO]"
    if [ "${HOST_OS}" != "Darwin" ]; then
        echo "  3) gpu-cuda    : PyTorch con supporto NVIDIA CUDA 13.0 (download grande)"
        echo "  4) gpu-rocm    : PyTorch con supporto AMD ROCm 7.1 (download grande)"
    fi
    echo "  5) auto        : Rilevamento automatico dell'hardware presente"
    echo "====================================================================="
    read -rp "Opzione [1-5] (default: 2): " USER_INPUT
    CHOICE="${USER_INPUT:-2}"
fi

case "${CHOICE}" in
    1|cpu-minimal)
        PROFILE="cpu-minimal"
        ;;
    2|cpu-train)
        PROFILE="cpu-train"
        ;;
    3|gpu-cuda|cuda)
        PROFILE="gpu-cuda"
        ;;
    4|gpu-rocm|rocm)
        PROFILE="gpu-rocm"
        ;;
    5|auto)
        PROFILE=$(detect_hardware | tail -n 1)
        ;;
    *)
        echo "[-] Opzione non valida: ${CHOICE}"
        if [ "${HOST_OS}" = "Darwin" ]; then
            echo "    Profili validi su macOS: cpu-minimal, cpu-train, auto"
        else
            echo "    Profili validi: cpu-minimal, cpu-train, gpu-cuda, gpu-rocm, auto"
        fi
        exit 1
        ;;
esac

if [ "${HOST_OS}" = "Darwin" ] && { [ "${PROFILE}" = "gpu-cuda" ] || [ "${PROFILE}" = "gpu-rocm" ]; }; then
    echo "[-] I profili CUDA e ROCm non sono supportati su macOS. Selezionare cpu-minimal, cpu-train o auto."
    exit 1
fi

TARGET_VENV="${VENVS_DIR}/${PROFILE}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "[-] Python 3.14 non trovato (comando: ${PYTHON_BIN})."
    echo "    Installare Python 3.14 con supporto venv o impostare PYTHON_BIN al suo percorso."
    exit 1
fi

PYTHON_VERSION="$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "${PYTHON_VERSION}" != "3.14" ]; then
    echo "[-] È richiesto Python 3.14; trovato Python ${PYTHON_VERSION}."
    exit 1
fi

echo ""
echo "====================================================================="
echo "[*] Profilo selezionato: [ ${PROFILE} ]"
echo "[*] Percorso ambiente  : ${TARGET_VENV}"
echo "====================================================================="

# -----------------------------------------------------------------------------
# CONTROLLO REQUISITI DI BASE: python3-venv e ensurepip
# -----------------------------------------------------------------------------
if ! "${PYTHON_BIN}" -c "import ensurepip" >/dev/null 2>&1; then
    echo "[-] Errore: il modulo 'ensurepip' di Python non e ancora installato sul sistema host."
    if [ "${HOST_OS}" = "Darwin" ]; then
        echo "    Installare Python 3 con Homebrew (brew install python) e riprovare."
    else
        echo "    Su Debian/Ubuntu la gestione dei venv richiede il pacchetto 'python3-venv'."
        echo "    Eseguire prima il comando di setup dell'host (richiede sudo una sola volta):"
        echo "        make setup"
        echo "    (oppure manualmente: sudo apt update && sudo apt install -y python3-venv python3-pip)"
    fi
    exit 1
fi

# -----------------------------------------------------------------------------
# CREAZIONE / AGGIORNAMENTO DELL'AMBIENTE VIRTUALE
# -----------------------------------------------------------------------------
if [ -d "${TARGET_VENV}" ]; then
    VENV_VERSION="unknown"
    if [ -x "${TARGET_VENV}/bin/python3" ]; then
        VENV_VERSION="$("${TARGET_VENV}/bin/python3" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    fi
    if [ "${VENV_VERSION}" != "3.14" ]; then
        echo "[*] Ricreo il profilo '${PROFILE}' (Python ${VENV_VERSION}, richiesto 3.14)..."
        rm -rf -- "${TARGET_VENV}"
    fi
fi

if [ -d "${TARGET_VENV}" ] && [ -f "${TARGET_VENV}/bin/python3" ] && [ -f "${TARGET_VENV}/bin/pip" ]; then
    echo "[✓] L'ambiente '${PROFILE}' esiste gia ed e completo su Python 3.14."
    echo "[*] Cambio istantaneo del puntatore 'venv' attivo..."
else
    echo "[+] Creazione nuovo ambiente virtuale in ${TARGET_VENV}..."
    "${PYTHON_BIN}" -m venv "${TARGET_VENV}"
    
    echo "[+] Aggiornamento pip, setuptools e wheel..."
    "${TARGET_VENV}/bin/pip" install --upgrade pip setuptools wheel
    
    echo "[+] Installazione dipendenze base del microservizio, backend SaaS e suite di test..."
    "${TARGET_VENV}/bin/pip" install --require-hashes \
        -r "${PROJECT_ROOT}/requirements.lock" \
        -r "${PROJECT_ROOT}/requirements-saas.lock" \
        -r "${PROJECT_ROOT}/requirements-test.lock"

    case "${PROFILE}" in
        cpu-minimal)
            echo "[+] Profilo 'cpu-minimal': runtime API e test senza PyTorch."
            ;;

        cpu-train)
            echo "[+] Profilo 'cpu-train': installazione PyTorch..."
            if [ "${HOST_OS}" = "Darwin" ]; then
                "${TARGET_VENV}/bin/pip" install "torch==2.15.0"
            else
                "${TARGET_VENV}/bin/pip" install \
                    "torch==2.15.0" \
                    --index-url https://download.pytorch.org/whl/cpu
            fi
            echo "[+] Installazione dipendenze scientifiche per addestramento offline..."
            "${TARGET_VENV}/bin/pip" install --require-hashes -r "${PROJECT_ROOT}/requirements-train.lock"
            ;;

        gpu-cuda)
            echo "[+] Profilo 'gpu-cuda': installazione PyTorch con supporto NVIDIA CUDA 13.0..."
            "${TARGET_VENV}/bin/pip" install \
                "torch==2.14.1" \
                --index-url https://download.pytorch.org/whl/cu130
            echo "[+] Installazione dipendenze scientifiche per addestramento offline..."
            "${TARGET_VENV}/bin/pip" install --require-hashes -r "${PROJECT_ROOT}/requirements-train.lock"
            ;;

        gpu-rocm)
            echo "[+] Profilo 'gpu-rocm': installazione PyTorch con supporto AMD ROCm 7.1..."
            "${TARGET_VENV}/bin/pip" install \
                "torch==2.13.0" \
                --index-url https://download.pytorch.org/whl/rocm7.1
            echo "[+] Installazione dipendenze scientifiche per addestramento offline..."
            "${TARGET_VENV}/bin/pip" install --require-hashes -r "${PROJECT_ROOT}/requirements-train.lock"
            ;;
    esac
fi

# -----------------------------------------------------------------------------
# CREAZIONE DEL SYMLINK UNIFICATO 'venv'
# -----------------------------------------------------------------------------
# ln -sfn:
# -s: crea un link simbolico
# -f: forza la sovrascrittura se il link esiste gia
# -n: tratta il link a directory come un file normale evitando ricorsioni
echo "[+] Aggiornamento link simbolico: ${SYMLINK_VENV} -> .venvs/${PROFILE}"
ln -sfn ".venvs/${PROFILE}" "${SYMLINK_VENV}"

# -----------------------------------------------------------------------------
# TEST DI VALIDAZIONE DELL'AMBIENTE ATTIVATO
# -----------------------------------------------------------------------------
echo ""
echo "====================================================================="
echo "[✓] Ambiente virtuale [ ${PROFILE} ] configurato ed agganciato con successo!"
echo "====================================================================="
echo "Per l'attivazione manuale nella shell corrente, eseguire:"
echo "    source venv/bin/activate"
echo ""
echo "Stato dei componenti rilevati in questo ambiente:"
"${SYMLINK_VENV}/bin/python3" -c "
import sys
print(f'  - Python Executable : {sys.executable}')
try:
    import fastapi
    print(f'  - FastAPI           : v{fastapi.__version__} (OK)')
except ImportError:
    print('  - FastAPI           : Assente')

try:
    import onnxruntime as ort
    print(f'  - ONNX Runtime      : v{ort.__version__} (OK)')
except ImportError:
    print('  - ONNX Runtime      : Assente')

try:
    import torch
    cuda_avail = torch.cuda.is_available()
    dev_name = torch.cuda.get_device_name(0) if cuda_avail else 'Nessuna (CPU mode)'
    print(f'  - PyTorch           : v{torch.__version__} (Accelerazione GPU: {cuda_avail} - {dev_name})')
except ImportError:
    print('  - PyTorch           : Non installato (modalita lightweight)')
"
echo "====================================================================="
