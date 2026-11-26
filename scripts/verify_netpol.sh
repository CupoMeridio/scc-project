#!/usr/bin/env bash
# FILE: scripts/verify_netpol.sh
# RUOLO: Verifica ASSERTIVA che le NetworkPolicy siano realmente enforce dal CNI.
#   - Pod NON autorizzato (nessuna label selezionata): le connessioni verso Redis (6379),
#     Postgres (5432) e ML (8000) devono FALLIRE.
#   - Pod AUTORIZZATO (label app=diabetes-saas-api): le stesse connessioni devono RIUSCIRE.
#   - Rileva e verifica gli IP cablati in postgres-ha-policy (kube-apiserver + rete docker kind).
# Lo script esce con codice 1 se una qualunque verifica non e' soddisfatta.
# Output grezzo salvato in docs/audit_reports/netpol_<data>.txt (D8).

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
TIMEOUT=3
REPORT_DIR="docs/audit_reports"
REPORT="${REPORT_DIR}/netpol_$(date +%Y%m%d-%H%M%S).txt"
FAILED=0

mkdir -p "$REPORT_DIR"
: > "$REPORT"

# say: stampa a video e appende al report; say_cmd: idem per l'output di un comando.
say() { printf '%s\n' "$*" | tee -a "$REPORT"; }
say_cmd() { printf '%s\n' "$*" | tee -a "$REPORT"; }

say "=== Verifica enforcement NetworkPolicy | $(date -Is) | namespace=${NAMESPACE} ==="

# ------------------------------------------------------------------------------
# STEP 0: contesto (versione kind e pod del CNI in kube-system)
# ------------------------------------------------------------------------------
say ""
say "[+] Versione kind:"
say_cmd "$(kind version 2>&1 || echo 'kind non trovato nel PATH')"
say ""
say "[+] Pod in kube-system (identificazione CNI):"
say_cmd "$(kubectl get pods -n kube-system -o wide 2>&1 || echo 'kubectl non disponibile')"
say ""
say "[+] CNI riconosciuto:"
say_cmd "$(kubectl get pods -n kube-system --no-headers 2>/dev/null | grep -iE 'calico|cilium|kindnet|flannel|weave' || echo 'CNI non identificato dai pod di kube-system')"

# ------------------------------------------------------------------------------
# STEP 0b: IP cablati di postgres-ha-policy (verificati, non assunti)
# Rileva il ClusterIP del kube-apiserver e la subnet della rete docker kind e
# confronta i valori con quelli dichiarati nel manifest k8s/networkpolicy.yaml.
# ------------------------------------------------------------------------------
say ""
say "[+] Step 0b: verifica degli IP cablati in postgres-ha-policy..."
APISERVER_IP="$(kubectl get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
DOCKER_SUBNET="$(docker network inspect kind --format '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' 2>/dev/null | grep -E '^[0-9]+\.' | head -n 1 || true)"
say "    kube-apiserver ClusterIP rilevato: ${APISERVER_IP:-<non rilevato>}"
say "    rete docker kind rilevata:        ${DOCKER_SUBNET:-<non rilevata>}"

POLICY_FILE="k8s/networkpolicy.yaml"
if [ -n "$APISERVER_IP" ]; then
  if grep -q "${APISERVER_IP}/32" "$POLICY_FILE"; then
    say "    [OK] ${APISERVER_IP}/32 presente in ${POLICY_FILE}."
  else
    say "    [FAIL] ${APISERVER_IP}/32 NON presente in ${POLICY_FILE}: aggiornare il manifest."
    FAILED=1
  fi
fi
if [ -n "$DOCKER_SUBNET" ]; then
  if grep -q "${DOCKER_SUBNET}" "$POLICY_FILE"; then
    say "    [OK] ${DOCKER_SUBNET} presente in ${POLICY_FILE}."
  else
    say "    [FAIL] ${DOCKER_SUBNET} NON presente in ${POLICY_FILE}: aggiornare il manifest."
    FAILED=1
  fi
fi

# ------------------------------------------------------------------------------
# STEP 1: pod non autorizzato -> tutte le connessioni dirette ai pod devono fallire
# Usiamo gli IP diretti dei pod (non solo i nomi DNS) così dimostriamo che il CNI
# blocca il traffico TCP L3/L4 anche aggirando CoreDNS.
# ------------------------------------------------------------------------------
say ""
say "[+] Step 1: pod NON autorizzato (nessuna label) - le connessioni devono FALLIRE..."
REDIS_POD_IP="$(kubectl get pod -l app=diabetes-redis -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
PG_POD_IP="$(kubectl get pod -l cnpg.io/cluster=diabetes-pg -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
ML_POD_IP="$(kubectl get pod -l app=diabetes-forecasting-api -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)"
if [ -z "$REDIS_POD_IP" ] || [ -z "$PG_POD_IP" ] || [ -z "$ML_POD_IP" ]; then
  say "    [FAIL] Impossibile rilevare i pod IP di destinazione (redis=${REDIS_POD_IP:-?} pg=${PG_POD_IP:-?} ml=${ML_POD_IP:-?})."
  exit 1
fi
UNAUTH_OUT="$(kubectl run np-test-unauth --rm -i --restart=Never --image=busybox:1.36 \
  --namespace "$NAMESPACE" --timeout=60s -- \
  sh -c "
    reach=0; blocked=0;
    for pair in '${REDIS_POD_IP} 6379' '${PG_POD_IP} 5432' '${ML_POD_IP} 8000'; do
      set -- \$pair;
      if nc -w ${TIMEOUT} \"\$1\" \"\$2\" </dev/null >/dev/null 2>&1; then
        echo \"UNAUTH REACHABLE \$1:\$2\"; reach=\$((reach+1));
      else
        echo \"UNAUTH BLOCKED \$1:\$2\"; blocked=\$((blocked+1));
      fi;
    done;
    echo \"UNAUTH SUMMARY reachable=\$reach blocked=\$blocked\";
    [ \"\$reach\" -eq 0 ];
  " 2>&1 || true)"
say_cmd "$UNAUTH_OUT"
UNAUTH_SUMMARY="$(printf '%s\n' "$UNAUTH_OUT" | grep 'UNAUTH SUMMARY' || true)"
if [ "$UNAUTH_SUMMARY" = "UNAUTH SUMMARY reachable=0 blocked=3" ]; then
  say "    [OK] tutte e tre le connessioni dirette bloccate dal CNI."
else
  say "    [FAIL] connessioni non tutte bloccate (atteso 'reachable=0 blocked=3'): ${UNAUTH_SUMMARY:-nessun riepilogo}"
  FAILED=1
fi

# ------------------------------------------------------------------------------
# STEP 2: pod autorizzato (label app=diabetes-saas-api) -> tutte le connessioni devono riuscire
# Verifica DNS + Service ClusterIP + NetworkPolicy (redis-service:6379, diabetes-pg-rw:5432, diabetes-forecasting-service:80).
# ------------------------------------------------------------------------------
say ""
say "[+] Step 2: pod AUTORIZZATO (label app=diabetes-saas-api) - le connessioni devono RIUSCIRE..."
AUTH_OUT="$(kubectl run np-test-auth --rm -i --restart=Never --image=busybox:1.36 \
  --namespace "$NAMESPACE" --timeout=60s \
  --labels="app=diabetes-saas-api" -- \
  sh -c "
    reach=0; blocked=0;
    for pair in 'redis-service 6379' 'diabetes-pg-rw 5432' 'diabetes-forecasting-service 80'; do
      set -- \$pair;
      if nc -w ${TIMEOUT} \"\$1\" \"\$2\" </dev/null >/dev/null 2>&1; then
        echo \"AUTH REACHABLE \$1:\$2\"; reach=\$((reach+1));
      else
        echo \"AUTH BLOCKED \$1:\$2\"; blocked=\$((blocked+1));
      fi;
    done;
    echo \"AUTH SUMMARY reachable=\$reach blocked=\$blocked\";
    [ \"\$reach\" -eq 3 ];
  " 2>&1 || true)"
say_cmd "$AUTH_OUT"
AUTH_SUMMARY="$(printf '%s\n' "$AUTH_OUT" | grep 'AUTH SUMMARY' || true)"
if [ "$AUTH_SUMMARY" = "AUTH SUMMARY reachable=3 blocked=0" ]; then
  say "    [OK] tutte e tre le connessioni riuscite."
else
  say "    [FAIL] connessioni non tutte riuscite (atteso 'reachable=3 blocked=0'): ${AUTH_SUMMARY:-nessun riepilogo}"
  FAILED=1
fi

# ------------------------------------------------------------------------------
# ESITO
# ------------------------------------------------------------------------------
say ""
if [ "$FAILED" -eq 0 ]; then
  say "[✓] NetworkPolicy enforce correttamente su ${NAMESPACE}."
  say "[✓] Report salvato in ${REPORT}."
  exit 0
fi
say "[-] Verifica NetworkPolicy FALLITA: le policy non risultano applicate."
say "    Se il CNI non enforce le policy, abilitare networking.disableDefaultCNI=true"
say "    in k8s/kind-config.yaml e installare Calico o Cilium prima di riapplicare le policy."
say "[-] Report salvato in ${REPORT}."
exit 1
