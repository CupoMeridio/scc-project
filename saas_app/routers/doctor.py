"""
FILE: saas_app/routers/doctor.py
RUOLO: Router HTTP sottile per il Servizio Diabetologo.
"""

from typing import List, Literal

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from saas_app.auth import Principal, require_roles
from saas_app.db import get_db
from saas_app.schemas import (
    CreateInterventionRequest,
    DoctorPatientDetailResponse,
    DoctorPatientItem,
    InterventionItem,
    RegisterPatientRequest,
    RegisterPatientResponse,
    UpdateDietRequest,
)
from saas_app.services import doctor_service

router = APIRouter(prefix="/api/doctor", tags=["Diabetologist Service"])


@router.get("/patients", response_model=List[DoctorPatientItem])
def list_assigned_patients(
    request: Request,
    current_user: Principal = Depends(require_roles("doctor")),
    db: Session = Depends(get_db),
) -> List[DoctorPatientItem]:
    return doctor_service.list_patients(db, current_user, request)


@router.get("/patients/{patient_id}", response_model=DoctorPatientDetailResponse)
def get_patient_detail(
    patient_id: int,
    request: Request,
    range: Literal["week", "month", "year"] = Query(default="week"),
    current_user: Principal = Depends(require_roles("doctor")),
    db: Session = Depends(get_db),
) -> DoctorPatientDetailResponse:
    return doctor_service.get_patient_detail(db, current_user, patient_id, range, request)


@router.post("/patients", response_model=RegisterPatientResponse, status_code=status.HTTP_201_CREATED)
def register_new_patient(
    payload: RegisterPatientRequest,
    request: Request,
    current_user: Principal = Depends(require_roles("doctor")),
    db: Session = Depends(get_db),
) -> RegisterPatientResponse:
    return doctor_service.register_patient(db, current_user, payload, request)


@router.patch("/patients/{patient_id}/diet")
def update_patient_diet(
    patient_id: int,
    payload: UpdateDietRequest,
    request: Request,
    current_user: Principal = Depends(require_roles("doctor")),
    db: Session = Depends(get_db),
) -> dict:
    return doctor_service.update_diet(db, current_user, patient_id, payload, request)


@router.post("/patients/{patient_id}/interventions", response_model=InterventionItem, status_code=status.HTTP_201_CREATED)
def create_intervention(
    patient_id: int,
    payload: CreateInterventionRequest,
    request: Request,
    current_user: Principal = Depends(require_roles("doctor")),
    db: Session = Depends(get_db),
) -> InterventionItem:
    return doctor_service.create_intervention(db, current_user, patient_id, payload, request)
