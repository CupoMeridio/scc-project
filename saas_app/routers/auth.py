"""
FILE: saas_app/routers/auth.py
RUOLO: Router IAM con cookie HttpOnly __Host-, rate limiting login (IP + account),
       timing attack mitigation, cambio password forzato.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from saas_app import repositories as repo
from saas_app.auth import (
    _DUMMY_BCRYPT_HASH,
    _extract_raw_token,
    add_audit_event,
    create_access_token,
    decode_token_or_401,
    get_current_user,
    hash_password,
    login_rate_limiter,
    verify_password,
)
from saas_app.config import saas_settings
from saas_app.db import get_db
from saas_app.models import User
from saas_app.schemas import ChangePasswordRequest, LoginRequest, TokenResponse, UserInfo

router = APIRouter(prefix="/api/auth", tags=["IAM / Authentication"])

_COMMON_PASSWORDS = {
    "password", "123456", "12345678", "qwerty", "abc123",
    "monkey", "letmein", "dragon", "password1", "iloveyou",
}


def _build_user_info(db: Session, user: User) -> UserInfo:
    patient_code = None
    if user.role == "patient":
        patient = repo.get_patient_by_user_id(db, user.id)
        if patient is not None:
            patient_code = patient.patient_code
    return UserInfo(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
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

    # Rate limit per IP
    login_rate_limiter.check_and_record(f"ip:{ip}")
    # Rate limit per account
    login_rate_limiter.check_and_record(f"user:{username}")

    user = repo.get_user_by_username(db, username)

    # Timing attack mitigation: se l'utente non esiste, verifica comunque bcrypt
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

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disattivato.",
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
    if payload.new_password.lower() in _COMMON_PASSWORDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password troppo comune.",
        )
    if payload.new_password == payload.old_password:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La nuova password deve essere diversa da quella attuale.",
        )

    # Il cambio password invalida automaticamente tutte le sessioni precedenti
    # (il claim 'pwdv' nei token non corrisponde più all'hash aggiornato).
    current_user.password_hash = hash_password(payload.new_password)
    current_user.must_change_password = False
    add_audit_event(db, current_user, "PASSWORD_CHANGED", getattr(request.state, "request_id", "unknown"))
    db.commit()

    # Emette un nuovo token per la sessione corrente, come cookie HttpOnly.
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
    if raw_token:
        payload = decode_token_or_401(raw_token, db)
        exp_ts = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
        repo.revoke_jti(db, payload["jti"], exp_ts)
    add_audit_event(db, current_user, "LOGOUT", getattr(request.state, "request_id", "unknown"))
    db.commit()

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
