"""
===============================================================================
FILE: app/main.py
RUOLO: Server Web ASGI (FastAPI) per il serving ad alta efficienza del modello ML.
DATASET CLINICO: OhioT1DM Type-1 Diabetes (Glucose Forecasting & Risk Classification)
OBIETTIVO:
    Esporre API REST ultra-rapide e resilienti protette da rate-limiting NGINX
    per la previsione della glicemia futura G(t+6) e della classe di rischio (1..7).
    
    ARCHITETTURA PRODUCTION-GRADE FAIL-CLOSED & SUPPLY CHAIN SECURITY:
    Il servizio esegue esclusivamente l'inferenza compilata tramite ONNX Runtime.
    All'avvio, il sistema verifica l'integrità crittografica SHA-256 degli artefatti
    (sia model.onnx che model.onnx.data) e valida i parametri dello scaler.
    Se un file manca, è corrotto o non corrisponde all'hash, il servizio rifiuta
    di avviarsi (fail-closed) e le sonde segnalano HTTP 503 Service Unavailable,
    impedendo a Kubernetes di instradare traffico errato verso il pod.
===============================================================================
"""

import os     # Interazione con il file system (verifica presenza del file model.onnx)
import re     # Espressioni regolari per sanitizzazione degli header HTTP e prevenzione log injection
import json   # Lettura del file dei metadati del modello
import time   # Misurazione del tempo di elaborazione della richiesta per calcolare la latenza
import uuid   # Generazione di identificativi casuali univoci a 128 bit (UUID4) per il tracciamento
import math   # Funzioni matematiche per verifica anti-NaN e anti-Inf
import hashlib # Modulo crittografico per verifica dell'integrità SHA-256 degli artefatti
import logging # Modulo standard per generare log strutturati con timestamp e severità
import threading # Gestione concorrenza thread-safe per Service Monitoring e telemetria
from contextlib import asynccontextmanager # Decoratore per definire il gestore asincrono del ciclo di vita (lifespan)
from typing import Dict, Any, Tuple, Optional

import numpy as np # Manipolazione efficiente dei tensori numerici
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse # Risposta HTTP specializzata per payload JSON personalizzati

# Importazione delle configurazioni e degli schemi validati
from app.config import settings
from app.schemas import (
    PredictionRequest,
    PredictionResponse,
    HealthResponse,
    MetricsResponse,
    RISK_METADATA,
    CLASS_BINS
)

# -----------------------------------------------------------------------------
# CONFIGURAZIONE DEI LOG STRUTTURATI
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("diabetes-api")

# Variabili globali allocate nello spazio di memoria del processo Python.
onnx_session = None       # Oggetto InferenceSession di ONNX Runtime
model_metadata: Dict[str, Any] = {} # Metadati di versione e parametri di audit
scaler_params: Dict[str, Any] = {}  # Medie e deviazioni standard per standardizzare le feature


def compute_file_sha256(filepath: str) -> str:
    """
    Calcola l'impronta crittografica SHA-256 di un file su disco a blocchi da 64 KB.
    """
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_artifacts_integrity(metadata: Dict[str, Any]) -> bool:
    """
    Verifica l'integrità crittografica di model.onnx e dei pesi esterni model.onnx.data
    rispetto ai valori registrati nei metadati (Supply Chain Security).
    """
    if not os.path.exists(settings.MODEL_PATH):
        logger.error(f"[FAIL-CLOSED] File del modello non trovato su disco: {settings.MODEL_PATH}")
        return False

    actual_model_hash = compute_file_sha256(settings.MODEL_PATH)
    expected_integrity = metadata.get("artifacts_integrity", {})
    expected_model_hash = expected_integrity.get("model.onnx", metadata.get("model_sha256"))

    if not expected_model_hash:
        logger.critical(f"[FAIL-CLOSED] Nessun hash SHA-256 registrato nei metadati per {settings.MODEL_PATH}!")
        return False

    if actual_model_hash.lower() != expected_model_hash.lower():
        logger.critical(
            f"[SECURITY ALERT] Hash SHA-256 non corrispondente per {settings.MODEL_PATH}! "
            f"Atteso: {expected_model_hash}, Calcolato: {actual_model_hash}."
        )
        return False

    # Verifica model.onnx.data (se i pesi sono memorizzati su file esterno)
    data_path = settings.MODEL_PATH + ".data"
    if os.path.exists(data_path):
        expected_data_hash = expected_integrity.get("model.onnx.data")
        if not expected_data_hash:
            logger.critical(f"[FAIL-CLOSED] File pesi esterni presente ({data_path}) ma nessun hash registrato nei metadati!")
            return False
        actual_data_hash = compute_file_sha256(data_path)
        if actual_data_hash.lower() != expected_data_hash.lower():
            logger.critical(
                f"[SECURITY ALERT] Hash SHA-256 non corrispondente per i pesi {data_path}! "
                f"Atteso: {expected_data_hash}, Calcolato: {actual_data_hash}."
            )
            return False

    logger.info(f"[✓] Integrità crittografica SHA-256 verificata con successo per tutti gli artefatti.")
    return True


def load_model_metadata() -> bool:
    """
    Legge, deserializza e convalida rigorosamente il file JSON dei metadati e dello scaler.
    
    COMPORTAMENTO FAIL-CLOSED:
    Se il file è assente, corrotto, privo dei parametri dello scaler o se l'impronta
    crittografica non corrisponde, la funzione restituisce False, impedendo al pod
    di dichiararsi pronto.
    """
    global model_metadata, scaler_params
    if not os.path.exists(settings.METADATA_PATH):
        logger.error(f"[FAIL-CLOSED] File metadati non trovato in: {settings.METADATA_PATH}")
        model_metadata = {}
        scaler_params = {}
        return False

    try:
        with open(settings.METADATA_PATH, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception as e:
        logger.error(f"[FAIL-CLOSED] Errore nella lettura JSON di {settings.METADATA_PATH}: {e}")
        model_metadata = {}
        scaler_params = {}
        return False

    # 1. Verifica presenza e validità delle statistiche di normalizzazione dello scaler
    scaler = metadata.get("scaler", {})
    required_keys = ["seq_mean", "seq_std", "exog_mean", "exog_std"]
    for key in required_keys:
        if key not in scaler or not scaler[key]:
            logger.error(f"[FAIL-CLOSED] Parametro scaler obbligatorio '{key}' mancante o non valido nei metadati.")
            model_metadata = {}
            scaler_params = {}
            return False

    # 2. Verifica di integrità degli artefatti del modello
    if not verify_artifacts_integrity(metadata):
        model_metadata = {}
        scaler_params = {}
        return False

    model_metadata = metadata
    scaler_params = scaler
    logger.info(
        f"[✓] Metadati e parametri scaler caricati con successo: Versione {model_metadata.get('model_version')} "
        f"| Algoritmo: {model_metadata.get('model_name', 'N/A')}"
    )
    return True


# -----------------------------------------------------------------------------
# GESTORE DEL CICLO DI VITA DELL'APPLICAZIONE (Lifespan Event)
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestore asincrono del ciclo di vita dell'applicazione web (Lifespan Context Manager).
    Inizializza ONNX Runtime solo se i metadati e l'integrità sono stati validati al 100%.
    """
    global onnx_session
    metadata_ok = load_model_metadata()

    if not metadata_ok:
        logger.error("[FAIL-CLOSED] Avvio interrotto: metadati o integrità modello non validi. Servizio NON pronto.")
        onnx_session = None
    else:
        logger.info(f"Ricerca modello ONNX compilato in: {settings.MODEL_PATH}")
        try:
            import onnxruntime as ort # Motore di inferenza C++ ad altissime prestazioni

            so = ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            onnx_session = ort.InferenceSession(
                settings.MODEL_PATH,
                sess_options=so,
                providers=['CPUExecutionProvider']
            )
            input_names = [inp.name for inp in onnx_session.get_inputs()]
            output_names = [out.name for out in onnx_session.get_outputs()]
            logger.info(f"[✓] Modello ONNX reale caricato con successo! Ingressi={input_names}, Uscite={output_names}")
        except Exception as e:
            logger.error(f"[X] Errore critico nel caricamento di ONNX Runtime: {e}")
            onnx_session = None

    yield

    onnx_session = None
    logger.info("Chiusura del servizio completata: rilascio delle sessioni ONNX.")


# -----------------------------------------------------------------------------
# RACCOLTA TELEMETRIA E SERVICE MONITORING (RF18, RNF01, RNF05, RNF06)
# -----------------------------------------------------------------------------
class MetricsTracker:
    """
    Raccoglitore in-memory thread-safe delle metriche di servizio.
    Registra i tempi di processo HTTP end-to-end, la conformità allo SLA (<= 1.0 s)
    e la distribuzione aggregata dei profili di rischio clinico per il Center Manager.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.total_predictions = 0
        self.successful_predictions = 0
        self.failed_predictions = 0
        self.total_latency_ms = 0.0
        self.max_latency_ms = 0.0
        self.sla_compliant_count = 0
        self.risk_distribution = {i: 0 for i in range(1, 8)}

    def record_prediction(self, latency_ms: float, risk_class: Optional[int] = None, success: bool = True):
        with self._lock:
            self.total_predictions += 1
            if success:
                self.successful_predictions += 1
                self.total_latency_ms += latency_ms
                if latency_ms > self.max_latency_ms:
                    self.max_latency_ms = latency_ms
                if latency_ms <= 1000.0:
                    self.sla_compliant_count += 1
                if risk_class is not None and 1 <= risk_class <= 7:
                    self.risk_distribution[risk_class] += 1
            else:
                self.failed_predictions += 1

    def get_metrics(self) -> Dict[str, Any]:
        with self._lock:
            uptime = time.time() - self.start_time
            avg_lat = (self.total_latency_ms / self.successful_predictions) if self.successful_predictions > 0 else 0.0
            sla_rate = (self.sla_compliant_count / self.successful_predictions * 100.0) if self.successful_predictions > 0 else 100.0
            error_rate = (self.failed_predictions / self.total_predictions * 100.0) if self.total_predictions > 0 else 0.0
            instance_id = os.environ.get("HOSTNAME", os.environ.get("COMPUTERNAME", "local-instance"))
            return {
                "instance_id": instance_id,
                "uptime_seconds": round(uptime, 2),
                "service_status": "healthy" if (onnx_session is not None and bool(scaler_params)) else "degraded",
                "total_predictions": self.total_predictions,
                "successful_predictions": self.successful_predictions,
                "failed_predictions": self.failed_predictions,
                "error_rate": round(error_rate, 2),
                "avg_process_latency_ms": round(avg_lat, 2),
                "max_process_latency_ms": round(self.max_latency_ms, 2),
                "sla_target_ms": 1000.0,
                "sla_compliance_rate": round(sla_rate, 2),
                "risk_class_distribution": dict(self.risk_distribution),
                "model_version": model_metadata.get("model_version", "unknown"),
                "app_version": settings.VERSION
            }


metrics_tracker = MetricsTracker()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="SaaS Cloud-Native per il monitoraggio predittivo e allerta precoce del Diabete di Tipo 1 (OhioT1DM)",
    lifespan=lifespan
)


# -----------------------------------------------------------------------------
# MIDDLEWARE DI SICUREZZA, TRACCIABILITÀ E MISURAZIONE LATENZA
# -----------------------------------------------------------------------------
@app.middleware("http")
async def security_and_tracing_middleware(request: Request, call_next) -> Response:
    """
    Middleware HTTP asincrono per tracciabilità distribuita, telemetria di latenza e sicurezza OWASP.
    """
    raw_req_id = request.headers.get("X-Request-ID", "")
    # Sanitizzazione per prevenire Log Injection o Header Injection:
    # Accetta solo identificativi alfanumerici e trattini (max 64 caratteri)
    if raw_req_id and re.match(r"^[A-Za-z0-9_\-]{1,64}$", raw_req_id):
        request_id = raw_req_id
    else:
        request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id

    start_time = time.perf_counter()
    response: Response = await call_next(request)
    process_time_ms = (time.perf_counter() - start_time) * 1000.0

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none';"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"

    # Telemetria Service Monitoring (RF18 / RNF01): tracciamento automatico end-to-end
    if request.url.path == "/predict" and request.method == "POST":
        if response.status_code == 200:
            risk_cls = getattr(request.state, "risk_class", None)
            metrics_tracker.record_prediction(process_time_ms, risk_cls, success=True)
        elif response.status_code in (500, 503):
            metrics_tracker.record_prediction(process_time_ms, None, success=False)

    return response


# -----------------------------------------------------------------------------
# SONDE DI STATO PER KUBERNETES: LIVENESS & READINESS PROBES
# -----------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse, tags=["Monitoring"])
def health() -> HealthResponse:
    """
    Liveness Probe per Kubernetes: verifica se il processo del web server è attivo e reattivo.
    """
    return HealthResponse(
        status="healthy",
        model_loaded=(onnx_session is not None and bool(scaler_params)),
        model_version=model_metadata.get("model_version", "unknown"),
        app_version=settings.VERSION
    )


@app.get("/ready", tags=["Monitoring"])
def ready():
    """
    Readiness Probe per Kubernetes: garantisce che nessun traffico venga instradato
    se il modello ONNX o i parametri di normalizzazione (scaler) non sono perfettamente caricati.
    """
    if onnx_session is None or not scaler_params:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "detail": "Modello ONNX o parametri scaler non validati in memoria. Pod non pronto."
            }
        )
    return HealthResponse(
        status="ready",
        model_loaded=True,
        model_version=model_metadata.get("model_version", "unknown"),
        app_version=settings.VERSION
    )


@app.get("/metrics", response_model=MetricsResponse, tags=["Monitoring"])
def get_service_metrics() -> MetricsResponse:
    """
    Endpoint per il Service Monitoring (RF18 / RNF01 / RNF05 / RNF06):
    Restituisce telemetria aggregata su latenza media, picco massimo,
    percentuale di rispetto SLA (<= 1.0 s) e distribuzione delle classi di rischio.
    Non espone dati sensibili dei singoli pazienti (conforme a RNF09).
    """
    return MetricsResponse(**metrics_tracker.get_metrics())


# -----------------------------------------------------------------------------
# FUNZIONI AUSILIARIE DI PRE-PROCESSING E CLASSIFICAZIONE CLINICA
# -----------------------------------------------------------------------------
def compute_discrete_risk_class(glucose_mmol: float) -> int:
    """
    Determina la classe di rischio clinico (1..7) in base alle soglie internazionali:
    - 1: [0.0, 3.0)  -> Ipoglicemia Severa
    - 2: [3.0, 3.9)  -> Ipoglicemia Lieve
    - 3: [3.9, 5.0)  -> Pre-target Basso
    - 4: [5.0, 7.8)  -> Target Ottimale (Euglicemia)
    - 5: [7.8, 10.0) -> Sopra Target
    - 6: [10.0, 13.9) -> Iperglicemia Elevata
    - 7: [>= 13.9)   -> Iperglicemia Grave
    """
    for i in range(len(CLASS_BINS) - 1):
        if CLASS_BINS[i] <= glucose_mmol < CLASS_BINS[i + 1]:
            return i + 1
    return 7


def prepare_model_inputs(payload: PredictionRequest) -> Tuple[np.ndarray, np.ndarray]:
    """
    Trasforma il payload validato nei tensori NumPy normalizzati attesi da ONNX.
    
    COMPORTAMENTO FAIL-CLOSED RIGOROSO:
    Se scaler_params non è popolato, solleva immediatamente un'eccezione HTTP 503:
    i dati grezzi non vengono MAI passati alla rete neurale senza normalizzazione!
    """
    if not scaler_params:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servizio non pronto: parametri di normalizzazione dello scaler non disponibili."
        )

    g_hist = payload.glucose_history

    # Calcolo automatico della derivata discreta dG se omessa
    if payload.dg_history is not None and len(payload.dg_history) == 13:
        dg_hist = payload.dg_history
    else:
        dg_hist = [0.0] + [g_hist[i] - g_hist[i - 1] for i in range(1, 13)]

    # Costruzione tensori NumPy
    seq_raw = np.stack([g_hist, dg_hist], axis=1).astype(np.float32) # [13, 2]
    seq_batch = np.expand_dims(seq_raw, axis=0)                     # [1, 13, 2]

    exog_raw = np.array(payload.exogenous_inputs, dtype=np.float32)
    exog_batch = np.expand_dims(exog_raw, axis=0)                   # [1, 14]

    # Standardizzazione Z-Score
    seq_mean = np.array(scaler_params["seq_mean"], dtype=np.float32)
    seq_std = np.array(scaler_params["seq_std"], dtype=np.float32)
    exog_mean = np.array(scaler_params["exog_mean"], dtype=np.float32)
    exog_std = np.array(scaler_params["exog_std"], dtype=np.float32)

    norm_seq = (seq_batch - seq_mean) / seq_std
    norm_exog = (exog_batch - exog_mean) / exog_std

    return norm_seq, norm_exog


# -----------------------------------------------------------------------------
# ENDPOINT DI INFERENZA PRINCIPALE: POST /predict
# -----------------------------------------------------------------------------
@app.post("/predict", response_model=PredictionResponse, tags=["Inference"])
def predict(payload: PredictionRequest, request: Request) -> PredictionResponse:
    """
    Endpoint per la previsione della glicemia futura (orizzonte 30 minuti)
    e la determinazione della classe di rischio clinico del paziente tramite ONNX Runtime.
    """
    request_id = getattr(request.state, "request_id", "UNKNOWN_REQ")
    current_glucose_mmol = payload.glucose_history[-1]

    # Controllo fail-closed di disponibilità del motore ONNX e dello scaler
    if onnx_session is None or not scaler_params:
        logger.error(f"[SERVICE UNAVAILABLE] Richiesta rifiutata: sessione ONNX o scaler assente. IncidentID={request_id}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servizio di inferenza non pronto: modello ONNX o scaler non inizializzato."
        )

    try:
        norm_seq, norm_exog = prepare_model_inputs(payload)

        ort_inputs = {"seq": norm_seq, "exog": norm_exog}
        ort_outs = onnx_session.run(["delta_glucose"], ort_inputs)
        delta_val = float(ort_outs[0].squeeze())
        model_ver = model_metadata.get("model_version", f"onnx-v{settings.VERSION}")

        # 1. Controllo anti-corruzione numerica
        if math.isnan(delta_val) or math.isinf(delta_val):
            logger.error(f"[ANOMALIA MATEMATICA] Inferenza ha prodotto NaN/Inf. IncidentID={request_id}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Inferenza fallita: output numerico corrotto. Ref={request_id}"
            )

        # 2. Calcolo glicemia assoluta con guardrail fisiologico
        raw_forecast_mmol = current_glucose_mmol + delta_val
        clamped_forecast_mmol = float(np.clip(raw_forecast_mmol, 1.0, 35.0))
        # Allineamento matematico consistente: il delta restituito riflette la differenza
        # effettiva tra il forecast clampato e il valore corrente
        effective_delta_mmol = clamped_forecast_mmol - current_glucose_mmol

        current_glucose_mg_dl = current_glucose_mmol * 18.018
        forecast_mg_dl = clamped_forecast_mmol * 18.018

        # 3. Classificazione del rischio clinico
        risk_class = compute_discrete_risk_class(clamped_forecast_mmol)
        request.state.risk_class = risk_class
        class_info = RISK_METADATA.get(
            risk_class,
            {"label": "Fascia non determinata", "alert": "UNKNOWN"}
        )

        return PredictionResponse(
            patient_id=payload.patient_id,
            current_glucose_mmol_l=round(current_glucose_mmol, 2),
            current_glucose_mg_dl=round(current_glucose_mg_dl, 1),
            forecasted_glucose_mmol_l=round(clamped_forecast_mmol, 2),
            forecasted_glucose_mg_dl=round(forecast_mg_dl, 1),
            delta_glucose=round(effective_delta_mmol, 2),
            risk_class=risk_class,
            risk_label=class_info["label"],
            clinical_alert=class_info["alert"],
            model_version=model_ver,
            inference_engine="ONNX-Runtime-C++",
            request_id=request_id,
            status="success"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[INCIDENT {request_id}] Fallimento inferenza: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Errore interno durante l'elaborazione dell'inferenza. Ref={request_id}"
        )
