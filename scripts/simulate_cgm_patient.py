#!/usr/bin/env python3
# ==============================================================================
# SCRIPT: simulate_cgm_patient.py
# RUOLO: Simulatore del Sensore Continuo di Glucosio (CGM) e Diario Paziente.
#        Riproduce in streaming le serie temporali cliniche dal dataset OhioT1DM
#        e interroga l'infrastruttura Cloud (Kubernetes Ingress / FastAPI)
#        trasmettendo finestre temporali da 60 minuti a passi di 5 minuti.
# ==============================================================================

import argparse
import csv
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Definizione delle costanti di conversione e cutoff clinici internazionali (OhioT1DM)
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Fattore di conversione standard glicemico (1 mmol/L = 18.018 mg/dL) allineato all'API
MMOL_TO_MGDL: float = 18.018


def map_clinical_to_patient_outcome(
    risk_class: int,
    forecasted_mmol: float,
    current_cho: float,
    diet_limit: float
) -> Tuple[str, str, bool]:
    """
    Mappa le 7 classi cliniche del modello sui 4 macro-esiti definiti
    nella documentazione dell'architettura funzionale del SaaS (part2.tex:RF06).

    Parametri:
    - risk_class: Classe di rischio stimata dalla rete (1..7)
    - forecasted_mmol: Glucosio predetto a +30 minuti (mmol/L)
    - current_cho: Carboidrati dichiarati dal paziente per il pasto corrente (g)
    - diet_limit: Soglia massima di carboidrati prescritta dal diabetologo (g)

    Ritorna:
    - outcome_code: Codice identificativo dell'esito
    - outcome_message: Messaggio mostrato al paziente sullo schermo
    - doctor_flag: True se l'evento deve essere registrato per il diabetologo
    """
    if risk_class in (1, 2):
        return (
            "RISK_OF_HYPOGLYCEMIA",
            "Rischio Ipoglicemia: glucosio previsto critico (< 3.9 mmol/L). Assumere zuccheri rapidi.",
            True
        )
    elif risk_class in (6, 7):
        return (
            "RISK_OF_HYPERGLYCEMIA",
            "Rischio Iperglicemia: glucosio previsto elevato (>= 10.0 mmol/L). Dose pasto eccessiva.",
            True
        )
    elif risk_class in (3, 5):
        msg = (
            "Procedi con cautela: glucosio previsto ai limiti del range ottimale (3.9 - 5.0 mmol/L)."
            if risk_class == 3
            else "Procedi con cautela: glucosio previsto sopra target (7.8 - 10.0 mmol/L)."
        )
        return ("PROCEED_WITH_WARNING", msg, False)
    else:  # Classe 4 (Target ottimale: 5.0 - 7.8 mmol/L)
        if current_cho > diet_limit:
            return (
                "PROCEED_WITH_WARNING",
                f"Procedi con avviso: glucosio nel target, ma carboidrati ({current_cho:.2f}) superiori alla soglia dieta ({diet_limit:.2f}).",
                False
            )
        return (
            "PROCEED_REGULAR",
            "Procedi normalmente: glucosio nel target ottimale e carboidrati conformi alla dieta.",
            False
        )


def load_patient_records(
    csv_path: Path,
    max_steps: int = 0
) -> List[Dict[str, Any]]:
    """
    Legge il file CSV del paziente ed estrae le sequenze temporali del sensore CGM,
    i valori esogeni (insulina e pasti) e i target reali annotati nel dataset.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"File CSV paziente non trovato: {csv_path}")

    records: List[Dict[str, Any]] = []
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            if max_steps > 0 and idx >= max_steps:
                break

            # Estrazione dei 13 campioni storici del glucosio (da t-12 a t)
            glucose_history: List[float] = []
            for step in range(12, 0, -1):
                col_name = f"G(t-{step})"
                glucose_history.append(float(row[col_name]))
            glucose_history.append(float(row["G(t)"]))

            # Estrazione dei 14 input esogeni [I(t), CHO(t), I(t+1..6), CHO(t+1..6)]
            exogenous: List[float] = [float(row["I(t)"]), float(row["CHO(t)"])]
            for step in range(1, 7):
                exogenous.append(float(row[f"I(t+{step})"]))
                exogenous.append(float(row[f"CHO(t+{step})"]))

            # Target reali annotati dal dataset (Ground Truth a +30 minuti)
            ground_truth_glucose = float(row["G(t+6)"]) if "G(t+6)" in row else None
            ground_truth_class = int(float(row["RiskClass(t+6)"])) if "RiskClass(t+6)" in row else None

            records.append({
                "step_index": idx,
                "glucose_history": glucose_history,
                "exogenous_inputs": exogenous,
                "current_glucose": glucose_history[-1],
                "current_cho": exogenous[1],
                "current_insulin": exogenous[0],
                "ground_truth_glucose": ground_truth_glucose,
                "ground_truth_class": ground_truth_class,
            })

    return records


def send_http_prediction(
    endpoint_url: str,
    payload: Dict[str, Any],
    local_client: Optional[Any] = None,
    insecure: bool = False,
    timeout: float = 10.0
) -> Tuple[int, Dict[str, Any], float]:
    """
    Invia la richiesta REST POST /predict all'endpoint Kubernetes o locale
    e restituisce il codice HTTP, il payload JSON di risposta e la latenza in millisecondi.
    """
    if local_client is not None:
        start_time = time.perf_counter()
        resp = local_client.post("/predict", json=payload)
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        return (resp.status_code, data, elapsed_ms)

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "CGM-Sensor-Simulator/1.0",
            "Accept": "application/json"
        },
        method="POST"
    )

    context = None
    if endpoint_url.startswith("https"):
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

    start_time = time.perf_counter()
    try:
        with urllib.request.urlopen(req, context=context, timeout=timeout) as resp:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            resp_data = json.loads(resp.read().decode("utf-8"))
            return (resp.status, resp_data, elapsed_ms)
    except urllib.error.HTTPError as err:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        try:
            err_data = json.loads(err.read().decode("utf-8"))
        except Exception:
            err_data = {"detail": str(err)}
        return (err.code, err_data, elapsed_ms)
    except Exception as err:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return (0, {"error": str(err)}, elapsed_ms)


def simulate_cgm_stream(
    patient_id: str,
    data_dir: Path,
    split: str,
    endpoint: str,
    steps: int,
    delay: float,
    diet_limit: float,
    insecure: bool,
    local: bool
) -> None:
    """
    Esegue il loop di streaming temporale del sensore CGM.
    """
    csv_file = data_dir / patient_id / f"{patient_id}-ws-{split}(t+30).csv"
    if not csv_file.exists():
        # Tentativo con percorso alternativo senza sottocartella
        csv_file = data_dir / f"{patient_id}-ws-{split}(t+30).csv"

    mode_label = "In-Memory TestClient (Local ONNX)" if local else f"Remote HTTP ({endpoint})"

    print("=" * 80)
    print("SIMULATORE SENSORE CGM (Continuous Glucose Monitor) — OhioT1DM Replay")
    print("=" * 80)
    print(f"Paziente ID:           {patient_id}")
    print(f"File dataset:          {csv_file}")
    print(f"Modalità esecuzione:   {mode_label}")
    print(f"Passi da simulare:     {steps if steps > 0 else 'Tutti i campioni del file'}")
    print(f"Frequenza di replay:   1 step ogni {delay:.2f}s (rappresenta 5 min di campionamento reale)")
    print(f"Limite dieta prescr.:  {diet_limit:.1f} g di carboidrati per pasto")
    print("=" * 80)

    records = load_patient_records(csv_file, max_steps=steps)
    total_records = len(records)
    print(f"[+] Caricati con successo {total_records} campioni temporali. Avvio dello streaming...\n")

    local_ctx = None
    local_client = None

    if local:
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            local_ctx = TestClient(app)
            local_client = local_ctx.__enter__()
        except Exception as e:
            print(f"[!] ERRORE nell'inizializzazione del runtime locale: {e}", file=sys.stderr)
            sys.exit(1)

    successful_requests = 0
    total_latency_ms = 0.0
    absolute_errors: List[float] = []
    outcome_counts: Dict[str, int] = {
        "PROCEED_REGULAR": 0,
        "PROCEED_WITH_WARNING": 0,
        "RISK_OF_HYPOGLYCEMIA": 0,
        "RISK_OF_HYPERGLYCEMIA": 0,
    }

    try:
        for rec in records:
            step = rec["step_index"]
            sim_minutes = step * 5
            curr_g_mmol = rec["current_glucose"]
            curr_g_mgdl = curr_g_mmol * MMOL_TO_MGDL
            cho = rec["current_cho"]
            ins = rec["current_insulin"]

            # Composizione del payload conforme al contratto Pydantic di /predict
            payload = {
                "patient_id": f"patient_{patient_id}",
                "glucose_history": rec["glucose_history"],
                "exogenous_inputs": rec["exogenous_inputs"]
            }

            status_code, resp_data, latency = send_http_prediction(
                endpoint,
                payload,
                local_client=local_client,
                insecure=insecure
            )

            if status_code == 200:
                successful_requests += 1
                total_latency_ms += latency

                pred_mmol = resp_data.get("forecasted_glucose_mmol_l", 0.0)
                pred_mgdl = resp_data.get("forecasted_glucose_mg_dl", 0.0)
                risk_cls = resp_data.get("risk_class", 4)
                risk_lbl = resp_data.get("risk_label", "Unknown")

                # Mappatura sui 4 esiti della relazione funzionale
                outcome_code, outcome_msg, doctor_flag = map_clinical_to_patient_outcome(
                    risk_cls, pred_mmol, cho, diet_limit
                )
                outcome_counts[outcome_code] = outcome_counts.get(outcome_code, 0) + 1

                # Calcolo errore rispetto alla misurazione reale successiva (Ground Truth)
                gt_mmol = rec["ground_truth_glucose"]
                err_str = "N/D"
                if gt_mmol is not None:
                    err = abs(pred_mmol - gt_mmol)
                    absolute_errors.append(err)
                    err_str = f"{err:.2f} mmol/L ({err * MMOL_TO_MGDL:.0f} mg/dL)"

                flag_icon = " [FLAG MEDICO]" if doctor_flag else ""

                print(f"--- [Step {step:03d} | +{sim_minutes:03d} min] "
                      f"Glucosio: {curr_g_mgdl:5.1f} mg/dL ({curr_g_mmol:4.2f} mmol/L) | "
                      f"Pasto: {cho:4.1f}g CHO | Insulina: {ins:3.1f}U ---")
                print(f"  HTTP {status_code} in {latency:5.1f} ms | "
                      f"Previsione a +30m: {pred_mgdl:5.1f} mg/dL ({pred_mmol:4.2f} mmol/L) | "
                      f"Classe: {risk_cls} ({risk_lbl})")
                print(f"  Esito Paziente: {outcome_code}{flag_icon}")
                print(f"  -> {outcome_msg}")
                if gt_mmol is not None:
                    print(f"  Confronto Reale (Ground Truth): {gt_mmol * MMOL_TO_MGDL:5.1f} mg/dL | Scostamento |Err|: {err_str}")
                print()

            else:
                print(f"[!] [Step {step:03d}] ERRORE HTTP {status_code} da {endpoint}: {resp_data}")
                if "WinError 10061" in str(resp_data) or "Connection refused" in str(resp_data):
                    print("    Suggerimento: Il server o cluster non sembra attivo. Usa il flag '--local' per eseguire la simulazione in locale.", file=sys.stderr)
                    break

            if delay > 0:
                time.sleep(delay)

    except KeyboardInterrupt:
        print("\n[!] Simulazione interrotta dall'utente.")
    finally:
        if local_ctx is not None:
            local_ctx.__exit__(None, None, None)

    # Stampa del report riassuntivo finale
    print("=" * 80)
    print("REPORT CONCLUSIVO SIMULAZIONE SENSORE CGM")
    print("=" * 80)
    print(f"Campioni inviati con successo:  {successful_requests}/{total_records} "
          f"({(successful_requests / total_records * 100.0) if total_records else 0:.1f}%)")
    if successful_requests > 0:
        avg_lat = total_latency_ms / successful_requests
        print(f"Latenza media di inferenza:     {avg_lat:.2f} ms (SLA contrattuale: <= 1000 ms)")
    if absolute_errors:
        mae_mmol = sum(absolute_errors) / len(absolute_errors)
        mae_mgdl = mae_mmol * MMOL_TO_MGDL
        print(f"Errore Assoluto Medio (MAE):    {mae_mmol:.3f} mmol/L ({mae_mgdl:.1f} mg/dL)")
    print("\nDistribuzione dei 4 Esiti Paziente generati:")
    for code, count in outcome_counts.items():
        pct = (count / successful_requests * 100.0) if successful_requests else 0.0
        print(f"  - {code:25s}: {count:3d} ({pct:5.1f}%)")
    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulatore del sensore CGM e diario pasti per l'API di monitoraggio glicemico."
    )
    parser.add_argument(
        "--patient",
        type=str,
        default="591",
        help="ID del paziente OhioT1DM (default: 591, es. 540, 544, 552...)"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("Diabetes/GlucoseClassification"),
        help="Cartella principale contenente i file dei pazienti"
    )
    parser.add_argument(
        "--split",
        type=str,
        default="testing",
        choices=["testing", "training"],
        help="Dataset split da utilizzare (default: testing)"
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        default="http://localhost/predict",
        help="URL dell'endpoint di inferenza (default: http://localhost/predict)"
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=10,
        help="Numero di campioni temporali consecutivi da simulare (default: 10, 0 = tutto il file)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Ritardo in secondi tra una lettura e l'altra per simulare lo streaming a video (default: 0.3)"
    )
    parser.add_argument(
        "--diet-limit",
        type=float,
        default=0.5,
        help="Soglia limite di carboidrati prescritti per pasto (default: 0.5 su scala assorbimento OhioT1DM, oppure grammi per dati grezzi)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Ignora la verifica del certificato SSL se si interroga l'endpoint HTTPS su localhost"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Esegue l'inferenza in-process usando l'app locale (utile per demo senza cluster attivo)"
    )

    args = parser.parse_args()

    # Verifica preliminare esistenza cartella dati
    if not args.data_dir.exists():
        print(f"[!] ERRORE: La cartella del dataset '{args.data_dir}' non esiste.", file=sys.stderr)
        print("    Assicurati di trovarti nella radice del progetto o specifica --data-dir.", file=sys.stderr)
        sys.exit(1)

    simulate_cgm_stream(
        patient_id=args.patient,
        data_dir=args.data_dir,
        split=args.split,
        endpoint=args.endpoint,
        steps=args.steps,
        delay=args.delay,
        diet_limit=args.diet_limit,
        insecure=args.insecure,
        local=args.local
    )


if __name__ == "__main__":
    main()
