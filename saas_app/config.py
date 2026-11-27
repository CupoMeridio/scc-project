"""
FILE: saas_app/config.py
RUOLO: Configurazione 12-Factor con isolamento per zona (SERVICE_MODE), JWT EdDSA e segreti dedicati.
"""

import os


_INSECURE_DEFAULTS = {
    "",
    "dev-insecure-change-in-production-jwt-secret-key-32bytes",
    "changeme",
    "secret",
}

VALID_SERVICE_MODES = frozenset({"all", "iam", "clinical", "manager", "ingest"})


class SaasSettings:
    PROJECT_NAME: str = "OhioT1DM SaaS Portal & IAM API"
    VERSION: str = "1.0.0"

    SAAS_ENV: str = os.getenv("SAAS_ENV", "production").lower()
    SERVICE_MODE: str = os.getenv("SERVICE_MODE", "all").lower()

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "sqlite:///./saas_local.db",
    )

    # Firma asimmetrica EdDSA (Ed25519): chiave privata SOLO in IAM; chiave pubblica nei servizi
    # per verifica locale senza chiamate HTTP verso IAM ad ogni richiesta (V1 / Q1).
    JWT_PRIVATE_KEY_PEM: str = os.getenv("JWT_PRIVATE_KEY_PEM", "").replace("\\n", "\n")
    JWT_PUBLIC_KEY_PEM: str = os.getenv("JWT_PUBLIC_KEY_PEM", "").replace("\\n", "\n")
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "10"))

    # Provisioning pazienti in due passi (SOLO clinical -> iam con credenziale dedicata).
    # Il manager non possiede alcuna credenziale né percorso di rete verso IAM.
    IAM_INTERNAL_URL: str = os.getenv("IAM_INTERNAL_URL", "")
    IAM_PROVISION_TOKEN: str = os.getenv("IAM_PROVISION_TOKEN", "")
    IAM_CA_CERT_PATH: str = os.getenv("IAM_CA_CERT_PATH", "")

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
    ML_CA_CERT_PATH: str = os.getenv("ML_CA_CERT_PATH", "")

    REDIS_URL: str = os.getenv("REDIS_URL", "")
    REDIS_REVOCATION_URL: str = os.getenv("REDIS_REVOCATION_URL", "")
    REDIS_CA_CERT_PATH: str = os.getenv("REDIS_CA_CERT_PATH", "")
    REDIS_QUEUE_NAME: str = os.getenv("REDIS_QUEUE_NAME", "cgm:ingest:queue")

    TLS_CERT_FILE: str = os.getenv("TLS_CERT_FILE", "")
    TLS_KEY_FILE: str = os.getenv("TLS_KEY_FILE", "")

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
        Fail-fast all'avvio se mancano chiavi o token validi per la zona attiva.

        - `iam` / `all`: richiede JWT_PRIVATE_KEY_PEM + JWT_PUBLIC_KEY_PEM (Ed25519).
        - `iam` e `clinical`: usano `IAM_PROVISION_TOKEN` dedicata per registrazione pazienti.
        - `clinical` e `manager`: verificano i JWT in locale con `JWT_PUBLIC_KEY_PEM` (zero segreti condivisi per auth).
        - `ingest` / `worker` / `seed`: non usano chiavi JWT né token di provisioning.
        """
        if self.SERVICE_MODE not in VALID_SERVICE_MODES:
            raise RuntimeError(f"[FAIL-CLOSED] SERVICE_MODE '{self.SERVICE_MODE}' non valido.")

        if self.SAAS_ENV == "test":
            return

        if not require_jwt:
            return

        if not (1 <= self.JWT_EXPIRE_MINUTES <= 10):
            raise RuntimeError(
                "[FAIL-CLOSED] JWT_EXPIRE_MINUTES in produzione deve essere compreso tra 1 e 10 minuti (V1)."
            )

        if self.SERVICE_MODE in ("all", "iam") and "BEGIN PRIVATE KEY" not in self.JWT_PRIVATE_KEY_PEM:
            raise RuntimeError("[FAIL-CLOSED] JWT_PRIVATE_KEY_PEM (Ed25519 PKCS8) mancante o non valida.")

        if self.SERVICE_MODE in ("all", "iam", "clinical", "manager") and (
            "BEGIN PUBLIC KEY" not in self.JWT_PUBLIC_KEY_PEM
        ):
            raise RuntimeError("[FAIL-CLOSED] JWT_PUBLIC_KEY_PEM (Ed25519 SPKI) mancante o non valida.")

        needs_provision_token = self.SERVICE_MODE == "iam" or (
            self.SERVICE_MODE == "clinical" and bool(self.IAM_INTERNAL_URL)
        )
        if needs_provision_token and (
            self.IAM_PROVISION_TOKEN in _INSECURE_DEFAULTS or len(self.IAM_PROVISION_TOKEN) < 32
        ):
            raise RuntimeError(
                "[FAIL-CLOSED] IAM_PROVISION_TOKEN mancante, insicuro o inferiore a 32 caratteri."
            )


saas_settings = SaasSettings()
