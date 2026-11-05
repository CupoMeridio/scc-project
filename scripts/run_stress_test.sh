#!/usr/bin/env bash
# FILE: scripts/run_stress_test.sh
# RUOLO: Esecutore dello stress test con wrk e validatore automatico dell'SLA.

# ------------------------------------------------------------------------------
# DIRETTIVE DI SICUREZZA BASH (Strict Mode)
# ------------------------------------------------------------------------------
# - set -e: Arresta immediatamente lo script se un comando restituisce un codice di errore diverso da 0.
# - set -u: Considera l'uso di una variabile non inizializzata come un errore bloccante.
# - set -o pipefail: Se un comando in una pipeline (es. cmd1 | cmd2) fallisce, l'intero comando fallisce
#   (il comportamento predefinito di Bash considera solo il codice di uscita dell'ultimo comando della pipe).
set -euo pipefail

# ------------------------------------------------------------------------------
# PARAMETRIZZAZIONE CON VALORI DI DEFAULT
# ------------------------------------------------------------------------------
# ${1:-http://localhost}: Se il primo argomento non viene passato al comando, usa "http://localhost".
TARGET_HOST="${1:-http://localhost}"
# ${2:-30s}: Durata del test (default 30 secondi).
DURATION="${2:-30s}"
# ${3:-4}: Numero di thread CPU allocati per generare il traffico (default 4 thread).
THREADS="${3:-4}"
# ${4:-100}: Numero di connessioni TCP simultanee aperte verso NGINX (default 100).
CONNECTIONS="${4:-100}"
# $(dirname "$0"): Ricava la cartella in cui risiede questo script (indipendentemente da dove viene lanciato).
LUA_SCRIPT="$(dirname "$0")/test_predict.lua"

echo "=========================================================="
echo " AVVIO STRESS TEST PER CERTIFICAZIONE SLA CON WRK"
echo " Target: $TARGET_HOST/predict"
echo " Carico: $THREADS threads, $CONNECTIONS connessioni, durata $DURATION"
echo " Obiettivo: >= 83.33 RPS (5000 req/min), Latenza <= 1.0s, Errori < 0.5%"
echo "=========================================================="

# command -v wrk &> /dev/null: Verifica la presenza dell'eseguibile 'wrk' nel PATH di sistema.
# Se non è installato, avvisa l'utente ed esce con codice 1.
if ! command -v wrk &> /dev/null; then
    echo "[-] Errore: wrk non e installato. Eseguire: sudo apt install -y wrk"
    exit 1
fi

# mktemp: Crea un file temporaneo sicuro e univoco (es. /tmp/tmp.1234abcd) per catturare l'output testuale di wrk
TMP_OUTPUT=$(mktemp)

# ESECUZIONE DEL COMANDO WRK:
# - -t: Numero di thread CPU per generare le richieste
# - -c: Numero di connessioni TCP simultanee (concorrenza)
# - -d: Durata complessiva del test
# - -s: Script Lua contenente la logica per generare il payload JSON POST
# - --latency: Ordina a wrk di stampare la tabella dei percentili dei tempi di risposta (50%, 75%, 90%, 99%)
# - tee: Mostra l'output in tempo reale sul terminale e CONTEMPORANEAMENTE lo scrive nel file temporaneo
wrk -t"$THREADS" -c"$CONNECTIONS" -d"$DURATION" -s "$LUA_SCRIPT" --latency "$TARGET_HOST/predict" | tee "$TMP_OUTPUT"

echo ""
echo "=========================================================="
echo " REPORT DI CONVALIDA SERVICE LEVEL AGREEMENT (SLA)"
echo "=========================================================="

# ESTRAZIONE AUTOMATICA DEI RISULTATI TRAMITE GREP E AWK:
# Cerca la riga contenente "Requests/sec:" e con awk '{print $2}' estrae il secondo campo (il valore numerico)
RPS=$(grep "Requests/sec:" "$TMP_OUTPUT" | awk '{print $2}')
if [ -z "$RPS" ]; then RPS="0.0"; fi # Se la variabile è vuota, assegna 0.0

# Cerca la riga del 99° percentile ("99%") e ne preleva il tempo (es. "12.45ms")
P99_RAW=$(grep "99%" "$TMP_OUTPUT" | awk '{print $2}')
if [ -z "$P99_RAW" ]; then P99_RAW="0ms"; fi

# CONVERSIONE DELLA LATENZA IN MILLISECONDI (ms):
# wrk stampa le unità di misura in modi diversi a seconda della velocità (es. 500us, 12ms, 1.2s):
P99_MS="0"
if [[ "$P99_RAW" =~ ([0-9.]+)ms ]]; then
    P99_MS="${BASH_REMATCH[1]}" # Già in millisecondi
elif [[ "$P99_RAW" =~ ([0-9.]+)s ]]; then
    # Se in secondi, moltiplica per 1000 usando awk
    P99_MS=$(awk -v s="${BASH_REMATCH[1]}" 'BEGIN { print s * 1000 }')
elif [[ "$P99_RAW" =~ ([0-9.]+)us ]]; then
    # Se in microsecondi, divide per 1000
    P99_MS=$(awk -v us="${BASH_REMATCH[1]}" 'BEGIN { print us / 1000 }')
fi

# Estrae il numero di risposte HTTP con errori (non-2xx o non-3xx)
NON_2XX=$(grep "Non-2xx or 3xx responses:" "$TMP_OUTPUT" | awk '{print $5}' || echo "0")
if [ -z "$NON_2XX" ]; then NON_2XX="0"; fi

# Estrae il numero totale di richieste completate
TOTAL_REQ=$(grep "requests in" "$TMP_OUTPUT" | awk '{print $1}' || echo "1")
if [ -z "$TOTAL_REQ" ] || [ "$TOTAL_REQ" -eq 0 ]; then TOTAL_REQ="1"; fi

# Calcola la percentuale di errore: (errori / totale) * 100
ERROR_PERCENT=$(awk -v err="$NON_2XX" -v tot="$TOTAL_REQ" 'BEGIN { printf "%.2f", (err/tot)*100 }')

# Elimina il file temporaneo non più necessario (pulizia)
rm -f "$TMP_OUTPUT"

# Variabile flag: 1 = superato, 0 = fallito
SLA_PASSED=1

# -----------------------------------------------------------------------------
# VERIFICA DELLE 3 CONDIZIONI DELL'SLA:
# -----------------------------------------------------------------------------
# 1. THROUGHPUT: La traccia richiede 5.000 req/min = 83.33 RPS
echo -n " 1. Throughput:       $RPS RPS (Obiettivo: >= 83.33 RPS) ... "
if awk -v rps="$RPS" 'BEGIN { exit !(rps >= 83.33) }'; then
    echo "[PASS]"
else
    echo "[FAIL]"
    SLA_PASSED=0
fi

# 2. LATENZA: La traccia richiede risposta <= 1.0 secondo (1000 ms) al 99° percentile
echo -n " 2. Latenza p99:      $P99_MS ms (Obiettivo: <= 1000.0 ms) ... "
if awk -v lat="$P99_MS" 'BEGIN { exit !(lat <= 1000.0) }'; then
    echo "[PASS]"
else
    echo "[FAIL]"
    SLA_PASSED=0
fi

# 3. AFFIDABILITÀ: La traccia richiede disponibilità >= 99.5% (quindi errori massimi ammessi <= 0.50%)
echo -n " 3. Tasso di Errori:  $ERROR_PERCENT % (Obiettivo: <= 0.50 %) ... "
if awk -v err="$ERROR_PERCENT" 'BEGIN { exit !(err <= 0.50) }'; then
    echo "[PASS]"
else
    echo "[FAIL]"
    SLA_PASSED=0
fi

echo "=========================================================="
# Se tutte e tre le condizioni sono soddisfatte, esce con 0 (successo).
# Se anche una sola fallisce, esce con 1 (fallimento per pipeline CI/CD).
if [ "$SLA_PASSED" -eq 1 ]; then
    echo "[✓] ESITO: Tutti i requisiti SLA sono PIENAMENTE RISPETTATI!"
    exit 0
else
    echo "[X] ESITO: Almeno un requisito SLA e stato violato."
    exit 1
fi
