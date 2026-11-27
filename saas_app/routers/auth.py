"""
FILE: saas_app/routers/auth.py
RUOLO: Router HTTP per autenticazione EdDSA (cookie HttpOnly), revoca Redis/DB e provisioning 2-step.
"""

import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_app import repositories as repo
from saas_app.auth import (
    _DUMMY_BCRYPT_HASH,
    _extract_raw_token,
    add_audit_event,
    create_access_token,
    decode_token_locally,
    generate_temporary_password,
    get_current_user,
    hash_password,
    login_rate_limiter,
    password_version,
    publish_revoked_jti,
    publish_revoked_pwdv,
    verify_password,
)
from saas_app.config import saas_settings
from saas_app.db import get_db
from saas_app.models import User
from saas_app.schemas import ChangePasswordRequest, LoginRequest, TokenResponse, UserInfo

router = APIRouter(prefix="/api/auth", tags=["Authentication & IAM"])

_WEAK_PASSWORDS = {
    "password", "password123", "12345678", "123456789", "qwerty123",
    "admin123", "diabetes", "diabetes123", "paziente123", "medico123",
    "changeme", "letmein123", "welcome123",
    "qwerty", "abc123", "11111111", "00000000", "iloveyou1", "test1234",
    "monkey", "letmein", "dragon", "password1", "iloveyou",
}


def _build_user_info(db: Session, user: User) -> UserInfo:
    patient_code = repo.get_patient_code_by_user_id(db, user.id) if user.role == "patient" else None
    return UserInfo(
        id=user.id,
        username=user.username,
        role=user.role,  # type: ignore[arg-type]
        patient_code=patient_code,
        must_change_password=user.must_change_password,
    )


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    ip = _client_ip(request)
    username = payload.username.strip()

    login_rate_limiter.check_and_record(f"ip:{ip}")
    login_rate_limiter.check_and_record(f"user:{username}")

    user = repo.get_user_by_username(db, username)

    if user is None:
        verify_password(payload.password, _DUMMY_BCRYPT_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide.",
        )

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide.",
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disattivato. Contatta l'amministratore.",
        )

    token, _, _ = create_access_token(user)
    add_audit_event(db, user, "LOGIN", getattr(request.state, "request_id", "unknown"))
    db.commit()

    response.set_cookie(
        key=saas_settings.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=saas_settings.COOKIE_SECURE,
        samesite="strict",
        max_age=saas_settings.JWT_EXPIRE_MINUTES * 60,
        path="/",
    )
    return TokenResponse(
        expires_in_minutes=saas_settings.JWT_EXPIRE_MINUTES,
        user=_build_user_info(db, user),
    )


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Password attuale non corretta.",
        )
    if payload.new_password == payload.old_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La nuova password deve essere diversa da quella attuale.",
        )
    if payload.new_password.lower() in _WEAK_PASSWORDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password troppo comune: scegline una più robusta.",
        )

    old_pwdv = password_version(current_user.password_hash)
    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    add_audit_event(db, current_user, "PASSWORD_CHANGED", getattr(request.state, "request_id", "unknown"))
    db.commit()
    publish_revoked_pwdv(current_user.id, old_pwdv)

    token, _, _ = create_access_token(current_user)
    response.set_cookie(
        key=saas_settings.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=saas_settings.COOKIE_SECURE,
        samesite="strict",
        max_age=saas_settings.JWT_EXPIRE_MINUTES * 60,
        path="/",
    )
    return {"status": "password_changed"}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    raw_token = _extract_raw_token(request)
    jti_to_revoke: Optional[str] = None
    exp_ts: Optional[datetime] = None
    if raw_token:
        payload = decode_token_locally(raw_token, db)
        jti_to_revoke = str(payload["jti"])
        exp_ts = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
        repo.revoke_jti(db, jti_to_revoke, exp_ts)
    add_audit_event(db, current_user, "LOGOUT", getattr(request.state, "request_id", "unknown"))
    db.commit()
    if jti_to_revoke and exp_ts is not None:
        publish_revoked_jti(jti_to_revoke, exp_ts)

    response.delete_cookie(
        key=saas_settings.COOKIE_NAME,
        path="/",
        httponly=True,
        secure=saas_settings.COOKIE_SECURE,
        samesite="strict",
    )
    return {"status": "logged_out"}


@router.get("/me", response_model=UserInfo)
def me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserInfo:
    return _build_user_info(db, current_user)


# -----------------------------------------------------------------------------
# Endpoint interni di provisioning pazienti (SOLO clinical -> iam con X-Provision-Token)
# Zero lookup PII, zero endpoint DELETE su pazienti (creazione in due passi V1).
# -----------------------------------------------------------------------------
class _InternalProvisionPatientRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    idempotency_key: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


class _InternalActivatePatientRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")


def _require_provision_caller(
    x_provision_token: Optional[str] = Header(default=None),
    x_forwarded_proto: Optional[str] = Header(default=None),
) -> None:
    if x_forwarded_proto:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Endpoint interno non accessibile dall'esterno.",
        )
    expected = saas_settings.IAM_PROVISION_TOKEN
    if not expected or not x_provision_token or not secrets.compare_digest(x_provision_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token di provisioning IAM non valido.",
        )


@router.post("/internal/patients/provision", status_code=status.HTTP_201_CREATED, include_in_schema=False)
def internal_provision_patient_account(
    payload: _InternalProvisionPatientRequest,
    _: None = Depends(_require_provision_caller),
    db: Session = Depends(get_db),
) -> dict:
    existing_by_key = db.execute(
        select(User).where(User.provision_key == payload.idempotency_key)
    ).scalar_one_or_none()
    if existing_by_key is not None:
        if existing_by_key.status != "pending" or existing_by_key.username != payload.username:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Chiave di provisioning già utilizzata.")
        temporary_password = generate_temporary_password()
        existing_by_key.password_hash = hash_password(temporary_password)
        db.commit()
        return {"user_id": existing_by_key.id, "temporary_password": temporary_password}

    temporary_password = generate_temporary_password()
    new_user = User(
        username=payload.username,
        password_hash=hash_password(temporary_password),
        role="patient",
        must_change_password=True,
        status="pending",
        provision_key=payload.idempotency_key,
    )
    db.add(new_user)
    try:
        db.commit()
        db.refresh(new_user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username già registrato.",
        )
    return {"user_id": new_user.id, "temporary_password": temporary_password}


@router.post("/internal/patients/{user_id}/activate", include_in_schema=False)
def internal_activate_patient_account(
    user_id: int,
    payload: _InternalActivatePatientRequest,
    _: None = Depends(_require_provision_caller),
    db: Session = Depends(get_db),
) -> dict:
    user = db.execute(
        select(User).where(User.id == user_id, User.role == "patient")
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account pending non trovato.")
    if user.provision_key != payload.idempotency_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Chiave di attivazione non corrispondente.")
    if user.status != "active":
        user.status = "active"
        db.commit()
    return {"status": "active", "user_id": user.id}
