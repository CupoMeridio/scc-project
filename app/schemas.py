"""
FILE: app/schemas.py
RUOLO: Definizione degli schemi di validazione dei dati tramite Pydantic v2.
"""

import math # Modulo matematico standard per verificare valori non numerici (NaN) o infiniti (Inf)
from typing import List, Optional # Modulo typing per annotazioni di tipo statico (liste e tipi opzionali)

# BaseModel: classe base di Pydantic da cui ereditano tutti i modelli di dati
# Field: funzione usata per aggiungere vincoli (lunghezza, regex, default) e descrizioni ai singoli campi
# field_validator: decoratore di Pydantic v2 per scrivere regole di validazione logica personalizzate
from pydantic import BaseModel, Field, field_validator

# -----------------------------------------------------------------------------
# COSTANTI PER I LIMITI FISIOLOGICI UMANI (RANGE DI SICUREZZA CLINICA)
# -----------------------------------------------------------------------------
# Se un sensore invia numeri al di fuori di queste soglie, si tratta di un malfunzionamento hardware.
GLUCOSE_MIN = 20.0      # mg/dL: valore di glicemia minimo compatibile con la coscienza umana
GLUCOSE_MAX = 600.0     # mg/dL: fondo scala dei dispositivi CGM (Continuous Glucose Monitors) commerciali
CARBS_MIN = 0.0         # grammi: non esistono pasti con carboidrati negativi
CARBS_MAX = 300.0       # grammi: pasto eccezionalmente abbondante (limite massimo ragionevole)
INSULIN_PAST_MIN = 0.0  # Unità (IU): dose minima
INSULIN_PAST_MAX = 100.0 # Unità (IU): dose accumulata massima sicura per evitare shock ipoglicemico

class PredictionRequest(BaseModel):
    """
    Schema del corpo (body) della richiesta HTTP POST inviata dal client all'endpoint /predict.
    Pydantic intercetta il JSON grezzo ed esegue il parsing e la validazione prima che il codice
    di business logic venga eseguito.
    """

    # patient_id: stringa opzionale con valore di default "ANONYMOUS_PATIENT".
    # - min_length=1: impedisce l'invio di stringhe vuote ("").
    # - max_length=64: previene attacchi di esaurimento memoria (buffer overflow logico con stringhe giganti).
    # - pattern=r"^[A-Za-z0-9_\-]+$": espressione regolare (regex) che accetta SOLO lettere maiuscole/minuscole,
    #   cifre numeriche da 0 a 9, trattini (-) e underscore (_). Blocca caratteri speciali come apici, virgolette,
    #   punti e virgola usati per attacchi di iniezione (SQL Injection, Log Injection, Command Injection).
    patient_id: str = Field(
        default="ANONYMOUS_PATIENT",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        description="ID anonimizzato del paziente o sensore (alfanumerico, trattini, underscore)"
    )

    # features: matrice bidimensionale di numeri decimali (List[List[float]]).
    # '...' (Ellipsis) come primo argomento indica che questo campo è OBBLIGATORIO (se manca nel JSON, fallisce).
    # json_schema_extra: aggiunge un esempio visivo che appare automaticamente nella documentazione Swagger UI.
    features: List[List[float]] = Field(
        ...,
        description="Matrice [12 x 3]: 12 istanti temporali di [glicemia_mg_dl, carbo_g, insulina_passata_u]",
        json_schema_extra={
            "example": [
                [110.0, 0.0, 0.0],
                [112.0, 0.0, 0.0],
                [115.0, 0.0, 0.0],
                [120.0, 0.0, 0.0],
                [130.0, 50.0, 0.0],
                [150.0, 0.0, 0.0],
                [170.0, 0.0, 0.0],
                [165.0, 0.0, 0.0],
                [155.0, 0.0, 0.0],
                [145.0, 0.0, 0.0],
                [135.0, 0.0, 0.0],
                [130.0, 0.0, 0.0]
            ]
        }
    )

    # @field_validator("features"): dice a Pydantic di passare il valore di 'features' a questa funzione
    # @classmethod: metodo legato alla classe, riceve 'cls' come primo argomento e 'matrix' come valore da convalidare.
    @field_validator("features")
    @classmethod
    def validate_clinical_features(cls, matrix: List[List[float]]) -> List[List[float]]:
        """
        Controlla l'integrità strutturale e fisiologica della serie temporale.
        Se una sola regola viene violata, solleva un 'ValueError':
        FastAPI trasforma automaticamente qualsiasi ValueError di Pydantic in una risposta
        HTTP 422 Unprocessable Entity con la spiegazione precisa dell'errore al client.
        """
        # 1. CONTROLLO TEMPORALE: Presenza obbligatoria di esattamente 12 intervalli temporali
        if len(matrix) != 12:
            raise ValueError(f"La sequenza temporale deve contenere esattamente 12 intervalli, ricevuti {len(matrix)}.")

        # 2. CONTROLLO DI CIASCUNA RIGA DELLA MATRICE
        for i, row in enumerate(matrix):
            # Ciascun intervallo deve contenere esattamente 3 numeri [Glicemia, Carbo, Insulina]
            if len(row) != 3:
                raise ValueError(f"L'intervallo temporale #{i} deve contenere esattamente 3 feature, ricevute {len(row)}.")

            # Scompattamento dei tre valori
            glucose, carbs, past_insulin = row[0], row[1], row[2]

            # 3. CONTROLLO ANTI-CORRUZIONE NUMERICA (NaN e Inf):
            # Se un sensore perde il segnale o trasmette 'null' o float('nan'),
            # le operazioni di algebra lineare (moltiplicazioni matriciali ONNX) restituirebbero tutti NaN,
            # rompendo l'inferenza e propagando valori indefiniti.
            nomi_canali = ["Glicemia", "Carboidrati", "Insulina attiva"]
            for val_idx, num in enumerate(row):
                if math.isnan(num) or math.isinf(num):
                    raise ValueError(f"Valore non numerico (NaN) o infinito rilevato per '{nomi_canali[val_idx]}' all'intervallo #{i}.")

            # 4. CONTROLLO RANGE GLICEMIA (20 - 600 mg/dL)
            if not (GLUCOSE_MIN <= glucose <= GLUCOSE_MAX):
                raise ValueError(
                    f"Glicemia all'intervallo #{i} fuori scala clinica: {glucose} mg/dL (ammessi {GLUCOSE_MIN}-{GLUCOSE_MAX})."
                )

            # 5. CONTROLLO RANGE CARBOIDRATI (0 - 300 g)
            if not (CARBS_MIN <= carbs <= CARBS_MAX):
                raise ValueError(
                    f"Carboidrati all'intervallo #{i} fuori scala: {carbs} g (ammessi {CARBS_MIN}-{CARBS_MAX})."
                )

            # 6. CONTROLLO RANGE INSULINA ATTIVA (0 - 100 U)
            if not (INSULIN_PAST_MIN <= past_insulin <= INSULIN_PAST_MAX):
                raise ValueError(
                    f"Insulina precedente all'intervallo #{i} fuori scala: {past_insulin} U (ammessi {INSULIN_PAST_MIN}-{INSULIN_PAST_MAX})."
                )

        # Se tutti i controlli passano, restituisce la matrice validata
        return matrix

class PredictionResponse(BaseModel):
    """
    Schema della risposta HTTP 200 restituita al client (es. al microinfusore o all'app dello smartphone).
    """
    patient_id: str = Field(..., description="ID del paziente associato alla stima")
    forecasted_insulin_units: float = Field(..., description="Dose calcolata in Unità di Insulina (IU)")
    unit: str = "IU (International Units)"
    model_mode: str = Field(..., description="'real' se calcolato con ONNX reale, 'mock' se con simulatore di test")
    model_version: str = Field(..., description="Versione del modello ML utilizzato per la predizione")
    clinical_safety_status: str = Field(..., description="Stato di sicurezza clinica: NORMAL, CLAMPED_MAX o HIGH_DOSE_WARNING")
    request_id: str = Field(..., description="Identificativo univoco della richiesta utile per tracciamento nei log di sistema")
    status: str = "success"

class HealthResponse(BaseModel):
    """
    Schema della risposta per i controlli di salute di Kubernetes (Liveness e Readiness Probes).
    """
    status: str          # Es. "healthy" o "ready"
    model_mode: str      # "real" o "mock"
    model_loaded: bool   # True se il file ONNX è attualmente allocato in memoria RAM
    model_version: str  # Versione letta dai metadati
    app_version: str    # Versione dell'API FastAPI
