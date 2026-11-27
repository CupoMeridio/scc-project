"""
FILE: tests/test_saas_api.py
RUOLO: Suite di test per IAM/Cookie HttpOnly, RBAC, revoca, correttezza clinica, servizi e idempotenza.
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker


def _ephemeral_ed25519_pems() -> tuple[str, str]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    pub_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return priv_pem, pub_pem

from app.main import app as ml_app
from saas_app import db as saas_db
from saas_app import repositories as saas_repo
from saas_app.auth import hash_device_key, login_rate_limiter, verify_password
from saas_app.config import SaasSettings, saas_settings
from saas_app.main import app as saas_app
from saas_app.models import AuditLog, CgmReading, Device, MealPrediction, Patient
from saas_app.seed import seed_database
from saas_app.services import doctor_service


@pytest.fixture()
def saas_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "test_saas.db"
    test_engine = saas_db.build_engine(f"sqlite:///{db_file}")
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(saas_db, "engine", test_engine)
    monkeypatch.setattr(saas_db, "SessionLocal", TestSessionLocal)

    from saas_app import main as saas_main
    monkeypatch.setattr(saas_main, "SessionLocal", TestSessionLocal)
    priv_pem, pub_pem = _ephemeral_ed25519_pems()
    monkeypatch.setattr(saas_settings, "SAAS_ENV", "test")
    monkeypatch.setattr(saas_settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(saas_settings, "JWT_PRIVATE_KEY_PEM", priv_pem)
    monkeypatch.setattr(saas_settings, "JWT_PUBLIC_KEY_PEM", pub_pem)
    monkeypatch.setattr(saas_settings, "IAM_PROVISION_TOKEN", "p" * 40)
    monkeypatch.setattr(saas_settings, "DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    saas_settings.validate_security_secrets()
    login_rate_limiter.reset()

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    alembic_cfg = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    alembic_command.upgrade(alembic_cfg, "head")
    with TestSessionLocal() as session:
        device_keys = seed_database(
            session,
            force_minimal=True,
            patient_pw="TestPatient-2026!",
            doctor_pw="TestDoctor-2026!",
            manager_pw="TestManager-2026!",
        )

    with TestClient(ml_app) as ml_tc:
        def fake_call_ml_predict(patient_code: str, glucose_history, exogenous_inputs):
            resp = ml_tc.post(
                "/predict",
                json={
                    "patient_id": f"patient_{patient_code}",
                    "glucose_history": glucose_history,
                    "exogenous_inputs": exogenous_inputs,
                },
            )
            assert resp.status_code == 200
            from saas_app.clinical import MlPredictResponse
            return MlPredictResponse.model_validate(resp.json())

        def fake_collect_metrics():
            return [ml_tc.get("/metrics").json()]

        from saas_app.services import manager_service, patient_service
        monkeypatch.setattr(patient_service, "call_ml_predict", fake_call_ml_predict)
        monkeypatch.setattr(manager_service, "collect_ml_pod_metrics", fake_collect_metrics)
        # doctor_service apre una Session separata per l'audit degli accessi negati.
        monkeypatch.setattr(doctor_service, "SessionLocal", TestSessionLocal)

        yield saas_app, TestSessionLocal, monkeypatch, device_keys


def _login_client(app, username: str, password: str) -> TestClient:
    client = TestClient(app)
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    assert saas_settings.COOKIE_NAME in client.cookies
    return client


def test_fail_fast_on_missing_secrets_in_production():
    cfg = SaasSettings()
    cfg.SAAS_ENV = "production"
    cfg.SERVICE_MODE = "iam"
    cfg.JWT_PRIVATE_KEY_PEM = ""
    with pytest.raises(RuntimeError):
        cfg.validate_security_secrets()


def test_rbac_cookie_logout_revocation_and_rate_limit(saas_env):
    app, _, _, _ = saas_env

    anon = TestClient(app)
    assert anon.get("/api/patient/summary").status_code == 401
    assert anon.get("/api/doctor/patients").status_code == 401
    assert anon.get("/api/manager/dashboard").status_code == 401

    # Password > 72 byte rifiutata con 422
    too_long = anon.post("/api/auth/login", json={"username": "manager", "password": "A" * 73})
    assert too_long.status_code == 422

    p_client = _login_client(app, "patient_591", "TestPatient-2026!")
    d_client = _login_client(app, "dr.rossi", "TestDoctor-2026!")
    m_client = _login_client(app, "manager", "TestManager-2026!")

    # Isolamento RBAC tramite cookie HttpOnly
    assert p_client.get("/api/doctor/patients").status_code == 403
    assert p_client.get("/api/manager/dashboard").status_code == 403
    assert d_client.get("/api/patient/summary").status_code == 403
    assert d_client.get("/api/manager/dashboard").status_code == 403
    assert m_client.get("/api/patient/summary").status_code == 403
    assert m_client.get("/api/doctor/patients").status_code == 403

    # Verifica revoca token al logout anche se il client conserva il vecchio cookie
    saved_cookie = p_client.cookies.get(saas_settings.COOKIE_NAME)
    out = p_client.post("/api/auth/logout")
    assert out.status_code == 200
    assert p_client.get("/api/auth/me").status_code == 401
    replay = TestClient(app, cookies={saas_settings.COOKIE_NAME: saved_cookie})
    assert replay.get("/api/auth/me").status_code == 401

    # Rate limit su /api/auth/login
    login_rate_limiter.reset()
    for _ in range(12):
        r = anon.post("/api/auth/login", json={"username": "manager", "password": "wrongpassword"})
        assert r.status_code == 401
    blocked = anon.post("/api/auth/login", json={"username": "manager", "password": "wrongpassword"})
    assert blocked.status_code == 429


def test_password_change_flow_and_session_invalidation(saas_env):
    """Il cambio password è forzato per i pazienti e invalida tutte le sessioni precedenti."""
    app, _, _, _ = saas_env
    client = TestClient(app)
    res = client.post("/api/auth/login", json={"username": "patient_591", "password": "TestPatient-2026!"})
    assert res.status_code == 200
    assert res.json()["user"]["must_change_password"] is True
    assert client.get("/api/auth/me").json()["must_change_password"] is True

    old_cookie = client.cookies.get(saas_settings.COOKIE_NAME)

    # Vecchia password errata → 401
    bad_old = client.post(
        "/api/auth/change-password",
        json={"old_password": "PasswordSbagliata1", "new_password": "NuovaPass456!"},
    )
    assert bad_old.status_code == 401

    # Nuova password troppo comune → 422
    common = client.post(
        "/api/auth/change-password",
        json={"old_password": "TestPatient-2026!", "new_password": "password"},
    )
    assert common.status_code == 422

    # Nuova password troppo corta → 422 (policy schema)
    too_short = client.post(
        "/api/auth/change-password",
        json={"old_password": "TestPatient-2026!", "new_password": "corta"},
    )
    assert too_short.status_code == 422

    # Cambio valido → 200, flag rimosso
    new_pw = "NuovaPass456!"
    ok = client.post(
        "/api/auth/change-password",
        json={"old_password": "TestPatient-2026!", "new_password": new_pw},
    )
    assert ok.status_code == 200
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["must_change_password"] is False

    # Le sessioni precedenti sono invalidate (claim pwdv non più valido)
    replay = TestClient(app, cookies={saas_settings.COOKIE_NAME: old_cookie})
    assert replay.get("/api/auth/me").status_code == 401

    # La vecchia password non funziona più, la nuova sì
    anon = TestClient(app)
    assert anon.post("/api/auth/login", json={"username": "patient_591", "password": "TestPatient-2026!"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "patient_591", "password": new_pw}).status_code == 200


def test_must_change_password_enforced_server_side(saas_env):
    """N3: con must_change_password attivo il server blocca tutto tranne /me, /change-password e /logout."""
    app, _, _, _ = saas_env
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": "patient_591", "password": "TestPatient-2026!"}).status_code == 200

    # La regola è imposta dal server (regola 14), non solo dal frontend.
    blocked = client.get("/api/patient/summary")
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert client.get("/api/patient/history").status_code == 403

    # I percorsi necessari al cambio restano accessibili.
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200

    # Ripetiamo il login (il logout ha revocato il token) e completiamo il cambio.
    assert client.post("/api/auth/login", json={"username": "patient_591", "password": "TestPatient-2026!"}).status_code == 200
    ok = client.post(
        "/api/auth/change-password",
        json={"old_password": "TestPatient-2026!", "new_password": "PasswordNuova456!"},
    )
    assert ok.status_code == 200
    # Dopo il cambio la navigazione torna normale.
    assert client.get("/api/patient/summary").status_code == 200


def test_register_patient_returns_server_generated_password_once(saas_env, caplog):
    """N3/Q5: la password è generata dal server, restituita una sola volta e mai scritta nei log o nell'audit."""
    app, SessionLocal, _, _ = saas_env
    d_client = _login_client(app, "dr.rossi", "TestDoctor-2026!")

    # Il payload non accetta più una password scelta dal medico: il campo extra viene ignorato.
    with caplog.at_level("DEBUG"):
        res = d_client.post(
            "/api/doctor/patients",
            json={
                "full_name": "Giulia Test",
                "username": "patient_777",
                "email": "giulia@diabetes-saas.local",
                "patient_code": "777",
                "diet_carb_limit_g": 60,
                "password": "PasswordSceltaDalMedico1!",
            },
        )
    assert res.status_code == 201, res.text
    body = res.json()
    temp_password = body["temporary_password"]
    assert len(temp_password) >= 16

    # La password scelta dal medico non è mai stata usata: il login funziona solo con quella temporanea.
    anon = TestClient(app)
    login_rate_limiter.reset()
    assert anon.post("/api/auth/login", json={"username": "patient_777", "password": "PasswordSceltaDalMedico1!"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "patient_777", "password": temp_password}).status_code == 200

    # In DB resta solo l'hash bcrypt, mai il testo in chiaro.
    with SessionLocal() as db:
        user = saas_repo.get_user_by_username(db, "patient_777")
        assert user is not None and user.must_change_password is True
        assert temp_password not in user.password_hash
        assert verify_password(temp_password, user.password_hash)

        # La password temporanea non deve comparire in nessun evento di audit.
        audit_rows = db.execute(select(AuditLog)).scalars().all()
        for row in audit_rows:
            assert temp_password not in row.action

    # Né nei log applicativi.
    assert temp_password not in caplog.text


def test_login_account_rate_limit_isolated_from_ip(saas_env):
    """Il limite per account scatta anche cambiando IP ad ogni tentativo."""
    app, _, _, _ = saas_env
    login_rate_limiter.reset()
    client = TestClient(app)
    for i in range(12):
        r = client.post(
            "/api/auth/login",
            json={"username": "manager", "password": "wrongpassword"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        assert r.status_code == 401
    blocked = client.post(
        "/api/auth/login",
        json={"username": "manager", "password": "wrongpassword"},
        headers={"X-Forwarded-For": "203.0.113.200"},
    )
    assert blocked.status_code == 429


def test_timing_attack_mitigation_hashes_unknown_user(saas_env, monkeypatch: pytest.MonkeyPatch):
    """Anche con utente inesistente bcrypt viene eseguito (nessun oracolo temporale)."""
    app, _, _, _ = saas_env
    from saas_app.routers import auth as auth_router

    calls = []
    real_verify = auth_router.verify_password

    def spy(password, password_hash):
        calls.append(password_hash)
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth_router, "verify_password", spy)
    anon = TestClient(app)
    r = anon.post("/api/auth/login", json={"username": "utente_inesistente", "password": "Qualunque123!"})
    assert r.status_code == 401
    assert len(calls) == 1


def test_jwt_algorithm_and_inactive_user_rejected(saas_env):
    """Token con algoritmo diverso da EdDSA (es. confusione HMAC con chiave pubblica) e utenti disattivati vengono rifiutati."""
    app, SessionLocal, _, _ = saas_env

    import jwt as pyjwt
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    forged = pyjwt.encode(
        {
            "sub": "1",
            "jti": "forged-jti",
            "username": "manager",
            "role": "manager",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        },
        "symmetric-key-confusion-attempt-32chars",
        algorithm="HS256",
    )
    forged_client = TestClient(app, cookies={saas_settings.COOKIE_NAME: forged})
    assert forged_client.get("/api/auth/me").status_code == 401

    # Utente disattivato: sessione esistente 403 e nuovo login 403
    p_client = _login_client(app, "patient_591", "TestPatient-2026!")
    with SessionLocal() as db:
        user = saas_repo.get_user_by_username(db, "patient_591")
        user.is_active = False
        db.commit()
    assert p_client.get("/api/auth/me").status_code == 403
    anon = TestClient(app)
    assert anon.post("/api/auth/login", json={"username": "patient_591", "password": "TestPatient-2026!"}).status_code == 403


def test_patient_meal_custom_targets_and_atomic_audit(saas_env):
    app, SessionLocal, _, _ = saas_env
    p_client = _login_client(app, "patient_591", "TestPatient-2026!")
    # N3: al primo accesso il server impone il cambio della password temporanea.
    assert p_client.post(
        "/api/auth/change-password",
        json={"old_password": "TestPatient-2026!", "new_password": "Paziente591Nuova!"},
    ).status_code == 200
    d_bianchi = _login_client(app, "dr.bianchi", "TestDoctor-2026!")

    summary = p_client.get("/api/patient/summary")
    assert summary.status_code == 200
    assert summary.json()["patient_code"] == "591"

    # Il medico abbassa target_max_mmol a 6.5 -> la previsione sopra 6.5 deve attivare RISK_OF_HYPERGLYCEMIA
    patients = d_bianchi.get("/api/doctor/patients").json()
    p591_id = next(p["patient_id"] for p in patients if p["patient_code"] == "591")
    assert d_bianchi.patch(
        f"/api/doctor/patients/{p591_id}/diet",
        json={"diet_carb_limit_g": 60.0, "target_min_mmol": 4.0, "target_max_mmol": 6.5},
    ).status_code == 200

    meal = p_client.post("/api/patient/meals", json={"carbs_g": 80.0, "insulin_u": 1.0})
    assert meal.status_code == 200
    data = meal.json()
    if data["forecasted_glucose_mmol_l"] >= 6.5:
        assert data["outcome_code"] == "RISK_OF_HYPERGLYCEMIA"
        assert data["doctor_flag"] is True

    for rng in ("week", "month", "year"):
        hist = p_client.get(f"/api/patient/history?range={rng}")
        assert hist.status_code == 200
        assert len(hist.json()["readings"]) > 0

    with SessionLocal() as db:
        audits = db.query(AuditLog).all()
        assert any(a.action == "PATIENT_DECLARE_MEAL" for a in audits)


def test_doctor_ownership_conflict_and_intervention(saas_env):
    app, SessionLocal, _, _ = saas_env
    rossi = _login_client(app, "dr.rossi", "TestDoctor-2026!")
    bianchi = _login_client(app, "dr.bianchi", "TestDoctor-2026!")

    rossi_codes = {p["patient_code"] for p in rossi.get("/api/doctor/patients").json()}
    assert "540" in rossi_codes and "591" not in rossi_codes

    p591_id = next(p["patient_id"] for p in bianchi.get("/api/doctor/patients").json() if p["patient_code"] == "591")
    # P6: accesso a paziente non assegnato → 404 (non rivela esistenza) + audit DOCTOR_ACCESS_DENIED
    denied = rossi.get(f"/api/doctor/patients/{p591_id}")
    assert denied.status_code == 404
    with SessionLocal() as db:
        denied_audits = db.query(AuditLog).filter(AuditLog.action == "DOCTOR_ACCESS_DENIED").all()
        assert len(denied_audits) >= 1
        assert denied_audits[-1].target_patient_id == p591_id

    # Paziente inesistente: stesso 404, non rivela esistenza
    assert rossi.get("/api/doctor/patients/99999").status_code == 404

    iv_res = bianchi.post(
        f"/api/doctor/patients/{p591_id}/interventions",
        json={
            "intervention_type": "diet_change",
            "cause": "patient_behavior",
            "notes": "Ridotti carboidrati a cena.",
            "new_diet_carb_limit_g": 45.0,
        },
    )
    assert iv_res.status_code == 201
    detail = bianchi.get(f"/api/doctor/patients/{p591_id}").json()
    assert detail["diet_carb_limit_g"] == 45.0

    # Registrazione nuovo paziente + errore 409 su duplicato (la password è generata dal server, Q5/N3)
    new_payload = {
        "username": "patient_999",
        "email": "p999@diabetes-saas.local",
        "full_name": "Nuovo Paziente Test",
        "patient_code": "999",
        "diet_carb_limit_g": 55.0,
    }
    reg = rossi.post("/api/doctor/patients", json=new_payload)
    assert reg.status_code == 201
    assert rossi.post("/api/doctor/patients", json=new_payload).status_code == 409

    # Il paziente accede con la password temporanea e la cambia al primo accesso (N3).
    temp_password = reg.json()["temporary_password"]
    new_p = _login_client(app, "patient_999", temp_password)
    assert new_p.post(
        "/api/auth/change-password",
        json={"old_password": temp_password, "new_password": "Paziente999Nuova!"},
    ).status_code == 200
    meal = new_p.post("/api/patient/meals", json={"carbs_g": 40.0, "insulin_u": 1.0})
    assert meal.status_code == 200
    assert meal.json()["outcome_code"] == "NO_RECENT_DATA"


def test_no_recent_data_uses_null_and_is_excluded_from_stats(saas_env):
    """N5: il pasto senza dati recenti ha valori nulli (non 0.0) e non entra in conteggi, MAE, distribuzione e bias."""
    app, SessionLocal, _, _ = saas_env
    manager = _login_client(app, "manager", "TestManager-2026!")
    doctor = _login_client(app, "dr.rossi", "TestDoctor-2026!")

    # Paziente nuovo, nessuna lettura CGM: ogni pasto ricade in NO_RECENT_DATA.
    reg = doctor.post(
        "/api/doctor/patients",
        json={
            "username": "patient_888",
            "email": "p888@diabetes-saas.local",
            "full_name": "Paziente Senza Sensore",
            "patient_code": "888",
            "diet_carb_limit_g": 60,
        },
    )
    assert reg.status_code == 201, reg.text
    temp_password = reg.json()["temporary_password"]
    p_client = _login_client(app, "patient_888", temp_password)
    assert p_client.post(
        "/api/auth/change-password",
        json={"old_password": temp_password, "new_password": "SenzaSensore123!"},
    ).status_code == 200

    before = manager.get("/api/manager/dashboard").json()

    meal = p_client.post("/api/patient/meals", json={"carbs_g": 45.0, "insulin_u": 1.0})
    assert meal.status_code == 200
    body = meal.json()
    assert body["outcome_code"] == "NO_RECENT_DATA"
    # Nessun 0.0 inventato: i valori mancanti sono nulli.
    assert body["current_glucose_mmol_l"] is None
    assert body["current_glucose_mg_dl"] is None
    assert body["forecasted_glucose_mmol_l"] is None
    assert body["forecasted_glucose_mg_dl"] is None
    assert body["risk_class"] is None

    history = p_client.get("/api/patient/history?range=week").json()
    record = next(m for m in history["meals"] if m["outcome_code"] == "NO_RECENT_DATA")
    assert record["current_glucose_mmol"] is None
    assert record["forecasted_glucose_mmol"] is None
    assert record["risk_class"] is None

    # Il record NO_RECENT_DATA non entra nelle statistiche aggregate del manager.
    after = manager.get("/api/manager/dashboard").json()
    assert after["total_meals_predictions"] == before["total_meals_predictions"]
    assert after["aggregated_risk_distribution"] == before["aggregated_risk_distribution"]
    assert after["avg_prediction_mae_mmol"] == before["avg_prediction_mae_mmol"]

    # Anche un record NO_RECENT_DATA "sporco" (con actual/forecast valorizzati, come farebbe
    # un backfill errato) deve restare fuori da MAE, distribuzione e bias: lo inseriamo a mano.
    with SessionLocal() as db:
        patient = saas_repo.get_patient_by_code(db, "888")
        now = datetime.now(timezone.utc)
        db.add(
            MealPrediction(
                patient_id=patient.id,
                declared_at=now,
                carbs_g=30.0,
                insulin_u=1.0,
                current_glucose_mmol=None,
                forecasted_glucose_mmol=99.0,
                actual_glucose_30m_mmol=1.0,
                risk_class=None,
                outcome_code="NO_RECENT_DATA",
                outcome_message="record sporco di test",
                doctor_flag=True,
                model_version="none",
            )
        )
        # Un pasto reale con coppia previsione/reale, per il calcolo del bias.
        db.add(
            MealPrediction(
                patient_id=patient.id,
                declared_at=now,
                carbs_g=40.0,
                insulin_u=1.5,
                current_glucose_mmol=6.0,
                forecasted_glucose_mmol=8.0,
                actual_glucose_30m_mmol=6.0,
                risk_class=4,
                outcome_code="PROCEED_REGULAR",
                outcome_message="ok",
                doctor_flag=False,
                model_version="1.0.0",
            )
        )
        db.commit()

    after2 = manager.get("/api/manager/dashboard").json()
    # Solo il pasto reale entra: il conteggio cresce di 1 e la classe 4 di 1.
    assert after2["total_meals_predictions"] == before["total_meals_predictions"] + 1
    assert (
        after2["aggregated_risk_distribution"]["4"]
        == before["aggregated_risk_distribution"]["4"] + 1
    )

    # Bias del medico per il paziente 888: solo il pasto reale (8.0 - 6.0 = 2.0 mmol/L).
    patients = doctor.get("/api/doctor/patients").json()
    p888_id = next(p["patient_id"] for p in patients if p["patient_code"] == "888")
    detail = doctor.get(f"/api/doctor/patients/{p888_id}").json()
    assert detail["trends"]["mean_prediction_error_mmol"] == 2.0


def test_p9_ground_truth_backfill_and_optional_mae(saas_env):
    """P9: il worker riempie actual_glucose_30m_mmol a +30 min; senza coppie MAE e bias sono nulli."""
    app, SessionLocal, _, _ = saas_env
    from saas_app.schemas import CgmIngestRequest
    from saas_app.services.ingestion_service import persist_sample

    manager = _login_client(app, "manager", "TestManager-2026!")
    doctor = _login_client(app, "dr.rossi", "TestDoctor-2026!")

    # Nessuna coppia previsione/reale nel database: il MAE non è 0.0 ma nullo.
    with SessionLocal() as db:
        db.execute(delete(MealPrediction))
        db.commit()
    assert manager.get("/api/manager/dashboard").json()["avg_prediction_mae_mmol"] is None

    # Nuovo paziente con un pasto dichiarato 30 minuti fa e senza verità a terra.
    reg = doctor.post(
        "/api/doctor/patients",
        json={
            "username": "patient_901",
            "email": "p901@diabetes-saas.local",
            "full_name": "Paziente Backfill",
            "patient_code": "901",
            "diet_carb_limit_g": 60,
        },
    )
    assert reg.status_code == 201, reg.text
    patient_id = reg.json()["patient_id"]

    with SessionLocal() as db:
        patient = saas_repo.get_patient_by_code(db, "901")
        db.add(
            MealPrediction(
                patient_id=patient.id,
                declared_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                carbs_g=40.0,
                insulin_u=1.0,
                current_glucose_mmol=6.0,
                forecasted_glucose_mmol=8.0,
                actual_glucose_30m_mmol=None,
                risk_class=4,
                outcome_code="PROCEED_REGULAR",
                outcome_message="ok",
                doctor_flag=False,
                model_version="1.0.0",
            )
        )
        db.commit()

    # Senza verità a terra il MAE resta nullo e il modello non è dichiarato "allineato".
    assert manager.get("/api/manager/dashboard").json()["avg_prediction_mae_mmol"] is None
    trends = doctor.get(f"/api/doctor/patients/{patient_id}").json()["trends"]
    assert trends["mean_prediction_error_mmol"] is None
    assert "Dati insufficienti" in trends["model_bias_label"]

    # Il worker salva la lettura CGM a +30 min: la verità a terra viene assegnata.
    with SessionLocal() as db:
        patient = saas_repo.get_patient_by_code(db, "901")
        persist_sample(
            db,
            CgmIngestRequest(
                patient_code="901", glucose_mmol=6.0, recorded_at=datetime.now(timezone.utc)
            ),
            patient,
        )
        refreshed = db.execute(
            select(MealPrediction).where(MealPrediction.patient_id == patient.id)
        ).scalar_one()
        assert refreshed.actual_glucose_30m_mmol == 6.0

    # Coppia previsione 8.0 / reale 6.0: MAE e bias attesi 2.0 mmol/L.
    assert manager.get("/api/manager/dashboard").json()["avg_prediction_mae_mmol"] == 2.0
    trends = doctor.get(f"/api/doctor/patients/{patient_id}").json()["trends"]
    assert trends["mean_prediction_error_mmol"] == 2.0


def test_manager_dashboard_privacy_and_offline_sla(saas_env):
    app, _, monkeypatch, _ = saas_env
    m_client = _login_client(app, "manager", "TestManager-2026!")

    res = m_client.get("/api/manager/dashboard")
    assert res.status_code == 200
    body_text = json.dumps(res.json())
    assert res.json()["service_available"] is True
    assert res.json()["ml_pods_online"] == 1
    assert "Marco Ferri" not in body_text
    assert "Alessandro Bruno" not in body_text
    assert "patient_591" not in body_text

    # Quando 0 pod ML sono raggiungibili, SLA non deve MAI risultare 100%
    def no_pods():
        return []

    from saas_app.services import manager_service
    monkeypatch.setattr(manager_service, "collect_ml_pod_metrics", no_pods)
    res_down = m_client.get("/api/manager/dashboard")
    assert res_down.status_code == 200
    assert res_down.json()["service_available"] is False
    assert res_down.json()["ml_pods_online"] == 0
    assert res_down.json()["overall_sla_compliance_rate"] == 0.0


def test_cgm_ingestion_idempotency(saas_env):
    app, SessionLocal, _, device_keys = saas_env
    client = TestClient(app)
    # Senza chiave dispositivo → 401
    assert client.post("/api/cgm/ingest", json={"patient_code": "591", "glucose_mmol": 6.5}).status_code == 401

    # Chiave reale generata dal seed (casuale, mostrata una sola volta): N1.
    device_key = device_keys["patient_591"]
    headers = {"X-Device-Key": device_key}

    # Timestamp valido (now)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    r1 = client.post(
        "/api/cgm/ingest",
        json={"patient_code": "591", "glucose_mmol": 6.8, "insulin_u": 1.0, "cho_rate": 0.0, "recorded_at": ts},
        headers=headers,
    )
    # Secondo invio con stesso timestamp ma valore diverso → immutabile, primo resta
    r2 = client.post(
        "/api/cgm/ingest",
        json={"patient_code": "591", "glucose_mmol": 7.1, "insulin_u": 1.0, "cho_rate": 0.0, "recorded_at": ts},
        headers=headers,
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r2.json()["status"] == "duplicate"

    with SessionLocal() as db:
        rows = db.query(CgmReading).filter(CgmReading.glucose_mmol == 7.1).all()
        assert len(rows) == 0  # il secondo non ha sovrascritto
        rows_orig = db.query(CgmReading).filter(CgmReading.glucose_mmol == 6.8).all()
        assert len(rows_orig) == 1


def test_seed_device_keys_are_random_and_only_hashed(tmp_path):
    """N1: il seed genera chiavi casuali (>=43 char) e in DB salva solo lo SHA-256 (regola 17)."""
    generated = []
    for run in range(2):
        engine = saas_db.build_engine(f"sqlite:///{tmp_path / f'seed_keys_{run}.db'}")
        saas_db.Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        with SessionLocal() as db:
            keys = seed_database(
                db,
                force_minimal=True,
                patient_pw="TestPatient-2026!",
                doctor_pw="TestDoctor-2026!",
                manager_pw="TestManager-2026!",
            )
            generated.append(keys)
            patient = saas_repo.get_patient_by_code(db, "591")
            device = saas_repo.get_device_by_patient_id(db, patient.id)
            # Nel DB è salvato solo l'hash: la chiave in chiaro non compare mai.
            assert device.key_hash == hash_device_key(keys["patient_591"])
            assert keys["patient_591"] not in device.key_hash

    # Ogni chiave è lunga almeno 43 caratteri (32 byte base64url senza padding).
    for key in (*generated[0].values(), *generated[1].values()):
        assert len(key) >= 43

    # Due seed indipendenti producono chiavi diverse: nessuna chiave è derivabile da codice/username.
    assert generated[0]["patient_591"] != generated[1]["patient_591"]


def test_derived_device_key_is_rejected(saas_env):
    """N1: una chiave derivabile da codice+username (vecchio schema) viene rifiutata con 401."""
    app, _, _, _ = saas_env
    client = TestClient(app)
    res = client.post(
        "/api/cgm/ingest",
        json={"patient_code": "591", "glucose_mmol": 6.5},
        headers={"X-Device-Key": "device-key-591-patient_591"},
    )
    assert res.status_code == 401


def test_cgm_upsert_is_atomic_under_concurrency(saas_env):
    """Due worker concorrenti con la stessa chiave non devono generare duplicati né 500."""
    _, SessionLocal, _, _ = saas_env
    with SessionLocal() as db:
        patient_id = db.execute(
            select(Patient.id).where(Patient.patient_code == "591")
        ).scalar_one()

    ts = datetime(2026, 11, 26, 3, 0, tzinfo=timezone.utc)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def write(value: float) -> None:
        try:
            with SessionLocal() as db:
                barrier.wait(timeout=10)
                saas_repo.insert_cgm_reading(db, patient_id, ts, value, 1.0, 0.0)
                db.commit()
        except Exception as exc:  # pragma: no cover - solo diagnostico
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(v,)) for v in (6.1, 9.4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"errori durante upsert concorrente: {errors}"
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(CgmReading).where(
                    CgmReading.patient_id == patient_id,
                    CgmReading.recorded_at == ts,
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


def test_alembic_migrations_and_queued_worker(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    from saas_app import worker
    from saas_app.services import ingestion_service

    db_file = tmp_path / "alembic_test.db"
    mig_engine = saas_db.build_engine(f"sqlite:///{db_file}")
    MigSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=mig_engine)

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(cfg, "head")

    tables = set(inspect(mig_engine).get_table_names())
    assert {"users", "patients", "devices", "cgm_readings", "meals_predictions", "interventions", "revoked_tokens", "audit_logs"}.issubset(tables)

    monkeypatch.setattr(saas_db, "engine", mig_engine)
    monkeypatch.setattr(saas_db, "SessionLocal", MigSessionLocal)
    monkeypatch.setattr(worker, "SessionLocal", MigSessionLocal)
    monkeypatch.setattr(saas_settings, "SAAS_ENV", "test")
    saas_settings.validate_security_secrets()

    with MigSessionLocal() as db:
        device_keys = seed_database(
            db,
            force_minimal=True,
            patient_pw="TestPatient-2026!",
            doctor_pw="TestDoctor-2026!",
            manager_pw="TestManager-2026!",
        )

    queued_events = []

    def fake_enqueue(event):
        queued_events.append(event)

    monkeypatch.setattr(saas_settings, "REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setattr(ingestion_service, "enqueue_cgm_sample", fake_enqueue)

    from saas_app import main as saas_main
    monkeypatch.setattr(saas_main, "SessionLocal", MigSessionLocal)
    monkeypatch.setattr(doctor_service, "SessionLocal", MigSessionLocal)

    ts = datetime.now(timezone.utc).isoformat()
    client = TestClient(saas_app)
    resp = client.post(
        "/api/cgm/ingest",
        json={"patient_code": "591", "glucose_mmol": 8.4, "insulin_u": 1.2, "cho_rate": 0.5, "recorded_at": ts},
        headers={"X-Device-Key": device_keys["patient_591"]},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert len(queued_events) == 1

    # Il worker consuma l'envelope in coda e lo salva in modo idempotente in DB
    from saas_app import queue_client as qc

    worker.process_raw_message(qc.encode_event(queued_events[0]))
    worker.process_raw_message(qc.encode_event(queued_events[0]))

    with MigSessionLocal() as db:
        saved = db.query(CgmReading).filter(CgmReading.glucose_mmol == 8.4).all()
        assert len(saved) == 1


class _FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def lrem(self, *args):
        self.ops.append(("lrem", args))
        return self

    def rpush(self, *args):
        self.ops.append(("rpush", args))
        return self

    def execute(self):
        return [getattr(self.client, op)(*args) for op, args in self.ops]


class _FakeRedis:
    """MinimoRedis in memoria per testare ack/retry/DLQ senza server Redis."""

    def __init__(self):
        self.lists = {}

    def _list(self, name):
        return self.lists.setdefault(name, [])

    def rpush(self, name, value):
        self._list(name).append(value)
        return len(self._list(name))

    def brpoplpush(self, source, destination, timeout=5):
        if not self._list(source):
            return None
        value = self._list(source).pop(0)
        self._list(destination).append(value)
        return value

    def lrem(self, name, count, value):
        items = self._list(name)
        if value in items:
            items.remove(value)
            return 1
        return 0

    def lrange(self, name, start, end):
        items = self._list(name)
        return list(items[start:] if end == -1 else items[start : end + 1])

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


def test_queue_ack_retry_dlq_and_crash_recovery():
    """Nessun messaggio va perso: ack, retry e DLQ dopo 3 tentativi."""
    from saas_app import queue_client as qc

    client = _FakeRedis()
    queue = saas_settings.REDIS_QUEUE_NAME
    event = {"patient_code": "591", "glucose_mmol": 6.5}

    client.rpush(queue, qc.encode_event(event))
    raw = qc.claim_for_processing(client, timeout=0)
    assert raw is not None
    assert len(client._list(qc.INFLIGHT_QUEUE_NAME)) == 1
    qc.ack_processed(client, raw)
    assert client._list(qc.INFLIGHT_QUEUE_NAME) == []

    # Primo fallimento: un nuovo evento viene reclamato e torna in coda con attempt=1
    client.rpush(queue, qc.encode_event(event))
    raw = qc.claim_for_processing(client, timeout=0)
    assert raw is not None
    assert qc.requeue_failed(client, raw) is True
    assert len(client._list(queue)) == 1
    assert qc.decode_envelope(client._list(queue)[0])[1] == 1

    # Altri due fallimenti: dopo MAX_PROCESS_ATTEMPTS finisce nella DLQ
    for expected_attempt in (2, 3):
        raw = qc.claim_for_processing(client, timeout=0)
        requeued = qc.requeue_failed(client, raw)
        assert requeued is (expected_attempt < qc.MAX_PROCESS_ATTEMPTS)
    assert client._list(queue) == []
    assert len(client._list(qc.DLQ_QUEUE_NAME)) == 1
    assert qc.decode_envelope(client._list(qc.DLQ_QUEUE_NAME)[0])[1] == 3

    # Un crash lascia il messaggio in-flight: al riavvio torna in coda
    orphan = qc.encode_event(event, 1)
    client.rpush(qc.INFLIGHT_QUEUE_NAME, orphan)
    assert qc.recover_inflight(client) == 1
    assert client._list(qc.INFLIGHT_QUEUE_NAME) == []
    assert client._list(queue) == [orphan]


def test_slow_db_does_not_block_health(saas_env):
    """P8: un DB lento in un endpoint `def` non deve bloccare /api/health."""
    app, _, monkeypatch, _ = saas_env
    from saas_app.services import patient_service

    original = patient_service.repo.get_patient_by_user_id

    def slow_lookup(db, user_id):
        time.sleep(0.4)
        return original(db, user_id)

    monkeypatch.setattr(patient_service.repo, "get_patient_by_user_id", slow_lookup)

    client = TestClient(app)
    assert client.post(
        "/api/auth/login", json={"username": "patient_591", "password": "TestPatient-2026!"}
    ).status_code == 200

    slow_call = threading.Thread(target=lambda: client.get("/api/patient/summary"))
    slow_call.start()
    time.sleep(0.05)

    start = time.perf_counter()
    health = client.get("/api/health")
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    slow_call.join(timeout=10)

    assert health.status_code == 200
    assert elapsed_ms < 100.0, f"/api/health ha impiegato {elapsed_ms:.1f} ms"


def test_history_is_downsampled_for_month_and_year():
    """P11: month/year non devono restituire una lettura ogni 5 minuti."""
    from types import SimpleNamespace

    from saas_app.services.patient_service import _cgm_points

    base = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    readings = [
        SimpleNamespace(
            recorded_at=base + timedelta(minutes=5 * i),
            glucose_mmol=6.0 + i / 100.0,
            insulin_u=1.0,
            cho_rate=0.0,
        )
        for i in range(120)
    ]

    assert len(_cgm_points(readings, "week")) == 120
    assert len(_cgm_points(readings, "month")) == 11  # 00:05..10:00 -> 11 bucket orari
    assert len(_cgm_points(readings, "year")) == 1  # un solo giorno -> 1 bucket giornaliero


def test_p3_security_headers_and_405_on_write_methods(saas_env):
    """P3: gli header di sicurezza devono essere presenti sulle risposte di /api e i metodi
    di scrittura non ammessi devono ricevere 405 (il filtro metodi NON è più nell'Ingress)."""
    app, _, _, _ = saas_env
    client = TestClient(app)

    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'none'" in res.headers["Content-Security-Policy"]
    assert "max-age=31536000" in res.headers["Strict-Transport-Security"]
    assert res.headers.get("X-Request-ID")

    for method in ("put", "delete"):
        rejected = getattr(client, method)("/api/health")
        assert rejected.status_code == 405, f"{method.upper()} /api/health -> {rejected.status_code}"
        # Gli header di sicurezza valgono anche sulle risposte di errore.
        assert rejected.headers["X-Frame-Options"] == "DENY"


def test_p3_frontend_nginx_rejects_write_methods_and_sets_headers():
    """P3: il portale statico (frontend/nginx.conf) deve respingere i metodi di scrittura con
    405 e dichiarare gli header di sicurezza (prima erano nell'Ingress, ora nel frontend)."""
    from pathlib import Path

    conf = (Path(__file__).resolve().parent.parent / "frontend" / "nginx.conf").read_text(
        encoding="utf-8"
    )
    assert "request_method" in conf and "405" in conf
    for header in (
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Content-Security-Policy",
    ):
        assert header in conf
