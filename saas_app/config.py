"""
FILE: saas_app/config.py
RUOLO: Configurazione 12-Factor con validazione fail-fast dei segreti di sicurezza.
"""

import os


_INSECURE_DEFAULTS = {
    "",
    "dev-insecure-change-in-production-jwt-secret-key-32bytes",
    "changeme",
    "secret",
}


class SaasSettings:
    PROJECT_NAME: str = "OhioT1DM SaaS Portal & IAM API"
    VERSION: str = "1.0.0"

    SAAS_ENV: str = os.getenv("SAAS_ENV", "production").lower()

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///./saas_local.db",
    )

    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))

    COOKIE_NAME: str = os.getenv("COOKIE_NAME", "__Host-scc_session")
    COOKIE_SECURE: bool = os.getenv(
        "COOKIE_SECURE",
        "false" if SAAS_ENV in ("test", "dev") else "true",
    ).lower() in ("1", "true", "yes")

    ML_SERVICE_URL: str = os.getenv(
        "ML_SERVICE_URL",
        "http://diabetes-forecasting-service",
    )
    ML_HEADLESS_HOST: str = os.getenv(
        "ML_HEADLESS_HOST",
        "diabetes-forecasting-headless",
    )
    ML_METRICS_PORT: int = int(os.getenv("ML_METRICS_PORT", "8000"))

    REDIS_URL: str = os.getenv("REDIS_URL", "")
    REDIS_QUEUE_NAME: str = os.getenv("REDIS_QUEUE_NAME", "cgm:ingest:queue")

    # Decisione D1: il dataset OhioT1DM non è né nel git né nell'immagine seed.
    # Il Job di seed lo riceve da un volume montato in sola lettura (k8s/seed-demo-job.yaml).
    DATASET_DIR: str = os.getenv(
        "DATASET_DIR",
        "Diabetes/GlucoseClassification",
    )

    CARBS_TO_HOVORKA_DIVISOR: float = float(os.getenv("CARBS_TO_HOVORKA_DIVISOR", "45.0"))

    CGM_MAX_AGE_MINUTES: int = int(os.getenv("CGM_MAX_AGE_MINUTES", "15"))
    CGM_STEP_MINUTES: int = int(os.getenv("CGM_STEP_MINUTES", "5"))
    CGM_STEP_TOLERANCE_MINUTES: int = int(os.getenv("CGM_STEP_TOLERANCE_MINUTES", "1"))
    CGM_TIMESTAMP_FUTURE_TOLERANCE_MINUTES: int = int(os.getenv("CGM_TIMESTAMP_FUTURE_TOLERANCE_MINUTES", "2"))
    CGM_TIMESTAMP_MAX_AGE_HOURS: int = int(os.getenv("CGM_TIMESTAMP_MAX_AGE_HOURS", "24"))

    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8001"))
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    def validate_security_secrets(self, require_jwt: bool = True) -> None:
        """
        Fail-fast all'avvio se mancano segreti crittografici validi (non in ambiente test).

        require_jwt=False per i processi che non firmano né verificano token (worker, seed):
        non li obbliga a montare JWT_SECRET_KEY (P12, principio del minimo privilegio).
        """
        if self.SAAS_ENV == "test":
            if not self.JWT_SECRET_KEY:
                self.JWT_SECRET_KEY = "test-only-ephemeral-jwt-key-minimum-32-chars"
            return

        if not require_jwt:
            return

        if self.JWT_SECRET_KEY in _INSECURE_DEFAULTS or len(self.JWT_SECRET_KEY) < 32:
            raise RuntimeError(
                "[FAIL-CLOSED] JWT_SECRET_KEY mancante, insicura o inferiore a 32 caratteri."
            )


saas_settings = SaasSettings()
