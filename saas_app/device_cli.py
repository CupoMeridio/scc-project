"""
FILE: saas_app/device_cli.py
RUOLO: Gestione delle chiavi dei dispositivi CGM (creazione e revoca) da riga di comando.

Uso:
    python -m saas_app.device_cli create --patient 591
    python -m saas_app.device_cli revoke --patient 591

La chiave in chiaro è stampata una sola volta; nel database resta solo l'hash SHA-256.
"""

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from saas_app import repositories as repo
from saas_app.auth import generate_device_key, hash_device_key
from saas_app.db import SessionLocal
from saas_app.models import Device, Patient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("saas-device-cli")


def _get_patient(db: Session, patient_code: str) -> Optional[Patient]:
    return db.execute(
        select(Patient).where(Patient.patient_code == patient_code)
    ).scalar_one_or_none()


def create_key(patient_code: str) -> int:
    """Genera (o ruota) la chiave del dispositivo del paziente e la stampa una sola volta."""
    with SessionLocal() as db:
        patient = _get_patient(db, patient_code)
        if patient is None:
            print(f"Paziente '{patient_code}' non trovato.", file=sys.stderr)
            return 1

        key = generate_device_key()
        device = repo.get_device_by_patient_id(db, patient.id)
        if device is None:
            db.add(Device(patient_id=patient.id, key_hash=hash_device_key(key)))
        else:
            repo.set_device_key_hash(db, device, hash_device_key(key))
        db.commit()

        print(f"DEVICE_KEY patient_{patient_code} {key}")
    return 0


def revoke(patient_code: str) -> int:
    """Imposta revoked_at sul dispositivo del paziente: la chiave non è più accettata."""
    with SessionLocal() as db:
        patient = _get_patient(db, patient_code)
        if patient is None:
            print(f"Paziente '{patient_code}' non trovato.", file=sys.stderr)
            return 1

        device = repo.get_device_by_patient_id(db, patient.id)
        if device is None:
            print(f"Nessun dispositivo registrato per il paziente '{patient_code}'.", file=sys.stderr)
            return 1

        repo.revoke_device(db, device, datetime.now(timezone.utc))
        db.commit()
        print(f"Dispositivo del paziente '{patient_code}' revocato.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m saas_app.device_cli",
        description="Gestione delle chiavi dei dispositivi CGM.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Genera la chiave di un dispositivo e la stampa una volta.")
    create_parser.add_argument("--patient", required=True, help="Codice paziente OhioT1DM (es. 591)")

    revoke_parser = subparsers.add_parser("revoke", help="Revoca il dispositivo di un paziente.")
    revoke_parser.add_argument("--patient", required=True, help="Codice paziente OhioT1DM (es. 591)")

    args = parser.parse_args(argv)
    if args.command == "create":
        return create_key(args.patient)
    return revoke(args.patient)


if __name__ == "__main__":
    raise SystemExit(main())
