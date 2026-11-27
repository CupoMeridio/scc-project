"""Initial SaaS schema: tabelle, trigger append-only audit_logs, viste e permessi per 6 ruoli DB.

Le password/creazioni dei 6 ruoli DB sono gestite dichiarativamente da CloudNativePG
(`managed.roles` in k8s/postgres-ha.yaml): `saas_migrator` non richiede CREATEROLE (V8).
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("provision_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("provision_key"),
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    op.create_table(
        "doctor_profiles",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("doctor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("patient_code", sa.String(length=32), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("diet_carb_limit_g", sa.Float(), nullable=False),
        sa.Column("target_min_mmol", sa.Float(), nullable=False),
        sa.Column("target_max_mmol", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("patient_code"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_patients_id", "patients", ["id"])
    op.create_index("ix_patients_doctor_id", "patients", ["doctor_id"])
    op.create_index("ix_patients_patient_code", "patients", ["patient_code"], unique=True)

    op.create_table(
        "devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_devices_id", "devices", ["id"])
    op.create_index("ix_devices_patient_id", "devices", ["patient_id"])
    op.create_index("ix_devices_key_hash", "devices", ["key_hash"], unique=True)

    op.create_table(
        "cgm_readings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("glucose_mmol", sa.Float(), nullable=False),
        sa.Column("insulin_u", sa.Float(), nullable=False),
        sa.Column("cho_rate", sa.Float(), nullable=False),
        sa.UniqueConstraint("patient_id", "recorded_at", name="uq_cgm_patient_time"),
    )
    op.create_index("ix_cgm_patient_recorded", "cgm_readings", ["patient_id", "recorded_at"])

    op.create_table(
        "meals_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("declared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("carbs_g", sa.Float(), nullable=False),
        sa.Column("insulin_u", sa.Float(), nullable=False),
        sa.Column("current_glucose_mmol", sa.Float(), nullable=True),
        sa.Column("forecasted_glucose_mmol", sa.Float(), nullable=True),
        sa.Column("actual_glucose_30m_mmol", sa.Float(), nullable=True),
        sa.Column("risk_class", sa.Integer(), nullable=True),
        sa.Column("outcome_code", sa.String(length=40), nullable=False),
        sa.Column("outcome_message", sa.Text(), nullable=False),
        sa.Column("doctor_flag", sa.Boolean(), nullable=False),
        sa.Column("model_version", sa.String(length=32), nullable=False),
    )
    op.create_index("ix_meals_patient_declared", "meals_predictions", ["patient_id", "declared_at"])

    op.create_table(
        "interventions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("doctor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("intervention_type", sa.String(length=32), nullable=False),
        sa.Column("cause", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("new_diet_carb_limit_g", sa.Float(), nullable=True),
    )
    op.create_index("ix_interventions_patient_id", "interventions", ["patient_id"])
    op.create_index("ix_interventions_doctor_id", "interventions", ["doctor_id"])

    op.create_table(
        "revoked_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("jti"),
    )
    op.create_index("ix_revoked_tokens_jti", "revoked_tokens", ["jti"], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("actor_role", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_patient_id", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])

    # --- Viste SQL aggregate per Manager (percorso unico SQLite/PostgreSQL, V5) ---
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.execute(sa.text("""
            CREATE VIEW v_manager_overview AS
            SELECT
                (SELECT COUNT(*) FROM patients) AS total_patients,
                (SELECT COUNT(*) FROM doctor_profiles) AS total_doctors,
                (SELECT COUNT(*) FROM meals_predictions WHERE outcome_code <> 'NO_RECENT_DATA') AS total_meals_predictions,
                (SELECT COUNT(*) FROM meals_predictions WHERE doctor_flag = 1) AS total_flagged_events,
                (SELECT COUNT(*) FROM interventions) AS total_interventions,
                (SELECT COUNT(*) FROM interventions WHERE cause = 'model_error') AS interventions_by_model_error,
                (SELECT COUNT(*) FROM interventions WHERE cause = 'patient_behavior') AS interventions_by_patient_behavior,
                (SELECT AVG(ABS(forecasted_glucose_mmol - actual_glucose_30m_mmol))
                 FROM meals_predictions
                 WHERE actual_glucose_30m_mmol IS NOT NULL
                   AND forecasted_glucose_mmol IS NOT NULL
                   AND outcome_code <> 'NO_RECENT_DATA') AS avg_prediction_mae_mmol,
                (SELECT COUNT(*) FROM meals_predictions
                 WHERE outcome_code <> 'NO_RECENT_DATA'
                   AND declared_at >= datetime('now', '-30 days')) AS meals_last_30_days;
        """))
        op.execute(sa.text("""
            CREATE VIEW v_manager_risk_distribution AS
            SELECT risk_class, COUNT(*) AS cnt
            FROM meals_predictions
            WHERE risk_class BETWEEN 1 AND 7
              AND outcome_code <> 'NO_RECENT_DATA'
            GROUP BY risk_class;
        """))
        op.execute(sa.text("""
            CREATE VIEW v_manager_weekly_interventions AS
            SELECT
                CAST((julianday('now') - julianday(created_at)) / 7 AS INTEGER) AS week_idx,
                COUNT(*) AS total_interventions,
                SUM(CASE WHEN cause = 'model_error' THEN 1 ELSE 0 END) AS model_error_count,
                SUM(CASE WHEN cause = 'patient_behavior' THEN 1 ELSE 0 END) AS patient_behavior_count
            FROM interventions
            WHERE created_at >= datetime('now', '-28 days')
              AND created_at <= datetime('now')
            GROUP BY 1;
        """))
        return

    # --- Viste PostgreSQL ---
    op.execute(sa.text("""
        CREATE OR REPLACE VIEW v_manager_overview AS
        SELECT
            (SELECT COUNT(*) FROM patients) AS total_patients,
            (SELECT COUNT(*) FROM doctor_profiles) AS total_doctors,
            (SELECT COUNT(*) FROM meals_predictions WHERE outcome_code <> 'NO_RECENT_DATA') AS total_meals_predictions,
            (SELECT COUNT(*) FROM meals_predictions WHERE doctor_flag IS TRUE) AS total_flagged_events,
            (SELECT COUNT(*) FROM interventions) AS total_interventions,
            (SELECT COUNT(*) FROM interventions WHERE cause = 'model_error') AS interventions_by_model_error,
            (SELECT COUNT(*) FROM interventions WHERE cause = 'patient_behavior') AS interventions_by_patient_behavior,
            (SELECT AVG(ABS(forecasted_glucose_mmol - actual_glucose_30m_mmol))
             FROM meals_predictions
             WHERE actual_glucose_30m_mmol IS NOT NULL
               AND forecasted_glucose_mmol IS NOT NULL
               AND outcome_code <> 'NO_RECENT_DATA') AS avg_prediction_mae_mmol,
            (SELECT COUNT(*) FROM meals_predictions
             WHERE outcome_code <> 'NO_RECENT_DATA'
               AND declared_at >= NOW() - INTERVAL '30 days') AS meals_last_30_days;
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE VIEW v_manager_risk_distribution AS
        SELECT risk_class, COUNT(*) AS cnt
        FROM meals_predictions
        WHERE risk_class BETWEEN 1 AND 7
          AND outcome_code <> 'NO_RECENT_DATA'
        GROUP BY risk_class;
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE VIEW v_manager_weekly_interventions AS
        SELECT
            FLOOR(EXTRACT(EPOCH FROM (NOW() - created_at)) / 604800.0)::int AS week_idx,
            COUNT(*) AS total_interventions,
            SUM(CASE WHEN cause = 'model_error' THEN 1 ELSE 0 END) AS model_error_count,
            SUM(CASE WHEN cause = 'patient_behavior' THEN 1 ELSE 0 END) AS patient_behavior_count
        FROM interventions
        WHERE created_at >= NOW() - INTERVAL '28 days'
          AND created_at <= NOW()
        GROUP BY 1;
    """))

    train_salt = os.getenv("TRAINING_PSEUDO_SALT", "default-training-pseudo-salt-override-in-k8s")
    bind.execute(sa.text("SELECT set_config('app.train_salt', :salt, true)"), {"salt": train_salt})
    op.execute(sa.text("""
        DO $$
        BEGIN
            EXECUTE format(
                'CREATE OR REPLACE VIEW v_training_cgm_series AS '
                'SELECT encode(sha256((%L || '':'' || patient_id::text)::bytea), ''hex'') AS pseudo_patient_id, '
                'recorded_at, glucose_mmol, insulin_u, cho_rate FROM cgm_readings',
                current_setting('app.train_salt')
            );
        END
        $$;
    """))

    # Trigger append-only su audit_logs: blocca UPDATE, DELETE e TRUNCATE.
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION prevent_audit_modification()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs è append-only: UPDATE, DELETE e TRUNCATE non consentiti';
        END;
        $$ LANGUAGE plpgsql;
    """))
    op.execute(sa.text("DROP TRIGGER IF EXISTS audit_log_no_modification ON audit_logs;"))
    op.execute(sa.text("""
        CREATE TRIGGER audit_log_no_modification
        BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_logs
        FOR EACH STATEMENT
        EXECUTE FUNCTION prevent_audit_modification();
    """))

    # Permessi per i 6 ruoli DB gestiti dichiarativamente da CloudNativePG (o fixture tests/pg/).
    op.execute(sa.text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;"))

    def _grant_if_exists(role_name: str, statements: Sequence[str]) -> None:
        exists = bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role_name}).first()
        if not exists:
            return
        for stmt in statements:
            op.execute(sa.text(stmt))

    # 1. Identity Zone (saas_iam): solo account/token/audit + patient_code (ZERO nome/email/dati clinici).
    _grant_if_exists("saas_iam", [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM saas_iam;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON users TO saas_iam;",
        "GRANT SELECT, INSERT ON revoked_tokens TO saas_iam;",
        "GRANT SELECT (id, user_id, patient_code) ON patients TO saas_iam;",
        "GRANT SELECT (id), INSERT ON audit_logs TO saas_iam;",
        "GRANT USAGE, SELECT ON SEQUENCE users_id_seq, revoked_tokens_id_seq, audit_logs_id_seq TO saas_iam;",
    ])

    # 2. Application Zone Clinica (saas_clinical): ZERO accesso a users, revoked_tokens, devices.
    _grant_if_exists("saas_clinical", [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM saas_clinical;",
        "GRANT SELECT, INSERT, UPDATE ON patients, meals_predictions, interventions TO saas_clinical;",
        "GRANT SELECT ON doctor_profiles, cgm_readings TO saas_clinical;",
        "GRANT SELECT, INSERT ON audit_logs TO saas_clinical;",
        "GRANT USAGE, SELECT ON SEQUENCE patients_id_seq, meals_predictions_id_seq, interventions_id_seq, audit_logs_id_seq TO saas_clinical;",
    ])

    # 3. Application Zone Manager (saas_manager): SOLO viste aggregate anonime + audit INSERT.
    _grant_if_exists("saas_manager", [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM saas_manager;",
        "GRANT SELECT ON v_manager_overview, v_manager_risk_distribution, v_manager_weekly_interventions TO saas_manager;",
        "GRANT SELECT (id), INSERT ON audit_logs TO saas_manager;",
        "GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO saas_manager;",
    ])

    # 4. DMZ Internal Ingest (saas_ingest): SOLO dispositivi, pazienti non-PII (id/patient_code/targets) e audit.
    _grant_if_exists("saas_ingest", [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM saas_ingest;",
        "GRANT SELECT ON devices TO saas_ingest;",
        "GRANT SELECT ON patients TO saas_ingest;",
        "GRANT SELECT (id), INSERT ON audit_logs TO saas_ingest;",
        "GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO saas_ingest;",
    ])

    # 5. Ingestion Worker (saas_worker): letture CGM + backfill verità a terra + cleanup token scaduti.
    _grant_if_exists("saas_worker", [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM saas_worker;",
        "GRANT SELECT ON patients TO saas_worker;",
        "GRANT SELECT, INSERT ON cgm_readings TO saas_worker;",
        "GRANT SELECT, UPDATE (actual_glucose_30m_mmol) ON meals_predictions TO saas_worker;",
        "GRANT SELECT (id, expires_at), DELETE ON revoked_tokens TO saas_worker;",
        "GRANT USAGE, SELECT ON SEQUENCE cgm_readings_id_seq TO saas_worker;",
    ])

    # 6. Training Zone (saas_trainer): SOLO vista pseudonimizzata v_training_cgm_series.
    _grant_if_exists("saas_trainer", [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM saas_trainer;",
        "GRANT SELECT ON v_training_cgm_series TO saas_trainer;",
    ])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_log_no_modification ON audit_logs;"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_audit_modification();"))
        op.execute(sa.text("DROP VIEW IF EXISTS v_training_cgm_series;"))
        op.execute(sa.text("DROP VIEW IF EXISTS v_manager_weekly_interventions;"))
        op.execute(sa.text("DROP VIEW IF EXISTS v_manager_risk_distribution;"))
        op.execute(sa.text("DROP VIEW IF EXISTS v_manager_overview;"))
    else:
        op.execute(sa.text("DROP VIEW IF EXISTS v_manager_weekly_interventions;"))
        op.execute(sa.text("DROP VIEW IF EXISTS v_manager_risk_distribution;"))
        op.execute(sa.text("DROP VIEW IF EXISTS v_manager_overview;"))

    op.drop_table("audit_logs")
    op.drop_table("revoked_tokens")
    op.drop_table("interventions")
    op.drop_table("meals_predictions")
    op.drop_table("cgm_readings")
    op.drop_table("devices")
    op.drop_table("patients")
    op.drop_table("doctor_profiles")
    op.drop_table("users")
