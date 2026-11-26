"""
===============================================================================
FILE: tests/test_api.py
RUOLO: Suite completa di test automatizzati (Unit and Integration Testing) con pytest.
CONTESTO CLINICO:
    Verifica del contratto API, della corretta inferenza ONNX, delle risposte delle
    sonde Kubernetes (Health e Readiness) e del blocco preventivo di dati anomali
    (corruzione numerica, valori non fisiologici, array di lunghezza errata).
===============================================================================
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app, compute_discrete_risk_class
from app.config import settings


@pytest.fixture
def client():
    """
    Fixture di test per l'istanziazione del client HTTP di collaudo.
    L'uso del context manager 'with TestClient(app) as test_client:' attiva esplicitamente
    il ciclo di vita ASGI (Lifespan Context Manager):
    - Inizializza i metadati e il runtime ONNX prima dei test.
    - Consegna l'oggetto 'test_client' alle funzioni di collaudo.
    - Esegue il Graceful Teardown al termine dei test.
    """
    with TestClient(app) as test_client:
        yield test_client


def test_health_liveness(client: TestClient):
    """
    TEST 1: Verifica della Liveness Probe (/health) per Kubernetes.
    - Verifica che il processo web sia attivo e risponda con codice HTTP 200.
    - Verifica che il payload contenga lo status 'healthy' e la versione applicativa.
    """
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "app_version" in data
    assert "model_loaded" in data


def test_ready_readiness_probe(client: TestClient):
    """
    TEST 2: Verifica della Readiness Probe (/ready) a regime.
    - Con il modello ONNX presente in memoria, risponde 200 OK e segnala model_loaded=True.
    """
    response = client.get("/ready")
    from app.main import onnx_session
    if onnx_session is not None:
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
        assert response.json()["model_loaded"] is True
    else:
        assert response.status_code == 503


def test_ready_readiness_gate_missing_model(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    TEST 3: Verifica del Readiness Gate con file modello assente.
    - Simula la configurazione in cui il file ONNX non è disponibile.
    - Verifica che la readiness probe risponda rigorosamente con HTTP 503 Service Unavailable,
      impedendo a Kubernetes di instradare traffico clinico su un pod non inizializzato.
    """
    missing_model_path = str(tmp_path / "non_existent_model.onnx")
    monkeypatch.setattr(settings, "MODEL_PATH", missing_model_path)
    from app import main
    monkeypatch.setattr(main, "onnx_session", None)
    
    with TestClient(app) as test_client:
        response = test_client.get("/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"


def test_predict_without_model_returns_503(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    TEST 4: Verifica di protezione dell'endpoint /predict in assenza del modello ONNX.
    - Se invocato senza sessione ONNX caricata, restituisce HTTP 503 anziché un crash 500.
    """
    missing_model_path = str(tmp_path / "non_existent_model.onnx")
    monkeypatch.setattr(settings, "MODEL_PATH", missing_model_path)
    from app import main
    monkeypatch.setattr(main, "onnx_session", None)
    
    with TestClient(app) as test_client:
        payload = {
            "patient_id": "TEST_PATIENT",
            "glucose_history": [5.5] * 13
        }
        response = test_client.post("/predict", json=payload)
        assert response.status_code == 503
        assert "non pronto" in response.json()["detail"].lower()


def test_scaler_fail_closed_behavior(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    TEST 4-BIS: Verifica del comportamento fail-closed se lo scaler manca o è corrotto.
    - Se i metadati mancano dei parametri dello scaler, /ready e /predict devono restituire 503,
      impedendo al modello di ricevere dati grezzi e rispondere con previsioni errate.
    """
    import json
    invalid_meta_path = str(tmp_path / "missing_scaler_metadata.json")
    with open(invalid_meta_path, "w") as f:
        json.dump({"model_version": "1.0.0"}, f) # Privo del blocco 'scaler'
        
    monkeypatch.setattr(settings, "METADATA_PATH", invalid_meta_path)
    from app import main
    monkeypatch.setattr(main, "onnx_session", None)
    monkeypatch.setattr(main, "scaler_params", {})
    
    with TestClient(app) as test_client:
        # 1. /ready deve rifiutare lo stato pronto
        ready_resp = test_client.get("/ready")
        assert ready_resp.status_code == 503
        
        # 2. /predict deve respingere l'inferenza fail-closed
        payload = {
            "patient_id": "TEST_PATIENT",
            "glucose_history": [5.5] * 13
        }
        pred_resp = test_client.post("/predict", json=payload)
        assert pred_resp.status_code == 503
        assert "non pronto" in pred_resp.json()["detail"].lower()


def test_integrity_hash_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    TEST 4-TER: Verifica della Supply Chain Security (mancata corrispondenza SHA-256).
    - Se i pesi del modello vengono manomessi o l'hash nei metadati non corrisponde,
      il caricamento fallisce all'avvio e il servizio si dichiara non pronto (HTTP 503).
    """
    import json
    from app import main
    
    # Crea un file metadati con un hash manomesso/fittizio
    tampered_meta_path = str(tmp_path / "tampered_metadata.json")
    tampered_meta = {
        "model_version": "1.0.0",
        "artifacts_integrity": {
            "model.onnx": "0000000000000000000000000000000000000000000000000000000000000000"
        },
        "scaler": {
            "seq_mean": [0.0, 0.0],
            "seq_std": [1.0, 1.0],
            "exog_mean": [0.0] * 14,
            "exog_std": [1.0] * 14
        }
    }
    with open(tampered_meta_path, "w") as f:
        json.dump(tampered_meta, f)
        
    monkeypatch.setattr(settings, "METADATA_PATH", tampered_meta_path)
    monkeypatch.setattr(main, "onnx_session", None)
    monkeypatch.setattr(main, "scaler_params", {})
    
    with TestClient(app) as test_client:
        response = test_client.get("/ready")
        assert response.status_code == 503


def test_predict_valid_clinical_payload(client: TestClient):
    """
    TEST 5: Verifica dell'endpoint di inferenza /predict con payload conforme e valido.
    COSA VERIFICA:
    - Risposta HTTP 200 con JSON ben formato.
    - Il paziente 'PATIENT_540' è correttamente tracciato.
    - Le stime del glucosio in mmol/L e in mg/dL sono coerenti e fisiologiche.
    - La classe di rischio è un intero compreso tra 1 e 7.
    - La risposta include l'header X-Request-ID e la misura di latenza.
    """
    valid_payload = {
        "patient_id": "PATIENT_540",
        "glucose_history": [
            4.2, 4.0, 3.8, 3.6, 3.5, 3.7, 3.9, 4.3, 5.0, 5.5, 6.1, 6.7, 7.3
        ],
        "exogenous_inputs": [
            3.6, 0.0,
            3.8, 0.0,
            3.9, 0.0,
            4.1, 0.0,
            4.2, 0.0,
            4.3, 0.0,
            4.5, 0.0
        ]
    }
    response = client.post("/predict", json=valid_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["patient_id"] == "PATIENT_540"
    assert data["current_glucose_mmol_l"] == 7.3
    assert 1.0 <= data["forecasted_glucose_mmol_l"] <= 35.0
    assert 1 <= data["risk_class"] <= 7
    assert "risk_label" in data
    assert "clinical_alert" in data
    assert data["inference_engine"] == "ONNX-Runtime-C++"
    assert "request_id" in data
    assert response.headers.get("X-Request-ID") is not None


def test_predict_invalid_history_length(client: TestClient):
    """
    TEST 6: Verifica del rifiuto di cronologie glicemiche con numero errato di intervalli.
    - L'invio di una lista con soli 5 elementi anziché 13 viene intercettato da Pydantic,
      che restituisce immediatamente HTTP 422 Unprocessable Entity.
    """
    invalid_payload = {
        "patient_id": "PATIENT_SHORT_HISTORY",
        "glucose_history": [5.0, 5.2, 5.4, 5.6, 5.8] # Solo 5 intervalli
    }
    response = client.post("/predict", json=invalid_payload)
    assert response.status_code == 422


def test_predict_out_of_bounds_clinical_values(client: TestClient):
    """
    TEST 7: Verifica del rifiuto di parametri biomedici fuori scala fisiologica.
    - L'invio di un glucosio negativo o assurdo (-5.0 mmol/L o 100.0 mmol/L)
      viola i vincoli biologici e viene respinto con HTTP 422.
    """
    invalid_glucose = {
        "patient_id": "PATIENT_OUT_OF_BOUNDS",
        "glucose_history": [5.0] * 12 + [-5.0] # Valore negativo
    }
    response = client.post("/predict", json=invalid_glucose)
    assert response.status_code == 422


def test_predict_nan_rejection(client: TestClient):
    """
    TEST 8: Verifica della protezione contro valori non numerici (null o stringhe corrotte).
    - L'iniezione di valori nulli o non numerici viene respinta da Pydantic con HTTP 422,
      prevenendo la corruzione dell'algebra lineare nel runtime ONNX.
    """
    # 1. Iniezione di null (standard JSON per valori assenti/corrotti)
    invalid_null = {
        "patient_id": "PATIENT_NULL",
        "glucose_history": [5.0] * 12 + [None]
    }
    response = client.post("/predict", json=invalid_null)
    assert response.status_code == 422

    # 2. Iniezione di stringa non numerica
    invalid_str = {
        "patient_id": "PATIENT_STR",
        "glucose_history": [5.0] * 12 + ["corrupted_signal"]
    }
    response_str = client.post("/predict", json=invalid_str)
    assert response_str.status_code == 422


def test_predict_invalid_exogenous_length(client: TestClient):
    """
    TEST 9: Verifica del rifiuto di vettori esogeni con dimensione non conforme.
    - Il vettore esogeno richiede 14 valori; l'invio di 4 valori restituisce HTTP 422.
    """
    invalid_exog = {
        "patient_id": "PATIENT_BAD_EXOG",
        "glucose_history": [5.0] * 13,
        "exogenous_inputs": [1.0, 2.0, 3.0, 4.0] # Solo 4 valori anziché 14
    }
    response = client.post("/predict", json=invalid_exog)
    assert response.status_code == 422


def test_predict_dg_history_validation(client: TestClient):
    """
    TEST 10: Verifica della validazione della serie temporale opzionale dg_history.
    - Un array valido di 13 valori finiti viene accettato (HTTP 200).
    - Un array con dimensione errata o valori fuori scala viene rifiutato con HTTP 422.
    """
    valid_payload = {
        "patient_id": "PATIENT_DG_VALID",
        "glucose_history": [5.0] * 13,
        "dg_history": [0.0] + [0.2] * 12
    }
    res_valid = client.post("/predict", json=valid_payload)
    assert res_valid.status_code == 200

    invalid_len = {
        "patient_id": "PATIENT_DG_INVALID_LEN",
        "glucose_history": [5.0] * 13,
        "dg_history": [0.0, 0.1, 0.2] # Solo 3 valori anziché 13
    }
    assert client.post("/predict", json=invalid_len).status_code == 422

    out_of_bounds = {
        "patient_id": "PATIENT_DG_OUT_OF_BOUNDS",
        "glucose_history": [5.0] * 13,
        "dg_history": [0.0] * 12 + [50.0] # Derivata biologicamente impossibile (+50 mmol/L in 5 min)
    }
    assert client.post("/predict", json=out_of_bounds).status_code == 422


def test_predict_exogenous_upper_bounds(client: TestClient):
    """
    TEST 11: Verifica dei limiti fisiologici massimi sulle variabili esogene.
    - Boli di insulina > 100 U o pasti > 300 g vengono respinti con HTTP 422.
    """
    excessive_insulin = {
        "patient_id": "PATIENT_OVERDOSE",
        "glucose_history": [5.0] * 13,
        "exogenous_inputs": [150.0, 0.0] + [0.0] * 12 # 150 U di insulina (> 100 U)
    }
    assert client.post("/predict", json=excessive_insulin).status_code == 422

    excessive_carbs = {
        "patient_id": "PATIENT_EXCESSIVE_CARBS",
        "glucose_history": [5.0] * 13,
        "exogenous_inputs": [0.0, 25.0] + [0.0] * 12 # Tasso assorbimento CHO > 15.0
    }
    assert client.post("/predict", json=excessive_carbs).status_code == 422


def test_request_id_sanitization_defense(client: TestClient):
    """
    TEST 12: Verifica della sanitizzazione di X-Request-ID contro Log/Header Injection.
    - Se il client trasmette un header con caratteri speciali (CRLF o script injection),
      il middleware genera un ID sicuro conforme.
    """
    malicious_header = "REQ-123\r\nInjected-Header: bad\n<script>"
    response = client.get("/health", headers={"X-Request-ID": malicious_header})
    assert response.status_code == 200
    safe_id = response.headers.get("X-Request-ID")
    assert safe_id is not None
    assert "\r" not in safe_id and "\n" not in safe_id and "<" not in safe_id


def test_discrete_risk_classes_boundaries():
    """
    TEST 13: Verifica rigorosa delle soglie di separazione per le 7 classi di rischio clinico.
    Fasce (mmol/L): [0, 3.0) -> 1, [3.0, 3.9) -> 2, [3.9, 5.0) -> 3,
                    [5.0, 7.8) -> 4, [7.8, 10.0) -> 5, [10.0, 13.9) -> 6, >= 13.9 -> 7.
    """
    assert compute_discrete_risk_class(2.5) == 1
    assert compute_discrete_risk_class(3.0) == 2
    assert compute_discrete_risk_class(3.89) == 2
    assert compute_discrete_risk_class(3.9) == 3
    assert compute_discrete_risk_class(4.99) == 3
    assert compute_discrete_risk_class(5.0) == 4
    assert compute_discrete_risk_class(7.79) == 4
    assert compute_discrete_risk_class(7.8) == 5
    assert compute_discrete_risk_class(9.99) == 5
    assert compute_discrete_risk_class(10.0) == 6
    assert compute_discrete_risk_class(13.89) == 6
    assert compute_discrete_risk_class(13.9) == 7
    assert compute_discrete_risk_class(25.0) == 7


def test_metrics_monitoring_endpoint(client: TestClient):
    """
    TEST 14: Verifica dell'endpoint di Service Monitoring (/metrics).
    - Risponde con HTTP 200 e schema MetricsResponse completo con instance_id.
    - Registra conformità allo SLA (1000 ms), latenza di processo e distribuzioni anonimizzate.
    """
    res_initial = client.get("/metrics")
    assert res_initial.status_code == 200
    metrics_init = res_initial.json()
    assert "instance_id" in metrics_init
    assert len(metrics_init["instance_id"]) > 0
    assert "uptime_seconds" in metrics_init
    assert metrics_init["uptime_seconds"] >= 0.0
    assert metrics_init["service_status"] in ["healthy", "degraded"]
    assert metrics_init["sla_target_ms"] == 1000.0
    assert "risk_class_distribution" in metrics_init

    # Esegue una predizione ipoglicemica reale (classe 1) e verifica l'aggiornamento dinamico
    # dei contatori, accertando che non sia un valore di riserva hardcoded
    valid_payload = {
        "patient_id": "METRICS_TEST_PATIENT",
        "glucose_history": [2.2] * 13,
        "exogenous_inputs": [0.0] * 14
    }
    res_pred = client.post("/predict", json=valid_payload)
    assert res_pred.status_code == 200
    pred_risk = res_pred.json()["risk_class"]
    assert pred_risk == 1

    res_after = client.get("/metrics")
    assert res_after.status_code == 200
    metrics_after = res_after.json()
    assert metrics_after["total_predictions"] >= 1
    assert metrics_after["successful_predictions"] >= 1
    assert "error_rate" in metrics_after
    assert metrics_after["error_rate"] >= 0.0
    assert metrics_after["avg_process_latency_ms"] > 0.0
    assert metrics_after["max_process_latency_ms"] >= metrics_after["avg_process_latency_ms"]
    assert metrics_after["sla_compliance_rate"] >= 0.0
    # JSON deserializza le chiavi intere dei dizionari come stringhe ("1", "2", ...)
    assert "1" in metrics_after["risk_class_distribution"]
    assert metrics_after["risk_class_distribution"]["1"] >= 1


def test_metrics_no_double_counting_on_error(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """
    TEST 15: Verifica anti-regressione sul bug del doppio conteggio degli errori.
    - Simula un fallimento e verifica che failed_predictions e total_predictions
      vengano incrementati esattamente di 1 (nessun doppio conteggio).
    """
    m_before = client.get("/metrics").json()
    total_before = m_before["total_predictions"]
    failed_before = m_before["failed_predictions"]

    from app import main
    # Simula un'anomalia matematica NaN nell'output di onnx_session.run
    class FakeSession:
        def run(self, output_names, input_feed):
            import numpy as np
            return [np.array([float("nan")])]

    monkeypatch.setattr(main, "onnx_session", FakeSession())

    payload = {
        "patient_id": "NAN_TEST_PATIENT",
        "glucose_history": [5.5] * 13,
        "exogenous_inputs": [0.0] * 14
    }
    res = client.post("/predict", json=payload)
    assert res.status_code == 500

    m_after = client.get("/metrics").json()
    assert m_after["total_predictions"] == total_before + 1
    assert m_after["failed_predictions"] == failed_before + 1
