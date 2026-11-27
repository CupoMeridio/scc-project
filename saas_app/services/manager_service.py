"""
FILE: saas_app/services/manager_service.py
RUOLO: Logica di dominio del Center Manager — statistiche aggregate via viste SQL (zero PII) e SLA Monitor.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from saas_app.auth import Principal, add_audit_event
from saas_app.ml_client import collect_ml_pod_metrics
from saas_app.schemas import ManagerDashboardResponse, PodMetricSummary, WeeklyInterventionsPoint

_METRICS_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ml-metrics")


def _stats_from_views(db: Session) -> dict:
    """Legge esclusivamente dalle viste aggregate (percorso unico in test e produzione, V5)."""
    ov = db.execute(
        text(
            "SELECT total_patients, total_doctors, total_meals_predictions, "
            "total_flagged_events, total_interventions, interventions_by_model_error, "
            "interventions_by_patient_behavior, avg_prediction_mae_mmol, meals_last_30_days "
            "FROM v_manager_overview"
        )
    ).mappings().one()

    total_patients = int(ov["total_patients"] or 0)
    meals_30 = int(ov["meals_last_30_days"] or 0)
    avg_mae = ov["avg_prediction_mae_mmol"]
    avg_meals_per_day = (meals_30 / (total_patients * 30.0)) if total_patients > 0 else 0.0

    risk_dist: Dict[str, int] = {str(i): 0 for i in range(1, 8)}
    for row in db.execute(text("SELECT risk_class, cnt FROM v_manager_risk_distribution")).mappings():
        r_cls = row["risk_class"]
        if r_cls is not None and 1 <= int(r_cls) <= 7:
            risk_dist[str(int(r_cls))] = int(row["cnt"] or 0)

    by_week = {
        int(row["week_idx"]): row
        for row in db.execute(
            text(
                "SELECT week_idx, total_interventions, model_error_count, patient_behavior_count "
                "FROM v_manager_weekly_interventions"
            )
        ).mappings()
    }
    weekly: List[WeeklyInterventionsPoint] = []
    for w_idx in range(3, -1, -1):
        row = by_week.get(w_idx, {})
        weekly.append(
            WeeklyInterventionsPoint(
                week_label=f"Sett -{w_idx}" if w_idx > 0 else "Corrente",
                total_interventions=int(row.get("total_interventions", 0) or 0),
                model_error_count=int(row.get("model_error_count", 0) or 0),
                patient_behavior_count=int(row.get("patient_behavior_count", 0) or 0),
            )
        )

    return {
        "total_patients": total_patients,
        "total_doctors": int(ov["total_doctors"] or 0),
        "total_meals_predictions": int(ov["total_meals_predictions"] or 0),
        "total_flagged_events": int(ov["total_flagged_events"] or 0),
        "total_interventions": int(ov["total_interventions"] or 0),
        "interventions_by_model_error": int(ov["interventions_by_model_error"] or 0),
        "interventions_by_patient_behavior": int(ov["interventions_by_patient_behavior"] or 0),
        "avg_prediction_mae_mmol": round(float(avg_mae), 3) if avg_mae is not None else None,
        "avg_meals_per_patient_per_day": round(avg_meals_per_day, 2),
        "weekly_interventions": weekly,
        "aggregated_risk_distribution": risk_dist,
    }


def _compute_aggregated_db_stats(db: Session, current_user: Principal, request_id: str) -> dict:
    stats = _stats_from_views(db)
    add_audit_event(db, current_user, "MANAGER_VIEW_DASHBOARD", request_id)
    db.commit()
    return stats


def get_dashboard(
    db: Session,
    current_user: Principal,
    request: Request,
) -> ManagerDashboardResponse:
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
