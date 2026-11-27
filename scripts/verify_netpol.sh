#!/usr/bin/env bash
# FILE: scripts/verify_netpol.sh
# RUOLO: Verifica ASSERTIVA dell'enforcement NetworkPolicy tra tutte le zone (arch.pdf):
#   1. Pod non autorizzato (nessuna label): bloccato verso Redis, PG, ML, ModelRepo, IAM.
#   2. Pod Application Clinical (`app=diabetes-clinical-api`): raggiunge IAM/PG/ML, bloccato da Redis/ModelRepo.
#   3. Pod Application Manager (`app=diabetes-manager-api`): raggiunge IAM/PG/ML, bloccato da Redis/ModelRepo.
#   4. Pod Training Zone (`app=diabetes-model-trainer`): raggiunge PG/ModelRepo, bloccato da Redis/IAM/ML.
#   - Rileva e verifica gli IP cablati per kube-apiserver + subnet kind.
# Esce con codice 1 se qualunque assert fallisce. Output in docs/audit_reports/netpol_<data>.txt.

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
TIMEOUT=3
REPORT_DIR="docs/audit_reports"
REPORT="${REPORT_DIR}/netpol_$(date +%Y%m%d-%H%M%S).txt"
FAILED=0

mkdir -p "$REPORT_DIR"
: > "$REPORT"

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
say_cmd() { printf '%s\n' "$*" | tee -a "$REPORT"; }

say "=== Verifica enforcement NetworkPolicy per Zone | $(date -Is) | namespace=${NAMESPACE} ==="

say ""
say "[+] Versione kind:"
say_cmd "$(kind version 2>&1 || echo 'kind non trovato nel PATH')"
say ""
say "[+] Pod in kube-system (identificazione CNI):"
say_cmd "$(kubectl get pods -n kube-system -o wide 2>&1 || echo 'kubectl non disponibile')"
say ""
say "[+] CNI riconosciuto:"
say_cmd "$(kubectl get pods -n kube-system --no-headers 2>/dev/null | grep -iE 'calico|cilium|kindnet|flannel|weave' || echo 'CNI non identificato')"

say ""
say "[+] Step 0b: verifica IP cablati in k8s/networkpolicy.yaml..."
APISERVER_IP="$(kubectl get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
DOCKER_SUBNET="$(docker network inspect kind --format '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' 2>/dev/null | grep -E '^[0-9]+\.' | head -n 1 || true)"
say "    kube-apiserver ClusterIP rilevato: ${APISERVER_IP:-<non rilevato>}"
say "    rete docker kind rilevata (IPv4): ${DOCKER_SUBNET:-<non rilevata>}"

POLICY_FILE="k8s/networkpolicy.yaml"
if [ -n "$APISERVER_IP" ]; then
  if grep -q "${APISERVER_IP}/32" "$POLICY_FILE"; then
    say "    [OK] ${APISERVER_IP}/32 presente in ${POLICY_FILE}."
  else
    say "    [FAIL] ${APISERVER_IP}/32 NON presente in ${POLICY_FILE}."
    FAILED=1
  fi
fi
if [ -n "$DOCKER_SUBNET" ]; then
  if grep -q "${DOCKER_SUBNET}" "$POLICY_FILE"; then
    say "    [OK] ${DOCKER_SUBNET} presente in ${POLICY_FILE}."
  else
    say "    [FAIL] ${DOCKER_SUBNET} NON presente in ${POLICY_FILE}."
    FAILED=1
  fi
fi

# Step 1: pod senza label -> traffico diretto verso IP dei pod bloccato
say ""
say "[+] Step 1: pod NON autorizzato (nessuna label) - tutte le connessioni dirette devono FALLIRE..."
REDIS_POD_IP="$(kubectl get pod -l app=diabetes-redis -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
PG_POD_IP="$(kubectl get pod -l cnpg.io/cluster=diabetes-pg -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
ML_POD_IP="$(kubectl get pod -l app=diabetes-forecasting-api -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
REPO_POD_IP="$(kubectl get pod -l app=diabetes-model-repo -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
IAM_POD_IP="$(kubectl get pod -l app=diabetes-iam-api -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
if [ -z "$REDIS_POD_IP" ] || [ -z "$PG_POD_IP" ] || [ -z "$ML_POD_IP" ] || [ -z "$REPO_POD_IP" ] || [ -z "$IAM_POD_IP" ]; then
  say "    [FAIL] Impossibile rilevare i pod IP di destinazione."
  exit 1
fi

UNAUTH_OUT="$(kubectl run np-test-unauth --rm -i --restart=Never --image=busybox:1.36 \
  --namespace "$NAMESPACE" --timeout=60s -- \
  sh -c "
    reach=0; blocked=0;
    for pair in '${REDIS_POD_IP} 6379' '${PG_POD_IP} 5432' '${ML_POD_IP} 8000' '${REPO_POD_IP} 8002' '${IAM_POD_IP} 8001'; do
      set -- \$pair;
      if nc -w ${TIMEOUT} \"\$1\" \"\$2\" </dev/null >/dev/null 2>&1; then
        echo \"UNAUTH REACHABLE \$1:\$2\"; reach=\$((reach+1));
      else
        echo \"UNAUTH BLOCKED \$1:\$2\"; blocked=\$((blocked+1));
      fi;
    done;
    echo \"UNAUTH SUMMARY reachable=\$reach blocked=\$blocked\";
  " 2>&1 || true)"
say_cmd "$UNAUTH_OUT"
UNAUTH_SUMMARY="$(printf '%s\n' "$UNAUTH_OUT" | grep 'UNAUTH SUMMARY' || true)"
if [ "$UNAUTH_SUMMARY" = "UNAUTH SUMMARY reachable=0 blocked=5" ]; then
  say "    [OK] tutte e 5 le connessioni dirette bloccate dal CNI."
else
  say "    [FAIL] atteso 'reachable=0 blocked=5', trovato: ${UNAUTH_SUMMARY:-nessun riepilogo}"
  FAILED=1
fi

run_zone_probe() {
  local pod_name="$1"
  local label="$2"
  local allow_list="$3"
  local deny_list="$4"
  local expected_summary="$5"

  say ""
  say "[+] Probe zona '${label}'..."
  local out
  out="$(kubectl run "$pod_name" --rm -i --restart=Never --image=busybox:1.36 \
    --namespace "$NAMESPACE" --timeout=60s --labels="app=${label}" -- \
    sh -c "
      allow_ok=0; allow_fail=0; deny_ok=0; deny_fail=0;
      for pair in ${allow_list}; do
        h=\${pair%:*}; p=\${pair#*:};
        if nc -w ${TIMEOUT} \"\$h\" \"\$p\" </dev/null >/dev/null 2>&1; then
          echo \"ALLOW OK \$h:\$p\"; allow_ok=\$((allow_ok+1));
        else
          echo \"ALLOW FAIL \$h:\$p\"; allow_fail=\$((allow_fail+1));
        fi;
      done;
      for pair in ${deny_list}; do
        h=\${pair%:*}; p=\${pair#*:};
        if nc -w ${TIMEOUT} \"\$h\" \"\$p\" </dev/null >/dev/null 2>&1; then
          echo \"DENY BREACH \$h:\$p\"; deny_fail=\$((deny_fail+1));
        else
          echo \"DENY BLOCKED \$h:\$p\"; deny_ok=\$((deny_ok+1));
        fi;
      done;
      echo \"ZONE SUMMARY allow_ok=\$allow_ok allow_fail=\$allow_fail deny_ok=\$deny_ok deny_fail=\$deny_fail\";
    " 2>&1 || true)"
  say_cmd "$out"
  local summary
  summary="$(printf '%s\n' "$out" | grep 'ZONE SUMMARY' || true)"
  if [ "$summary" = "$expected_summary" ]; then
    say "    [OK] ${label}: ${summary}"
  else
    say "    [FAIL] ${label}: atteso '${expected_summary}', trovato '${summary:-nessuno}'"
    FAILED=1
  fi
}

# Step 2: Clinical API -> accede IAM(provisioning)/PG/ML/Redis(revoca RO), bloccata da ModelRepo
run_zone_probe "np-test-clinical" "diabetes-clinical-api" \
  "diabetes-iam-service:443 diabetes-pg-rw:5432 diabetes-forecasting-service:443 redis-service:6379" \
  "diabetes-model-repo:443" \
  "ZONE SUMMARY allow_ok=4 allow_fail=0 deny_ok=1 deny_fail=0"

# Step 3: Manager API -> accede PG/ML/Redis(revoca RO); BLOCCATA da IAM e ModelRepo (V1)
run_zone_probe "np-test-manager" "diabetes-manager-api" \
  "diabetes-pg-rw:5432 diabetes-forecasting-service:443 redis-service:6379" \
  "diabetes-iam-service:443 diabetes-model-repo:443" \
  "ZONE SUMMARY allow_ok=3 allow_fail=0 deny_ok=2 deny_fail=0"

# Step 4: Training Zone -> accede PG/ModelRepo, bloccata da Redis/IAM/ML
run_zone_probe "np-test-trainer" "diabetes-model-trainer" \
  "diabetes-pg-rw:5432 diabetes-model-repo:443" \
  "redis-service:6379 diabetes-iam-service:443 diabetes-forecasting-service:443" \
  "ZONE SUMMARY allow_ok=2 allow_fail=0 deny_ok=3 deny_fail=0"

say ""
if [ "$FAILED" -eq 0 ]; then
  say "[✓] NetworkPolicy per zone enforce correttamente su ${NAMESPACE}."
  say "[✓] Report salvato in ${REPORT}."
  exit 0
fi
say "[-] Verifica NetworkPolicy FALLITA."
say "[-] Report salvato in ${REPORT}."
exit 1
