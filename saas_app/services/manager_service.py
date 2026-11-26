"""
FILE: saas_app/services/manager_service.py
RUOLO: Logica di dominio del Center Manager — statistiche aggregate e SLA Monitor (UC08, RF16-RF18).
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from fastapi import Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from saas_app.auth import add_audit_event
from saas_app.ml_client import collect_ml_pod_metrics
from saas_app.models import Intervention, MealPrediction, Patient, User
from saas_app.schemas import ManagerDashboardResponse, PodMetricSummary, WeeklyInterventionsPoint

# La raccolta metriche ML resta parallela alle statistiche DB senza usare l'event loop.
_METRICS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ml-metrics")

# I record senza dati del sensore non sono predizioni reali: restano fuori da ogni
# statistica aggregata (conteggi, MAE, distribuzione) per non falsare i numeri (N5).
_NO_RECENT_DATA = "NO_RECENT_DATA"


def _compute_aggregated_db_stats(db: Session, current_user: User, request_id: str) -> dict:
    total_patients = int(db.execute(select(func.count(Patient.id))).scalar_one() or 0)
    total_doctors = int(db.execute(select(func.count(User.id)).where(User.role == "doctor")).scalar_one() or 0)
    total_meals = int(
        db.execute(
            select(func.count(MealPrediction.id)).where(MealPrediction.outcome_code != _NO_RECENT_DATA)
        ).scalar_one()
        or 0
    )
    total_flags = int(
        db.execute(select(func.count(MealPrediction.id)).where(MealPrediction.doctor_flag.is_(True))).scalar_one() or 0
    )

    # Conteggi per causa aggregati in SQL: nessun caricamento delle righe in memoria.
    cause_counts = dict(
        db.execute(select(Intervention.cause, func.count(Intervention.id)).group_by(Intervention.cause)).all()
    )
    total_interventions = sum(cause_counts.values())
    by_model_err = int(cause_counts.get("model_error", 0))
    by_patient_beh = int(cause_counts.get("patient_behavior", 0))

    # MAE calcolato in SQL (media di |previsto - reale|) sulle sole coppie valide.
    avg_mae = db.execute(
        select(
            func.avg(func.abs(MealPrediction.forecasted_glucose_mmol - MealPrediction.actual_glucose_30m_mmol))
        ).where(
            MealPrediction.actual_glucose_30m_mmol.is_not(None),
            MealPrediction.forecasted_glucose_mmol.is_not(None),
            MealPrediction.outcome_code != _NO_RECENT_DATA,
        )
    ).scalar_one()  # None se non esiste alcuna coppia previsione/reale: MAE non definito, non 0.0 (P9).

    risk_dist: Dict[str, int] = {str(i): 0 for i in range(1, 8)}
    risk_counts = db.execute(
        select(MealPrediction.risk_class, func.count(MealPrediction.id)).group_by(MealPrediction.risk_class)
    ).all()
    for r_cls, cnt in risk_counts:
        # risk_class è nullo per NO_RECENT_DATA: fuori dalla distribuzione (N5).
        if r_cls is not None and 1 <= int(r_cls) <= 7:
            risk_dist[str(r_cls)] = int(cnt)

    now = datetime.now(timezone.utc)
    weekly: List[WeeklyInterventionsPoint] = []
    for w_idx in range(3, -1, -1):
        start = now - timedelta(days=7 * (w_idx + 1))
        end = now - timedelta(days=7 * w_idx)
        # Conteggi della finestra settimanale aggregati in SQL (nessuna riga caricata).
        win_total, win_model, win_behavior = db.execute(
            select(
                func.count(Intervention.id),
                func.sum(case((Intervention.cause == "model_error", 1), else_=0)),
                func.sum(case((Intervention.cause == "patient_behavior", 1), else_=0)),
            ).where(Intervention.created_at >= start, Intervention.created_at < end)
        ).one()
        weekly.append(
            WeeklyInterventionsPoint(
                week_label=f"Sett -{w_idx}" if w_idx > 0 else "Corrente",
                total_interventions=int(win_total or 0),
                model_error_count=int(win_model or 0),
                patient_behavior_count=int(win_behavior or 0),
            )
        )

    cutoff_30 = now - timedelta(days=30)
    meals_30 = int(
        db.execute(
            select(func.count(MealPrediction.id)).where(
                MealPrediction.declared_at >= cutoff_30,
                MealPrediction.outcome_code != _NO_RECENT_DATA,
            )
        ).scalar_one()
        or 0
    )
    avg_meals_per_day = (meals_30 / (total_patients * 30.0)) if total_patients > 0 else 0.0

    add_audit_event(db, current_user, "MANAGER_VIEW_DASHBOARD", request_id)
    db.commit()

    return {
        "total_patients": total_patients,
        "total_doctors": total_doctors,
        "total_meals_predictions": total_meals,
        "total_flagged_events": total_flags,
        "total_interventions": total_interventions,
        "interventions_by_model_error": by_model_err,
        "interventions_by_patient_behavior": by_patient_beh,
        "avg_prediction_mae_mmol": round(avg_mae, 3) if avg_mae is not None else None,
        "avg_meals_per_patient_per_day": round(avg_meals_per_day, 2),
        "weekly_interventions": weekly,
        "aggregated_risk_distribution": risk_dist,
    }


def get_dashboard(
    db: Session,
    current_user: User,
    request: Request,
) -> ManagerDashboardResponse:
    # Endpoint `def`: FastAPI esegue questa funzione nel thread pool, quindi la
    # Session di richiesta resta in un solo thread. Le metriche ML vengono
    # raccolte in parallelo da un executor dedicato, senza toccare l'event loop.
    request_id = getattr(request.state, "request_id", "unknown")
    metrics_future = _METRICS_EXECUTOR.submit(collect_ml_pod_metrics)
    stats = _compute_aggregated_db_stats(db, current_user, request_id)
    raw_pods = metrics_future.result()

    pod_summaries = [
        PodMetricSummary(
            instance_id=str(p.get("instance_id", "unknown")),
            service_status=str(p.get("service_status", "unknown")),
            uptime_seconds=float(p.get("uptime_seconds", 0.0)),
            total_predictions=int(p.get("total_predictions", 0)),
            error_rate=float(p.get("error_rate", 0.0)),
            avg_process_latency_ms=float(p.get("avg_process_latency_ms", 0.0)),
            max_process_latency_ms=float(p.get("max_process_latency_ms", 0.0)),
            sla_compliance_rate=float(p.get("sla_compliance_rate", 0.0)),
        )
        for p in raw_pods
    ]

    if pod_summaries:
        service_available = any(p.service_status == "healthy" for p in pod_summaries)
        avg_lat = sum(p.avg_process_latency_ms for p in pod_summaries) / len(pod_summaries)
        sla_rate = sum(p.sla_compliance_rate for p in pod_summaries) / len(pod_summaries)
        err_rate = sum(p.error_rate for p in pod_summaries) / len(pod_summaries)
    else:
        # Nessun pod ML raggiungibile -> NON riportare 100% SLA!
        service_available = False
        avg_lat = 0.0
        sla_rate = 0.0
        err_rate = 100.0

    return ManagerDashboardResponse(
        **stats,
        service_available=service_available,
        ml_pods_online=len(pod_summaries),
        avg_ml_latency_ms=round(avg_lat, 2),
        overall_sla_compliance_rate=round(sla_rate, 2),
        overall_error_rate=round(err_rate, 2),
        ml_pods=pod_summaries,
    )
