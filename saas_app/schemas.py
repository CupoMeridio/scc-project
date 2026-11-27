"""
FILE: saas_app/schemas.py
RUOLO: Schemi Pydantic v2 con validazione limite 72 byte bcrypt e normalizzazione UTC.
"""

from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _check_bcrypt_length(password: str) -> str:
    if len(password.encode("utf-8")) > 72:
        raise ValueError("La password non può superare 72 byte UTF-8.")
    return password


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def validate_password_bytes(cls, v: str) -> str:
        return _check_bcrypt_length(v)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=72)
    new_password: str = Field(..., min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def validate_password_bytes(cls, v: str) -> str:
        return _check_bcrypt_length(v)


class UserInfo(BaseModel):
    id: int
    username: str
    role: Literal["patient", "doctor", "manager"]
    patient_code: Optional[str] = None
    must_change_password: bool = False


class TokenResponse(BaseModel):
    expires_in_minutes: int
    user: UserInfo


class MealDeclareRequest(BaseModel):
    carbs_g: float = Field(..., ge=0.0, le=300.0, description="Carboidrati del pasto in grammi")
    insulin_u: float = Field(default=0.0, ge=0.0, le=50.0, description="Bolo di insulina annunciato (IU)")


class MealDeclareResponse(BaseModel):
    meal_id: int
    declared_at: datetime
    carbs_g: float
    diet_carb_limit_g: float
    # Nulli quando l'esito è NO_RECENT_DATA: nessun valore inventato (N5).
    current_glucose_mmol_l: Optional[float] = None
    current_glucose_mg_dl: Optional[float] = None
    forecasted_glucose_mmol_l: Optional[float] = None
    forecasted_glucose_mg_dl: Optional[float] = None
    risk_class: Optional[int] = None
    outcome_code: str
    outcome_message: str
    doctor_flag: bool
    model_version: str


class CgmPoint(BaseModel):
    # from_attributes: un CgmReading ORM si converte direttamente in CgmPoint
    # (CgmPoint.model_validate(reading)) senza ripetere la mappatura dei campi.
    model_config = ConfigDict(from_attributes=True)

    recorded_at: datetime
    glucose_mmol: float
    insulin_u: float
    cho_rate: float


class MealHistoryItem(BaseModel):
    id: int
    declared_at: datetime
    carbs_g: float
    insulin_u: float
    # Nulli quando l'esito è NO_RECENT_DATA: nessun valore inventato (N5).
    current_glucose_mmol: Optional[float] = None
    forecasted_glucose_mmol: Optional[float] = None
    actual_glucose_30m_mmol: Optional[float] = None
    risk_class: Optional[int] = None
    outcome_code: str
    outcome_message: str
    doctor_flag: bool


class PatientSummaryResponse(BaseModel):
    patient_code: str
    full_name: str
    doctor_name: str
    diet_carb_limit_g: float
    target_min_mmol: float
    target_max_mmol: float
    latest_glucose_mmol: Optional[float] = None
    latest_glucose_mg_dl: Optional[float] = None
    latest_reading_at: Optional[datetime] = None
    total_meals_recorded: int
    flagged_events_count: int


class PatientHistoryResponse(BaseModel):
    range: Literal["week", "month", "year"]
    diet_carb_limit_g: float
    readings: List[CgmPoint]
    meals: List[MealHistoryItem]


class DoctorPatientItem(BaseModel):
    patient_id: int
    patient_code: str
    full_name: str
    diet_carb_limit_g: float
    latest_glucose_mmol: Optional[float] = None
    latest_outcome: Optional[str] = None
    active_flags_count: int
    status_color: Literal["green", "orange", "red"]
    status_summary: str


class InterventionItem(BaseModel):
    id: int
    created_at: datetime
    intervention_type: str
    cause: str
    notes: str
    new_diet_carb_limit_g: Optional[float] = None


class PatientTrendSummary(BaseModel):
    # Nullo finché non esiste almeno una coppia previsione/reale (P9): non si inventa 0.0.
    mean_prediction_error_mmol: Optional[float] = None
    model_bias_label: str
    above_diet_meals_pct: float
    meal_regularity_per_day: float
    flagged_events_count: int


class DoctorPatientDetailResponse(BaseModel):
    patient_id: int
    patient_code: str
    full_name: str
    email: str
    diet_carb_limit_g: float
    target_min_mmol: float
    target_max_mmol: float
    range: Literal["week", "month", "year"]
    readings: List[CgmPoint]
    meals: List[MealHistoryItem]
    interventions: List[InterventionItem]
    trends: PatientTrendSummary


class RegisterPatientRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.\-]+$")
    email: str = Field(..., min_length=5, max_length=120)
    full_name: str = Field(..., min_length=2, max_length=120)
    patient_code: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    diet_carb_limit_g: float = Field(default=60.0, ge=10.0, le=300.0)
    target_min_mmol: float = Field(default=3.9, ge=2.5, le=6.0)
    target_max_mmol: float = Field(default=10.0, ge=6.0, le=18.0)


class RegisterPatientResponse(DoctorPatientItem):
    """Esito della registrazione: include la password temporanea mostrata una sola volta (Q5)."""

    temporary_password: str


class UpdateDietRequest(BaseModel):
    diet_carb_limit_g: float = Field(..., ge=10.0, le=300.0)
    target_min_mmol: Optional[float] = Field(default=None, ge=2.5, le=6.0)
    target_max_mmol: Optional[float] = Field(default=None, ge=6.0, le=18.0)


class CreateInterventionRequest(BaseModel):
    intervention_type: Literal["reminder", "diet_change"]
    cause: Literal["model_error", "patient_behavior"]
    notes: str = Field(..., min_length=3, max_length=1000)
    new_diet_carb_limit_g: Optional[float] = Field(default=None, ge=10.0, le=300.0)


class WeeklyInterventionsPoint(BaseModel):
    week_label: str
    total_interventions: int
    model_error_count: int
    patient_behavior_count: int


class PodMetricSummary(BaseModel):
    instance_id: str
    service_status: str
    uptime_seconds: float
    total_predictions: int
    error_rate: float
    avg_process_latency_ms: float
    max_process_latency_ms: float
    sla_compliance_rate: float


class ManagerDashboardResponse(BaseModel):
    total_patients: int
    total_doctors: int
    total_meals_predictions: int
    total_flagged_events: int
    total_interventions: int
    interventions_by_model_error: int
    interventions_by_patient_behavior: int
    # Nullo finché non esiste almeno una coppia previsione/reale (P9): non si inventa 0.0.
    avg_prediction_mae_mmol: Optional[float] = None
    avg_meals_per_patient_per_day: float
    weekly_interventions: List[WeeklyInterventionsPoint]
    aggregated_risk_distribution: Dict[str, int]
    service_available: bool
    ml_pods_online: int
    avg_ml_latency_ms: float
    overall_sla_compliance_rate: float
    overall_error_rate: float
    ml_pods: List[PodMetricSummary]


class CgmIngestRequest(BaseModel):
    patient_code: Optional[str] = Field(default=None, min_length=1, max_length=32)
    glucose_mmol: float = Field(..., ge=1.0, le=35.0)
    insulin_u: float = Field(default=0.0, ge=0.0, le=100.0)
    cho_rate: float = Field(default=0.0, ge=0.0, le=15.0)
    recorded_at: Optional[datetime] = None

    @field_validator("recorded_at")
    @classmethod
    def normalize_utc(cls, dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


class CgmIngestResponse(BaseModel):
    status: str
    patient_code: str
    recorded_at: datetime
