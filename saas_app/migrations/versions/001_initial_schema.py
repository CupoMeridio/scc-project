"""Initial SaaS schema: tabelle, trigger append-only audit_logs e ruoli DB (saas_migrator/saas_app).

Consolida le ex migrazioni 001 (schema) e 002 (trigger append-only) in un'unica
migrazione iniziale, estendendo il trigger a TRUNCATE e introducendo la separazione
dei ruoli PostgreSQL: saas_migrator (proprietario, solo migrazioni) e saas_app
(permessi minimi CRUD; audit_logs in sola lettura+inserimento).

Revision ID: 001_initial
Revises:
Create Date: 2026-11-26 00:30:00+00:00
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
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("doctor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("patient_code", sa.String(length=32), nullable=False),
        sa.Column("diet_carb_limit_g", sa.Float(), nullable=False),
        sa.Column("target_min_mmol", sa.Float(), nullable=False),
        sa.Column("target_max_mmol", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("patient_code"),
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

    # --- PostgreSQL-only: trigger append-only + ruoli DB con permessi minimi ---
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Trigger append-only su audit_logs: blocca UPDATE, DELETE e TRUNCATE.
    # FOR EACH STATEMENT è più efficiente e copre anche il caso di tabella vuota.
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

    # Ruolo applicativo saas_app: permessi minimi (SELECT/INSERT/UPDATE/DELETE).
    # Su audit_logs: solo SELECT e INSERT (nessuna modifica o cancellazione).
    # La password viene da env var SAAS_APP_PASSWORD (iniettata dal Job di migrazione).
    # Viene passata come parametro di set_config (mai interpolata nel testo SQL) e
    # applicata con format(%L): una password che contiene apici o '$$' non rompe la migrazione.
    app_password = os.getenv("SAAS_APP_PASSWORD", "")
    if app_password:
        bind.execute(sa.text("SELECT set_config('app.saas_app_pw', :pw, true)"), {"pw": app_password})
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'saas_app') THEN
                    EXECUTE format('CREATE ROLE saas_app WITH LOGIN PASSWORD %L',
                                   current_setting('app.saas_app_pw'));
                ELSE
                    EXECUTE format('ALTER ROLE saas_app WITH LOGIN PASSWORD %L',
                                   current_setting('app.saas_app_pw'));
                END IF;
            END
            $$;
        """))

    # Grant sulle tabelle esistenti
    op.execute(sa.text(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO saas_app;"
    ))
    op.execute(sa.text(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO saas_app;"
    ))
    # audit_logs: revoca UPDATE e DELETE (sola lettura + inserimento)
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM saas_app;"))
    # Grant di default per le tabelle create da future migrazioni
    op.execute(sa.text(
        "ALTER DEFAULT PRIVILEGES FOR ROLE saas_migrator IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO saas_app;"
    ))
    op.execute(sa.text(
        "ALTER DEFAULT PRIVILEGES FOR ROLE saas_migrator IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO saas_app;"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("DROP TRIGGER IF EXISTS audit_log_no_modification ON audit_logs;"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS prevent_audit_modification();"))
        op.execute(sa.text("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM saas_app;"))
        op.execute(sa.text("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM saas_app;"))
        op.execute(sa.text("DROP ROLE IF EXISTS saas_app;"))

    op.drop_table("audit_logs")
    op.drop_table("revoked_tokens")
    op.drop_table("interventions")
    op.drop_table("meals_predictions")
    op.drop_table("cgm_readings")
    op.drop_table("devices")
    op.drop_table("patients")
    op.drop_table("users")
