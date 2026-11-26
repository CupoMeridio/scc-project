"""
FILE: saas_app/services/doctor_service.py
RUOLO: Logica di dominio del Servizio Diabetologo (UC04-UC07, RF09-RF15, RF21, RNF08).
"""

from typing import List, Literal, Optional, Tuple

from fastapi import HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_app import repositories as repo
from saas_app.auth import add_audit_event, generate_temporary_password, hash_password
from saas_app.clinical import range_cutoff
from saas_app.db import SessionLocal
from saas_app.models import Intervention, MealPrediction, Patient, User
from saas_app.schemas import (
    CgmPoint,
    CreateInterventionRequest,
    DoctorPatientDetailResponse,
    DoctorPatientItem,
    InterventionItem,
    MealHistoryItem,
    PatientTrendSummary,
    RegisterPatientRequest,
    RegisterPatientResponse,
    UpdateDietRequest,
)


def _require_assigned_patient(db: Session, doctor_user: User, patient_id: int, request_id: str = "unknown") -> Patient:
    patient = repo.get_patient_by_id(db, patient_id)
    if patient is None or patient.doctor_id != doctor_user.id:
        # Non rivelare l'esistenza del paziente: 404 sia per inesistente che per non assegnato.
        # Registra l'accesso negato in transazione separata (l'HTTPException fa rollback).
        with SessionLocal() as audit_db:
            add_audit_event(
                audit_db, doctor_user, "DOCTOR_ACCESS_DENIED", request_id,
                patient.id if patient else patient_id,
            )
            audit_db.commit()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Paziente non trovato.",
        )
    return patient


def _compute_triage_status(
    flags_count: int,
    latest_meal: Optional[MealPrediction],
) -> Tuple[Literal["green", "orange", "red"], str]:
    if flags_count > 0 or (
        latest_meal is not None
        and latest_meal.outcome_code in ("RISK_OF_HYPOGLYCEMIA", "RISK_OF_HYPERGLYCEMIA")
    ):
        return "red", f"{flags_count} segnalazioni di rischio (ipo/iperglicemia)"
    if latest_meal is not None and latest_meal.outcome_code == "PROCEED_WITH_WARNING":
        return "orange", "Carboidrati sopra dieta o valori borderline"
    return "green", "Valori nella norma"


def list_patients(db: Session, current_user: User, request: Request) -> List[DoctorPatientItem]:
    request_id = getattr(request.state, "request_id", "unknown")
    patients = repo.list_patients_for_doctor(db, current_user.id)
    patient_ids = [p.id for p in patients]

    # Query batch (una per tipo di dato) al posto di 4 query per ogni paziente (RN08).
    users = repo.get_users_by_ids(db, [p.user_id for p in patients])
    latest_cgm = repo.get_latest_cgm_readings_for_patients(db, patient_ids)
    latest_meals = repo.get_latest_meals_for_patients(db, patient_ids)
    flags = repo.count_flags_by_patient(db, patient_ids)

    items: List[DoctorPatientItem] = []
    for p in patients:
        p_user = users.get(p.user_id)
        cgm = latest_cgm.get(p.id)
        meal = latest_meals.get(p.id)
        flags_count = flags.get(p.id, 0)
        color, summary = _compute_triage_status(flags_count, meal)

        items.append(
            DoctorPatientItem(
                patient_id=p.id,
                patient_code=p.patient_code,
                full_name=p_user.full_name if p_user else p.patient_code,
                diet_carb_limit_g=p.diet_carb_limit_g,
                latest_glucose_mmol=cgm.glucose_mmol if cgm else None,
                latest_outcome=meal.outcome_code if meal else None,
                active_flags_count=flags_count,
                status_color=color,
                status_summary=summary,
            )
        )

    add_audit_event(db, current_user, "DOCTOR_LIST_PATIENTS", request_id)
    db.commit()
    return items


def get_patient_detail(
    db: Session,
    current_user: User,
    patient_id: int,
    range_name: Literal["week", "month", "year"],
    request: Request,
) -> DoctorPatientDetailResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    patient = _require_assigned_patient(db, current_user, patient_id, request_id)
    p_user = repo.get_user_by_id(db, patient.user_id)
    if p_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account paziente mancante.")

    cutoff = range_cutoff(range_name)
    readings = repo.get_cgm_readings_since(db, patient.id, cutoff)
    meals = repo.get_meals_since(db, patient.id, cutoff)
    interventions = repo.list_interventions_for_patient(db, patient.id)

    # I pasti senza dati del sensore (NO_RECENT_DATA) non hanno una previsione reale
    # e restano fuori dal calcolo del bias del modello (N5).
    errors = [
        (m.forecasted_glucose_mmol - m.actual_glucose_30m_mmol)
        for m in meals
        if m.actual_glucose_30m_mmol is not None
        and m.forecasted_glucose_mmol is not None
        and m.outcome_code != "NO_RECENT_DATA"
    ]
    mean_err = sum(errors) / len(errors) if errors else None
    if mean_err is None:
        # Nessuna coppia previsione/reale ancora disponibile (P9): non si dichiara
        # che il modello è allineato senza prove.
        bias_label = "Dati insufficienti per valutare il modello"
    elif mean_err > 0.35:
        bias_label = "Il modello tende a sovrastimare leggermente la glicemia"
    elif mean_err < -0.35:
        bias_label = "Il modello tende a sottostimare leggermente la glicemia"
    else:
        bias_label = "Previsioni allineate ai valori misurati"

    above_diet = sum(1 for m in meals if m.carbs_g > patient.diet_carb_limit_g)
    above_diet_pct = (above_diet / len(meals) * 100.0) if meals else 0.0
    days_span = {"week": 7.0, "month": 30.0, "year": 365.0}[range_name]
    regularity = len(meals) / days_span
    flagged = sum(1 for m in meals if m.doctor_flag)

    add_audit_event(db, current_user, f"DOCTOR_VIEW_PATIENT_{range_name.upper()}", request_id, patient.id)
    db.commit()

    return DoctorPatientDetailResponse(
        patient_id=patient.id,
        patient_code=patient.patient_code,
        full_name=p_user.full_name,
        email=p_user.email,
        diet_carb_limit_g=patient.diet_carb_limit_g,
        target_min_mmol=patient.target_min_mmol,
        target_max_mmol=patient.target_max_mmol,
        range=range_name,
        readings=[CgmPoint.model_validate(r) for r in readings],
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
        interventions=[
            InterventionItem(
                id=iv.id,
                created_at=iv.created_at,
                intervention_type=iv.intervention_type,
                cause=iv.cause,
                notes=iv.notes,
                new_diet_carb_limit_g=iv.new_diet_carb_limit_g,
            )
            for iv in interventions
        ],
        trends=PatientTrendSummary(
            mean_prediction_error_mmol=round(mean_err, 2) if mean_err is not None else None,
            model_bias_label=bias_label,
            above_diet_meals_pct=round(above_diet_pct, 1),
            meal_regularity_per_day=round(regularity, 2),
            flagged_events_count=flagged,
        ),
    )


def register_patient(
    db: Session,
    current_user: User,
    payload: RegisterPatientRequest,
    request: Request,
) -> RegisterPatientResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    # La password non è più scelta dal medico: il server ne genera una temporanea,
    # la restituisce una sola volta e impone il cambio al primo accesso (Q5, N3).
    temporary_password = generate_temporary_password()
    new_user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(temporary_password),
        role="patient",
        must_change_password=True,
    )
    db.add(new_user)
    try:
        db.flush()
        new_patient = Patient(
            user_id=new_user.id,
            doctor_id=current_user.id,
            patient_code=payload.patient_code,
            diet_carb_limit_g=payload.diet_carb_limit_g,
            target_min_mmol=payload.target_min_mmol,
            target_max_mmol=payload.target_max_mmol,
        )
        db.add(new_patient)
        db.flush()
        add_audit_event(db, current_user, "DOCTOR_REGISTER_PATIENT", request_id, new_patient.id)
        db.commit()
        db.refresh(new_patient)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username, email o codice paziente già registrato.",
        )

    return RegisterPatientResponse(
        patient_id=new_patient.id,
        patient_code=new_patient.patient_code,
        full_name=new_user.full_name,
        diet_carb_limit_g=new_patient.diet_carb_limit_g,
        latest_glucose_mmol=None,
        latest_outcome=None,
        active_flags_count=0,
        status_color="green",
        status_summary="Nuovo paziente registrato (in attesa di letture CGM)",
        temporary_password=temporary_password,
    )


def update_diet(
    db: Session,
    current_user: User,
    patient_id: int,
    payload: UpdateDietRequest,
    request: Request,
) -> dict:
    request_id = getattr(request.state, "request_id", "unknown")
    patient = _require_assigned_patient(db, current_user, patient_id, request_id)
    new_min = payload.target_min_mmol if payload.target_min_mmol is not None else patient.target_min_mmol
    new_max = payload.target_max_mmol if payload.target_max_mmol is not None else patient.target_max_mmol
    if new_min >= new_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="target_min_mmol deve essere inferiore a target_max_mmol.",
        )

    patient.diet_carb_limit_g = payload.diet_carb_limit_g
    patient.target_min_mmol = new_min
    patient.target_max_mmol = new_max
    add_audit_event(db, current_user, "DOCTOR_UPDATE_DIET", request_id, patient.id)
    db.commit()
    return {"status": "updated", "diet_carb_limit_g": patient.diet_carb_limit_g}


def create_intervention(
    db: Session,
    current_user: User,
    patient_id: int,
    payload: CreateInterventionRequest,
    request: Request,
) -> InterventionItem:
    request_id = getattr(request.state, "request_id", "unknown")
    patient = _require_assigned_patient(db, current_user, patient_id, request_id)
    iv = Intervention(
        patient_id=patient.id,
        doctor_id=current_user.id,
        intervention_type=payload.intervention_type,
        cause=payload.cause,
        notes=payload.notes,
        new_diet_carb_limit_g=payload.new_diet_carb_limit_g,
    )
    if payload.intervention_type == "diet_change" and payload.new_diet_carb_limit_g is not None:
        patient.diet_carb_limit_g = payload.new_diet_carb_limit_g

    db.add(iv)
    add_audit_event(db, current_user, "DOCTOR_CREATE_INTERVENTION", request_id, patient.id)
    db.commit()
    db.refresh(iv)

    return InterventionItem(
        id=iv.id,
        created_at=iv.created_at,
        intervention_type=iv.intervention_type,
        cause=iv.cause,
        notes=iv.notes,
        new_diet_carb_limit_g=iv.new_diet_carb_limit_g,
    )
