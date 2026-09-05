"""Intervention-level Training Dataset Builder with Strict Anti-Leakage and Data Quality Validation."""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.customer import Customer
from app.models.event import Event
from app.models.promise_to_pay import PromiseToPay
from app.models.recovery_case import RecoveryCase
from app.models.recovery_plan import RecoveryPlan, RecoveryPlanStep
from app.models.voice_call import VoiceCall
from app.ml.features import (
    InterventionTrainingRow,
    RecoveryFeatureVector,
    validate_pre_intervention_features,
)

logger = logging.getLogger(__name__)

# Standard attribution window: 72 hours (3 days)
DEFAULT_ATTRIBUTION_WINDOW_HOURS: int = getattr(settings, "ATTRIBUTION_WINDOW_HOURS", 72)
MIN_TRAINING_SAMPLES_THRESHOLD: int = 50


@dataclass
class DatasetStatistics:
    """Statistical summary and data quality audit for the generated dataset."""

    total_rows: int = 0
    recovered_rows: int = 0
    recovery_rate: float = 0.0
    intervention_distribution: Dict[str, int] = field(default_factory=dict)
    missing_values: Dict[str, int] = field(default_factory=dict)
    date_range: Dict[str, Optional[str]] = field(default_factory=lambda: {"min": None, "max": None})
    discarded_rows_count: int = 0
    discarded_reasons: Dict[str, int] = field(default_factory=dict)
    insufficient_data: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "recovered_rows": self.recovered_rows,
            "recovery_rate": self.recovery_rate,
            "intervention_distribution": self.intervention_distribution,
            "missing_values": self.missing_values,
            "date_range": self.date_range,
            "discarded_rows_count": self.discarded_rows_count,
            "discarded_reasons": self.discarded_reasons,
            "insufficient_data": self.insufficient_data,
        }


@dataclass
class DatasetBuildResult:
    """Complete structured output of dataset builder."""

    dataframe: pd.DataFrame
    rows: List[InterventionTrainingRow]
    statistics: DatasetStatistics


class RecoveryMLDatasetBuilder:
    """Extracts, validates, and builds intervention-level point-in-time training datasets."""

    @classmethod
    def generate_synthetic_training_dataset(
        cls, sample_count: int = 100, seed: int = 42
    ) -> pd.DataFrame:
        """Generate synthetic dataset for sandbox/testing purposes."""
        from app.ml.synthetic import generate_synthetic_demo_dataset
        records = generate_synthetic_demo_dataset(n_samples=sample_count, seed=seed)
        flattened = []
        for r in records:
            row = dict(r.get("features", {}))
            row["id"] = r.get("id")
            row["case_id"] = str(r.get("id"))
            row["recovered"] = int(r.get("label", 0))
            flattened.append(row)
        return pd.DataFrame(flattened)

    VALID_INTERVENTION_TYPES: Set[str] = {
        "PAYMENT_RETRY",
        "EMAIL",
        "VOICE",
        "WHATSAPP",
        "NO_ACTION",
        "EMAIL_PAYMENT_RECOVERY",
        "EMAIL_FOLLOWUP",
        "VOICE_RECOVERY_CALL",
        "WHATSAPP_REMINDER",
        "AUTO_DEBIT_RETRY",
    }

    @classmethod
    def normalize_intervention_type(cls, raw_type: str) -> str:
        """Map detailed action types into standardized high-level intervention categories."""
        val = str(raw_type or "UNKNOWN").upper()
        if "EMAIL" in val:
            return "EMAIL"
        if "VOICE" in val:
            return "VOICE"
        if "WHATSAPP" in val:
            return "WHATSAPP"
        if "RETRY" in val or "AUTO_DEBIT" in val:
            return "PAYMENT_RETRY"
        if "NO_ACTION" in val or "NONE" in val:
            return "NO_ACTION"
        return "UNKNOWN"

    @classmethod
    def extract_pre_intervention_features(
        cls,
        db: Session,
        case: RecoveryCase,
        customer: Optional[Customer],
        prediction_timestamp: datetime,
        current_step_number: int = 1,
    ) -> Dict[str, Any]:
        """Compute pre-intervention features strictly prior to prediction_timestamp."""
        # 1. Case attributes
        amount = float(case.amount_at_risk or 0.0)
        currency = str(case.currency or "INR").upper()

        # Diagnosis / failure details
        diag = case.diagnoses[0] if getattr(case, "diagnoses", None) else None
        failure_category = str(getattr(diag, "category", "UNKNOWN") or "UNKNOWN").upper()
        failure_code = str(getattr(diag, "failure_code", "UNKNOWN") or "UNKNOWN").upper()

        # Days since case creation / failure
        case_created = case.created_at
        if case_created.tzinfo is None:
            case_created = case_created.replace(tzinfo=timezone.utc)
        if prediction_timestamp.tzinfo is None:
            prediction_timestamp = prediction_timestamp.replace(tzinfo=timezone.utc)

        days_since_failure = max(0.0, (prediction_timestamp - case_created).total_seconds() / 86400.0)
        days_overdue = days_since_failure

        # 2. Historical Customer features (strictly timestamped <= prediction_timestamp)
        cust_age_days = 0.0
        prev_success = 0
        prev_failed = 0
        prev_recoveries = 0
        prev_ptps = 0
        prev_ptp_fulfilled = 0
        prev_voice = 0
        prev_email = 0
        prev_whatsapp = 0

        if customer:
            if customer.created_at:
                c_created = customer.created_at
                if c_created.tzinfo is None:
                    c_created = c_created.replace(tzinfo=timezone.utc)
                cust_age_days = max(0.0, (prediction_timestamp - c_created).total_seconds() / 86400.0)

            # Query historical events before prediction_timestamp
            events = db.scalars(
                select(Event).where(
                    Event.occurred_at <= prediction_timestamp,
                )
            ).all()

            # Filter events for this customer
            for ev in events:
                payload = ev.payload or {}
                # Match by customer external ID or email
                if (
                    str(payload.get("customer_id")) == str(customer.external_customer_id)
                    or payload.get("email") == customer.email
                ):
                    if ev.event_type in ["payment.captured", "payment.successful"]:
                        prev_success += 1
                    elif ev.event_type in ["payment.failed"]:
                        prev_failed += 1

            # Historical completed cases for this customer
            prior_cases = db.scalars(
                select(RecoveryCase).where(
                    RecoveryCase.customer_id == customer.id,
                    RecoveryCase.id != case.id,
                    RecoveryCase.created_at <= prediction_timestamp,
                )
            ).all()
            for pc in prior_cases:
                if pc.status == "RECOVERED":
                    prev_recoveries += 1

            # Historical PTPs
            prior_ptps = db.scalars(
                select(PromiseToPay).where(
                    PromiseToPay.created_at <= prediction_timestamp,
                )
            ).all()
            for ptp in prior_ptps:
                # check if belongs to customer's cases
                if any(ptp.recovery_case_id == pc.id for pc in prior_cases):
                    prev_ptps += 1
                    if ptp.status == "FULFILLED":
                        prev_ptp_fulfilled += 1

            # Historical Voice calls
            prior_calls = db.scalars(
                select(VoiceCall).where(
                    VoiceCall.customer_id == customer.id,
                    VoiceCall.created_at <= prediction_timestamp,
                )
            ).all()
            prev_voice = len(prior_calls)

        fulfillment_rate = (prev_ptp_fulfilled / prev_ptps) if prev_ptps > 0 else 0.0

        # 3. History of attempts on THIS case prior to this step
        prev_attempts = max(0, current_step_number - 1)
        prev_outcome = "NONE"
        if current_step_number > 1 and case.recovery_plan:
            for st in case.recovery_plan.steps:
                st_exec = st.executed_at
                if st_exec:
                    if st_exec.tzinfo is None:
                        st_exec = st_exec.replace(tzinfo=timezone.utc)
                    if st.step_number < current_step_number and st_exec <= prediction_timestamp:
                        prev_outcome = "FAILED" if st.status in ["COMPLETED", "FAILED"] else st.status

        # Construct raw features
        feat_vector = RecoveryFeatureVector(
            amount_at_risk=amount,
            currency=currency,
            days_overdue=days_overdue,
            failure_code=failure_code,
            failure_category=failure_category,
            payment_type="card",
            is_subscription_or_invoice=1,
            customer_age_days=cust_age_days,
            previous_successful_payments=prev_success,
            previous_failed_payments=prev_failed,
            previous_recoveries=prev_recoveries,
            previous_promises_to_pay=prev_ptps,
            previous_ptp_fulfillment_rate=fulfillment_rate,
            previous_voice_attempts=prev_voice,
            previous_email_attempts=prev_email,
            previous_whatsapp_attempts=prev_whatsapp,
            hour_of_day=prediction_timestamp.hour,
            day_of_week=prediction_timestamp.weekday(),
            days_since_failure=days_since_failure,
            previous_intervention_outcome=prev_outcome,
            number_of_previous_recovery_attempts=prev_attempts,
            previous_recovery_time_seconds=0.0,
        )

        feat_dict = feat_vector.to_dict()
        validate_pre_intervention_features(feat_dict)
        return feat_dict

    @classmethod
    def build_training_dataset(
        cls,
        db: Session,
        output_csv_path: Optional[str] = None,
        attribution_window_hours: int = DEFAULT_ATTRIBUTION_WINDOW_HOURS,
        min_samples: int = MIN_TRAINING_SAMPLES_THRESHOLD,
    ) -> DatasetBuildResult:
        """Construct an intervention-level point-in-time ML training dataset.
        
        Performs strict anti-leakage checks, quality audits, and returns statistics.
        """
        stats = DatasetStatistics()
        seen_intervention_ids: Set[str] = set()
        valid_rows: List[InterventionTrainingRow] = []
        now_utc = datetime.now(timezone.utc)

        # 1. Fetch eligible historical plan steps and voice calls
        steps = db.scalars(
            select(RecoveryPlanStep)
            .join(RecoveryPlan, RecoveryPlanStep.recovery_plan_id == RecoveryPlan.id)
            .order_by(RecoveryPlanStep.created_at.asc())
        ).all()

        for step in steps:
            intervention_id = str(step.id)

            # Quality Check: Duplicate Intervention ID
            if intervention_id in seen_intervention_ids:
                stats.discarded_rows_count += 1
                stats.discarded_reasons["DUPLICATE_INTERVENTION_ID"] = (
                    stats.discarded_reasons.get("DUPLICATE_INTERVENTION_ID", 0) + 1
                )
                continue
            seen_intervention_ids.add(intervention_id)

            # Check associated case and customer
            plan = step.recovery_plan
            case = plan.recovery_case if plan else None
            if not case:
                stats.discarded_rows_count += 1
                stats.discarded_reasons["MISSING_CASE_ID"] = (
                    stats.discarded_reasons.get("MISSING_CASE_ID", 0) + 1
                )
                continue

            # Check amounts
            amount = float(case.amount_at_risk or 0.0)
            if amount < 0:
                stats.discarded_rows_count += 1
                stats.discarded_reasons["NEGATIVE_AMOUNT"] = (
                    stats.discarded_reasons.get("NEGATIVE_AMOUNT", 0) + 1
                )
                continue

            # Check timestamp
            pred_time = step.executed_at or step.scheduled_at or step.created_at
            if not pred_time:
                stats.discarded_rows_count += 1
                stats.discarded_reasons["MISSING_TIMESTAMP"] = (
                    stats.discarded_reasons.get("MISSING_TIMESTAMP", 0) + 1
                )
                continue

            if pred_time.tzinfo is None:
                pred_time = pred_time.replace(tzinfo=timezone.utc)

            # Quality Check: Future prediction timestamp
            if pred_time > now_utc + timedelta(minutes=5):
                stats.discarded_rows_count += 1
                stats.discarded_reasons["FUTURE_PREDICTION_TIMESTAMP"] = (
                    stats.discarded_reasons.get("FUTURE_PREDICTION_TIMESTAMP", 0) + 1
                )
                continue

            # Normalize Intervention Type
            norm_type = cls.normalize_intervention_type(step.action_type)
            if norm_type == "UNKNOWN":
                stats.discarded_rows_count += 1
                stats.discarded_reasons["INVALID_INTERVENTION_TYPE"] = (
                    stats.discarded_reasons.get("INVALID_INTERVENTION_TYPE", 0) + 1
                )
                continue

            # 2. Extract strictly pre-intervention features
            try:
                features = cls.extract_pre_intervention_features(
                    db=db,
                    case=case,
                    customer=case.customer,
                    prediction_timestamp=pred_time,
                    current_step_number=step.step_number,
                )
            except Exception as e:
                logger.warning(f"[FEATURE_EXTRACTION_ERROR] Step {step.id}: {e}")
                stats.discarded_rows_count += 1
                stats.discarded_reasons["FEATURE_EXTRACTION_ERROR"] = (
                    stats.discarded_reasons.get("FEATURE_EXTRACTION_ERROR", 0) + 1
                )
                continue

            # 3. Determine Outcome Label within Attribution Window
            window_end = pred_time + timedelta(hours=attribution_window_hours)
            recovered = 0
            recovered_amount = 0.0
            time_to_rec: Optional[float] = None

            # Check if case was recovered within [pred_time, window_end]
            if case.status == "RECOVERED" and case.updated_at:
                c_up = case.updated_at
                if c_up.tzinfo is None:
                    c_up = c_up.replace(tzinfo=timezone.utc)

                if pred_time <= c_up <= window_end:
                    recovered = 1
                    recovered_amount = float(case.recovered_amount or case.amount_at_risk or 0.0)
                    time_to_rec = (c_up - pred_time).total_seconds()

            # Record row
            row = InterventionTrainingRow(
                case_id=str(case.id),
                intervention_id=intervention_id,
                intervention_type=norm_type,
                prediction_timestamp=pred_time.isoformat(),
                features=features,
                recovered=recovered,
                amount_at_risk=amount,
                amount_recovered=recovered_amount,
                time_to_recovery_seconds=time_to_rec,
            )
            valid_rows.append(row)

            # Update distribution stats
            stats.intervention_distribution[norm_type] = (
                stats.intervention_distribution.get(norm_type, 0) + 1
            )
            if recovered == 1:
                stats.recovered_rows += 1

        # 4. Compile statistics
        stats.total_rows = len(valid_rows)
        stats.recovery_rate = (stats.recovered_rows / stats.total_rows) if stats.total_rows > 0 else 0.0
        stats.insufficient_data = stats.total_rows < min_samples

        if valid_rows:
            dates = [r.prediction_timestamp for r in valid_rows]
            stats.date_range = {"min": min(dates), "max": max(dates)}

        # Convert to DataFrame
        flattened_dicts = [r.to_dict(flatten_features=True) for r in valid_rows]
        df = pd.DataFrame(flattened_dicts) if flattened_dicts else pd.DataFrame(columns=[
            "case_id", "intervention_id", "intervention_type", "prediction_timestamp",
            "recovered", "amount_at_risk", "amount_recovered", "time_to_recovery_seconds"
        ])

        # Missing values check on dataframe
        if not df.empty:
            stats.missing_values = {col: int(df[col].isna().sum()) for col in df.columns if df[col].isna().sum() > 0}

        # Save to disk if requested
        if output_csv_path and not df.empty:
            out_file = Path(output_csv_path)
            out_file.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out_file, index=False)
            logger.info(f"[DATASET_SAVED] Saved {len(df)} rows to {out_file.resolve()}")

        return DatasetBuildResult(dataframe=df, rows=valid_rows, statistics=stats)
