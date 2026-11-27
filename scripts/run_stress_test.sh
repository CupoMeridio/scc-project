#!/usr/bin/env bash
# FILE: scripts/run_stress_test.sh
# RUOLO: Esecutore dello stress test con wrk e validatore automatico dell'SLA.
#   - Target 'internal' (default): esegue un pre-flight check via port-forward
#     e poi lancia `wrk` in-cluster (pod effimero con label app=diabetes-clinical-api
#     consentita da NetworkPolicy) verso https://diabetes-forecasting-service/predict (Q2),
#     monitorando l'HPA durante l'esecuzione (V2).
#   - Target 'fullpath': esegue il login su https://localhost/api/auth/login e
#     lancia `wrk` sul percorso end-to-end Ingress -> clinical -> ML -> PostgreSQL
#     (/api/patient/meals) con cookie HttpOnly __Host-access_token.
#   - Salva l'output grezzo in docs/audit_reports/wrk_<data>.txt (Decisione D8).

set -euo pipefail
export LC_ALL=C

TARGET_HOST="${1:-internal}"
DURATION="${2:-30s}"
THREADS="${3:-4}"
CONNECTIONS="${4:-100}"
LUA_SCRIPT="$(dirname "$0")/test_predict.lua"
PF_PID=""
HPA_PID=""
IN_CLUSTER=0
CM_NAME="wrk-lua-bench"
POD_NAME="wrk-bench"
TMP_LUA=""

cleanup() {
    if [ -n "$HPA_PID" ]; then
        kill "$HPA_PID" >/dev/null 2>&1 || true
    fi
    if [ -n "$PF_PID" ]; then
        kill "$PF_PID" >/dev/null 2>&1 || true
    fi
    if [ -n "$TMP_LUA" ]; then
        rm -f "$TMP_LUA" >/dev/null 2>&1 || true
    fi
    if [ "$IN_CLUSTER" -eq 1 ]; then
        kubectl delete pod "$POD_NAME" --ignore-not-found >/dev/null 2>&1 || true
        kubectl delete configmap "$CM_NAME" --ignore-not-found >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

CURL_ARGS="-s -o /dev/null -w %{http_code}"
if [ "$TARGET_HOST" = "internal" ]; then
    if ! command -v kubectl &> /dev/null; then
        echo "[-] Errore: kubectl richiesto per il target 'internal'."
        exit 1
    fi
    IN_CLUSTER=1
    echo "[+] Verifica pre-flight tramite port-forward temporaneo su svc/diabetes-forecasting-service (18000 -> 443 TLS interno)..."
    kubectl port-forward svc/diabetes-forecasting-service 18000:443 >/dev/null 2>&1 &
    PF_PID=$!
    sleep 2
    PREFLIGHT_URL="https://127.0.0.1:18000/predict"
    WRK_URL="https://diabetes-forecasting-service/predict"
    CURL_ARGS="-k $CURL_ARGS"
elif [ "$TARGET_HOST" = "fullpath" ]; then
    STRESS_USER="${STRESS_USER:-patient_591}"
    STRESS_PASSWORD="${STRESS_PASSWORD:-}"
    if [ -z "$STRESS_PASSWORD" ]; then
        echo "[-] Errore: il target 'fullpath' richiede STRESS_PASSWORD (password utente attivo su IAM)." >&2
        exit 1
    fi
    TMP_COOKIE=$(mktemp)
    LOGIN_STATUS=$(curl -k -s -c "$TMP_COOKIE" -o /dev/null -w "%{http_code}" \
      -X POST "https://localhost/api/auth/login" \
      -H "Content-Type: application/json" \
      -d "{\"username\":\"${STRESS_USER}\",\"password\":\"${STRESS_PASSWORD}\"}")
    if [ "$LOGIN_STATUS" != "200" ]; then
        rm -f "$TMP_COOKIE"
        echo "[-] Login fullpath fallito per '${STRESS_USER}' (HTTP ${LOGIN_STATUS}). Verifica che la password non sia da cambiare." >&2
        exit 1
    fi
    COOKIE_NAME="${COOKIE_NAME:-__Host-scc_session}"
    COOKIE_VAL=$(awk -v cname="$COOKIE_NAME" '$6 == cname {print $7}' "$TMP_COOKIE")
    rm -f "$TMP_COOKIE"
    if [ -z "$COOKIE_VAL" ]; then
        echo "[-] Cookie ${COOKIE_NAME} non ricevuto dal login." >&2
        exit 1
    fi
    TMP_LUA=$(mktemp --suffix=.lua)
    cat > "$TMP_LUA" <<EOF
wrk.method = "POST"
wrk.headers["Content-Type"] = "application/json"
wrk.headers["Cookie"] = "${COOKIE_NAME}=${COOKIE_VAL}"
wrk.body = '{"carbs_g":45.0,"insulin_u":4.0}'
EOF
    LUA_SCRIPT="$TMP_LUA"
    WRK_URL="https://localhost/api/patient/meals"
    PREFLIGHT_URL=""
else
    WRK_URL="$TARGET_HOST/predict"
    PREFLIGHT_URL="$TARGET_HOST/predict"
    CURL_ARGS="-k $CURL_ARGS"
fi

echo "=========================================================="
echo " AVVIO STRESS TEST PER CERTIFICAZIONE SLA CON WRK"
echo " Target: $WRK_URL (mode=${1:-internal})"
echo " Carico: $THREADS threads, $CONNECTIONS connessioni, durata $DURATION"
echo " Obiettivo: >= 83.33 RPS (5000 req/min), Latenza <= 1.0s, Errori <= 0.50%"
echo "=========================================================="

if [ -n "$PREFLIGHT_URL" ]; then
    echo "[+] Verifica pre-flight su $PREFLIGHT_URL..."
    PREFLIGHT_STATUS=$(curl $CURL_ARGS -X POST "$PREFLIGHT_URL" \
      -H "Content-Type: application/json" \
      -d '{"patient_id":"PREFLIGHT","glucose_history":[4.2,4.0,3.8,3.6,3.5,3.7,3.9,4.3,5.0,5.5,6.1,6.7,7.3],"exogenous_inputs":[3.6,0.0,3.8,0.0,3.9,0.0,4.1,0.0,4.2,0.0,4.3,0.0,4.5,0.0]}' || echo "000")

    if [ -n "$PF_PID" ]; then
        kill "$PF_PID" >/dev/null 2>&1 || true
        PF_PID=""
    fi

    if [ "$PREFLIGHT_STATUS" = "308" ] || [ "$PREFLIGHT_STATUS" = "404" ]; then
        echo "[-] ERRORE PRE-FLIGHT (HTTP $PREFLIGHT_STATUS): /predict non e esposto sull'Ingress."
        echo "    Usare il target interno: ./scripts/run_stress_test.sh internal 30s 4 100"
        exit 1
    elif [ "$PREFLIGHT_STATUS" != "200" ]; then
        echo "[-] ERRORE PRE-FLIGHT: /predict ha risposto HTTP $PREFLIGHT_STATUS (atteso 200 OK)."
        echo "    Assicurarsi che il cluster KinD e i pod ML siano operativi (make deploy)."
        exit 1
    fi
    echo "[✓] Pre-flight completato con successo (HTTP 200 OK)."
fi

mkdir -p docs/audit_reports
REPORT_TS=$(date -u +%Y%m%d-%H%M%SZ)
REPORT_FILE="docs/audit_reports/wrk_${TARGET_HOST}_${REPORT_TS}.txt"
TMP_OUTPUT=$(mktemp)
HPA_LOG=$(mktemp)

{
  echo "=== wrk Stress Test Report (D8/V2) ==="
  echo "Date (UTC): $(date -u -Is)"
  echo "Command: wrk -t${THREADS} -c${CONNECTIONS} -d${DURATION} -s ${LUA_SCRIPT} --latency ${WRK_URL}"
  echo "Target mode: ${TARGET_HOST}"
  if command -v kubectl >/dev/null 2>&1; then
    echo "--- Deployments & Replicas (Pre-Run) ---"
    kubectl get deployment diabetes-forecasting-api diabetes-clinical-api -o wide 2>&1 || true
    echo "--- HPA State (Pre-Run) ---"
    kubectl get hpa 2>&1 || true
    echo "--- Pod Images & Resources ---"
    kubectl get pods -l 'app in (diabetes-forecasting-api,diabetes-clinical-api)' -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[0].image}{"\treq="}{.spec.containers[0].resources.requests}{"\tlim="}{.spec.containers[0].resources.limits}{"\tstatus="}{.status.phase}{"\n"}{end}' 2>&1 || true
  fi
  echo "--- Raw wrk Output ---"
} > "$REPORT_FILE"

if command -v kubectl >/dev/null 2>&1; then
    kubectl get hpa -w > "$HPA_LOG" 2>&1 &
    HPA_PID=$!
fi

if [ "$IN_CLUSTER" -eq 1 ]; then
    kubectl create configmap "$CM_NAME" --from-file=test_predict.lua="$LUA_SCRIPT" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl delete pod "$POD_NAME" --ignore-not-found >/dev/null 2>&1 || true
    OVERRIDES="{\"spec\":{\"containers\":[{\"name\":\"${POD_NAME}\",\"image\":\"williamyeh/wrk\",\"args\":[\"-t${THREADS}\",\"-c${CONNECTIONS}\",\"-d${DURATION}\",\"-s\",\"/scripts/test_predict.lua\",\"--latency\",\"${WRK_URL}\"],\"volumeMounts\":[{\"name\":\"lua\",\"mountPath\":\"/scripts\"}]}],\"volumes\":[{\"name\":\"lua\",\"configMap\":{\"name\":\"${CM_NAME}\"}}]}}"
    kubectl run "$POD_NAME" --rm -i --restart=Never --image=williamyeh/wrk \
      --labels="app=diabetes-clinical-api" --overrides="$OVERRIDES" | tee "$TMP_OUTPUT" | tee -a "$REPORT_FILE"
elif command -v wrk >/dev/null 2>&1; then
    wrk -t"$THREADS" -c"$CONNECTIONS" -d"$DURATION" -s "$LUA_SCRIPT" --latency "$WRK_URL" | tee "$TMP_OUTPUT" | tee -a "$REPORT_FILE"
else
    docker run --rm --network host -v "${LUA_SCRIPT}:/scripts/bench.lua:ro" williamyeh/wrk \
      -t"$THREADS" -c"$CONNECTIONS" -d"$DURATION" -s /scripts/bench.lua --latency "$WRK_URL" | tee "$TMP_OUTPUT" | tee -a "$REPORT_FILE"
fi

if [ -n "$HPA_PID" ]; then
    kill "$HPA_PID" >/dev/null 2>&1 || true
    wait "$HPA_PID" 2>/dev/null || true
    HPA_PID=""
fi

if command -v kubectl >/dev/null 2>&1; then
    {
      echo "--- HPA Timeline (kubectl get hpa -w) ---"
      cat "$HPA_LOG"
      echo "--- HPA & Pod State (Post-Run) ---"
      kubectl get hpa 2>&1 || true
      kubectl top pods 2>&1 || true
    } >> "$REPORT_FILE"
fi
rm -f "$HPA_LOG"
echo "[✓] Output grezzo wrk salvato in $REPORT_FILE"

echo ""
echo "=========================================================="
echo " REPORT DI CONVALIDA SERVICE LEVEL AGREEMENT (SLA)"
echo "=========================================================="

RPS=$(grep "Requests/sec:" "$TMP_OUTPUT" | awk '{print $2}' | head -n 1 || true)
if [ -z "$RPS" ]; then RPS="0.0"; fi

# Ancoriamo a inizio riga per non confondere "68.99%" (+/- Stdev) con "99%"!
P99_RAW=$(grep -E '^[[:space:]]*99%' "$TMP_OUTPUT" | awk '{print $2}' | head -n 1 || true)
if [ -z "$P99_RAW" ]; then P99_RAW="0ms"; fi

P99_MS="0"
if [[ "$P99_RAW" =~ ([0-9.]+)ms ]]; then
    P99_MS="${BASH_REMATCH[1]}"
elif [[ "$P99_RAW" =~ ([0-9.]+)s ]]; then
    P99_MS=$(awk -v s="${BASH_REMATCH[1]}" 'BEGIN { print s * 1000 }')
elif [[ "$P99_RAW" =~ ([0-9.]+)us ]]; then
    P99_MS=$(awk -v us="${BASH_REMATCH[1]}" 'BEGIN { print us / 1000 }')
fi

NON_2XX=$(grep "Non-2xx or 3xx responses:" "$TMP_OUTPUT" | awk '{print $5}' || true)
if [ -z "$NON_2XX" ]; then NON_2XX="0"; fi

TOTAL_REQ=$(grep "requests in" "$TMP_OUTPUT" | awk '{print $1}' || true)
if [ -z "$TOTAL_REQ" ] || [ "$TOTAL_REQ" -eq 0 ]; then TOTAL_REQ="1"; fi

ERROR_PERCENT=$(awk -v err="$NON_2XX" -v tot="$TOTAL_REQ" 'BEGIN { printf "%.2f", (err/tot)*100 }')
rm -f "$TMP_OUTPUT"

SLA_PASSED=1

echo -n " 1. Throughput:       $RPS RPS (Obiettivo: >= 83.33 RPS) ... "
if awk -v rps="$RPS" 'BEGIN { exit !(rps >= 83.33) }'; then
    echo "[PASS]"
else
    echo "[FAIL]"
    SLA_PASSED=0
fi

echo -n " 2. Latenza p99:      $P99_MS ms (Obiettivo: <= 1000.0 ms) ... "
if awk -v lat="$P99_MS" 'BEGIN { exit !(lat <= 1000.0) }'; then
    echo "[PASS]"
else
    echo "[FAIL]"
    SLA_PASSED=0
fi

echo -n " 3. Tasso di Errori:  $ERROR_PERCENT % (Obiettivo: <= 0.50 %) ... "
if awk -v err="$ERROR_PERCENT" 'BEGIN { exit !(err <= 0.50) }'; then
    echo "[PASS]"
else
    echo "[FAIL]"
    SLA_PASSED=0
fi

echo "=========================================================="
if [ "$SLA_PASSED" -eq 1 ]; then
    echo "[✓] ESITO: Tutti i requisiti SLA sono PIENAMENTE RISPETTATI!"
    exit 0
else
    echo "[X] ESITO: Almeno un requisito SLA e stato violato."
    exit 1
fi
