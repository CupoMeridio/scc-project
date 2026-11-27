#!/usr/bin/env bash
# FILE: scripts/verify_backup.sh
# RUOLO: Prova assertiva end-to-end (V3 / V6): esegue il CronJob di backup cifrato con chiave pubblica,
#        verifica assenza chiave privata nel cluster, decifra con chiave privata esterna,
#        prova fallimento su file manomesso e ripristina in un DB temporaneo confrontando i conteggi.

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
PRIV_FILE="${BACKUP_PRIVATE_KEY_FILE:-${HOME}/.config/scc-cluster/backup_private.b64}"
PY_BIN="./venv/bin/python3"
if [ ! -x "$PY_BIN" ]; then
  PY_BIN="python3"
fi

if [ ! -f "$PRIV_FILE" ]; then
  echo "[-] Chiave privata backup non trovata in ${PRIV_FILE}." >&2
  exit 1
fi
BK_PRIV_B64="$(tr -d '[:space:]' < "$PRIV_FILE")"

mkdir -p docs/audit_reports
TS="$(date -u +%Y%m%d-%H%M%SZ)"
REPORT="docs/audit_reports/backup_restore_${TS}.txt"
TMP_DIR="$(mktemp -d)"
RESTORE_CID=""
cleanup() {
  kubectl delete job pg-backup-verify -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  kubectl delete pod pg-backup-fetch -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  if [ -n "$RESTORE_CID" ]; then
    docker rm -f "$RESTORE_CID" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }

{
  echo "=== Asymmetric Encrypted Backup & Restore Verification (V3 / V6) ==="
  echo "Date (UTC): $(date -u -Is)"
  echo "Command: $0"
} > "$REPORT"

# 1. Verifica che nessun Secret/ConfigMap in cluster contenga la chiave privata
if kubectl get secret,cm -n "$NAMESPACE" -o yaml | grep -qF "$BK_PRIV_B64"; then
  say "[-] FAIL: la chiave privata backup è stata trovata dentro il cluster!"
  exit 1
fi
say "[OK] Chiave privata assente dal cluster (solo BACKUP_PUBLIC_KEY_B64 in pg-backup-public)."

# 2. Esecuzione CronJob live
kubectl delete job pg-backup-verify -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
kubectl create job --from=cronjob/diabetes-pg-backup pg-backup-verify -n "$NAMESPACE" >/dev/null
kubectl wait --for=condition=complete job/pg-backup-verify -n "$NAMESPACE" --timeout=180s >/dev/null
JOB_LOG="$(kubectl logs job/pg-backup-verify -n "$NAMESPACE")"
say "$JOB_LOG"

# 3. Estrazione ultimo .dump.enc dal PVC
kubectl run pg-backup-fetch -n "$NAMESPACE" --restart=Never --image=busybox:1.36 \
  --overrides='{"spec":{"containers":[{"name":"fetch","image":"busybox:1.36","command":["sh","-c","sleep 60"],"volumeMounts":[{"name":"bk","mountPath":"/backups","readOnly":true}]}],"volumes":[{"name":"bk","persistentVolumeClaim":{"claimName":"pg-backups"}}]}}' >/dev/null
kubectl wait --for=condition=Ready pod/pg-backup-fetch -n "$NAMESPACE" --timeout=60s >/dev/null
ENC_NAME="$(kubectl exec -n "$NAMESPACE" pg-backup-fetch -- sh -c 'ls -t /backups/*.dump.enc | head -n1')"
kubectl cp "${NAMESPACE}/pg-backup-fetch:${ENC_NAME}" "${TMP_DIR}/backup.dump.enc" >/dev/null
kubectl delete pod pg-backup-fetch -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
say "[OK] Estratto ${ENC_NAME} ($(stat -c%s "${TMP_DIR}/backup.dump.enc") bytes)."

# 4. Verifica rifiuto manomissione (GCM tag mismatch -> codice uscita != 0)
"$PY_BIN" - "${TMP_DIR}/backup.dump.enc" "${TMP_DIR}/tampered.dump.enc" <<'PY'
from pathlib import Path
import sys
b = bytearray(Path(sys.argv[1]).read_bytes())
b[len(b) // 2] ^= 0xFF
Path(sys.argv[2]).write_bytes(bytes(b))
PY
if "$PY_BIN" -m saas_app.backup_crypto open --in "${TMP_DIR}/tampered.dump.enc" --out "${TMP_DIR}/bad.dump" --private-key "$BK_PRIV_B64" >/dev/null 2>&1; then
  say "[-] FAIL: la decifratura su file manomesso NON è fallita!"
  exit 1
fi
say "[OK] Manomissione backup cifrato rifiutata (InvalidTag)."

# 5. Decifratura valida + restore in DB temporaneo su PostgreSQL HA + confronto righe
"$PY_BIN" -m saas_app.backup_crypto open --in "${TMP_DIR}/backup.dump.enc" --out "${TMP_DIR}/restored.dump" --private-key "$BK_PRIV_B64"
MAGIC="$(head -c 5 "${TMP_DIR}/restored.dump")"
if [ "$MAGIC" != "PGDMP" ]; then
  say "[-] FAIL: magic bytes attesi 'PGDMP', trovati '${MAGIC}'."
  exit 1
fi

PG_MIG="$(kubectl get secret -n "$NAMESPACE" migrator-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)"
RESTORE_CID="scc-pg-backup-restore-$$"
RESTORE_PW="$(openssl rand -hex 16)"
docker run -d --name "$RESTORE_CID" \
  -e POSTGRES_PASSWORD="$RESTORE_PW" \
  -e POSTGRES_DB=diabetes_restore \
  postgres:16-alpine >/dev/null
for i in $(seq 1 40); do
  if docker exec "$RESTORE_CID" pg_isready -h 127.0.0.1 -p 5432 -U postgres -d diabetes_restore >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
for ROLE_NAME in saas_iam saas_clinical saas_manager saas_ingest saas_worker saas_trainer saas_migrator; do
  docker exec "$RESTORE_CID" psql -U postgres -d diabetes_restore -c "CREATE ROLE ${ROLE_NAME} NOLOGIN;" >/dev/null
done
docker cp "${TMP_DIR}/restored.dump" "${RESTORE_CID}:/tmp/restored.dump" >/dev/null
docker exec "$RESTORE_CID" pg_restore -U postgres -d diabetes_restore --no-owner /tmp/restored.dump >/dev/null
docker exec "$RESTORE_CID" rm -f /tmp/restored.dump >/dev/null

SRC_COUNTS="$(kubectl exec -n "$NAMESPACE" diabetes-pg-1 -c postgres -- env PGPASSWORD="$PG_MIG" psql -h 127.0.0.1 -U saas_migrator -d diabetes_saas -At -c "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM patients), (SELECT count(*) FROM cgm_readings), (SELECT count(*) FROM meals_predictions), (SELECT count(*) FROM audit_logs);")"
DST_COUNTS="$(docker exec "$RESTORE_CID" psql -U postgres -d diabetes_restore -At -c "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM patients), (SELECT count(*) FROM cgm_readings), (SELECT count(*) FROM meals_predictions), (SELECT count(*) FROM audit_logs);")"

say "Source row counts (users|patients|cgm|meals|audit):   ${SRC_COUNTS}"
say "Restored row counts (users|patients|cgm|meals|audit): ${DST_COUNTS}"
if [ "$SRC_COUNTS" != "$DST_COUNTS" ]; then
  say "[-] FAIL: conteggi sorgente e ripristino non coincidono!"
  exit 1
fi
say "[✓] Backup cifrato decifrato, autenticato e ripristinato con 100% parità righe. Report: $REPORT"
