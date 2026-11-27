"""
FILE: saas_app/auth.py
RUOLO: IAM (cookie HttpOnly + JWT EdDSA Ed25519 con JTI/revoca Redis in sola lettura, rate limiter, RBAC, audit).
"""

import hashlib
import logging
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque, Dict, Optional, Tuple, Union

import bcrypt
import jwt
import redis
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_app.config import saas_settings
from saas_app.db import get_db
from saas_app.iam_client import AuthenticatedUser
from saas_app.models import AuditLog, RevokedToken, User

Principal = Union[User, AuthenticatedUser]

logger = logging.getLogger("saas-api.auth")

_JWT_ALGORITHM = "EdDSA"
_cached_priv_pem: Optional[str] = None
_cached_priv_key: Optional[Ed25519PrivateKey] = None
_cached_pub_pem: Optional[str] = None
_cached_pub_key: Optional[Ed25519PublicKey] = None
_key_cache_lock = threading.Lock()


def _get_jwt_private_key():
    global _cached_priv_pem, _cached_priv_key
    pem = saas_settings.JWT_PRIVATE_KEY_PEM
    if pem == _cached_priv_pem and _cached_priv_key is not None:
        return _cached_priv_key
    with _key_cache_lock:
        if pem != _cached_priv_pem or _cached_priv_key is None:
            _cached_priv_key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
            _cached_priv_pem = pem
        return _cached_priv_key


def _get_jwt_public_key():
    global _cached_pub_pem, _cached_pub_key
    pem = saas_settings.JWT_PUBLIC_KEY_PEM
    if pem == _cached_pub_pem and _cached_pub_key is not None:
        return _cached_pub_key
    with _key_cache_lock:
        if pem != _cached_pub_pem or _cached_pub_key is None:
            _cached_pub_key = serialization.load_pem_public_key(pem.encode("utf-8"))
            _cached_pub_pem = pem
        return _cached_pub_key

# Hash fittizio per mitigare timing attack su username inesistenti
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"dummy-timing-attack-mitigation", bcrypt.gensalt()).decode("utf-8")

_revocation_redis: Optional[redis.Redis] = None
_revocation_lock = threading.Lock()


def _redis_kwargs() -> dict:
    kwargs: dict = {
        "decode_responses": True,
        "socket_connect_timeout": 1.0,
        "socket_timeout": 1.0,
    }
    if saas_settings.REDIS_CA_CERT_PATH:
        kwargs["ssl_ca_certs"] = saas_settings.REDIS_CA_CERT_PATH
        kwargs["ssl_cert_reqs"] = "required"
    return kwargs


def _get_revocation_redis(readonly: bool = True) -> Optional[redis.Redis]:
    """Client Redis dedicato alla revoca (ACL sola lettura `revocation_ro` in clinical/manager)."""
    url = (
        saas_settings.REDIS_REVOCATION_URL
        if readonly and saas_settings.REDIS_REVOCATION_URL
        else saas_settings.REDIS_URL
    )
    if not url:
        return None
    global _revocation_redis
    with _revocation_lock:
        if _revocation_redis is None:
            _revocation_redis = redis.from_url(url, **_redis_kwargs())
        return _revocation_redis


def reset_revocation_redis() -> None:
    global _revocation_redis
    with _revocation_lock:
        if _revocation_redis is not None:
            try:
                _revocation_redis.close()
            except Exception:
                pass
            _revocation_redis = None


def publish_revoked_jti(jti: str, expires_at: datetime) -> None:
    r = _get_revocation_redis(readonly=False)
    if r is None:
        return
    ttl = max(60, int((expires_at - datetime.now(timezone.utc)).total_seconds()) + 60)
    try:
        r.setex(f"revoked:jti:{jti}", ttl, "1")
    except Exception as exc:
        logger.warning("Impossibile pubblicare revoca JTI su Redis: %s", exc)


def publish_revoked_pwdv(user_id: int, old_pwdv: str) -> None:
    r = _get_revocation_redis(readonly=False)
    if r is None:
        return
    ttl = max(600, saas_settings.JWT_EXPIRE_MINUTES * 60 + 120)
    try:
        r.setex(f"revoked:pwdv:{user_id}:{old_pwdv}", ttl, "1")
    except Exception as exc:
        logger.warning("Impossibile pubblicare invalidazione pwdv su Redis: %s", exc)


class LoginRateLimiter:
    """Limitatore brute-force su /api/auth/login con contatore atomico Redis condiviso tra repliche."""

    _REDIS_RETRY_INTERVAL = 30.0

    def __init__(self, max_attempts: int = 12, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._redis: Optional[redis.Redis] = None
        self._last_redis_check: float = 0.0

    def _get_redis(self) -> Optional[redis.Redis]:
        now = time.monotonic()
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
                self._redis = redis.from_url(saas_settings.REDIS_URL, **_redis_kwargs())
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
    """Password temporanea generata dal server per un nuovo account (16 caratteri base64url)."""
    return secrets.token_urlsafe(12)


_PASSWORD_CHANGE_ALLOWED_PATHS = frozenset({
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/logout",
})


def password_version(password_hash: str) -> str:
    """Impronta non sensibile dell'hash password: cambia ad ogni cambio password."""
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
        "mcp": bool(user.must_change_password),
        "pwdv": password_version(user.password_hash),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, _get_jwt_private_key(), algorithm=_JWT_ALGORITHM)
    return token, jti, expires_at


def _extract_raw_token(request: Request) -> Optional[str]:
    """Solo cookie HttpOnly. Bearer rimosso (D6)."""
    return request.cookies.get(saas_settings.COOKIE_NAME)


def _check_redis_revocation(payload: dict) -> None:
    r = _get_revocation_redis(readonly=True)
    if r is None:
        return
    jti = str(payload.get("jti", ""))
    sub = str(payload.get("sub", ""))
    pwdv = str(payload.get("pwdv", ""))
    keys = [f"revoked:jti:{jti}"]
    if pwdv:
        keys.append(f"revoked:pwdv:{sub}:{pwdv}")
    try:
        values = r.mget(keys)
        if any(values):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sessione revocata o non più valida.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[FAIL-CLOSED] Redis revoca non raggiungibile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Verifica sessione temporaneamente non disponibile.",
        ) from exc


def decode_token_locally(raw_token: str, db: Optional[Session] = None) -> dict:
    """Verifica locale della firma asimmetrica EdDSA tramite chiave pubblica + stato revoca."""
    try:
        payload = jwt.decode(
            raw_token,
            _get_jwt_public_key(),
            algorithms=[_JWT_ALGORITHM],
        )
        jti = payload.get("jti")
        if not jti or not payload.get("sub") or not payload.get("role"):
            raise ValueError("Missing claims")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token non valido o scaduto.",
        )

    _check_redis_revocation(payload)
    if db is not None and saas_settings.SERVICE_MODE in ("all", "iam"):
        revoked = db.execute(select(RevokedToken.id).where(RevokedToken.jti == jti)).first()
        if revoked is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sessione revocata.",
            )
    return payload


def verify_stateless_principal(raw_token: str, request_path: str) -> AuthenticatedUser:
    """Verifica autonoma nei pod clinical/manager: nessuna chiamata HTTP all'IAM (V1/Q1)."""
    payload = decode_token_locally(raw_token, db=None)
    must_change = bool(payload.get("mcp", False))
    if must_change and request_path not in _PASSWORD_CHANGE_ALLOWED_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PASSWORD_CHANGE_REQUIRED",
                "message": "È necessario cambiare la password prima di continuare.",
            },
        )
    return AuthenticatedUser(
        id=int(payload["sub"]),
        username=str(payload["username"]),
        role=str(payload["role"]),
        is_active=True,
        must_change_password=must_change,
    )


def verify_local_user_from_token(raw_token: str, request_path: str, db: Session) -> User:
    """Verifica firma EdDSA, revoca, stato utente, pwdv e cambio password sul DB IAM."""
    payload = decode_token_locally(raw_token, db=db)
    user_id = int(payload["sub"])

    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utente non trovato.",
        )
    if user.status != "active" or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disattivato o non attivo.",
        )
    if payload.get("pwdv") != password_version(user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessione non più valida. Effettua di nuovo il login.",
        )
    if user.must_change_password and request_path not in _PASSWORD_CHANGE_ALLOWED_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PASSWORD_CHANGE_REQUIRED",
                "message": "È necessario cambiare la password prima di continuare.",
            },
        )
    return user


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> Principal:
    raw_token = _extract_raw_token(request)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticazione richiesta (RF01).",
        )
    if saas_settings.SERVICE_MODE in ("clinical", "manager"):
        return verify_stateless_principal(raw_token, request.url.path)
    return verify_local_user_from_token(raw_token, request.url.path, db)


def require_roles(*allowed_roles: str) -> Callable[[Principal], Principal]:
    """Dependency RBAC server-side (RF02, RNF08)."""
    allowed = set(allowed_roles)

    def _checker(current_user: Principal = Depends(get_current_user)) -> Principal:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Accesso negato al ruolo '{current_user.role}'.",
            )
        return current_user

    return _checker


def add_audit_event(
    db: Session,
    user: Principal,
    action: str,
    request_id: str,
    target_patient_id: Optional[int] = None,
) -> None:
    """Aggiunge AuditLog nella transazione attiva senza commit separati."""
    db.add(
        AuditLog(
            actor_user_id=user.id,
            actor_role=user.role,
            action=action,
            target_patient_id=target_patient_id,
            request_id=request_id,
        )
    )
