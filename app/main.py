"""
FILE: app/main.py
RUOLO: Server Web ASGI (FastAPI) per il serving ad alta efficienza del modello ML.
"""

import os     # Interazione con il file system (verifica presenza del file model.onnx)
import json   # Lettura del file dei metadati del modello
import time   # Misurazione del tempo di elaborazione della richiesta per calcolare la latenza
import uuid   # Generazione di identificativi casuali univoci a 128 bit (UUID4) per il tracciamento
import math   # Funzioni matematiche per verifica anti-NaN e anti-Inf
import logging # Modulo standard per generare log strutturati con timestamp e severità
from contextlib import asynccontextmanager # Decoratore per definire il gestore asincrono del ciclo di vita (lifespan)
import numpy as np # Manipolazione efficiente dei tensori numerici

# Classi e funzioni del framework FastAPI:
# - FastAPI: classe principale dell'applicazione web
# - HTTPException: eccezione usata per interrompere la richiesta e restituire codici di errore HTTP (es. 400, 500)
# - Request: oggetto che rappresenta la richiesta HTTP in arrivo dal client (header, body, IP sorgente)
# - Response: oggetto che rappresenta la risposta HTTP da inviare al client
# - status: modulo contenente costanti numeriche leggibili per i codici HTTP (es. status.HTTP_200_OK)
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse # Risposta HTTP specializzata per restituire payload JSON personalizzati

# Importazione delle configurazioni e degli schemi validati
from app.config import settings
from app.schemas import (
    PredictionRequest,
    PredictionResponse,
    HealthResponse
)

# -----------------------------------------------------------------------------
# CONFIGURAZIONE DEI LOG STRUTTURATI
# -----------------------------------------------------------------------------
# logging.basicConfig(): configura il comportamento del logger di root.
# - level: legge la severità minima da registrare (es. INFO, WARNING, ERROR).
#   Tutti i messaggi con severità pari o superiore a quella specificata verranno stampati sullo standard output.
# - format: definisce il layout del log: timestamp ISO, livello di gravità tra parentesi e messaggio.
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
# Crea un'istanza dedicata del logger con il nome "diabetes-api" per identificare la sorgente nei log di Kubernetes
logger = logging.getLogger("diabetes-api")

# Variabili globali allocate nello spazio di memoria del processo Python.
# Mantengono il modello ONNX caricato in RAM per essere riutilizzato da tutte le richieste concorrenti.
onnx_session = None   # Oggetto InferenceSession di ONNX Runtime
input_name = None     # Nome della porta di ingresso del grafo ONNX (es. "input_time_series")
output_name = None    # Nome della porta di uscita del grafo ONNX (es. "forecasted_insulin")
model_metadata = {}   # Dizionario in RAM contenente versione, checksum e metriche

def load_model_metadata() -> None:
    """
    Legge e deserializza il file JSON contenente i metadati di versione e audit del modello.
    
    PARAMETRI:
    - Nessuno (opera sul percorso globale settings.METADATA_PATH).
    
    VALORE DI RITORNO:
    - Nessuno (aggiorna la variabile globale 'model_metadata' allocata nello heap del processo).
    
    DETTAGLI DI SINTASSI:
    - 'global model_metadata': indica a Python di modificare la variabile definita a livello di modulo
      anziché crearne una locale all'interno della funzione.
    - 'os.path.exists(...)': restituisce un booleano (True/False) verificando la presenza del file su disco.
    - 'with open(...) as f': costrutto Context Manager che garantisce la chiusura automatica del file descriptor
      anche in caso di eccezioni durante la lettura.
    - 'json.load(f)': analizza il flusso di testo e lo converte in un dizionario nativo Python.
    """
    global model_metadata
    if os.path.exists(settings.METADATA_PATH):
        try:
            with open(settings.METADATA_PATH, "r", encoding="utf-8") as f:
                model_metadata = json.load(f)
                logger.info(f"[✓] Metadati modello caricati: Versione {model_metadata.get('model_version')}")
        except Exception as e:
            logger.warning(f"[!] Impossibile leggere {settings.METADATA_PATH}: {e}")
            model_metadata = {}
    else:
        model_metadata = {}

# -----------------------------------------------------------------------------
# GESTORE DEL CICLO DI VITA DELL'APPLICAZIONE (Lifespan Event)
# -----------------------------------------------------------------------------
# @asynccontextmanager: permette di definire codice da eseguire PRIMA dell'apertura del server
# e codice da eseguire DOPO la chiusura del server, separati dall'istruzione 'yield'.
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestore asincrono del ciclo di vita dell'applicazione web (Lifespan Context Manager).
    
    PARAMETRI:
    - app (FastAPI): Istanza dell'applicazione web FastAPI iniettata automaticamente dal server ASGI.
    
    VALORE DI RITORNO:
    - Generatore asincrono (AsyncGenerator): cede il controllo operativo al server mediante l'istruzione 'yield'.
    
    DETTAGLI DI SINTASSI E FUNZIONAMENTO:
    - '@asynccontextmanager': decoratore standard del modulo contextlib che converte una funzione con 'yield'
      in un gestore di contesto asincrono. Sostituisce i precedenti gestori deprecati @app.on_event("startup")
      e @app.on_event("shutdown"), garantendo una gestione atomica e sicura delle risorse.
    - Codice prima di 'yield' (Cold-Start Initialization): eseguito una sola volta all'avvio del processo.
      Inizializza i pesi, carica il modello ONNX in memoria RAM e legge i metadati di versione prima di accettare traffico.
    - Istruzione 'yield': sospende la funzione e avvia il loop di elaborazione delle richieste HTTP.
    - Codice dopo 'yield' (Graceful Teardown): eseguito alla ricezione del segnale SIGTERM inviato da Kubernetes,
      rilascia i puntatori alla memoria del modello ONNX e resetta le sessioni prima della chiusura definitiva.
    """
    global onnx_session, input_name, output_name
    load_model_metadata()
    
    logger.info(f"Avvio servizio in modalità: MODEL_MODE={settings.MODEL_MODE.upper()}")
    logger.info(f"Ricerca modello ONNX in: {settings.MODEL_PATH}")
    
    if os.path.exists(settings.MODEL_PATH):
        try:
            import onnxruntime as ort # Motore di inferenza C++ ad altissime prestazioni
            
            # ort.SessionOptions(): oggetto di configurazione per ottimizzare l'esecuzione su CPU
            so = ort.SessionOptions()
            
            # intra_op_num_threads=1: impone a ONNX di usare 1 solo thread CPU per ciascuna operazione interna.
            # MOTIVAZIONE CLOUD: In un pod Kubernetes con limiti di CPU (es. 0.5 core), permettere a ONNX
            # di lanciare decine di thread paralleli causerebbe un pesante overhead di context switching del kernel.
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            
            # ORT_ENABLE_ALL: abilita tutte le fusioni matematiche del grafo (es. unisce moltiplicazione e bias in un'unica istruzione SIMD)
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            
            # Inizializza la sessione compilata caricando il file .onnx nella memoria del processo
            # providers=['CPUExecutionProvider']: indica di eseguire i calcoli sui core della CPU dell'host
            onnx_session = ort.InferenceSession(
                settings.MODEL_PATH,
                sess_options=so,
                providers=['CPUExecutionProvider']
            )
            # Legge dinamicamente i nomi delle porte di I/O dichiarati all'atto dell'export PyTorch
            input_name = onnx_session.get_inputs()[0].name
            output_name = onnx_session.get_outputs()[0].name
            logger.info(f"[✓] Modello ONNX caricato con successo! Input='{input_name}', Output='{output_name}'")
        except Exception as e:
            logger.error(f"[X] Errore critico nel caricamento di ONNX Runtime: {e}")
            onnx_session = None
    else:
        if settings.MODEL_MODE == "real":
            logger.error(f"[X] MODALITÀ REALE ATTIVA ma il file '{settings.MODEL_PATH}' non è stato trovato!")
        else:
            logger.warning(f"[!] Modalità MOCK attiva: simulatore fisiologico abilitato per sviluppo.")
        onnx_session = None

    # 'yield': a questo punto l'applicazione è pronta e comincia ad accettare richieste HTTP dai client!
    yield
    
    # Questo codice viene eseguito quando il pod viene terminato (Graceful Shutdown)
    onnx_session = None
    input_name = None
    output_name = None
    logger.info("Chiusura del servizio completata: rilascio delle risorse.")

# Istanziazione dell'applicazione FastAPI collegata al gestore lifespan
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description="SaaS Time-Series Forecasting API per Type-1 Diabetes Insulin Intake",
    lifespan=lifespan
)

# -----------------------------------------------------------------------------
# MIDDLEWARE DI SICUREZZA, TRACCIABILITÀ E MISURAZIONE LATENZA
# -----------------------------------------------------------------------------
# @app.middleware("http"): intercetta OGNI richiesta HTTP prima che raggiunga l'endpoint
# e intercetta OGNI risposta HTTP prima che venga spedita indietro al client.
@app.middleware("http")
async def security_and_tracing_middleware(request: Request, call_next) -> Response:
    """
    Middleware HTTP asincrono per tracciabilità distribuita, telemetria di latenza e sicurezza difensiva.
    
    PARAMETRI:
    - request (Request): Oggetto FastAPI/Starlette che incapsula tutti i dati della richiesta HTTP in ingresso
      (metodo, URL, header, cookie, body e stato della sessione).
    - call_next (Callable): Funzione asincrona che inoltra la richiesta al livello successivo della catena ASGI
      (altri middleware o la funzione di endpoint finale) restituendo l'oggetto Response generato.
      
    VALORE DI RITORNO:
    - response (Response): Oggetto della risposta HTTP in uscita arricchito con header diagnostici e di sicurezza OWASP.
    """
    # 1. TRACCIABILITÀ DISTRIBUITA (Request ID):
    # Cerca l'header 'X-Request-ID' inviato da NGINX; se assente, ne genera uno nuovo usando i primi 12 caratteri di un UUID4 casuale.
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
    # Salva il request_id nello stato della richiesta per renderlo accessibile dentro gli endpoint
    request.state.request_id = request_id
    
    # 2. MISURAZIONE DELLA LATENZA:
    # time.time(): orario di arrivo della richiesta in secondi con precisione al microsecondo
    start_time = time.time()
    
    # call_next(request): invoca l'endpoint effettivo (es. /predict) e ne attende la risposta
    response: Response = await call_next(request)
    
    # Calcola il tempo trascorso moltiplicando per 1000 per ottenere i millisecondi (ms)
    process_time_ms = (time.time() - start_time) * 1000
    
    # 3. INIEZIONE DEGLI HEADER DI SICUREZZA NELLA RISPOSTA (Standard OWASP):
    # Restituisce l'ID per consentire al client di citarlo in caso di anomalie
    response.headers["X-Request-ID"] = request_id
    # X-Frame-Options: DENY -> impedisce che la pagina venga inclusa in un iframe di un sito terzo (Anti-Clickjacking)
    response.headers["X-Frame-Options"] = "DENY"
    # X-Content-Type-Options: nosniff -> obbliga il browser a rispettare il Content-Type dichiarato (Anti-MIME Sniffing)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # X-XSS-Protection: abilita il filtro integrato dei browser contro Cross-Site Scripting
    response.headers["X-XSS-Protection"] = "1; mode=block"
    # Strict-Transport-Security (HSTS): impone l'uso esclusivo del protocollo cifrato HTTPS per 1 anno (31536000 secondi)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Header diagnostico con la latenza misurata (utile per verificare l'SLA < 1000 ms)
    response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    
    return response

# -----------------------------------------------------------------------------
# ENDPOINT PER KUBERNETES: LIVENESS PROBE (/health)
# -----------------------------------------------------------------------------
@app.get("/health", response_model=HealthResponse, tags=["Monitoring"])
def health() -> HealthResponse:
    """
    Sonda di vitalità per Kubernetes (Liveness Probe).
    
    OBIETTIVO:
    Verificare se il processo Python dell'applicazione è vivo, reattivo ed in grado di gestire richieste.
    
    PARAMETRI:
    - Nessuno.
    
    VALORE DI RITORNO:
    - HealthResponse: Oggetto Pydantic contenente:
      * status: stringa "healthy".
      * model_mode: modalità di configurazione attiva ("real" o "mock").
      * model_loaded: booleano (True se la sessione ONNX Runtime è allocata in memoria).
      * model_version: stringa con la versione letta dal file dei metadati.
      * app_version: versione dell'applicazione FastAPI.
    """
    return HealthResponse(
        status="healthy",
        model_mode=settings.MODEL_MODE,
        model_loaded=onnx_session is not None,
        model_version=model_metadata.get("model_version", "unknown"),
        app_version=settings.VERSION
    )

# -----------------------------------------------------------------------------
# ENDPOINT PER KUBERNETES: READINESS PROBE (/ready)
# -----------------------------------------------------------------------------
@app.get("/ready", tags=["Monitoring"])
def ready():
    """
    Sonda di prontezza al traffico per Kubernetes (Readiness Probe Gate).
    
    OBIETTIVO:
    Verificare se il pod è in grado di elaborare l'inferenza reale prima che Kubernetes vi instradi il traffico.
    Se MODEL_MODE='real' e il modello ONNX non è caricato, restituisce HTTP 503 per impedire che i pazienti
    ricevano risposte fallimentari.
    
    PARAMETRI:
    - Nessuno.
    
    VALORE DI RITORNO:
    - HealthResponse (HTTP 200): se il servizio è pronto ad accettare il carico di lavoro.
    - JSONResponse (HTTP 503): se la modalità reale è attiva ma la sessione ONNX non è caricata in memoria.
    """
    if settings.MODEL_MODE == "real" and onnx_session is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "detail": "La modalita di produzione richiede il modello ONNX caricato, ma la sessione e assente.",
                "model_mode": "real"
            }
        )
    return HealthResponse(
        status="ready",
        model_mode=settings.MODEL_MODE,
        model_loaded=onnx_session is not None,
        model_version=model_metadata.get("model_version", "simulator-1.0.0"),
        app_version=settings.VERSION
    )

def _mock_forecast(features_array: np.ndarray) -> float:
    """
    Simulatore fisiologico deterministico basato su equazioni cliniche standard di bilancio insulinico.
    Utilizzato come formula di riserva durante lo sviluppo locale o in assenza del file compilato ONNX.
    
    PARAMETRI:
    - features_array (np.ndarray): Matrice tridimensionale NumPy di forma [1, 12, 3] corrispondente a:
      * Dimensione 0: lotto di campioni (batch size = 1).
      * Dimensione 1: 12 intervalli temporali discreti consecutivi da 5 minuti ciascuno (totale 60 minuti).
      * Dimensione 2: 3 feature per intervallo: indice 0 = glicemia (mg/dL), indice 1 = carboidrati (g), indice 2 = insulina passata (U).
      
    VALORE DI RITORNO:
    - float: Stima della dose di insulina in Unità Internazionali (IU) arrotondata a 2 decimali.
    
    DETTAGLI DI SINTASSI:
    - 'features_array[0, -1, :]': sintassi di slicing avanzato di NumPy:
      * 0: seleziona il primo (e unico) elemento del batch.
      * -1: seleziona l'ultimo intervallo temporale della serie storica (il momento attuale della somministrazione).
      * ':': seleziona tutti e tre i canali informativi [glicemia, carboidrati, insulina attiva].
    """
    last_step = features_array[0, -1, :]
    glucose, carbs, past_insulin = float(last_step[0]), float(last_step[1]), float(last_step[2])
    
    carb_ratio = 10.0   # ICR (Insulin to Carb Ratio): 1 Unità di insulina ogni 10 grammi di carboidrati
    sensitivity = 40.0  # ISF (Insulin Sensitivity Factor): 1 Unità riduce la glicemia di 40 mg/dL
    target_glucose = 100.0 # Valore glicemico basale di riferimento (mg/dL)
    
    carb_dose = carbs / carb_ratio
    correction_dose = max(0.0, (glucose - target_glucose) / sensitivity)
    active_discount = past_insulin * 0.5 # Fattore di sconto per l'insulina ancora attiva (IOB)
    raw_dose = max(0.0, carb_dose + correction_dose - active_discount)
    return round(float(raw_dose), 2)

# -----------------------------------------------------------------------------
# ENDPOINT DI INFERENZA: /predict (HTTP POST)
# -----------------------------------------------------------------------------
@app.post("/predict", response_model=PredictionResponse, tags=["Inference"])
def predict(payload: PredictionRequest, request: Request) -> PredictionResponse:
    """
    Endpoint principale di inferenza per il calcolo del dosaggio insulinico.
    
    PARAMETRI:
    - payload (PredictionRequest): Oggetto di input validato contenente l'ID paziente e la matrice [12 x 3].
    - request (Request): Oggetto della richiesta HTTP FastAPI impiegato per estrarre l'identificativo
      univoco di tracciamento 'request_id' registrato dal middleware di sicurezza.
      
    VALORE DI RITORNO:
    - PredictionResponse: Risposta conforme contenente la dose stimata, lo stato dei guardrail di sicurezza clinica,
      la versione del modello impiegato e il codice di tracciamento della transazione.
      
    DETTAGLI DI SINTASSI:
    - 'np.array([payload.features], dtype=np.float32)': racchiudendo 'payload.features' tra parentesi quadre
      si aggiunge la dimensione di batch richiesta dalla rete neurale, passando da [12, 3] a [1, 12, 3].
    - 'dtype=np.float32': converte la rappresentazione binaria in float a 32 bit attesa dal motore ONNX.
    - 'ort_outs[0].squeeze()': la funzione .squeeze() di NumPy elimina le dimensioni unitarie ridondanti
      (es. da un tensore con forma [1, 1] estrae il singolo valore scalare).
    """
    # Recupera il Request-ID univoco salvato dal middleware
    request_id = getattr(request.state, "request_id", "UNKNOWN_REQ")
    
    try:
        # np.array([payload.features], dtype=np.float32):
        # - Racchiude la lista 2D [12, 3] tra parentesi quadre per aggiungere la dimensione del batch: [1, 12, 3].
        # - dtype=np.float32: obbliga la rappresentazione su 32 bit attesa dal file ONNX esportato da PyTorch.
        arr = np.array([payload.features], dtype=np.float32)
        
        if onnx_session is not None:
            # ort_inputs: dizionario che mappa il nome del nodo di ingresso del grafo col tensore NumPy
            ort_inputs = {input_name: arr}
            # onnx_session.run(): invoca il motore ONNX Runtime in C++.
            # Restituisce una lista di array NumPy (uno per ogni nodo di uscita del modello).
            ort_outs = onnx_session.run([output_name], ort_inputs)
            
            # .squeeze(): rimuove le dimensioni ridondanti di grandezza 1 (es. trasforma [[3.45]] in 3.45)
            raw_val = float(ort_outs[0].squeeze())
            active_mode = "real"
            model_ver = model_metadata.get("model_version", f"onnx-v{settings.VERSION}")
        else:
            if settings.MODEL_MODE == "real":
                # In modalita produzione 'real', se onnx_session e None, restituisce 503 Service Unavailable
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Servizio di inferenza non pronto: modello ONNX non caricato in modalita reale."
                )
            raw_val = _mock_forecast(arr)
            active_mode = "mock"
            model_ver = "mock-simulator-v1.0.0"

        # ---------------------------------------------------------------------
        # CONTROLLO ANTI-NAN / ANTI-INF DI RITORNO DAL MODELLO
        # ---------------------------------------------------------------------
        if math.isnan(raw_val) or math.isinf(raw_val):
            logger.error(f"[ANOMALIA MATEMATICA] Output del modello non numerico (NaN/Inf). IncidentID={request_id}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Inferenza fallita: output del modello non numerico. Riferimento incidente: {request_id}"
            )

        # ---------------------------------------------------------------------
        # CLINICAL SAFETY GUARDRAILS (SAFETY CLAMP INSUPERABILE)
        # ---------------------------------------------------------------------
        # Se la rete neurale produce un valore anomalo a causa di un pattern non previsto,
        # viene impedito il superamento del limite massimo di sicurezza (20.0 IU) stabilito dal protocollo clinico.
        if raw_val > settings.MAX_INSULIN_DOSE:
            final_dose = settings.MAX_INSULIN_DOSE
            safety_status = "CLAMPED_MAX"
            logger.warning(
                f"[SAFETY ALERT] Dose stimata grezza ({raw_val:.2f} IU) superiore al massimo consentito. "
                f"Applicato taglio forzato a {settings.MAX_INSULIN_DOSE} IU. IncidentID={request_id}"
            )
        elif raw_val >= settings.WARNING_INSULIN_DOSE:
            final_dose = raw_val
            safety_status = "HIGH_DOSE_WARNING"
        else:
            final_dose = max(0.0, raw_val)
            safety_status = "NORMAL"

        # Restituisce l'oggetto validato secondo lo schema PredictionResponse
        return PredictionResponse(
            patient_id=payload.patient_id,
            forecasted_insulin_units=round(final_dose, 2), # Arrotondato a 2 cifre decimali
            unit="IU (International Units)",
            model_mode=active_mode,
            model_version=model_ver,
            clinical_safety_status=safety_status,
            request_id=request_id,
            status="success"
        )
        
    except HTTPException:
        # Propagazione diretta delle eccezioni HTTP controllate (es. 503, 422) senza mascherarle
        raise
    except Exception as e:
        # MITIGAZIONE INFORMATION DISCLOSURE (CWE-209):
        # Nessuna esposizione al client esterno dell'errore tecnico interno né dello stack trace,
        # prevenendo la dispersione di informazioni su librerie e percorsi di sistema.
        # Tracciamento dell'errore completo con exc_info=True esclusivamente nei log interni del server,
        # con restituzione al client di un messaggio generico associato all'identificativo univoco dell'incidente.
        logger.error(f"[INCIDENT {request_id}] Fallimento durante l'inferenza: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Errore interno durante l'elaborazione dell'inferenza. Riferimento incidente: {request_id}"
        )
