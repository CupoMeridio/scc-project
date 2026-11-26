"""
FILE: saas_app/routers/manager.py
RUOLO: Router HTTP sottile per il Center Manager.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from saas_app.auth import require_roles
from saas_app.db import get_db
from saas_app.models import User
from saas_app.schemas import ManagerDashboardResponse
from saas_app.services import manager_service

router = APIRouter(prefix="/api/manager", tags=["Center Manager Service"])


@router.get("/dashboard", response_model=ManagerDashboardResponse)
def get_manager_dashboard(
    request: Request,
    current_user: User = Depends(require_roles("manager")),
    db: Session = Depends(get_db),
) -> ManagerDashboardResponse:
    return manager_service.get_dashboard(db, current_user, request)
