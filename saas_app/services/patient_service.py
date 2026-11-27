"""
FILE: saas_app/services/patient_service.py
RUOLO: Logica di dominio del Servizio Paziente (UC02, UC03, RF03-RF08, RNF07).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Literal, Optional, Sequence, Tuple

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from saas_app import repositories as repo
from saas_app.auth import Principal, add_audit_event
from saas_app.clinical import build_exogenous_vector, map_prediction_to_outcome, range_cutoff
from saas_app.config import saas_settings
from saas_app.ml_client import MlServiceUnavailableError, call_ml_predict
from saas_app.models import CgmReading, MealPrediction, Patient
from saas_app.schemas import (
    CgmPoint,
    MealDeclareRequest,
    MealDeclareResponse,
    MealHistoryItem,
    PatientHistoryResponse,
    PatientSummaryResponse,
)

_NO_RECENT_DATA_OUTCOME = (
    "NO_RECENT_DATA",
    "Dati del sensore non recenti: misura la glicemia con il metodo abituale "
    "e contatta il medico se necessario.",
)


def _require_patient_profile(db: Session, user: Principal) -> Patient:
    patient = repo.get_patient_by_user_id(db, user.id)
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profilo paziente non trovato.")
    return patient


def _build_cgm_window(db: Session, patient_id: int) -> Optional[Tuple[list[float], float]]:
    """
    Valida e prepara la finestra di 13 campioni CGM consecutivi (passo 5 min).
    Ritorna (values, baseline_insulin) se valida, None se i dati sono mancanti,
    troppo vecchi, non consecutivi o con buchi.
    """
    recent = repo.get_recent_cgm_readings(db, patient_id, limit=13)
    if len(recent) < 13:
        return None

    now = datetime.now(timezone.utc)
    max_age = timedelta(minutes=saas_settings.CGM_MAX_AGE_MINUTES)
    step = timedelta(minutes=saas_settings.CGM_STEP_MINUTES)
    tol = timedelta(minutes=saas_settings.CGM_STEP_TOLERANCE_MINUTES)

    # L'ultima lettura (più recente) non deve essere nel futuro né troppo vecchia
    last = recent[-1]
    last_dt = last.recorded_at
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    if last_dt > now + tol:
        return None
    if last_dt < now - max_age:
        return None

    # Verifica consecutività: ogni campione deve distare ~step dal successivo
    for i in range(len(recent) - 1):
        curr_dt = recent[i].recorded_at
        next_dt = recent[i + 1].recorded_at
        if curr_dt.tzinfo is None:
            curr_dt = curr_dt.replace(tzinfo=timezone.utc)
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        gap = next_dt - curr_dt
        if abs(gap - step) > tol:
            return None

    values = [r.glucose_mmol for r in recent]
    baseline_insulin = recent[-1].insulin_u
    return values, baseline_insulin


def _cgm_points(
    readings: Sequence[CgmReading], range_name: Literal["week", "month", "year"]
) -> List[CgmPoint]:
    """Costruisce i punti CGM; month/year sono aggregati lato server.

    Senza aggregazione, `range=year` restituirebbe fino a ~105.000 punti per
    paziente. Le medie orarie (month) e giornaliere (year) mantengono la forma
    della serie con un payload gestibile.
    """
    if range_name == "week":
        return [CgmPoint.model_validate(r) for r in readings]

    buckets: dict[datetime, dict[str, Any]] = {}
    for r in readings:
        ts = r.recorded_at.astimezone(timezone.utc)
        if range_name == "month":
            key = ts.replace(minute=0, second=0, microsecond=0)
        else:
            key = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        bucket = buckets.setdefault(
            key, {"n": 0, "glucose": 0.0, "insulin": 0.0, "cho": 0.0}
        )
        bucket["n"] += 1
        bucket["glucose"] += r.glucose_mmol
        bucket["insulin"] += r.insulin_u
        bucket["cho"] += r.cho_rate

    points: List[CgmPoint] = []
    for key in sorted(buckets):
        b = buckets[key]
        # Punto aggregato (media del bucket), non una lettura: costruzione esplicita.
        points.append(
            CgmPoint(
                recorded_at=key,
                glucose_mmol=round(b["glucose"] / b["n"], 2),
                insulin_u=round(b["insulin"] / b["n"], 3),
                cho_rate=round(b["cho"] / b["n"], 4),
            )
        )
    return points


def get_summary(db: Session, current_user: Principal, request: Request) -> PatientSummaryResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    patient = _require_patient_profile(db, current_user)
    doctor = repo.get_doctor_profile(db, patient.doctor_id)
    latest = repo.get_latest_cgm_reading(db, patient.id)
    total_meals, flagged_count = repo.count_patient_meals_and_flags(db, patient.id)

    add_audit_event(db, current_user, "PATIENT_VIEW_SUMMARY", request_id, patient.id)
    db.commit()

    return PatientSummaryResponse(
        patient_code=patient.patient_code,
        full_name=patient.full_name,
        doctor_name=doctor.full_name if doctor else "Diabetologo assegnato",
        diet_carb_limit_g=patient.diet_carb_limit_g,
        target_min_mmol=patient.target_min_mmol,
        target_max_mmol=patient.target_max_mmol,
        latest_glucose_mmol=latest.glucose_mmol if latest else None,
        latest_glucose_mg_dl=round(latest.glucose_mmol * 18.018, 1) if latest else None,
        latest_reading_at=latest.recorded_at if latest else None,
        total_meals_recorded=total_meals,
        flagged_events_count=flagged_count,
    )


def get_history(
    db: Session,
    current_user: Principal,
    range_name: Literal["week", "month", "year"],
    request: Request,
) -> PatientHistoryResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    patient = _require_patient_profile(db, current_user)
    cutoff = range_cutoff(range_name)
    readings = repo.get_cgm_readings_since(db, patient.id, cutoff)
    meals = repo.get_meals_since(db, patient.id, cutoff)

    add_audit_event(db, current_user, f"PATIENT_VIEW_HISTORY_{range_name.upper()}", request_id, patient.id)
    db.commit()

    return PatientHistoryResponse(
        range=range_name,
        diet_carb_limit_g=patient.diet_carb_limit_g,
        readings=_cgm_points(readings, range_name),
        meals=[
            MealHistoryItem(
                id=m.id,
                declared_at=m.declared_at,
                carbs_g=m.carbs_g,
                insulin_u=m.insulin_u,
                current_glucose_mmol=m.current_glucose_mmol,
                forecasted_glucose_mmol=m.forecasted_glucose_mmol,
                actual_glucose_30m_mmol=m.actual_glucose_30m_mmol,
                risk_class=m.risk_class,
                outcome_code=m.outcome_code,
                outcome_message=m.outcome_message,
                doctor_flag=m.doctor_flag,
            )
            for m in meals
        ],
    )


def _prepare_meal_inputs(
    db: Session,
    current_user: Principal,
    payload: MealDeclareRequest,
) -> Tuple[Patient, list[float], list[float]]:
    patient = _require_patient_profile(db, current_user)
    cgm = _build_cgm_window(db, patient.id)
    if cgm is None:
        raise _NoRecentDataError()
    glucose_history, baseline_ins = cgm
    exog = build_exogenous_vector(
        carbs_g=payload.carbs_g,
        bolus_insulin_u=payload.insulin_u,
        baseline_insulin_u=baseline_ins,
    )
    return patient, glucose_history, exog


class _NoRecentDataError(Exception):
    """Segnala che i dati CGM sono mancanti, troppo vecchi o non consecutivi."""
    pass


def _save_meal_and_audit(
    db: Session,
    current_user: Principal,
    patient: Patient,
    payload: MealDeclareRequest,
    current_mmol: Optional[float],
    forecast_mmol: Optional[float],
    risk_class: Optional[int],
    outcome_code: str,
    outcome_msg: str,
    doctor_flag: bool,
    model_version: str,
    request_id: str,
) -> MealPrediction:
    meal_record = MealPrediction(
        patient_id=patient.id,
        carbs_g=payload.carbs_g,
        insulin_u=payload.insulin_u,
        current_glucose_mmol=current_mmol,
        forecasted_glucose_mmol=forecast_mmol,
        risk_class=risk_class,
        outcome_code=outcome_code,
        outcome_message=outcome_msg,
        doctor_flag=doctor_flag,
        model_version=model_version,
    )
    db.add(meal_record)
    add_audit_event(db, current_user, "PATIENT_DECLARE_MEAL", request_id, patient.id)
    # Transazione atomica unica: pasto + audit committano insieme
    db.commit()
    db.refresh(meal_record)
    return meal_record


def declare_meal(
    db: Session,
    current_user: Principal,
    payload: MealDeclareRequest,
    request: Request,
) -> MealDeclareResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    patient = _require_patient_profile(db, current_user)

    try:
        patient, glucose_history, exog = _prepare_meal_inputs(db, current_user, payload)
    except _NoRecentDataError:
        outcome_code, outcome_msg = _NO_RECENT_DATA_OUTCOME
        # Nessun dato del sensore: si salva il pasto con valori nulli (non 0.0) e
        # classe di rischio nulla, così nessuna statistica li conta come reali (N5).
        meal_record = _save_meal_and_audit(
            db,
            current_user,
            patient,
            payload,
            current_mmol=None,
            forecast_mmol=None,
            risk_class=None,
            outcome_code=outcome_code,
            outcome_msg=outcome_msg,
            doctor_flag=True,
            model_version="none",
            request_id=request_id,
        )
        return MealDeclareResponse(
            meal_id=meal_record.id,
            declared_at=meal_record.declared_at,
            carbs_g=meal_record.carbs_g,
            diet_carb_limit_g=patient.diet_carb_limit_g,
            current_glucose_mmol_l=None,
            current_glucose_mg_dl=None,
            forecasted_glucose_mmol_l=None,
            forecasted_glucose_mg_dl=None,
            risk_class=None,
            outcome_code=outcome_code,
            outcome_message=outcome_msg,
            doctor_flag=True,
            model_version="none",
        )

    try:
        ml_resp = call_ml_predict(patient.patient_code, glucose_history, exog)
    except MlServiceUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servizio di predizione ML temporaneamente non disponibile.",
        )

    forecast_mmol = ml_resp.forecasted_glucose_mmol_l
    current_mmol = ml_resp.current_glucose_mmol_l

    risk_class, outcome_code, outcome_msg, doctor_flag = map_prediction_to_outcome(
        forecast_mmol=forecast_mmol,
        carbs_g=payload.carbs_g,
        diet_limit_g=patient.diet_carb_limit_g,
        target_min_mmol=patient.target_min_mmol,
        target_max_mmol=patient.target_max_mmol,
    )

    meal_record = _save_meal_and_audit(
        db,
        current_user,
        patient,
        payload,
        current_mmol,
        forecast_mmol,
        risk_class,
        outcome_code,
        outcome_msg,
        doctor_flag,
        ml_resp.model_version,
        request_id,
    )

    return MealDeclareResponse(
        meal_id=meal_record.id,
        declared_at=meal_record.declared_at,
        carbs_g=meal_record.carbs_g,
        diet_carb_limit_g=patient.diet_carb_limit_g,
        current_glucose_mmol_l=current_mmol,
        current_glucose_mg_dl=round(current_mmol * 18.018, 1),
        forecasted_glucose_mmol_l=forecast_mmol,
        forecasted_glucose_mg_dl=round(forecast_mmol * 18.018, 1),
        risk_class=risk_class,
        outcome_code=outcome_code,
        outcome_message=outcome_msg,
        doctor_flag=doctor_flag,
        model_version=ml_resp.model_version,
    )
