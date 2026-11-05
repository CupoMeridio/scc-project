"""
FILE: tests/test_api.py
RUOLO: Suite di test automatizzati (Unit e Integration Testing) con pytest.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import settings

@pytest.fixture
def client():
    """
    Fixture di test per l'istanziazione controllata del client HTTP di collaudo.
    
    COSA FA:
    Crea un'istanza di 'TestClient' collegata all'applicazione FastAPI 'app'.
    L'uso del context manager 'with TestClient(app) as test_client:' attiva esplicitamente
    il ciclo di vita ASGI (Lifespan Context Manager):
    - Esegue le operazioni di Cold-Start prima di 'yield' (inizializzazione modello).
    - Consegna l'oggetto 'test_client' alle funzioni di test che lo richiedono come parametro.
    - Esegue le operazioni di Graceful Teardown dopo 'yield' al completamento del test.
    
    MECCANISMO DI DEPENDENCY INJECTION:
    Nei test di pytest, qualsiasi funzione che dichiara un argomento con lo stesso nome
    della fixture (es. 'def test_something(client):') riceve automaticamente l'oggetto restituito da 'yield'.
    """
    with TestClient(app) as test_client:
        yield test_client

def test_health_liveness(client: TestClient):
    """
    TEST 1: Verifica della Liveness Probe (/health) per Kubernetes.
    
    PARAMETRI:
    - client (TestClient): Client HTTP locale iniettato automaticamente dalla fixture 'client'.
    
    COSA VERIFICA:
    - response.status_code == 200: l'endpoint risponde con codice di successo HTTP standard.
    - data["status"] == "healthy": il payload JSON conferma la corretta vitalità del processo.
    - "app_version" in data: la risposta include la versione applicativa per verifiche di rollout.
    """
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "app_version" in data

def test_ready_readiness_probe_current_mode(client: TestClient):
    """
    TEST 2: Verifica della Readiness Probe (/ready) nello stato operativo corrente.
    
    PARAMETRI:
    - client (TestClient): Client HTTP locale iniettato automaticamente dalla fixture 'client'.
    
    COSA VERIFICA:
    - In modalità 'mock': l'endpoint deve sempre rispondere con HTTP 200.
    - In modalità 'real': risponde con HTTP 503 se la sessione ONNX è assente, oppure con HTTP 200 se presente.
    """
    response = client.get("/ready")
    if settings.MODEL_MODE == "real":
        from app.main import onnx_session
        if onnx_session is None:
            assert response.status_code == 503
        else:
            assert response.status_code == 200
    else:
        assert response.status_code == 200

def test_ready_readiness_gate_real_mode_missing_model(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    TEST 3: Verifica del Readiness Gate in modalità reale (MODEL_MODE=real) con modello assente.
    
    PARAMETRI:
    - monkeypatch (pytest.MonkeyPatch): Fixture nativa di pytest impiegata per sovrascrivere
      in modo sicuro attributi di configurazione (settings.MODEL_MODE, settings.MODEL_PATH)
      e variabili di modulo durante il test, ripristinandoli automaticamente alla fine.
    - tmp_path (pathlib.Path): Fixture nativa di pytest che fornisce il percorso di una cartella temporanea
      isolata e sicura nel file system per simulare file mancanti senza toccare i file di progetto.
      
    COSA VERIFICA:
    - La readiness probe risponde con HTTP 503 Service Unavailable quando la modalità reale
      è attiva ma il file ONNX non è presente su disco, impedendo a Kubernetes di instradare traffico.
    """
    missing_model_path = str(tmp_path / "non_existent_model.onnx")
    monkeypatch.setattr(settings, "MODEL_MODE", "real")
    monkeypatch.setattr(settings, "MODEL_PATH", missing_model_path)
    from app import main
    monkeypatch.setattr(main, "onnx_session", None)
    
    with TestClient(app) as test_client:
        response = test_client.get("/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["model_mode"] == "real"

def test_predict_in_real_mode_without_model_returns_503(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    TEST 4: Verifica di protezione dell'endpoint /predict in modalità reale senza modello ONNX.
    
    PARAMETRI:
    - monkeypatch (pytest.MonkeyPatch): Sovrascrive temporaneamente le impostazioni applicative.
    - tmp_path (pathlib.Path): Genera percorsi temporanei isolati per test deterministici.
    
    COSA VERIFICA:
    - Se invocato in real mode senza sessione ONNX valida, /predict solleva un'eccezione HTTP 503
      (Service Unavailable) e non un errore generico 500, informando il chiamante che il servizio non è pronto.
    """
    missing_model_path = str(tmp_path / "non_existent_model.onnx")
    monkeypatch.setattr(settings, "MODEL_MODE", "real")
    monkeypatch.setattr(settings, "MODEL_PATH", missing_model_path)
    from app import main
    monkeypatch.setattr(main, "onnx_session", None)
    
    with TestClient(app) as test_client:
        payload = {
            "patient_id": "TEST_PATIENT",
            "features": [[100.0, 0.0, 0.0]] * 12
        }
        response = test_client.post("/predict", json=payload)
        assert response.status_code == 503
        assert "non pronto" in response.json()["detail"].lower()

def test_predict_valid_clinical_payload(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """
    TEST 5: Verifica dell'endpoint di inferenza /predict con payload conforme e valido.
    
    PARAMETRI:
    - client (TestClient): Client HTTP di test collegato all'app FastAPI.
    - monkeypatch (pytest.MonkeyPatch): Utilizzato per abilitare la modalità mock nel caso in cui
      il file compilato ONNX non sia presente sul disco locale durante i test unitari.
      
    COSA VERIFICA:
    - response.status_code == 200: la richiesta è elaborata con successo.
    - data["patient_id"] == "PATIENT_TEST_01": l'identificativo paziente è preservato nella risposta.
    - 0.0 <= dose <= MAX_INSULIN_DOSE: il valore calcolato rispetta rigorosamente i guardrail clinici.
    - "request_id" in data: la risposta contiene l'identificativo di correlazione per tracciamento OWASP.
    """
    from app.main import onnx_session
    if onnx_session is None and settings.MODEL_MODE == "real":
        monkeypatch.setattr(settings, "MODEL_MODE", "mock")

    valid_payload = {
        "patient_id": "PATIENT_TEST_01",
        "features": [
            [110.0, 0.0, 0.0],
            [112.0, 0.0, 0.0],
            [115.0, 0.0, 0.0],
            [120.0, 0.0, 0.0],
            [130.0, 45.0, 0.0], # Pasto da 45g
            [150.0, 0.0, 0.0],
            [165.0, 0.0, 0.0],
            [160.0, 0.0, 0.0],
            [150.0, 0.0, 0.0],
            [140.0, 0.0, 0.0],
            [130.0, 0.0, 0.0],
            [125.0, 0.0, 0.0]
        ]
    }
    response = client.post("/predict", json=valid_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["patient_id"] == "PATIENT_TEST_01"
    assert 0.0 <= data["forecasted_insulin_units"] <= settings.MAX_INSULIN_DOSE
    assert "request_id" in data
    assert data["clinical_safety_status"] in ["NORMAL", "HIGH_DOSE_WARNING", "CLAMPED_MAX"]

def test_predict_invalid_dimensions(client: TestClient):
    """
    TEST 6: Verifica del rifiuto di matrici temporali con dimensioni non conformi.
    
    PARAMETRI:
    - client (TestClient): Client HTTP di test.
    
    COSA VERIFICA:
    - L'invio di una matrice con soli 3 intervalli anziché 12 viene intercettato da Pydantic,
      che restituisce immediatamente HTTP 422 Unprocessable Entity senza invocare la rete neurale.
    """
    invalid_payload = {
        "patient_id": "PATIENT_INVALID",
        "features": [
            [120.0, 0.0, 0.0],
            [130.0, 10.0, 0.0],
            [140.0, 0.0, 0.0]
        ]
    }
    response = client.post("/predict", json=invalid_payload)
    assert response.status_code == 422

def test_predict_out_of_bounds_clinical_values(client: TestClient):
    """
    TEST 7: Verifica del rifiuto di parametri biomedici fuori scala (Guasto Hardware Sensori).
    
    PARAMETRI:
    - client (TestClient): Client HTTP di test.
    
    COSA VERIFICA:
    - L'invio di una glicemia negativa (-50.0 mg/dL) viola la regola di validazione fisiologica
      in app/schemas.py e viene respinto con HTTP 422.
    """
    invalid_glucose = {
        "patient_id": "PATIENT_OUT_OF_BOUNDS",
        "features": [[-50.0, 0.0, 0.0]] * 12
    }
    response = client.post("/predict", json=invalid_glucose)
    assert response.status_code == 422

def test_predict_nan_rejection(client: TestClient):
    """
    TEST 8: Verifica della protezione contro valori non numerici (NaN / Infinito).
    
    PARAMETRI:
    - client (TestClient): Client HTTP di test.
    
    COSA VERIFICA:
    - L'iniezione di float('nan') nei carboidrati viene bloccata dal validatore di Pydantic
      con HTTP 422, prevenendo la corruzione dell'algebra lineare nel runtime ONNX.
    """
    invalid_nan = {
        "patient_id": "PATIENT_NAN",
        "features": [
            [120.0, 0.0, 0.0],
            [125.0, float("nan"), 0.0],
            [130.0, 0.0, 0.0],
            [135.0, 0.0, 0.0],
            [140.0, 0.0, 0.0],
            [145.0, 0.0, 0.0],
            [150.0, 0.0, 0.0],
            [140.0, 0.0, 0.0],
            [135.0, 0.0, 0.0],
            [130.0, 0.0, 0.0],
            [125.0, 0.0, 0.0],
            [120.0, 0.0, 0.0]
        ]
    }
    response = client.post("/predict", json=invalid_nan)
    assert response.status_code == 422
