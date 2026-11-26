"""
FILE: saas_app/auth.py
RUOLO: IAM (cookie HttpOnly + JWT con JTI e revoca DB, rate limiting login, RBAC, audit atomico).
"""

import hashlib
import logging
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, Optional, Tuple

import bcrypt
import jwt
import redis
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_app.config import saas_settings
from saas_app.db import get_db
from saas_app.models import AuditLog, RevokedToken, User

logger = logging.getLogger("saas-api.auth")

_JWT_ALGORITHM = "HS256"

# Hash fittizio per mitigare timing attack su username inesistenti
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"dummy-timing-attack-mitigation", bcrypt.gensalt()).decode("utf-8")


class LoginRateLimiter:
    """Limitatore brute-force su /api/auth/login con contatore atomico Redis condiviso tra repliche.
    Fallback in-memory per-processo quando Redis non è disponibile (es. test)."""

    _REDIS_RETRY_INTERVAL = 30.0  # secondi tra tentativi di riconnessione Redis

    def __init__(self, max_attempts: int = 12, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._redis: Optional[redis.Redis] = None
        self._last_redis_check: float = 0.0

    def _get_redis(self) -> Optional[redis.Redis]:
        now = time.monotonic()
        # Riprova la connessione Redis ogni _REDIS_RETRY_INTERVAL secondi,
        # non sticky: se Redis torna disponibile viene ripristinato.
        if self._redis is not None:
            try:
                self._redis.ping()
                return self._redis
            except Exception:
                self._redis = None
                self._last_redis_check = now
        if now - self._last_redis_check < self._REDIS_RETRY_INTERVAL:
            return None
        self._last_redis_check = now
        if saas_settings.REDIS_URL:
            try:
                self._redis = redis.from_url(
                    saas_settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                )
                self._redis.ping()
                return self._redis
            except Exception:
                self._redis = None
        return None

    def check_and_record(self, client_key: str) -> None:
        r = self._get_redis()
        if r is not None:
            try:
                redis_key = f"login_rate:{client_key}"
                pipe = r.pipeline(transaction=True)
                pipe.incr(redis_key)
                pipe.expire(redis_key, self.window_seconds, nx=True)
                results = pipe.execute()
                count = int(results[0])
                if count > self.max_attempts:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Troppi tentativi di accesso ravvicinati. Riprova tra un minuto.",
                    )
                return
            except HTTPException:
                raise
            except Exception:
                self._redis = None
                # fallback a in-memory

        now = time.monotonic()
        with self._lock:
            q = self._hits[client_key]
            while q and (now - q[0]) > self.window_seconds:
                q.popleft()
            if len(q) >= self.max_attempts:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Troppi tentativi di accesso ravvicinati. Riprova tra un minuto.",
                )
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
        r = self._get_redis()
        if r is not None:
            try:
                for key in r.scan_iter("login_rate:*"):
                    r.delete(key)
            except Exception as exc:
                logger.warning("Reset del rate limiter Redis non riuscito: %s", exc)


login_rate_limiter = LoginRateLimiter()


def verify_password(plain_password: str, password_hash: str) -> bool:
    if len(plain_password.encode("utf-8")) > 72:
        return False
    return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))


def hash_password(raw_password: str) -> str:
    raw_bytes = raw_password.encode("utf-8")
    if len(raw_bytes) > 72:
        raise ValueError("La password supera il limite di 72 byte UTF-8.")
    return bcrypt.hashpw(raw_bytes, bcrypt.gensalt()).decode("utf-8")


def generate_device_key() -> str:
    """Chiave CGM casuale per dispositivo: 32 byte casuali (43 caratteri base64url)."""
    return secrets.token_urlsafe(32)


def hash_device_key(raw_key: str) -> str:
    """Nel DB si salva solo l'hash SHA-256 della chiave dispositivo (regola 17)."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_temporary_password() -> str:
    """Password temporanea generata dal server per un nuovo account (16 caratteri base64url).

    Come per le chiavi dispositivo, è mostrata una sola volta e non è mai loggata né
    scritta nell'audit (Q5).
    """
    return secrets.token_urlsafe(12)


# Percorsi ancora consentiti quando must_change_password è attivo (regola 14:
# la regola è imposta dal server, non solo dal frontend).
_PASSWORD_CHANGE_ALLOWED_PATHS = frozenset({
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/logout",
})


def _password_version(password_hash: str) -> str:
    """Impronta dell'hash password: cambia ad ogni cambio password e invalida tutte le sessioni."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def create_access_token(user: User) -> Tuple[str, str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=saas_settings.JWT_EXPIRE_MINUTES)
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user.id),
        "jti": jti,
        "username": user.username,
        "role": user.role,
        "pwdv": _password_version(user.password_hash),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, saas_settings.JWT_SECRET_KEY, algorithm=_JWT_ALGORITHM)
    return token, jti, expires_at


def _extract_raw_token(request: Request) -> Optional[str]:
    """Solo cookie HttpOnly. Bearer rimosso (D6)."""
    return request.cookies.get(saas_settings.COOKIE_NAME)


def decode_token_or_401(raw_token: str, db: Session) -> dict:
    try:
        payload = jwt.decode(
            raw_token,
            saas_settings.JWT_SECRET_KEY,
            algorithms=[_JWT_ALGORITHM],
        )
        jti = payload.get("jti")
        if not jti:
            raise ValueError("Missing JTI")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token non valido o scaduto.",
        )

    revoked = db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti)).first()
    if revoked is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione revocata.",
        )
    return payload


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    raw_token = _extract_raw_token(request)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticazione richiesta (RF01).",
        )
    payload = decode_token_or_401(raw_token, db)
    user_id = int(payload["sub"])

    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utente non trovato.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disattivato.",
        )
    if payload.get("pwdv") != _password_version(user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione non più valida. Effettua di nuovo il login.",
        )
    # Cambio password obbligatorio imposto dal server (N3): l'account resta utilizzabile
    # solo su /me, /change-password e /logout finché la password temporanea non è cambiata.
    if user.must_change_password and request.url.path not in _PASSWORD_CHANGE_ALLOWED_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PASSWORD_CHANGE_REQUIRED",
                "message": "È necessario cambiare la password prima di continuare.",
            },
        )
    return user


def require_roles(*allowed_roles: str) -> Callable[[User], User]:
    """Dependency RBAC server-side (RF02, RNF08)."""
    allowed = set(allowed_roles)

    def _checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Accesso negato al ruolo '{current_user.role}'.",
            )
        return current_user

    return _checker


def add_audit_event(
    db: Session,
    user: User,
    action: str,
    request_id: str,
    target_patient_id: Optional[int] = None,
) -> None:
    """
    Aggiunge un record AuditLog nella transazione attiva SENZA eseguire commit separati:
    l'azione clinica e la relativa traccia di audit committano o falliscono insieme.
    """
    db.add(
        AuditLog(
            actor_user_id=user.id,
            actor_role=user.role,
            action=action,
            target_patient_id=target_patient_id,
            request_id=request_id,
        )
    )
