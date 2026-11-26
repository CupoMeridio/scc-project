"""
FILE: saas_app/clinical.py
RUOLO: Costanti cliniche condivise, validazione risposta ML, profilo esogeno e mapping RF06/RF07.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Literal, Tuple

from pydantic import BaseModel, Field

from common import CLASS_BINS, classify_glucose
from saas_app.config import saas_settings


class MlPredictResponse(BaseModel):
    """Contratto validato della risposta proveniente dal servizio ONNX."""

    patient_id: str
    current_glucose_mmol_l: float = Field(..., ge=1.0, le=35.0)
    forecasted_glucose_mmol_l: float = Field(..., ge=1.0, le=35.0)
    risk_class: int = Field(..., ge=1, le=7)
    model_version: str = "1.0.0"


def range_cutoff(range_name: Literal["week", "month", "year"]) -> datetime:
    """Calcola la data iniziale UTC per le finestre storiche settimanale, mensile e annuale."""
    days = {"week": 7, "month": 30, "year": 365}[range_name]
    return datetime.now(timezone.utc) - timedelta(days=days)


def map_prediction_to_outcome(
    forecast_mmol: float,
    carbs_g: float,
    diet_limit_g: float,
    target_min_mmol: float = 3.9,
    target_max_mmol: float = 10.0,
) -> Tuple[int, str, str, bool]:
    """
    Mappa la predizione nei 4 macro-esiti clinici (RF06, RF07) rispettando
    sia le soglie internazionali (7 classi) sia i target personalizzati dal diabetologo.
    Ritorna: (risk_class, outcome_code, outcome_message, doctor_flag)
    """
    cls = classify_glucose(forecast_mmol)

    if cls in (1, 2) or forecast_mmol < target_min_mmol:
        return (
            cls,
            "RISK_OF_HYPOGLYCEMIA",
            (
                f"Quantità insufficiente o rischio ipoglicemia: previsione a +30 min pari a "
                f"{forecast_mmol:.2f} mmol/L ({forecast_mmol * 18.018:.0f} mg/dL, "
                f"soglia minima {target_min_mmol:.1f} mmol/L). Segnalato per revisione medica."
            ),
            True,
        )
    if cls in (6, 7) or forecast_mmol >= target_max_mmol:
        return (
            cls,
            "RISK_OF_HYPERGLYCEMIA",
            (
                f"Quantità eccessiva o rischio iperglicemia: previsione a +30 min pari a "
                f"{forecast_mmol:.2f} mmol/L ({forecast_mmol * 18.018:.0f} mg/dL, "
                f"soglia massima {target_max_mmol:.1f} mmol/L). Segnalato per revisione medica."
            ),
            True,
        )
    if carbs_g > diet_limit_g:
        return (
            cls,
            "PROCEED_WITH_WARNING",
            (
                f"Carboidrati superiori alla dieta ({carbs_g:.0f} g > {diet_limit_g:.0f} g), "
                "ma la previsione a +30 min resta nei target. Puoi procedere con cautela."
            ),
            False,
        )
    if cls in (3, 5):
        return (
            cls,
            "PROCEED_WITH_WARNING",
            (
                f"Previsione a +30 min ai margini dell'intervallo ottimale ({forecast_mmol:.2f} mmol/L). "
                "Puoi procedere con cautela."
            ),
            False,
        )
    return (
        cls,
        "PROCEED_REGULAR",
        (
            f"Tutto regolare: la previsione a +30 min ({forecast_mmol:.2f} mmol/L / "
            f"{forecast_mmol * 18.018:.0f} mg/dL) rientra nei limiti della dieta."
        ),
        False,
    )


def build_exogenous_vector(
    carbs_g: float,
    bolus_insulin_u: float,
    baseline_insulin_u: float = 1.0,
) -> List[float]:
    """
    Converte carboidrati (g), bolo annunciato (IU) e basale recente dal sensore/pompa
    nel vettore esogeno [I(t), CHO(t), I(t+1..6), CHO(t+1..6)].
    Il bolo viene applicato con profilo impulsivo decadente nei primi passi invece di
    essere ripetuto integralmente su tutta la finestra.
    """
    peak_cho = min(6.5, max(0.0, carbs_g / saas_settings.CARBS_TO_HOVORKA_DIVISOR))
    cho_weights = [0.25, 0.45, 0.70, 0.90, 1.00, 0.95, 0.85]
    bolus_decay = [0.55, 0.30, 0.15, 0.0, 0.0, 0.0, 0.0]

    exog: List[float] = []
    for bw, cw in zip(bolus_decay, cho_weights):
        ins_step = round(max(0.0, min(100.0, baseline_insulin_u + bolus_insulin_u * bw)), 3)
        cho_step = round(peak_cho * cw, 4)
        exog.extend([ins_step, cho_step])
    return exog
