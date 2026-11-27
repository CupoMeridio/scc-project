"""
FILE: saas_app/seed.py
RUOLO: Popolamento iniziale idempotente del database (account demo RBAC e serie reali OhioT1DM).
"""

import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saas_app.auth import generate_device_key, hash_device_key, hash_password
from saas_app.clinical import map_prediction_to_outcome
from saas_app.config import saas_settings
from saas_app.models import CgmReading, Device, DoctorProfile, Intervention, MealPrediction, Patient, User

logger = logging.getLogger("saas-api.seed")

DEMO_PATIENTS: List[Tuple[str, str, int, float]] = [
    # (patient_code, full_name, doctor_index 0|1, diet_carb_limit_g)
    ("540", "Marco Ferri", 0, 55.0),
    ("544", "Elena Conti", 0, 60.0),
    ("552", "Luca Moretti", 0, 65.0),
    ("559", "Giulia Rinaldi", 0, 50.0),
    ("563", "Davide Gallo", 0, 70.0),
    ("567", "Chiara Esposito", 0, 55.0),
    ("570", "Andrea Romano", 1, 60.0),
    ("575", "Sara Lombardi", 1, 50.0),
    ("584", "Matteo Greco", 1, 65.0),
    ("588", "Sofia Fontana", 1, 55.0),
    ("591", "Alessandro Bruno", 1, 60.0),
    ("596", "Federica De Luca", 1, 50.0),
]


def _load_patient_csv_rows(data_dir: Path, patient_code: str, max_rows: int = 180) -> List[dict]:
    csv_path = data_dir / patient_code / f"{patient_code}-ws-testing(t+30).csv"
    if not csv_path.exists():
        return []
    rows: List[dict] = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            if idx >= max_rows:
                break
            rows.append(row)
    return rows


def seed_database(
    db: Session,
    force_minimal: bool = False,
    patient_pw: str | None = None,
    doctor_pw: str | None = None,
    manager_pw: str | None = None,
) -> Dict[str, str]:
    """
    Popola utenti demo, pazienti OhioT1DM, serie storiche e interventi se il DB è vuoto.
    Idempotente: il controllo su existing_user previene doppi inserimenti.
    Ritorna le chiavi dispositivo in chiaro generate in questo run (username -> chiave),
    da mostrare una sola volta. Se il seed non crea dati, ritorna un dizionario vuoto.
    """
    try:
        return _seed_database_locked(
            db,
            force_minimal=force_minimal,
            patient_pw=patient_pw,
            doctor_pw=doctor_pw,
            manager_pw=manager_pw,
        )
    except IntegrityError:
        db.rollback()
        logger.info("Seed già eseguito (IntegrityError: dati demo già presenti).")
        return {}


def _seed_database_locked(
    db: Session,
    force_minimal: bool = False,
    patient_pw: str | None = None,
    doctor_pw: str | None = None,
    manager_pw: str | None = None,
) -> Dict[str, str]:
    existing_user = db.execute(select(User.id).limit(1)).first()
    if existing_user is not None:
        return {}

    if not patient_pw or not doctor_pw or not manager_pw:
        raise ValueError("Il seed demo richiede password esplicite per pazienti, medici e manager.")

    raw_now = datetime.now(timezone.utc)
    now = raw_now.replace(minute=(raw_now.minute // 5) * 5, second=0, microsecond=0)
    patient_pw_hash = hash_password(patient_pw)
    doctor_pw_hash = hash_password(doctor_pw)
    manager_pw_hash = hash_password(manager_pw)

    # 1. Center Manager e 2 Diabetologi (IAM senza PII clinica; anagrafica medici in doctor_profiles)
    manager = User(
        username="manager",
        password_hash=manager_pw_hash,
        role="manager",
    )
    doc_0 = User(
        username="dr.rossi",
        password_hash=doctor_pw_hash,
        role="doctor",
    )
    doc_1 = User(
        username="dr.bianchi",
        password_hash=doctor_pw_hash,
        role="doctor",
    )
    db.add_all([manager, doc_0, doc_1])
    db.flush()

    db.add_all([
        DoctorProfile(user_id=doc_0.id, full_name="Dr. Paolo Rossi", email="rossi@diabetes-saas.local"),
        DoctorProfile(user_id=doc_1.id, full_name="Dr.ssa Marta Bianchi", email="bianchi@diabetes-saas.local"),
    ])
    db.flush()

    doctors = [doc_0, doc_1]
    # Decisione D1: il dataset non è nell'immagine; arriva da un volume montato
    # in sola lettura (k8s/seed-demo-job.yaml). Se la cartella manca, il seed
    # crea comunque gli account demo ma senza serie storiche.
    data_dir = Path(saas_settings.DATASET_DIR)
    if not data_dir.is_dir():
        logger.warning(
            "Dataset OhioT1DM non trovato in '%s': i pazienti demo saranno creati senza serie storiche. "
            "Montare la cartella come volume e impostare DATASET_DIR (vedi k8s/seed-demo-job.yaml).",
            data_dir,
        )
    max_rows = 30 if force_minimal else 144  # ~12 ore ad alta risoluzione per ogni paziente

    created_patients: List[Patient] = []
    device_keys: Dict[str, str] = {}

    for idx, (code, full_name, doc_idx, diet_limit) in enumerate(DEMO_PATIENTS):
        p_user = User(
            username=f"patient_{code}",
            password_hash=patient_pw_hash,
            role="patient",
            must_change_password=True,  # password iniziale assegnata dal medico: da cambiare al primo accesso
        )
        db.add(p_user)
        db.flush()

        patient = Patient(
            user_id=p_user.id,
            doctor_id=doctors[doc_idx].id,
            patient_code=code,
            full_name=full_name,
            email=f"patient_{code}@diabetes-saas.local",
            diet_carb_limit_g=diet_limit,
            target_min_mmol=3.9,
            target_max_mmol=10.0,
        )
        db.add(patient)
        db.flush()
        created_patients.append(patient)

        # Crea un device CGM per il paziente: chiave casuale, nel DB solo l'hash (N1, regola 17).
        # La chiave in chiaro viene mostrata una sola volta (device-keys) e non è derivabile.
        device_key = generate_device_key()
        device_keys[p_user.username] = device_key
        db.add(Device(
            patient_id=patient.id,
            key_hash=hash_device_key(device_key),
        ))

        rows = _load_patient_csv_rows(data_dir, code, max_rows=max_rows)
        readings_batch: List[CgmReading] = []

        if rows:
            # Distribuisce gli ultimi max_rows campioni ogni 5 min fino ad adesso,
            # più alcuni campioni su 20 e 90 giorni fa per supportare viste mese/anno.
            total = len(rows)
            for r_i, row in enumerate(rows):
                g_val = float(row["G(t)"])
                ins_val = float(row.get("I(t)", 1.0))
                cho_val = float(row.get("CHO(t)", 0.0))
                gt_next = float(row["G(t+6)"]) if "G(t+6)" in row else g_val
                # Scostamento lieve realistico per visualizzare il confronto misurato vs predetto
                bias = 0.55 if idx % 3 == 0 else (-0.45 if idx % 3 == 1 else 0.18)
                pred_val = round(max(1.0, min(35.0, gt_next + bias)), 2)

                # Ultimi 72 campioni (~6h) in tempo reale fino a now; precedenti distribuiti fino a -45 giorni
                if r_i >= total - 72:
                    ts = now - timedelta(minutes=5 * (total - 1 - r_i))
                else:
                    days_back = int((total - 72 - r_i) * 0.65) + 1
                    ts = now - timedelta(days=days_back, minutes=(r_i % 12) * 5)

                readings_batch.append(
                    CgmReading(
                        patient_id=patient.id,
                        recorded_at=ts,
                        glucose_mmol=round(g_val, 2),
                        insulin_u=round(ins_val, 2),
                        cho_rate=round(cho_val, 4),
                    )
                )

                # Crea eventi pasto nei picchi CHO o ogni ~18 righe
                if (cho_val > 0.35 or r_i % 24 == 10) and len(rows) > 6:
                    carbs_g = round(max(15.0, min(110.0, cho_val * saas_settings.CARBS_TO_HOVORKA_DIVISOR if cho_val > 0.1 else 45.0 + (r_i % 3) * 15.0)), 1)
                    risk_cls, outcome_code, outcome_msg, doc_flag = map_prediction_to_outcome(
                        pred_val, carbs_g, diet_limit
                    )
                    db.add(
                        MealPrediction(
                            patient_id=patient.id,
                            declared_at=ts,
                            carbs_g=carbs_g,
                            insulin_u=round(ins_val, 2),
                            current_glucose_mmol=round(g_val, 2),
                            forecasted_glucose_mmol=pred_val,
                            actual_glucose_30m_mmol=round(gt_next, 2),
                            risk_class=risk_cls,
                            outcome_code=outcome_code,
                            outcome_message=outcome_msg,
                            doctor_flag=doc_flag,
                            model_version="1.0.0",
                        )
                    )
        else:
            # Fallback sintetico deterministico per ambienti di test senza CSV
            for step in range(20):
                ts = now - timedelta(minutes=5 * (19 - step))
                g_val = round(6.2 + 0.15 * (step % 5), 2)
                readings_batch.append(
                    CgmReading(
                        patient_id=patient.id,
                        recorded_at=ts,
                        glucose_mmol=g_val,
                        insulin_u=1.2,
                        cho_rate=0.0,
                    )
                )
            db.add(
                MealPrediction(
                    patient_id=patient.id,
                    declared_at=now - timedelta(hours=2),
                    carbs_g=50.0,
                    insulin_u=1.5,
                    current_glucose_mmol=6.4,
                    forecasted_glucose_mmol=7.1,
                    actual_glucose_30m_mmol=6.9,
                    risk_class=4,
                    outcome_code="PROCEED_REGULAR",
                    outcome_message="Valori nella norma.",
                    doctor_flag=False,
                    model_version="1.0.0",
                )
            )

        db.add_all(readings_batch)

    db.flush()

    # 3. Interventi clinici distribuiti su 4 settimane per grafici Center Manager & Doctor
    sample_interventions = [
        (0, 0, 24, "diet_change", "patient_behavior", "Ridotta soglia carboidrati serali dopo picchi post-prandiali.", 50.0),
        (1, 0, 21, "reminder", "patient_behavior", "Promemoria registrazione puntuale dei pasti prima dell'assunzione.", None),
        (2, 0, 16, "reminder", "model_error", "Sovrastima lieve del modello durante attività fisica pomeridiana.", None),
        (3, 0, 12, "diet_change", "patient_behavior", "Adeguato tetto carboidrati a 45 g per colazione.", 45.0),
        (6, 1, 18, "reminder", "model_error", "Segnalato scostamento sistematico in fase notturna al team ML.", None),
        (7, 1, 9, "diet_change", "patient_behavior", "Confermato piano alimentare e frazionamento spuntini.", 50.0),
        (10, 1, 4, "reminder", "patient_behavior", "Contattato paziente per episodio iperglicemico post-cena.", None),
        (11, 1, 2, "reminder", "model_error", "Sottostima lieve rilevata nella finestra t+30m.", None),
    ]
    for p_idx, d_idx, days_ago, itype, cause, notes, new_limit in sample_interventions:
        if p_idx < len(created_patients):
            db.add(
                Intervention(
                    patient_id=created_patients[p_idx].id,
                    doctor_id=doctors[d_idx].id,
                    created_at=now - timedelta(days=days_ago),
                    intervention_type=itype,
                    cause=cause,
                    notes=notes,
                    new_diet_carb_limit_g=new_limit,
                )
            )

    db.commit()
    logger.info(f"[✓] Seed iniziale completato: {len(created_patients)} pazienti OhioT1DM caricati.")
    return device_keys
