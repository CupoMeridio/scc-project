"""
FILE: tests/test_zone_architecture.py
RUOLO: Test delle zone isolate (V1 EdDSA/Redis revoca/2-step provisioning),
       Model Repository fail-closed e Training Zone (con importorskip torch per V5).
"""

import base64
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import main as ml_main
from app.config import settings as ml_settings
from saas_app import auth as saas_auth
from saas_app import db as saas_db
from saas_app import iam_client, model_repo
from saas_app.auth import login_rate_limiter
from saas_app.config import SaasSettings, saas_settings
from saas_app.main import create_app
from saas_app.models import User
from saas_app.seed import seed_database


def _generate_ed25519_pair() -> tuple[str, str]:
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


class _FakeRevocationRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_file = tmp_path / "zone_test.db"
    test_engine = saas_db.build_engine(f"sqlite:///{db_file}")
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(saas_db, "engine", test_engine)
    monkeypatch.setattr(saas_db, "SessionLocal", TestSessionLocal)

    from saas_app import main as saas_main
    from saas_app.services import doctor_service

    monkeypatch.setattr(saas_main, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(doctor_service, "SessionLocal", TestSessionLocal)

    priv_pem, pub_pem = _generate_ed25519_pair()
    monkeypatch.setattr(saas_settings, "SAAS_ENV", "test")
    monkeypatch.setattr(saas_settings, "COOKIE_SECURE", False)
    monkeypatch.setattr(saas_settings, "JWT_PRIVATE_KEY_PEM", priv_pem)
    monkeypatch.setattr(saas_settings, "JWT_PUBLIC_KEY_PEM", pub_pem)
    monkeypatch.setattr(saas_settings, "IAM_PROVISION_TOKEN", "p" * 40)
    monkeypatch.setattr(saas_settings, "DATABASE_URL", f"sqlite:///{db_file}")
    saas_settings.validate_security_secrets(require_jwt=True)
    login_rate_limiter.reset()

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    alembic_cfg = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    alembic_command.upgrade(alembic_cfg, "head")

    with TestSessionLocal() as session:
        seed_database(
            session,
            force_minimal=True,
            patient_pw="TestPatient-2026!",
            doctor_pw="TestDoctor-2026!",
            manager_pw="TestManager-2026!",
        )
    return TestSessionLocal


def test_service_mode_router_isolation(isolated_db):
    """Ogni Deployment SaaS espone SOLO i router della propria zona (V1)."""
    iam_app = create_app("iam")
    with TestClient(iam_app) as tc:
        assert tc.get("/api/auth/me").status_code == 401
        assert tc.post("/api/auth/internal/verify", json={}).status_code == 404
        assert tc.post("/api/auth/internal/users/lookup", json={}).status_code == 404
        assert tc.delete("/api/auth/internal/patients/1").status_code == 404
        assert tc.get("/api/patient/summary").status_code == 404
        assert tc.get("/api/doctor/patients").status_code == 404
        assert tc.get("/api/manager/dashboard").status_code == 404
        assert tc.post("/api/cgm/ingest", json={}).status_code == 404

    clinical_app = create_app("clinical")
    with TestClient(clinical_app) as tc:
        assert tc.post("/api/auth/login", json={}).status_code == 404
        assert tc.get("/api/patient/summary").status_code == 401
        assert tc.get("/api/doctor/patients").status_code == 401
        assert tc.get("/api/manager/dashboard").status_code == 404
        assert tc.post("/api/cgm/ingest", json={}).status_code == 404

    manager_app = create_app("manager")
    with TestClient(manager_app) as tc:
        assert tc.post("/api/auth/login", json={}).status_code == 404
        assert tc.get("/api/patient/summary").status_code == 404
        assert tc.get("/api/manager/dashboard").status_code == 401
        assert tc.post("/api/cgm/ingest", json={}).status_code == 404

    ingest_app = create_app("ingest")
    with TestClient(ingest_app) as tc:
        assert tc.post("/api/auth/login", json={}).status_code == 404
        assert tc.get("/api/patient/summary").status_code == 404
        assert tc.get("/api/manager/dashboard").status_code == 404
        assert tc.post("/api/cgm/ingest", json={}).status_code in (401, 422)

    cfg = SaasSettings()
    cfg.SAAS_ENV = "production"
    cfg.SERVICE_MODE = "ingest"
    cfg.JWT_PRIVATE_KEY_PEM = ""
    cfg.JWT_PUBLIC_KEY_PEM = ""
    cfg.validate_security_secrets(require_jwt=False)

    # Manager e Clinical in produzione non devono richiedere chiave privata JWT né IAM per verify.
    priv_pem, pub_pem = _generate_ed25519_pair()
    mgr_cfg = SaasSettings()
    mgr_cfg.SAAS_ENV = "production"
    mgr_cfg.SERVICE_MODE = "manager"
    mgr_cfg.JWT_PRIVATE_KEY_PEM = ""
    mgr_cfg.JWT_PUBLIC_KEY_PEM = pub_pem
    mgr_cfg.IAM_PROVISION_TOKEN = ""
    mgr_cfg.validate_security_secrets(require_jwt=True)

    # IAM in produzione richiede chiave privata e token provisioning.
    iam_cfg = SaasSettings()
    iam_cfg.SAAS_ENV = "production"
    iam_cfg.SERVICE_MODE = "iam"
    iam_cfg.JWT_PRIVATE_KEY_PEM = priv_pem
    iam_cfg.JWT_PUBLIC_KEY_PEM = pub_pem
    iam_cfg.IAM_PROVISION_TOKEN = "x" * 32
    iam_cfg.validate_security_secrets(require_jwt=True)


def test_eddsa_local_verification_revocation_and_2step_provisioning(
    isolated_db, monkeypatch: pytest.MonkeyPatch
):
    """V1: verifica locale EdDSA senza IAM, revoca via Redis RO e provisioning 2-step pending/active."""
    fake_redis = _FakeRevocationRedis()
    monkeypatch.setattr(saas_auth, "_get_revocation_redis", lambda readonly=True: fake_redis)

    iam_app = create_app("iam")
    clinical_app = create_app("clinical")
    manager_app = create_app("manager")

    with TestClient(iam_app) as iam_tc:
        # 1. Endpoint provisioning protetto da X-Provision-Token (401) e blocco header proxy (403).
        assert (
            iam_tc.post(
                "/api/auth/internal/patients/provision",
                json={"username": "p_unauth", "idempotency_key": "idem_12345"},
            ).status_code
            == 401
        )
        assert (
            iam_tc.post(
                "/api/auth/internal/patients/provision",
                json={"username": "p_unauth", "idempotency_key": "idem_12345"},
                headers={"X-Provision-Token": "p" * 40, "X-Forwarded-Proto": "https"},
            ).status_code
            == 403
        )

        # Login medico + login manager + login paziente con must_change_password=True
        doc_login = iam_tc.post(
            "/api/auth/login",
            json={"username": "dr.rossi", "password": "TestDoctor-2026!"},
        )
        assert doc_login.status_code == 200
        doc_cookie = doc_login.cookies.get(saas_settings.COOKIE_NAME)

        mgr_login = iam_tc.post(
            "/api/auth/login",
            json={"username": "manager", "password": "TestManager-2026!"},
        )
        assert mgr_login.status_code == 200
        mgr_cookie = mgr_login.cookies.get(saas_settings.COOKIE_NAME)

        pat_login = iam_tc.post(
            "/api/auth/login",
            json={"username": "patient_591", "password": "TestPatient-2026!"},
        )
        assert pat_login.status_code == 200
        pat_cookie = pat_login.cookies.get(saas_settings.COOKIE_NAME)
        assert doc_cookie and mgr_cookie and pat_cookie

        # 2. Simulazione "IAM spento": rimuoviamo chiave privata e blocchiamo URL IAM.
        # Clinical e Manager devono continuare a servire token EdDSA validi e imporre mcp=True!
        saved_priv_pem = saas_settings.JWT_PRIVATE_KEY_PEM
        monkeypatch.setattr(saas_settings, "JWT_PRIVATE_KEY_PEM", "")
        monkeypatch.setattr(saas_settings, "IAM_INTERNAL_URL", "http://unreachable-iam")

        with TestClient(clinical_app) as clin_tc, TestClient(manager_app) as mgr_tc:
            monkeypatch.setattr(saas_settings, "SERVICE_MODE", "clinical")
            # Paziente con mcp=True viene bloccato localmente con 403 senza chiamare IAM
            clin_tc.cookies.set(saas_settings.COOKIE_NAME, pat_cookie)
            blocked_pat = clin_tc.get("/api/patient/summary")
            assert blocked_pat.status_code == 403
            assert blocked_pat.json()["detail"]["code"] == "PASSWORD_CHANGE_REQUIRED"

            # Medico accede a /api/doctor/patients anche con IAM irraggiungibile
            clin_tc.cookies.set(saas_settings.COOKIE_NAME, doc_cookie)
            patients_res = clin_tc.get("/api/doctor/patients")
            assert patients_res.status_code == 200
            assert len(patients_res.json()) >= 1
            assert patients_res.json()[0]["full_name"]

            # Manager accede alle viste aggregate (stesso percorso unico di produzione, V5)
            monkeypatch.setattr(saas_settings, "SERVICE_MODE", "manager")
            mgr_tc.cookies.set(saas_settings.COOKIE_NAME, mgr_cookie)
            from saas_app.services import manager_service
            monkeypatch.setattr(manager_service, "collect_ml_pod_metrics", lambda: [])
            dash_res = mgr_tc.get("/api/manager/dashboard")
            assert dash_res.status_code == 200
            assert dash_res.json()["total_patients"] >= 1

        # 3. Collega il solo servizio Clinical all'IAM per testare provisioning 2-step pending -> active
        class _IamTestClientProxy:
            def post(self, path, json=None, headers=None):
                rel = path.replace("http://diabetes-iam-service", "")
                return iam_tc.post(rel, json=json, headers=headers)

        monkeypatch.setattr(saas_settings, "IAM_INTERNAL_URL", "http://diabetes-iam-service")
        monkeypatch.setattr(saas_settings, "SERVICE_MODE", "clinical")
        monkeypatch.setattr(iam_client, "get_iam_client", lambda: _IamTestClientProxy())

        with TestClient(clinical_app) as clin_tc:
            clin_tc.cookies.set(saas_settings.COOKIE_NAME, doc_cookie)
            reg_res = clin_tc.post(
                "/api/doctor/patients",
                json={
                    "patient_code": "888",
                    "username": "patient_888",
                    "full_name": "Paziente Otto",
                    "email": "p888@example.test",
                },
            )
            assert reg_res.status_code == 201
            temp_pw = reg_res.json()["temporary_password"]
            assert reg_res.json()["patient_code"] == "888"

            # Collisione su patient_code -> il profilo clinico fallisce, ma l'account IAM resta `pending` (non loggabile)
            dup_res = clin_tc.post(
                "/api/doctor/patients",
                json={
                    "patient_code": "888",
                    "username": "patient_888_dup",
                    "full_name": "Paziente Duplicato",
                    "email": "p888_dup@example.test",
                },
            )
            assert dup_res.status_code == 409

        # Ripristiniamo la chiave privata su IAM per testare login nuovo paziente e logout/revoca
        monkeypatch.setattr(saas_settings, "JWT_PRIVATE_KEY_PEM", saved_priv_pem)
        assert iam_tc.post(
            "/api/auth/login",
            json={"username": "patient_888", "password": temp_pw},
        ).status_code == 200

        with isolated_db() as s:
            dup_u = s.execute(select(User).where(User.username == "patient_888_dup")).scalar_one()
            assert dup_u.status == "pending"

        # Logout pubblica JTI su Redis -> Clinical vede immediatamente il token revocato
        iam_tc.cookies.clear()
        iam_tc.cookies.set(saas_settings.COOKIE_NAME, doc_cookie)
        assert iam_tc.post("/api/auth/logout").status_code == 200
        with TestClient(clinical_app) as clin_tc:
            monkeypatch.setattr(saas_settings, "SERVICE_MODE", "clinical")
            clin_tc.cookies.clear()
            clin_tc.cookies.set(saas_settings.COOKIE_NAME, doc_cookie)
            assert clin_tc.get("/api/doctor/patients").status_code == 401


def test_model_repo_integrity_publish_and_ml_fail_closed(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """V4: Model Repository verifica SHA-256 + Ed25519; ML è fail-closed se non firmato o alterato."""
    from common.model_signing import sign_manifest_ed25519

    priv_pem, pub_pem = _generate_ed25519_pair()
    repo_root = Path(__file__).resolve().parent.parent / "model"
    storage_dir = tmp_path / "repo_storage"
    monkeypatch.setattr(model_repo, "MODEL_REPO_DIR", storage_dir)
    monkeypatch.setattr(model_repo, "BASELINE_MODEL_DIR", repo_root)
    monkeypatch.setattr(model_repo, "MODEL_REPO_UPLOAD_TOKEN", "r" * 40)
    monkeypatch.setattr(model_repo, "MODEL_SIGNING_PUBLIC_KEY_PEM", pub_pem)
    monkeypatch.setattr(model_repo, "SAAS_ENV", "test")

    with TestClient(model_repo.app) as repo_tc:
        ready = repo_tc.get("/ready")
        assert ready.status_code == 200
        manifest = repo_tc.get("/models/current/manifest").json()
        assert "model.onnx" in manifest["files"]
        assert "model_metadata.json" in manifest["files"]

        meta_bytes = (repo_root / "model_metadata.json").read_bytes()
        onnx_bytes = (repo_root / "model.onnx").read_bytes()
        data_bytes = (repo_root / "model.onnx.data").read_bytes()

        bad_auth = repo_tc.put(
            "/models/publish",
            headers={"X-Repo-Token": "wrong"},
            json={
                "metadata_b64": base64.b64encode(meta_bytes).decode("ascii"),
                "onnx_b64": base64.b64encode(onnx_bytes).decode("ascii"),
            },
        )
        assert bad_auth.status_code == 401

        # Manifesto NON firmato respinto quando MODEL_SIGNING_PUBLIC_KEY_PEM è attiva -> 422
        unsigned_meta = json.loads(meta_bytes.decode("utf-8"))
        unsigned_meta["model_version"] = "2.0.0-unsigned"
        unsigned_res = repo_tc.put(
            "/models/publish",
            headers={"X-Repo-Token": "r" * 40},
            json={
                "metadata_b64": base64.b64encode(json.dumps(unsigned_meta).encode("utf-8")).decode("ascii"),
                "onnx_b64": base64.b64encode(onnx_bytes).decode("ascii"),
                "data_b64": base64.b64encode(data_bytes).decode("ascii"),
            },
        )
        assert unsigned_res.status_code == 422

        # Checksum alterato respinto -> 422
        signed_meta = json.loads(meta_bytes.decode("utf-8"))
        signed_meta["model_version"] = "2.0.0-challenger"
        signed_meta["manifest_signature"] = sign_manifest_ed25519(signed_meta, priv_pem)
        tampered = repo_tc.put(
            "/models/publish",
            headers={"X-Repo-Token": "r" * 40},
            json={
                "metadata_b64": base64.b64encode(json.dumps(signed_meta).encode("utf-8")).decode("ascii"),
                "onnx_b64": base64.b64encode(b"corrupted-onnx").decode("ascii"),
                "data_b64": base64.b64encode(data_bytes).decode("ascii"),
            },
        )
        assert tampered.status_code == 422

        # Pubblicazione valida e firmata
        pub_ok = repo_tc.put(
            "/models/publish",
            headers={"X-Repo-Token": "r" * 40},
            json={
                "metadata_b64": base64.b64encode(json.dumps(signed_meta).encode("utf-8")).decode("ascii"),
                "onnx_b64": base64.b64encode(onnx_bytes).decode("ascii"),
                "data_b64": base64.b64encode(data_bytes).decode("ascii"),
            },
        )
        assert pub_ok.status_code == 200
        assert repo_tc.get("/ready").json()["model_version"] == "2.0.0-challenger"

        orig_model_path = ml_settings.MODEL_PATH
        orig_meta_path = ml_settings.METADATA_PATH
        cache_dir = tmp_path / "ml_cache"

        class _RespWrapper(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        def _failing_urlopen(_req, timeout=5.0, context=None):
            raise OSError("repository unreachable")

        monkeypatch.setattr(ml_settings, "MODEL_REPO_URL", "http://diabetes-model-repo")
        monkeypatch.setattr(ml_settings, "MODEL_CACHE_DIR", str(cache_dir))
        monkeypatch.setattr(ml_main.urllib.request, "urlopen", _failing_urlopen)
        assert ml_main.initialize_model_state() is False
        assert ml_main.onnx_session is None

        # Con chiave pubblica errata, la verifica Ed25519 fallisce e /ready risponde 503
        _, wrong_pub = _generate_ed25519_pair()

        def _repo_urlopen(req, timeout=5.0, context=None):
            url = req.full_url.replace("http://diabetes-model-repo", "")
            r = repo_tc.get(url)
            assert r.status_code == 200
            return _RespWrapper(r.content)

        monkeypatch.setattr(ml_main.urllib.request, "urlopen", _repo_urlopen)
        monkeypatch.setattr(ml_settings, "MODEL_SIGNING_PUBLIC_KEY_PEM", wrong_pub)
        assert ml_main.initialize_model_state() is False
        assert ml_main.onnx_session is None
        with TestClient(ml_main.app) as ml_tc:
            assert ml_tc.get("/ready").status_code == 503

        # Con chiave pubblica corretta, il pod scarica, verifica SHA-256 + Ed25519 e parte
        monkeypatch.setattr(ml_settings, "MODEL_SIGNING_PUBLIC_KEY_PEM", pub_pem)
        assert ml_main.initialize_model_state() is True
        assert ml_main.onnx_session is not None
        assert ml_main.model_metadata["model_version"] == "2.0.0-challenger"

        monkeypatch.setattr(ml_settings, "MODEL_SIGNING_PUBLIC_KEY_PEM", "")
        monkeypatch.setattr(ml_settings, "MODEL_REPO_URL", "")
        monkeypatch.setattr(ml_settings, "MODEL_PATH", orig_model_path)
        monkeypatch.setattr(ml_settings, "METADATA_PATH", orig_meta_path)
        assert ml_main.initialize_model_state() is True


def test_backup_asymmetric_encryption_and_tamper_rejection(tmp_path):
    """V6: backup cifrato X25519+AES-256-GCM; chiave privata non necessaria per cifrare; manomissione fallisce."""
    from cryptography.exceptions import InvalidTag
    from saas_app.backup_crypto import generate_backup_keypair, open_file, seal_file

    priv_b64, pub_b64 = generate_backup_keypair()
    plain = tmp_path / "sample.dump"
    sealed = tmp_path / "sample.dump.enc"
    restored = tmp_path / "restored.dump"

    payload = b"PGDMP" + (b"clinical-row-data-1234567890" * 5000)
    plain.write_bytes(payload)
    seal_file(plain, sealed, pub_b64)
    open_file(sealed, restored, priv_b64)
    assert restored.read_bytes() == payload

    # Chiave privata errata rifiutata
    wrong_priv, _ = generate_backup_keypair()
    with pytest.raises(InvalidTag):
        open_file(sealed, tmp_path / "bad.dump", wrong_priv)

    # Manomissione di 1 byte nel payload cifrato rifiutata
    corrupted = bytearray(sealed.read_bytes())
    corrupted[len(corrupted) // 2] ^= 0xFF
    bad_enc = tmp_path / "tampered.dump.enc"
    bad_enc.write_bytes(bytes(corrupted))
    with pytest.raises(InvalidTag):
        open_file(bad_enc, tmp_path / "bad2.dump", priv_b64)


def test_training_zone_splits_champion_eval_and_promotion_gate():
    """Training Zone: skip pulito se torch assente (V5); split post-trained_until, bootstrap CI ed eval ONNX."""
    pytest.importorskip("torch")
    import numpy as np
    from model.retrain_pipeline import (
        SplitTensors,
        TrainingRow,
        bootstrap_promotion_decision,
        build_temporal_splits_from_series,
        evaluate_onnx_champion,
        should_promote_challenger,
    )

    t_old = datetime(2019, 1, 1, 6, 0, tzinfo=timezone.utc)
    t_new = datetime(2026, 1, 1, 6, 0, tzinfo=timezone.utc)
    cutoff = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = [
        TrainingRow(
            pseudo_patient_id="pseudo_a",
            recorded_at=t_old + timedelta(minutes=5 * i),
            glucose_mmol=6.0 + 0.3 * np.sin(i / 6.0),
            insulin_u=1.0,
            cho_rate=0.0,
        )
        for i in range(60)
    ] + [
        TrainingRow(
            pseudo_patient_id="pseudo_a",
            recorded_at=t_new + timedelta(minutes=5 * i),
            glucose_mmol=6.5 + 0.3 * np.sin(i / 6.0),
            insulin_u=1.2 if i % 12 == 0 else 0.0,
            cho_rate=4.0 if i % 24 == 0 else 0.0,
        )
        for i in range(90)
    ]
    train, val, test = build_temporal_splits_from_series(rows, trained_until=cutoff)
    assert len(train.y_delta) > 0
    assert len(val.y_delta) > 0
    assert len(test.y_delta) > 0
    assert train.seq.shape[1:] == (13, 2)
    assert train.exog.shape[1] == 14

    assert should_promote_challenger(1.100, 0.70, 1.090, 0.70) is True
    assert should_promote_challenger(1.100, 0.70, 1.098, 0.75) is False
    assert should_promote_challenger(1.100, 0.70, 1.050, 0.65) is False

    # Bootstrap CI: Challenger identico o rumoroso non promosso; chiaramente migliore promosso
    rng = np.random.default_rng(7)
    g_true = np.full(120, 6.5, dtype=np.float32)
    target_cls = np.full(120, 4, dtype=np.int64)
    champ_pred = g_true + rng.normal(0.0, 1.1, size=120).astype(np.float32)
    worse_chal = g_true + rng.normal(0.0, 1.25, size=120).astype(np.float32)
    better_chal = g_true + rng.normal(0.0, 0.45, size=120).astype(np.float32)

    ok_bad, _ = bootstrap_promotion_decision(g_true, champ_pred, worse_chal, target_cls)
    ok_tie, _ = bootstrap_promotion_decision(g_true, champ_pred, champ_pred, target_cls)
    ok_good, ci_good = bootstrap_promotion_decision(g_true, champ_pred, better_chal, target_cls)
    assert ok_bad is False
    assert ok_tie is False
    assert ok_good is True
    assert ci_good["rmse_gain_ci_lo"] >= 0.005

    model_dir = Path(__file__).resolve().parent.parent / "model"
    rmse, mae, acc, meta = evaluate_onnx_champion(model_dir, test)
    assert rmse >= 0.0
    assert mae >= 0.0
    assert 0.0 <= acc <= 1.0
    assert "scaler" in meta

    empty = SplitTensors(
        seq=np.empty((0, 13, 2), dtype=np.float32),
        exog=np.empty((0, 14), dtype=np.float32),
        y_delta=np.empty((0,), dtype=np.float32),
        g_now=np.empty((0,), dtype=np.float32),
        risk_classes=np.empty((0,), dtype=np.int64),
    )
    assert empty.seq.shape == (0, 13, 2)
