"""
FILE: saas_app/seed_cli.py
RUOLO: Script CLI / Kubernetes Job dedicato per migrazioni Alembic e popolamento demo offline.
"""

import logging
import os

from alembic import command
from alembic.config import Config

from saas_app.db import SessionLocal
from saas_app.seed import seed_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("saas-seed-job")


def run_migrations() -> None:
    alembic_cfg = Config("alembic.ini")
    logger.info("Esecuzione migrazioni Alembic (upgrade head)...")
    command.upgrade(alembic_cfg, "head")


def main() -> None:
    run_migrations()
    run_demo_seed = os.getenv("RUN_DEMO_SEED", "true").lower() in ("1", "true", "yes")
    if not run_demo_seed:
        logger.info("RUN_DEMO_SEED=false: migrazioni completate senza dati demo.")
        return

    with SessionLocal() as db:
        device_keys = seed_database(
            db,
            patient_pw=os.getenv("DEMO_PATIENT_PASSWORD"),
            doctor_pw=os.getenv("DEMO_DOCTOR_PASSWORD"),
            manager_pw=os.getenv("DEMO_MANAGER_PASSWORD"),
        )

    # Le chiavi dispositivo in chiaro esistono solo qui e per una sola esecuzione (regola 17):
    # sono stampate una volta su stdout del Job, che `make seed-demo` usa per creare il Secret
    # `device-keys`. Nel database resta solo l'hash SHA-256.
    for username, key in device_keys.items():
        print(f"DEVICE_KEY {username} {key}", flush=True)


if __name__ == "__main__":
    main()
