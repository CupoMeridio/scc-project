"""
FILE: saas_app/routers/patient.py
RUOLO: Router HTTP sottile per il Servizio Paziente.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from saas_app.auth import require_roles
from saas_app.db import get_db
from saas_app.models import User
from saas_app.schemas import (
    MealDeclareRequest,
    MealDeclareResponse,
    PatientHistoryResponse,
    PatientSummaryResponse,
)
from saas_app.services import patient_service

router = APIRouter(prefix="/api/patient", tags=["Patient Service"])


@router.get("/summary", response_model=PatientSummaryResponse)
def get_patient_summary(
    request: Request,
    current_user: User = Depends(require_roles("patient")),
    db: Session = Depends(get_db),
) -> PatientSummaryResponse:
    return patient_service.get_summary(db, current_user, request)


@router.get("/history", response_model=PatientHistoryResponse)
def get_patient_history(
    request: Request,
    range: Literal["week", "month", "year"] = Query(default="week"),
    current_user: User = Depends(require_roles("patient")),
    db: Session = Depends(get_db),
) -> PatientHistoryResponse:
    return patient_service.get_history(db, current_user, range, request)


@router.post("/meals", response_model=MealDeclareResponse)
def declare_meal(
    payload: MealDeclareRequest,
    request: Request,
    current_user: User = Depends(require_roles("patient")),
    db: Session = Depends(get_db),
) -> MealDeclareResponse:
    return patient_service.declare_meal(db, current_user, payload, request)
