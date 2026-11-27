"""
FILE: tests/pg/test_postgres_security.py
RUOLO: Test di integrazione reali su PostgreSQL 16 (V5): migrazione Alembic senza CREATEROLE,
       matrice permessi dei 6 ruoli DB, viste aggregate Manager, vista pseudonimizzata Training,
       trigger append-only audit_logs e percorso unico manager_service.
"""

import os
import secrets
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from saas_app.seed import seed_database  # noqa: E402
from saas_app.services import manager_service  # noqa: E402

PG_ADMIN_URL = os.getenv("PG_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(
    not PG_ADMIN_URL,
    reason="Richiede PostgreSQL reale via PG_ADMIN_URL (esegui `make test-pg`).",
)

ROLES = (
    "saas_iam",
    "saas_clinical",
    "saas_manager",
    "saas_ingest",
    "saas_worker",
    "saas_trainer",
)


def _with_db(admin_url: str, dbname: str) -> str:
    base, _sep, _old = admin_url.rpartition("/")
    return f"{base}/{dbname}"


def _with_role(admin_url: str, role: str, password: str, dbname: str) -> str:
    # admin_url forma postgresql+psycopg://user:pass@host:port/db
    scheme, rest = admin_url.split("://", 1)
    _creds, host_part = rest.split("@", 1)
    host_only = host_part.rsplit("/", 1)[0]
    return f"{scheme}://{role}:{password}@{host_only}/{dbname}"


@pytest.fixture(scope="module")
def pg_security_env():
    dbname = f"scc_pg_test_{uuid.uuid4().hex[:8]}"
    migrator_pw = "Mig_" + secrets.token_hex(12)
    role_pws = {r: f"Pw_{r}_" + secrets.token_hex(12) for r in ROLES}

    admin_engine = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    with admin_engine.connect() as conn:
        if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname='saas_migrator'")).first():
            conn.execute(text(f"CREATE ROLE saas_migrator WITH LOGIN PASSWORD '{migrator_pw}'"))
        else:
            conn.execute(text(f"ALTER ROLE saas_migrator WITH LOGIN PASSWORD '{migrator_pw}' NOCREATEROLE"))
        for r, pw in role_pws.items():
            if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": r}).first():
                conn.execute(text(f"CREATE ROLE {r} WITH LOGIN PASSWORD '{pw}'"))
            else:
                conn.execute(text(f"ALTER ROLE {r} WITH LOGIN PASSWORD '{pw}'"))
        conn.execute(text(f"CREATE DATABASE {dbname} OWNER saas_migrator"))

    migrator_url = _with_role(PG_ADMIN_URL, "saas_migrator", migrator_pw, dbname)
    alembic_cfg = AlembicConfig(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", migrator_url)

    old_salt = os.environ.get("TRAINING_PSEUDO_SALT")
    os.environ["TRAINING_PSEUDO_SALT"] = "pg-test-salt-" + secrets.token_hex(8)
    try:
        alembic_command.upgrade(alembic_cfg, "head")
        mig_engine = create_engine(migrator_url, future=True)
        MigSession = sessionmaker(bind=mig_engine, autocommit=False, autoflush=False)
        with MigSession() as db:
            seed_database(
                db,
                force_minimal=True,
                patient_pw="TestPatient-2026!",
                doctor_pw="TestDoctor-2026!",
                manager_pw="TestManager-2026!",
            )
        yield dbname, migrator_url, role_pws
    finally:
        if old_salt is None:
            os.environ.pop("TRAINING_PSEUDO_SALT", None)
        else:
            os.environ["TRAINING_PSEUDO_SALT"] = old_salt
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": dbname},
            )
            conn.execute(text(f"DROP DATABASE IF EXISTS {dbname}"))
        admin_engine.dispose()


def _role_engine(role_url: str):
    return create_engine(role_url, future=True)


def _assert_denied(engine, sql: str) -> None:
    with engine.connect() as conn:
        with pytest.raises(Exception):
            conn.execute(text(sql))
        conn.rollback()


def test_pg_least_privilege_matrix(pg_security_env, monkeypatch: pytest.MonkeyPatch):
    dbname, migrator_url, role_pws = pg_security_env

    # 1. saas_manager: legge SOLO le 3 viste aggregate + esegue manager_service reale; vietato tutto il resto.
    mgr_url = _with_role(PG_ADMIN_URL, "saas_manager", role_pws["saas_manager"], dbname)
    mgr_engine = _role_engine(mgr_url)
    MgrSession = sessionmaker(bind=mgr_engine, autocommit=False, autoflush=False)
    try:
        with MgrSession() as db:
            fake_req = SimpleNamespace(state=SimpleNamespace(request_id="pg-test-req"))
            fake_user = SimpleNamespace(id=1, role="manager", username="manager")
            monkeypatch.setattr(manager_service, "collect_ml_pod_metrics", lambda: [])
            dash = manager_service.get_dashboard(db, fake_user, fake_req)
            assert dash.total_patients >= 1
            assert dash.total_doctors >= 2
        for forbidden in (
            "SELECT count(*) FROM users",
            "SELECT count(*) FROM doctor_profiles",
            "SELECT count(*) FROM patients",
            "SELECT count(*) FROM cgm_readings",
            "SELECT count(*) FROM meals_predictions",
            "SELECT count(*) FROM interventions",
            "SELECT count(*) FROM devices",
            "SELECT actor_username, detail_json FROM audit_logs",
            "SELECT count(*) FROM v_training_cgm_series",
        ):
            _assert_denied(mgr_engine, forbidden)
    finally:
        mgr_engine.dispose()

    # 2. saas_clinical: accede al dominio clinico, ZERO accesso a users / revoked_tokens / devices.
    clin_engine = _role_engine(_with_role(PG_ADMIN_URL, "saas_clinical", role_pws["saas_clinical"], dbname))
    try:
        with clin_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM patients")).scalar_one() >= 1
            assert conn.execute(text("SELECT count(*) FROM doctor_profiles")).scalar_one() >= 2
        for forbidden in (
            "SELECT count(*) FROM users",
            "SELECT count(*) FROM revoked_tokens",
            "SELECT count(*) FROM devices",
        ):
            _assert_denied(clin_engine, forbidden)
    finally:
        clin_engine.dispose()

    # 3. saas_iam: gestisce users/revoked_tokens e solo patient_code da patients; zero PII clinica.
    iam_engine = _role_engine(_with_role(PG_ADMIN_URL, "saas_iam", role_pws["saas_iam"], dbname))
    try:
        with iam_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() >= 3
            assert conn.execute(text("SELECT patient_code FROM patients LIMIT 1")).scalar_one()
        for forbidden in (
            "SELECT full_name FROM patients LIMIT 1",
            "SELECT email FROM patients LIMIT 1",
            "SELECT count(*) FROM doctor_profiles",
            "SELECT count(*) FROM cgm_readings",
            "SELECT count(*) FROM meals_predictions",
            "SELECT count(*) FROM devices",
        ):
            _assert_denied(iam_engine, forbidden)
    finally:
        iam_engine.dispose()

    # 4. saas_ingest & saas_worker
    ing_engine = _role_engine(_with_role(PG_ADMIN_URL, "saas_ingest", role_pws["saas_ingest"], dbname))
    wrk_engine = _role_engine(_with_role(PG_ADMIN_URL, "saas_worker", role_pws["saas_worker"], dbname))
    try:
        with ing_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM devices")).scalar_one() >= 1
        _assert_denied(ing_engine, "SELECT count(*) FROM users")
        _assert_denied(ing_engine, "SELECT count(*) FROM cgm_readings")

        with wrk_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM cgm_readings")).scalar_one() >= 1
        _assert_denied(wrk_engine, "SELECT count(*) FROM users")
        _assert_denied(wrk_engine, "SELECT count(*) FROM devices")
    finally:
        ing_engine.dispose()
        wrk_engine.dispose()

    # 5. saas_trainer: legge SOLO v_training_cgm_series con pseudo_patient_id SHA-256 (64 char hex).
    trn_engine = _role_engine(_with_role(PG_ADMIN_URL, "saas_trainer", role_pws["saas_trainer"], dbname))
    try:
        with trn_engine.connect() as conn:
            pid = conn.execute(text("SELECT pseudo_patient_id FROM v_training_cgm_series LIMIT 1")).scalar_one()
            assert len(pid) == 64 and all(c in "0123456789abcdef" for c in pid)
        for forbidden in (
            "SELECT count(*) FROM users",
            "SELECT count(*) FROM patients",
            "SELECT count(*) FROM cgm_readings",
            "SELECT count(*) FROM doctor_profiles",
        ):
            _assert_denied(trn_engine, forbidden)
    finally:
        trn_engine.dispose()

    # 6. Trigger append-only su audit_logs + blocco DROP TRIGGER dai ruoli applicativi
    mig_engine = _role_engine(migrator_url)
    clin_engine2 = _role_engine(_with_role(PG_ADMIN_URL, "saas_clinical", role_pws["saas_clinical"], dbname))
    try:
        for stmt in (
            "UPDATE audit_logs SET action='TAMPERED'",
            "DELETE FROM audit_logs",
            "TRUNCATE TABLE audit_logs",
        ):
            _assert_denied(mig_engine, stmt)
            _assert_denied(clin_engine2, stmt)
        _assert_denied(clin_engine2, "DROP TRIGGER audit_log_no_modification ON audit_logs")
    finally:
        mig_engine.dispose()
        clin_engine2.dispose()
