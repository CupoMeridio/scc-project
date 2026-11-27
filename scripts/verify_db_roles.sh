#!/usr/bin/env bash
# FILE: scripts/verify_db_roles.sh
# RUOLO: Prova assertiva (V3) dei 6 ruoli DB minimi e trigger append-only su PostgreSQL HA reale.
#        Esce con codice != 0 se un permesso o un trigger è rotto; salva output grezzo in docs/audit_reports/.

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
mkdir -p docs/audit_reports
TS="$(date -u +%Y%m%d-%H%M%SZ)"
REPORT="docs/audit_reports/db_roles_${TS}.txt"

say() { printf '%s\n' "$*" | tee -a "$REPORT"; }

{
  echo "=== PostgreSQL Least-Privilege Roles & Append-Only Trigger Report (V3) ==="
  echo "Date (UTC): $(date -u -Is)"
  echo "Command: $0"
  kubectl get cluster diabetes-pg -n "$NAMESPACE" 2>&1 || true
} > "$REPORT"

PG_IAM=$(kubectl get secret -n "$NAMESPACE" pg-role-iam -o jsonpath='{.data.password}' | base64 -d)
PG_CLIN=$(kubectl get secret -n "$NAMESPACE" pg-role-clinical -o jsonpath='{.data.password}' | base64 -d)
PG_MGR=$(kubectl get secret -n "$NAMESPACE" pg-role-manager -o jsonpath='{.data.password}' | base64 -d)
PG_ING=$(kubectl get secret -n "$NAMESPACE" pg-role-ingest -o jsonpath='{.data.password}' | base64 -d)
PG_WRK=$(kubectl get secret -n "$NAMESPACE" pg-role-worker -o jsonpath='{.data.password}' | base64 -d)
PG_TRN=$(kubectl get secret -n "$NAMESPACE" pg-role-trainer -o jsonpath='{.data.password}' | base64 -d)
PG_MIG=$(kubectl get secret -n "$NAMESPACE" migrator-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)

expect_ok() {
  local role="$1" pw="$2" sql="$3"
  local out
  if out=$(kubectl exec -n "$NAMESPACE" diabetes-pg-1 -c postgres -- \
    env PGPASSWORD="$pw" psql -h 127.0.0.1 -U "$role" -d diabetes_saas -At -c "$sql" 2>&1); then
    say "[OK]   ALLOW ${role}: ${sql} -> ${out}"
  else
    say "[FAIL] EXPECTED ALLOW ${role}: ${sql} -> ${out}"
    exit 1
  fi
}

expect_deny() {
  local role="$1" pw="$2" sql="$3"
  local out
  if out=$(kubectl exec -n "$NAMESPACE" diabetes-pg-1 -c postgres -- \
    env PGPASSWORD="$pw" psql -h 127.0.0.1 -U "$role" -d diabetes_saas -At -c "$sql" 2>&1); then
    say "[FAIL] EXPECTED DENY ${role}: ${sql} -> ${out}"
    exit 1
  else
    say "[OK]   DENY  ${role}: ${sql} (${out%%$'\n'*})"
  fi
}

expect_ok saas_manager "$PG_MGR" "SELECT total_patients, total_doctors FROM v_manager_overview;"
expect_deny saas_manager "$PG_MGR" "SELECT count(*) FROM users;"
expect_deny saas_manager "$PG_MGR" "SELECT count(*) FROM patients;"
expect_deny saas_manager "$PG_MGR" "SELECT count(*) FROM cgm_readings;"
expect_deny saas_manager "$PG_MGR" "SELECT actor_username, detail_json FROM audit_logs;"
expect_deny saas_manager "$PG_MGR" "SELECT count(*) FROM v_training_cgm_series;"

expect_ok saas_clinical "$PG_CLIN" "SELECT count(*) FROM patients;"
expect_ok saas_clinical "$PG_CLIN" "SELECT count(*) FROM doctor_profiles;"
expect_deny saas_clinical "$PG_CLIN" "SELECT count(*) FROM users;"
expect_deny saas_clinical "$PG_CLIN" "SELECT count(*) FROM revoked_tokens;"
expect_deny saas_clinical "$PG_CLIN" "SELECT count(*) FROM devices;"

expect_ok saas_iam "$PG_IAM" "SELECT count(*) FROM users;"
expect_ok saas_iam "$PG_IAM" "SELECT count(patient_code) FROM patients;"
expect_deny saas_iam "$PG_IAM" "SELECT full_name FROM patients LIMIT 1;"
expect_deny saas_iam "$PG_IAM" "SELECT email FROM patients LIMIT 1;"
expect_deny saas_iam "$PG_IAM" "SELECT count(*) FROM cgm_readings;"

expect_ok saas_ingest "$PG_ING" "SELECT count(*) FROM devices;"
expect_deny saas_ingest "$PG_ING" "SELECT count(*) FROM users;"
expect_deny saas_ingest "$PG_ING" "SELECT count(*) FROM cgm_readings;"

expect_ok saas_worker "$PG_WRK" "SELECT count(*) FROM cgm_readings;"
expect_deny saas_worker "$PG_WRK" "SELECT count(*) FROM users;"
expect_deny saas_worker "$PG_WRK" "SELECT count(*) FROM devices;"

expect_ok saas_trainer "$PG_TRN" "SELECT count(*) FROM v_training_cgm_series;"
expect_deny saas_trainer "$PG_TRN" "SELECT count(*) FROM users;"
expect_deny saas_trainer "$PG_TRN" "SELECT count(*) FROM patients;"

expect_deny saas_clinical "$PG_CLIN" "UPDATE audit_logs SET action='TAMPER';"
expect_deny saas_clinical "$PG_CLIN" "DELETE FROM audit_logs;"
expect_deny saas_clinical "$PG_CLIN" "TRUNCATE TABLE audit_logs;"
expect_deny saas_clinical "$PG_CLIN" "DROP TRIGGER audit_log_no_modification ON audit_logs;"
expect_deny saas_migrator "$PG_MIG" "DELETE FROM audit_logs;"

say "[✓] Tutti i controlli sui 6 ruoli DB e sul trigger append-only sono passati. Report: $REPORT"
