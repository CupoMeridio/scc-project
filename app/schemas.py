"""
===============================================================================
FILE: app/schemas.py
RUOLO: Schemi di validazione dei dati (Input & Output) tramite Pydantic v2.
CONTESTO CLINICO:
    Validazione delle serie temporali di glucosio per il dataset OhioT1DM.
    Include controlli fisiologici rigorosi (anti-NaN, range biologici ammessi)
    e strutturazione della risposta con fasce di rischio clinico da 1 a 7.
===============================================================================
"""

import math # Modulo matematico standard per verificare valori non numerici (NaN) o infiniti (Inf)
from typing import List, Optional, Dict, Any # Annotazioni di tipo statico per type hinting
from pydantic import BaseModel, Field, field_validator # Classi base e decoratori di validazione Pydantic v2

# -----------------------------------------------------------------------------
# COSTANTI PER I LIMITI FISIOLOGICI UMANI (RANGE DI SICUREZZA CLINICA)
# -----------------------------------------------------------------------------
# Se un sensore CGM (Continuous Glucose Monitor) invia valori al di fuori di queste soglie,
# siamo in presenza di una disconnessione, sonda danneggiata o malfunzionamento hardware.
#
# Conversione standard: 1 mmol/L = 18.018 mg/dL.
GLUCOSE_MIN_MMOL = 1.0   # mmol/L (~18 mg/dL): limite inferiore estremo compatibile con la vita
GLUCOSE_MAX_MMOL = 35.0  # mmol/L (~630 mg/dL): fondo scala dei moderni sensori medici CGM
CARBS_MIN_G = 0.0        # grammi: non esistono pasti con carboidrati negativi
CARBS_MAX_G = 300.0      # grammi: porzione pasto eccezionalmente abbondante
INSULIN_MIN_U = 0.0      # Unità (IU): dosaggio minimo
INSULIN_MAX_U = 100.0    # Unità (IU): limite massimo precauzionale per singolo evento

# SOGLIE CLINICHE INTERNAZIONALI PER LE 7 CLASSI DI RISCHIO GLICEMICO (in mmol/L)
CLASS_BINS = [0.0, 3.0, 3.9, 5.0, 7.8, 10.0, 13.9, float("inf")]

RISK_METADATA: Dict[int, Dict[str, str]] = {
    1: {"label": "Ipoglicemia Severa", "range": "[0.0, 3.0) mmol/L", "alert": "CRITICAL_HYPO"},
    2: {"label": "Ipoglicemia Lieve", "range": "[3.0, 3.9) mmol/L", "alert": "WARNING_HYPO"},
    3: {"label": "Pre-target Basso", "range": "[3.9, 5.0) mmol/L", "alert": "NORMAL"},
    4: {"label": "Target Ottimale", "range": "[5.0, 7.8) mmol/L", "alert": "OPTIMAL"},
    5: {"label": "Sopra Target", "range": "[7.8, 10.0) mmol/L", "alert": "ELEVATED"},
    6: {"label": "Iperglicemia Elevata", "range": "[10.0, 13.9) mmol/L", "alert": "WARNING_HYPER"},
    7: {"label": "Iperglicemia Grave", "range": "[>= 13.9) mmol/L", "alert": "CRITICAL_HYPER"}
}


class PredictionRequest(BaseModel):
    """
    Schema del corpo (body) della richiesta HTTP POST inviata al server (/predict).
    Pydantic valida il JSON in ingresso prima che qualsiasi riga di logica applicativa
    venga eseguita, bloccando alla radice tentativi di iniezione o dati corrotti.
    """

    # patient_id: identificativo anonimizzato del paziente (es. 'PATIENT_540')
    # - min_length=1: impedisce l'invio di stringhe vuote
    # - max_length=64: previene attacchi di esaurimento memoria (buffer overflow logico)
    # - pattern regex: accetta SOLO lettere, cifre, underscore e trattini, bloccando caratteri speciali
    patient_id: str = Field(
        default="ANONYMOUS_PATIENT",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        description="ID anonimizzato del paziente o del sensore (alfanumerico, trattini, underscore)"
    )

    # glucose_history: lista di 13 misurazioni consecutive di glucosio (in mmol/L)
    # Copre 60 minuti di monitoraggio (1 misurazione ogni 5 minuti: da G(t-12) fino a G(t))
    glucose_history: List[float] = Field(
        ...,
        description="13 misurazioni consecutive di glucosio ogni 5 min (da G(t-12) a G(t)) espresse in mmol/L",
        json_schema_extra={
            "example": [
                4.2, 4.0, 3.8, 3.6, 3.5, 3.7, 3.9, 4.3, 5.0, 5.5, 6.1, 6.7, 7.3
            ]
        }
    )

    # exogenous_inputs: 14 feature di carboidrati e insulina [I(t), CHO(t), I(t+1..6), CHO(t+1..6)]
    # Se il paziente non ha inserito pasti o boli, la lista viene inizializzata a tutti zeri
    exogenous_inputs: Optional[List[float]] = Field(
        default_factory=lambda: [0.0] * 14,
        description="14 valori: [I(t), CHO(t)] + 6 coppie [I(t+k), CHO(t+k)] pianificate. Default: tutti zeri.",
        json_schema_extra={
            "example": [
                3.6, 0.0,
                3.8, 0.0,
                3.9, 0.0,
                4.1, 0.0,
                4.2, 0.0,
                4.3, 0.0,
                4.5, 0.0
            ]
        }
    )

    # dg_history: 13 valori opzionali di variazione glicemica dG(t-m).
    # Se omesso o impostato a None, l'API calcola automaticamente la derivata discreta dG = G(t) - G(t-1).
    dg_history: Optional[List[float]] = Field(
        default=None,
        description="13 valori opzionali di derivata dG. Se omessi, vengono calcolati in automatico dalla serie temporale."
    )

    @field_validator("dg_history")
    @classmethod
    def validate_dg_history(cls, dg: Optional[List[float]]) -> Optional[List[float]]:
        """
        Controlla che la serie opzionale delle derivate dG contenga esattamente 13 valori finiti
        e compresi nei limiti fisiologici di velocita di variazione [-15.0, 15.0] mmol/L/5min.
        """
        if dg is None:
            return None
        if len(dg) != 13:
            raise ValueError(
                f"La serie delle derivate dG deve contenere esattamente 13 misurazioni, ricevute {len(dg)}."
            )
        for idx, val in enumerate(dg):
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"Valore non numerico (NaN/Inf) rilevato nella derivata #{idx}.")
            if not (-15.0 <= val <= 15.0):
                raise ValueError(
                    f"Derivata dG al passo #{idx} ({val:.2f} mmol/L/5min) fuori dai limiti fisiologici [-15.0, 15.0]."
                )
        return dg

    @field_validator("glucose_history")
    @classmethod
    def validate_glucose_history(cls, history: List[float]) -> List[float]:
        """
        Controlla l'integrità strutturale e i range fisiologici della serie temporale di glucosio.
        Se un valore è anomalo o corrotto, solleva un ValueError che FastAPI converte
        automaticamente in un codice HTTP 422 Unprocessable Entity.
        """
        if len(history) != 13:
            raise ValueError(
                f"La cronologia glicemica deve contenere esattamente 13 misurazioni (60 minuti), ricevute {len(history)}."
            )

        for idx, val in enumerate(history):
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"Valore non numerico (NaN/Inf) rilevato alla misurazione #{idx}.")
            if not (GLUCOSE_MIN_MMOL <= val <= GLUCOSE_MAX_MMOL):
                raise ValueError(
                    f"Glucosio al passo #{idx} ({val:.2f} mmol/L) fuori dai limiti fisiologici consentiti "
                    f"[{GLUCOSE_MIN_MMOL:.1f} - {GLUCOSE_MAX_MMOL:.1f} mmol/L]."
                )

        return history

    @field_validator("exogenous_inputs")
    @classmethod
    def validate_exogenous(cls, exog: Optional[List[float]]) -> List[float]:
        """
        Controlla che il vettore esogeno contenga esattamente 14 elementi numerici validi.
        """
        if exog is None:
            return [0.0] * 14
        if len(exog) != 14:
            raise ValueError(
                f"Il vettore delle variabili esogene deve contenere esattamente 14 valori, ricevuti {len(exog)}."
            )
        for idx, val in enumerate(exog):
            if math.isnan(val) or math.isinf(val):
                raise ValueError(f"Valore non numerico (NaN/Inf) nella variabile esogena #{idx}.")
            if val < 0.0:
                raise ValueError(f"Valore negativo non ammesso per la variabile esogena #{idx}: {val}.")
            # Controllo tetti massimi clinici: indici pari = Insulina (<=100 U), dispari = Carboidrati (<=300 g)
            max_limit = INSULIN_MAX_U if idx % 2 == 0 else CARBS_MAX_G
            if val > max_limit:
                label = "Insulina" if idx % 2 == 0 else "Carboidrati"
                raise ValueError(
                    f"Valore eccessivo per {label} al passo #{idx}: {val:.1f} (massimo consentito: {max_limit:.1f})."
                )
        return exog


class PredictionResponse(BaseModel):
    """
    Schema formale della risposta HTTP 200 restituita al client / dispositivo medico.
    Fornisce la previsione sia in unità internazionali (mmol/L) sia nell'unità standard
    clinica italiana (mg/dL), unitamente alla classificazione del rischio e al livello di allerta.
    """
    patient_id: str = Field(..., description="Identificativo del paziente")
    current_glucose_mmol_l: float = Field(..., description="Glucosio attuale G(t) in mmol/L")
    current_glucose_mg_dl: float = Field(..., description="Glucosio attuale convertito in mg/dL (G * 18.018)")
    forecasted_glucose_mmol_l: float = Field(..., description="Previsione di glicemia a +30 minuti (G(t+6)) in mmol/L")
    forecasted_glucose_mg_dl: float = Field(..., description="Previsione a +30 minuti convertita in mg/dL")
    delta_glucose: float = Field(..., description="Variazione glicemica stimata nei successivi 30 minuti (mmol/L)")
    risk_class: int = Field(..., description="Classe di rischio clinico (1: Ipoglicemia Severa ... 7: Iperglicemia Grave)")
    risk_label: str = Field(..., description="Descrizione testuale della fascia di rischio")
    clinical_alert: str = Field(..., description="Codice di allerta clinica: OPTIMAL, NORMAL, ELEVATED, WARNING_*, CRITICAL_*")
    model_version: str = Field(..., description="Versione del modello ML caricato in memoria")
    inference_engine: str = Field(default="ONNX-Runtime-C++", description="Motore di esecuzione dell'inferenza")
    request_id: str = Field(..., description="Identificativo univoco di tracciamento transazione (Audit Log)")
    status: str = "success"


class HealthResponse(BaseModel):
    """
    Schema della risposta per le sonde di controllo dello stato del container in Kubernetes:
    - Liveness Probe (/health): verifica la vitalità del processo web.
    - Readiness Probe (/ready): verifica la disponibilità del modello per servire traffico.
    """
    status: str          # Es. "healthy" o "ready"
    model_loaded: bool   # True se il runtime ONNX è correttamente inizializzato in RAM
    model_version: str  # Versione del modello caricata dai metadati
    app_version: str    # Versione dell'applicazione FastAPI
