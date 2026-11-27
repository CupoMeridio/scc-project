#!/usr/bin/env python3
"""
FILE: model/retrain_pipeline.py
RUOLO: Pipeline di retraining nella Training Zone con regola Champion vs Challenger (V4).

- Legge SOLO dalla vista pseudonimizzata PostgreSQL `v_training_cgm_series` (ruolo `saas_trainer`).
- Separa i dati tramite `trained_until` del Champion: il test set valuta SOLO finestre
  successive a `trained_until` (dati mai visti dal Champion; nella demo sono simulati),
  mentre il Challenger può addestrarsi su storico + porzione iniziale dei nuovi dati.
- Applica bootstrap appaiato sulle finestre di test (IC 95%) prima di promuovere:
    RMSE_gain_CI_lo >= MIN_RMSE_IMPROVEMENT AND Acc_diff_CI_lo >= -MAX_ACC_REGRESSION.
- Firma il manifest JSON con chiave privata Ed25519 (presente SOLO nella Training Zone).
"""

import argparse
import datetime
import json
import logging
import os
import shutil
import ssl
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from common.model_signing import sign_manifest_ed25519
from model.train_lstm import (
    CLASS_DESCRIPTIONS,
    EXOG,
    GlucoseHybridLSTM,
    compute_sha256,
    evaluate_model,
    export_onnx_model,
    to_class,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("training-zone")

SEQ_HISTORY_STEPS = 13
HORIZON_STEPS = 6
WINDOW_GAP_STEPS = HORIZON_STEPS
STEP_SECONDS = 300
STEP_TOLERANCE_SECONDS = 65


@dataclass
class TrainingRow:
    pseudo_patient_id: str
    recorded_at: datetime.datetime
    glucose_mmol: float
    insulin_u: float
    cho_rate: float


@dataclass
class SplitTensors:
    seq: np.ndarray
    exog: np.ndarray
    y_delta: np.ndarray
    g_now: np.ndarray
    risk_classes: np.ndarray


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime.datetime]:
    if not raw or not isinstance(raw, str):
        return None
    val = raw.strip()
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def build_temporal_splits_from_series(
    rows: Sequence[TrainingRow],
    trained_until: Optional[datetime.datetime] = None,
) -> Tuple[SplitTensors, SplitTensors, SplitTensors]:
    """
    Costruisce finestre temporali valide (passo 5 min) per ciascun `pseudo_patient_id`.
    Se `trained_until` è specificato, tutte le finestre con target `<= trained_until`
    entrano solo in `train` (storico); `val` e `test` vengono estratti esclusivamente
    dalle finestre con target `> trained_until` con gap di sicurezza di 30 minuti.
    """
    grouped: Dict[str, List[TrainingRow]] = {}
    for row in rows:
        grouped.setdefault(row.pseudo_patient_id, []).append(row)

    train_buckets: List[Tuple[np.ndarray, np.ndarray, float, float, int]] = []
    val_buckets: List[Tuple[np.ndarray, np.ndarray, float, float, int]] = []
    test_buckets: List[Tuple[np.ndarray, np.ndarray, float, float, int]] = []

    for pid in sorted(grouped):
        series = sorted(grouped[pid], key=lambda r: r.recorded_at)
        n = len(series)
        if n < SEQ_HISTORY_STEPS + HORIZON_STEPS + 10:
            continue

        hist_windows: List[Tuple[np.ndarray, np.ndarray, float, float, int]] = []
        new_windows: List[Tuple[np.ndarray, np.ndarray, float, float, int]] = []
        for idx in range(SEQ_HISTORY_STEPS - 1, n - HORIZON_STEPS):
            valid = True
            for j in range(idx - (SEQ_HISTORY_STEPS - 1), idx + HORIZON_STEPS):
                dt_s = (series[j + 1].recorded_at - series[j].recorded_at).total_seconds()
                if abs(dt_s - STEP_SECONDS) > STEP_TOLERANCE_SECONDS:
                    valid = False
                    break
            if not valid:
                continue

            g_vals = np.array(
                [series[k].glucose_mmol for k in range(idx - (SEQ_HISTORY_STEPS - 1), idx + 1)],
                dtype=np.float32,
            )
            dg_vals = np.zeros(SEQ_HISTORY_STEPS, dtype=np.float32)
            dg_vals[1:] = g_vals[1:] - g_vals[:-1]
            seq_item = np.stack([g_vals, dg_vals], axis=1)

            exog_vals: List[float] = []
            for k in range(idx, idx + HORIZON_STEPS + 1):
                exog_vals.append(float(series[k].insulin_u))
                exog_vals.append(float(series[k].cho_rate))
            exog_item = np.array(exog_vals, dtype=np.float32)

            g_now_val = float(series[idx].glucose_mmol)
            g_target_val = float(series[idx + HORIZON_STEPS].glucose_mmol)
            y_delta_val = g_target_val - g_now_val
            target_cls = int(to_class(np.array([g_target_val], dtype=np.float32))[0])
            target_ts = series[idx + HORIZON_STEPS].recorded_at
            item = (seq_item, exog_item, y_delta_val, g_now_val, target_cls)

            if trained_until is not None and target_ts <= trained_until:
                hist_windows.append(item)
            else:
                new_windows.append(item)

        if trained_until is not None and hist_windows:
            train_buckets.extend(hist_windows)
            w_len = len(new_windows)
            if w_len >= 12:
                i_val = int(w_len * 0.30)
                i_test = int(w_len * 0.50)
                train_buckets.extend(new_windows[:i_val])
                if i_val + WINDOW_GAP_STEPS < i_test:
                    val_buckets.extend(new_windows[i_val + WINDOW_GAP_STEPS : i_test])
                if i_test + WINDOW_GAP_STEPS < w_len:
                    test_buckets.extend(new_windows[i_test + WINDOW_GAP_STEPS :])
        else:
            w_len = len(new_windows)
            if w_len < 20:
                continue
            i_val = int(w_len * 0.50)
            i_test = int(w_len * 0.65)
            train_buckets.extend(new_windows[:i_val])
            if i_val + WINDOW_GAP_STEPS < i_test:
                val_buckets.extend(new_windows[i_val + WINDOW_GAP_STEPS : i_test])
            if i_test + WINDOW_GAP_STEPS < w_len:
                test_buckets.extend(new_windows[i_test + WINDOW_GAP_STEPS :])

    def _pack(items: List[Tuple[np.ndarray, np.ndarray, float, float, int]]) -> SplitTensors:
        if not items:
            return SplitTensors(
                seq=np.empty((0, SEQ_HISTORY_STEPS, 2), dtype=np.float32),
                exog=np.empty((0, 14), dtype=np.float32),
                y_delta=np.empty((0,), dtype=np.float32),
                g_now=np.empty((0,), dtype=np.float32),
                risk_classes=np.empty((0,), dtype=np.int64),
            )
        return SplitTensors(
            seq=np.stack([x[0] for x in items], axis=0).astype(np.float32),
            exog=np.stack([x[1] for x in items], axis=0).astype(np.float32),
            y_delta=np.array([x[2] for x in items], dtype=np.float32),
            g_now=np.array([x[3] for x in items], dtype=np.float32),
            risk_classes=np.array([x[4] for x in items], dtype=np.int64),
        )

    return _pack(train_buckets), _pack(val_buckets), _pack(test_buckets)


def should_promote_challenger(
    champion_rmse: float,
    champion_acc: float,
    challenger_rmse: float,
    challenger_acc: float,
    min_rmse_improvement: float = 0.005,
    max_acc_regression: float = 0.01,
) -> bool:
    """Regola di soglia puntuale Champion vs Challenger."""
    return (
        challenger_rmse <= (champion_rmse - min_rmse_improvement)
        and challenger_acc >= (champion_acc - max_acc_regression)
    )


def bootstrap_promotion_decision(
    g_true: np.ndarray,
    champ_pred: np.ndarray,
    chal_pred: np.ndarray,
    target_classes: np.ndarray,
    min_rmse_improvement: float = 0.005,
    max_acc_regression: float = 0.01,
    min_test_windows: int = 50,
    n_boot: int = 500,
) -> Tuple[bool, Dict[str, float]]:
    """
    Verifica la regola di promozione tramite bootstrap appaiato (IC 95%, seed 42):
    promuove SOLO se il test ha abbastanza finestre, le metriche puntuali migliorano
    e il limite inferiore dell'IC 95% su `RMSE_champ - RMSE_chal` supera la soglia.
    """
    n = len(g_true)
    champ_rmse = float(np.sqrt(np.mean((champ_pred - g_true) ** 2)))
    chal_rmse = float(np.sqrt(np.mean((chal_pred - g_true) ** 2)))
    champ_cls = to_class(champ_pred)
    chal_cls = to_class(chal_pred)
    champ_acc = float(np.mean(champ_cls == target_classes))
    chal_acc = float(np.mean(chal_cls == target_classes))

    if n < min_test_windows:
        return False, {
            "n_test": float(n),
            "rmse_gain": champ_rmse - chal_rmse,
            "rmse_gain_ci_lo": float("-inf"),
            "rmse_gain_ci_hi": float("inf"),
            "acc_diff": chal_acc - champ_acc,
            "acc_diff_ci_lo": float("-inf"),
            "acc_diff_ci_hi": float("inf"),
        }

    rng = np.random.default_rng(42)
    champ_sq = (champ_pred - g_true) ** 2
    chal_sq = (chal_pred - g_true) ** 2
    champ_ok = (champ_cls == target_classes).astype(np.float64)
    chal_ok = (chal_cls == target_classes).astype(np.float64)

    rmse_gains = np.empty(n_boot, dtype=np.float64)
    acc_diffs = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        rmse_gains[b] = np.sqrt(np.mean(champ_sq[idx])) - np.sqrt(np.mean(chal_sq[idx]))
        acc_diffs[b] = np.mean(chal_ok[idx]) - np.mean(champ_ok[idx])

    rmse_lo, rmse_hi = np.percentile(rmse_gains, [2.5, 97.5])
    acc_lo, acc_hi = np.percentile(acc_diffs, [2.5, 97.5])

    point_ok = should_promote_challenger(
        champ_rmse, champion_acc=champ_acc, challenger_rmse=chal_rmse, challenger_acc=chal_acc,
        min_rmse_improvement=min_rmse_improvement, max_acc_regression=max_acc_regression,
    )
    ci_ok = bool(rmse_lo >= min_rmse_improvement and acc_lo >= -max_acc_regression)
    stats = {
        "n_test": float(n),
        "rmse_gain": float(champ_rmse - chal_rmse),
        "rmse_gain_ci_lo": float(rmse_lo),
        "rmse_gain_ci_hi": float(rmse_hi),
        "acc_diff": float(chal_acc - champ_acc),
        "acc_diff_ci_lo": float(acc_lo),
        "acc_diff_ci_hi": float(acc_hi),
    }
    return bool(point_ok and ci_ok), stats


def _bump_patch_version(current_version: str) -> str:
    parts = current_version.strip().split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return datetime.datetime.now(datetime.timezone.utc).strftime("1.0.%Y%m%d%H%M")


def _normalize(seq: np.ndarray, exog: np.ndarray, scaler: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray]:
    s_mean = np.array(scaler["seq_mean"], dtype=np.float32)
    s_std = np.array(scaler["seq_std"], dtype=np.float32)
    e_mean = np.array(scaler["exog_mean"], dtype=np.float32)
    e_std = np.array(scaler["exog_std"], dtype=np.float32)
    return (seq - s_mean) / s_std, (exog - e_mean) / e_std


def evaluate_onnx_champion(
    model_dir: Path, test: SplitTensors
) -> Tuple[float, float, float, Dict[str, Any]]:
    """Valuta in ONNX Runtime il modello Champion verificandone SHA-256."""
    rmse, mae, acc, meta, _ = evaluate_onnx_champion_with_preds(model_dir, test)
    return rmse, mae, acc, meta


def evaluate_onnx_champion_with_preds(
    model_dir: Path, test: SplitTensors
) -> Tuple[float, float, float, Dict[str, Any], np.ndarray]:
    """Valuta in ONNX Runtime il Champion e restituisce anche il vettore delle predizioni."""
    import onnxruntime as ort

    meta_path = model_dir / "model_metadata.json"
    onnx_path = model_dir / "model.onnx"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    integrity = meta.get("artifacts_integrity", {})

    if compute_sha256(str(onnx_path)).lower() != str(integrity.get("model.onnx", "")).lower():
        raise RuntimeError("Checksum model.onnx del Champion non valido.")
    data_path = model_dir / "model.onnx.data"
    if data_path.is_file():
        if compute_sha256(str(data_path)).lower() != str(integrity.get("model.onnx.data", "")).lower():
            raise RuntimeError("Checksum model.onnx.data del Champion non valido.")

    seq_n, exog_n = _normalize(test.seq, test.exog, meta["scaler"])
    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    sess = ort.InferenceSession(str(onnx_path), sess_options=so, providers=["CPUExecutionProvider"])
    delta_preds = sess.run(None, {"seq": seq_n, "exog": exog_n})[0].reshape(-1)

    g_pred = test.g_now + delta_preds
    g_true = test.g_now + test.y_delta
    rmse = float(np.sqrt(np.mean((g_pred - g_true) ** 2)))
    mae = float(np.mean(np.abs(g_pred - g_true)))
    acc = float(np.mean(to_class(g_pred) == test.risk_classes))
    return rmse, mae, acc, meta, g_pred


def train_and_export_challenger(
    train: SplitTensors,
    val: SplitTensors,
    test: SplitTensors,
    output_dir: Path,
    champion_version: str,
    trained_until: datetime.datetime,
    signing_private_pem: str = "",
    epochs: int = 12,
    batch_size: int = 128,
) -> Tuple[float, float, float, Dict[str, Any], np.ndarray]:
    """Addestra il Challenger su `train`, esporta in ONNX e firma il manifest Ed25519."""
    torch.manual_seed(42)
    np.random.seed(42)

    scaler = {
        "seq_mean": train.seq.mean(axis=(0, 1)).astype(float).tolist(),
        "seq_std": (train.seq.std(axis=(0, 1)) + 1e-6).astype(float).tolist(),
        "exog_mean": train.exog.mean(axis=0).astype(float).tolist(),
        "exog_std": (train.exog.std(axis=0) + 1e-6).astype(float).tolist(),
    }
    s_tr, e_tr = _normalize(train.seq, train.exog, scaler)
    s_va, e_va = _normalize(val.seq, val.exog, scaler)
    s_te, e_te = _normalize(test.seq, test.exog, scaler)

    loader = DataLoader(
        TensorDataset(torch.from_numpy(s_tr), torch.from_numpy(e_tr), torch.from_numpy(train.y_delta)),
        batch_size=batch_size,
        shuffle=True,
    )
    model = GlucoseHybridLSTM()
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-5)
    criterion = nn.MSELoss()

    best_rmse = np.inf
    best_state = None
    for _epoch in range(1, epochs + 1):
        model.train()
        for b_seq, b_exog, b_y in loader:
            optimizer.zero_grad()
            loss = criterion(model(b_seq, b_exog), b_y)
            loss.backward()
            optimizer.step()
        val_rmse, _, _, _ = evaluate_model(model, s_va, e_va, val.y_delta, val.g_now, val.risk_classes)
        if val_rmse < best_rmse:
            best_rmse = val_rmse
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

    if best_state is not None:
        model.load_state_dict(best_state)

    test_rmse, test_mae, test_acc, g_pred = evaluate_model(
        model, s_te, e_te, test.y_delta, test.g_now, test.risk_classes
    )
    baseline_rmse = float(np.sqrt(np.mean(test.y_delta ** 2)))
    baseline_acc = float(np.mean(to_class(test.g_now) == test.risk_classes))

    output_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = output_dir / "model.onnx"
    export_onnx_model(
        model,
        torch.randn(1, SEQ_HISTORY_STEPS, 2),
        torch.randn(1, 14),
        str(onnx_path),
    )

    onnx_sha = compute_sha256(str(onnx_path))
    integrity: Dict[str, str] = {"model.onnx": onnx_sha}
    data_path = output_dir / "model.onnx.data"
    if data_path.is_file():
        integrity["model.onnx.data"] = compute_sha256(str(data_path))

    new_version = _bump_patch_version(champion_version)
    metadata: Dict[str, Any] = {
        "model_name": "OhioT1DM-GlucoseHybridLSTM",
        "model_version": new_version,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "trained_until": trained_until.isoformat(),
        "model_sha256": onnx_sha,
        "artifacts_integrity": integrity,
        "framework": "PyTorch -> ONNX Runtime",
        "clinical_task": "Forecasting Glucose at t+30 min (G(t+6)) and Risk Classification (OhioT1DM)",
        "prediction_target": "delta_glucose (mmol/L) -> G_pred = G(t) + delta_glucose",
        "test_metrics": {
            "test_rmse_mmol_l": round(test_rmse, 4),
            "test_rmse_mg_dl": round(test_rmse * 18.018, 2),
            "test_mae_mmol_l": round(test_mae, 4),
            "test_risk_class_accuracy": round(test_acc, 4),
            "baseline_persistence_rmse_mmol_l": round(baseline_rmse, 4),
            "baseline_persistence_accuracy": round(baseline_acc, 4),
        },
        "scaler": scaler,
        "risk_classes": CLASS_DESCRIPTIONS,
        "input_schema": {
            "glucose_history_steps": SEQ_HISTORY_STEPS,
            "glucose_history_interval_min": 5,
            "exogenous_features_count": 14,
            "exogenous_features": EXOG,
        },
    }
    if signing_private_pem:
        metadata["manifest_signature"] = sign_manifest_ed25519(metadata, signing_private_pem)
    (output_dir / "model_metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return test_rmse, test_mae, test_acc, metadata, g_pred


def fetch_training_rows(database_url: str) -> List[TrainingRow]:
    import psycopg

    pg_url = database_url.replace("postgresql+psycopg://", "postgresql://")
    rows: List[TrainingRow] = []
    with psycopg.connect(pg_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pseudo_patient_id, recorded_at, glucose_mmol, insulin_u, cho_rate "
                "FROM v_training_cgm_series ORDER BY pseudo_patient_id, recorded_at ASC"
            )
            for pid, rec_at, g, ins, cho in cur.fetchall():
                if rec_at.tzinfo is None:
                    rec_at = rec_at.replace(tzinfo=datetime.timezone.utc)
                rows.append(
                    TrainingRow(
                        pseudo_patient_id=str(pid),
                        recorded_at=rec_at,
                        glucose_mmol=float(g),
                        insulin_u=float(ins),
                        cho_rate=float(cho),
                    )
                )
    return rows


def _model_ssl_context() -> Optional[ssl.SSLContext]:
    ca_path = os.getenv("MODEL_CA_CERT_PATH", "")
    return ssl.create_default_context(cafile=ca_path) if ca_path else None


def download_champion_artifacts(repo_url: str, target_dir: Path) -> None:
    repo = repo_url.rstrip("/")
    ctx = _model_ssl_context()
    with urllib.request.urlopen(f"{repo}/models/current/manifest", context=ctx, timeout=10.0) as resp:  # nosec B310
        manifest = json.loads(resp.read().decode("utf-8"))
    target_dir.mkdir(parents=True, exist_ok=True)
    for fname in manifest.get("files", []):
        if fname not in {"model.onnx", "model.onnx.data", "model_metadata.json"}:
            continue
        with urllib.request.urlopen(f"{repo}/models/current/{fname}", context=ctx, timeout=15.0) as art:  # nosec B310
            (target_dir / fname).write_bytes(art.read())


def publish_challenger_artifacts(repo_url: str, token: str, challenger_dir: Path) -> dict:
    import base64
    import httpx

    payload = {
        "metadata_b64": base64.b64encode((challenger_dir / "model_metadata.json").read_bytes()).decode("ascii"),
        "onnx_b64": base64.b64encode((challenger_dir / "model.onnx").read_bytes()).decode("ascii"),
    }
    data_path = challenger_dir / "model.onnx.data"
    if data_path.is_file():
        payload["data_b64"] = base64.b64encode(data_path.read_bytes()).decode("ascii")

    verify_opt: bool | str = os.getenv("MODEL_CA_CERT_PATH", "") or True
    resp = httpx.put(
        repo_url.rstrip("/") + "/models/publish",
        json=payload,
        headers={"X-Repo-Token": token},
        verify=verify_opt,
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


def trigger_ml_rollout_restart(namespace: str = "default", deployment: str = "diabetes-forecasting-api") -> None:
    """Attiva il rolling update uno-alla-volta su Kubernetes usando il token RBAC minimo del Job."""
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    if not token_path.is_file() or not ca_path.is_file():
        logger.info("ServiceAccount Kubernetes assente: rollout restart saltato (esecuzione locale).")
        return

    api_host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    api_port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
    url = f"https://{api_host}:{api_port}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}"
    sa_token = token_path.read_text(encoding="utf-8").strip()
    restarted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    body = json.dumps(
        {"spec": {"template": {"metadata": {"annotations": {"retrain.diabetes.io/restartedAt": restarted_at}}}}}
    ).encode("utf-8")

    ctx = ssl.create_default_context(cafile=str(ca_path))
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"Bearer {sa_token}",
            "Content-Type": "application/strategic-merge-patch+json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10.0) as resp:  # nosec B310
        logger.info("[✓] Rolling update di %s attivato (HTTP %s).", deployment, resp.status)


def run_retrain_job(
    database_url: str,
    model_repo_url: str,
    repo_upload_token: str,
    signing_private_pem: str = "",
    min_sequences: int = 200,
    min_test_windows: int = 50,
    min_rmse_improvement: float = 0.005,
    max_acc_regression: float = 0.01,
    epochs: int = 12,
) -> str:
    rows = fetch_training_rows(database_url)
    if not rows:
        logger.info("Nessuna riga disponibile in v_training_cgm_series: retraining saltato.")
        return "skipped"

    work_dir = Path(tempfile.mkdtemp(prefix="retrain_"))
    try:
        champion_dir = work_dir / "champion"
        challenger_dir = work_dir / "challenger"
        download_champion_artifacts(model_repo_url, champion_dir)
        champ_meta_raw = json.loads((champion_dir / "model_metadata.json").read_text(encoding="utf-8"))
        trained_until = _parse_iso_utc(champ_meta_raw.get("trained_until"))

        train, val, test = build_temporal_splits_from_series(rows, trained_until=trained_until)
        total_windows = len(train.y_delta) + len(val.y_delta) + len(test.y_delta)
        logger.info(
            "Training Zone (trained_until=%s, demo dati simulati se >2020): lette %d righe -> "
            "finestre train=%d, val=%d, test=%d (tot=%d)",
            trained_until.isoformat() if trained_until else "none",
            len(rows),
            len(train.y_delta),
            len(val.y_delta),
            len(test.y_delta),
            total_windows,
        )
        if total_windows < min_sequences or len(train.y_delta) == 0 or len(val.y_delta) == 0 or len(test.y_delta) < min_test_windows:
            logger.info(
                "Finestre insufficienti (tot=%d<%d o test=%d<%d): retraining saltato.",
                total_windows, min_sequences, len(test.y_delta), min_test_windows,
            )
            return "skipped"

        champ_rmse, champ_mae, champ_acc, champ_meta, champ_pred = evaluate_onnx_champion_with_preds(
            champion_dir, test
        )
        logger.info(
            "Champion attuale (%s): RMSE=%.4f mmol/L | MAE=%.4f | RiskAcc=%.2f%%",
            champ_meta.get("model_version", "unknown"),
            champ_rmse,
            champ_mae,
            champ_acc * 100.0,
        )

        max_ts = max(r.recorded_at for r in rows)
        chal_rmse, chal_mae, chal_acc, chal_meta, chal_pred = train_and_export_challenger(
            train, val, test, challenger_dir, str(champ_meta.get("model_version", "1.0.0")),
            trained_until=max_ts, signing_private_pem=signing_private_pem, epochs=epochs,
        )
        logger.info(
            "Challenger candidato (%s): RMSE=%.4f mmol/L | MAE=%.4f | RiskAcc=%.2f%%",
            chal_meta["model_version"],
            chal_rmse,
            chal_mae,
            chal_acc * 100.0,
        )

        g_true = test.g_now + test.y_delta
        promote, ci = bootstrap_promotion_decision(
            g_true=g_true,
            champ_pred=champ_pred,
            chal_pred=chal_pred,
            target_classes=test.risk_classes,
            min_rmse_improvement=min_rmse_improvement,
            max_acc_regression=max_acc_regression,
            min_test_windows=min_test_windows,
        )
        logger.info(
            "Bootstrap 95%% CI (n=%d): ΔRMSE=%.4f [%.4f, %.4f] mmol/L | ΔAcc=%.4f [%.4f, %.4f]",
            int(ci["n_test"]),
            ci["rmse_gain"], ci["rmse_gain_ci_lo"], ci["rmse_gain_ci_hi"],
            ci["acc_diff"], ci["acc_diff_ci_lo"], ci["acc_diff_ci_hi"],
        )

        if promote:
            res = publish_challenger_artifacts(model_repo_url, repo_upload_token, challenger_dir)
            logger.info("[✓] Challenger PROMOSSO: %s", res)
            trigger_ml_rollout_restart()
            return "promoted"

        logger.info("Challenger NON promosso (IC 95%% o soglie puntuali non superate).")
        return "kept_champion"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Training Zone CronJob: retraining Champion/Challenger.")
    parser.add_argument("--epochs", type=int, default=int(os.getenv("TRAIN_EPOCHS", "12")))
    parser.add_argument("--min-sequences", type=int, default=int(os.getenv("MIN_TRAINING_SEQUENCES", "200")))
    parser.add_argument("--min-test-windows", type=int, default=int(os.getenv("MIN_TEST_WINDOWS", "50")))
    parser.add_argument(
        "--min-rmse-improvement",
        type=float,
        default=float(os.getenv("MIN_RMSE_IMPROVEMENT", "0.005")),
    )
    parser.add_argument(
        "--max-acc-regression",
        type=float,
        default=float(os.getenv("MAX_ACC_REGRESSION", "0.01")),
    )
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL", "")
    repo_url = os.getenv("MODEL_REPO_URL", "http://diabetes-model-repo")
    token = os.getenv("MODEL_REPO_UPLOAD_TOKEN", "")
    signing_priv = os.getenv("MODEL_SIGNING_PRIVATE_KEY_PEM", "").replace("\\n", "\n")
    if not db_url or not token or "BEGIN PRIVATE KEY" not in signing_priv:
        logger.error("[FAIL-CLOSED] DATABASE_URL, MODEL_REPO_UPLOAD_TOKEN o MODEL_SIGNING_PRIVATE_KEY_PEM mancanti.")
        return 1

    outcome = run_retrain_job(
        database_url=db_url,
        model_repo_url=repo_url,
        repo_upload_token=token,
        signing_private_pem=signing_priv,
        min_sequences=args.min_sequences,
        min_test_windows=args.min_test_windows,
        min_rmse_improvement=args.min_rmse_improvement,
        max_acc_regression=args.max_acc_regression,
        epochs=args.epochs,
    )
    logger.info("Esito job di retraining: %s", outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
