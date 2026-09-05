"""Dashboard Aggregation and Analytics Service for Revenue Recovery Command Center."""
from datetime import datetime, timezone, timedelta
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy import func, and_, or_, desc
from sqlalchemy.orm import Session

from app.models.recovery_case import RecoveryCase
from app.models.outcome import RecoveryOutcome
from app.models.recovery_action import RecoveryAction
from app.models.promise_to_pay import PromiseToPay
from app.models.audit_log import AuditLog
from app.models.voice_call import VoiceCall
from app.ml.recovery_probability_model import (
    RecoveryProbabilityModelService,
    DEFAULT_MODEL_VERSION,
    MODELS_DIR,
)
from app.services.next_best_action import NextBestActionService
import json

logger = logging.getLogger(__name__)


class DashboardService:
    """Provides high-performance aggregated metrics, performance trends, and audit timelines for Command Center."""

    @classmethod
    def get_summary_kpis(
        cls,
        db: Session,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compute authoritative top-level KPI metrics across recovery operations."""
        case_filters = []
        if start_date:
            case_filters.append(RecoveryCase.created_at >= start_date)
        if end_date:
            case_filters.append(RecoveryCase.created_at <= end_date)
        if currency:
            case_filters.append(RecoveryCase.currency == currency.upper())

        # 1. Total Revenue at Risk
        at_risk_query = db.query(func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0))
        if case_filters:
            at_risk_query = at_risk_query.filter(and_(*case_filters))
        total_at_risk = float(at_risk_query.scalar() or 0.0)

        # 2. Total Authoritative Revenue Recovered (from verified RecoveryOutcome)
        outcome_filters = []
        if start_date:
            outcome_filters.append(RecoveryOutcome.occurred_at >= start_date)
        if end_date:
            outcome_filters.append(RecoveryOutcome.occurred_at <= end_date)

        recovered_query = db.query(func.coalesce(func.sum(RecoveryOutcome.amount_recovered), 0))
        if outcome_filters:
            recovered_query = recovered_query.filter(and_(*outcome_filters))
        total_recovered = float(recovered_query.scalar() or 0.0)

        # 3. Recovery Rate
        recovery_rate = (
            round((total_recovered / total_at_risk) * 100.0, 2)
            if total_at_risk > 0.0
            else 0.0
        )

        # 4. Active Recovery Cases
        active_cases_query = db.query(func.count(RecoveryCase.id)).filter(
            RecoveryCase.status.in_(["OPEN", "IN_PROGRESS", "PAUSED"])
        )
        if case_filters:
            active_cases_query = active_cases_query.filter(and_(*case_filters))
        active_cases_count = int(active_cases_query.scalar() or 0)

        # 5. Active Promise-to-Pay Count & Volume
        ptp_query = db.query(
            func.count(PromiseToPay.id),
            func.coalesce(func.sum(PromiseToPay.promised_amount), 0),
        ).filter(PromiseToPay.status.in_(["ACTIVE", "PENDING"]))
        ptp_count, ptp_volume = ptp_query.first() or (0, 0)
        active_ptp_count = int(ptp_count or 0)
        active_ptp_volume = float(ptp_volume or 0.0)

        # 6. Total Recovery Actions Executed
        actions_count = int(db.query(func.count(RecoveryAction.id)).scalar() or 0)

        # 7. Model Status & Total Expected Recovery Value
        model_pipe = RecoveryProbabilityModelService.load_model()
        decision_mode = "ML_NBA" if model_pipe is not None else "RULE_BASED_COLD_START"

        # Calculate Expected Recovery Value from open cases
        open_cases = (
            db.query(RecoveryCase)
            .filter(RecoveryCase.status.in_(["OPEN", "IN_PROGRESS"]))
            .limit(50)
            .all()
        )
        total_erv = 0.0
        for c in open_cases:
            amt = float(c.amount_at_risk or 0.0)
            if model_pipe is not None:
                total_erv += round(amt * 0.55, 2)  # approximate baseline ERV aggregation
            else:
                total_erv += round(amt * 0.40, 2)

        return {
            "total_revenue_at_risk": total_at_risk,
            "total_revenue_recovered": total_recovered,
            "recovery_rate_percentage": recovery_rate,
            "active_recovery_cases": active_cases_count,
            "active_promise_to_pay_count": active_ptp_count,
            "active_promise_to_pay_volume": active_ptp_volume,
            "total_recovery_actions": actions_count,
            "expected_recovery_value": round(total_erv, 2),
            "decision_mode": decision_mode,
            "currency": currency or "INR",
        }

    @classmethod
    def get_recovery_performance(cls, db: Session) -> Dict[str, Any]:
        """Aggregate case volume and status breakdown with time-to-recovery statistics."""
        total_cases = int(db.query(func.count(RecoveryCase.id)).scalar() or 0)
        recovered_cases = int(
            db.query(func.count(RecoveryCase.id))
            .filter(RecoveryCase.status == "RECOVERED")
            .scalar()
            or 0
        )
        in_progress_cases = int(
            db.query(func.count(RecoveryCase.id))
            .filter(RecoveryCase.status.in_(["IN_PROGRESS", "OPEN", "PAUSED"]))
            .scalar()
            or 0
        )
        closed_failed_cases = int(
            db.query(func.count(RecoveryCase.id))
            .filter(RecoveryCase.status.in_(["CLOSED", "FAILED", "EXPIRED"]))
            .scalar()
            or 0
        )

        avg_time_sec = db.query(func.avg(RecoveryOutcome.time_to_recovery_seconds)).scalar()
        avg_recovered_amt = db.query(func.avg(RecoveryOutcome.amount_recovered)).scalar()

        pct = round((recovered_cases / total_cases * 100.0), 2) if total_cases > 0 else 0.0

        return {
            "total_cases": total_cases,
            "recovered_cases": recovered_cases,
            "in_progress_cases": in_progress_cases,
            "not_recovered_cases": closed_failed_cases,
            "recovery_percentage": pct,
            "average_time_to_recovery_seconds": float(avg_time_sec or 0.0),
            "average_time_to_recovery_hours": round(float(avg_time_sec or 0.0) / 3600.0, 1),
            "average_recovered_amount": round(float(avg_recovered_amt or 0.0), 2),
        }

    @classmethod
    def get_intervention_performance(cls, db: Session) -> List[Dict[str, Any]]:
        """Compute performance breakdown per channel (EMAIL, VOICE, PAYMENT_RETRY, WHATSAPP)."""
        channels = ["EMAIL", "VOICE", "PAYMENT_RETRY", "WHATSAPP"]
        results = []

        for ch in channels:
            # Query attempts from actions / plans / voice calls
            if ch == "VOICE":
                attempts = int(db.query(func.count(VoiceCall.id)).scalar() or 0)
            elif ch == "WHATSAPP":
                attempts = int(
                    db.query(func.count(RecoveryAction.id))
                    .filter(RecoveryAction.action_type.like("%WHATSAPP%"))
                    .scalar()
                    or 0
                )
            elif ch == "EMAIL":
                attempts = int(
                    db.query(func.count(RecoveryAction.id))
                    .filter(or_(RecoveryAction.action_type.like("%EMAIL%"), RecoveryAction.action_type.like("%LINK%")))
                    .scalar()
                    or 0
                )
            else:  # PAYMENT_RETRY
                attempts = int(
                    db.query(func.count(RecoveryAction.id))
                    .filter(RecoveryAction.action_type.like("%RETRY%"))
                    .scalar()
                    or 0
                )

            # Query attributed recoveries from authoritative RecoveryOutcome
            attr_query = db.query(
                func.count(RecoveryOutcome.id),
                func.coalesce(func.sum(RecoveryOutcome.amount_recovered), 0),
            ).filter(
                RecoveryOutcome.outcome_type == "RECOVERED",
                or_(RecoveryOutcome.attribution == ch, RecoveryOutcome.attribution == f"STEP_{ch}"),
            )
            attr_count_raw, attr_vol_raw = attr_query.first() or (0, 0)
            attr_count = int(attr_count_raw or 0)
            attr_vol = float(attr_vol_raw or 0.0)

            # Fallback if attribution was stored as VOICE on outcome
            if attr_count == 0 and attempts > 0 and ch == "VOICE":
                # Check voice calls with completed status on recovered cases
                recovered_voice = (
                    db.query(func.count(RecoveryCase.id), func.coalesce(func.sum(RecoveryCase.amount_at_risk), 0))
                    .join(VoiceCall, VoiceCall.recovery_case_id == RecoveryCase.id)
                    .filter(RecoveryCase.status == "RECOVERED", VoiceCall.status == "COMPLETED")
                    .first()
                )
                if recovered_voice and recovered_voice[0] > 0:
                    attr_count = int(recovered_voice[0])
                    attr_vol = float(recovered_voice[1])

            rec_rate = round((attr_count / attempts * 100.0), 2) if attempts > 0 else 0.0
            avg_amt = round(attr_vol / attr_count, 2) if attr_count > 0 else 0.0

            results.append(
                {
                    "intervention": ch,
                    "interventions_attempted": attempts,
                    "successful_recoveries": attr_count,
                    "recovery_rate": rec_rate,
                    "amount_recovered": attr_vol,
                    "average_recovered_amount": avg_amt,
                }
            )

        return results

    @classmethod
    def get_recent_recommendations(cls, db: Session, limit: int = 10) -> List[Dict[str, Any]]:
        """Fetch active recovery cases with current Next-Best-Action recommendations."""
        cases = (
            db.query(RecoveryCase)
            .order_by(desc(RecoveryCase.created_at))
            .limit(limit)
            .all()
        )

        recommendations = []
        for case in cases:
            try:
                nba = NextBestActionService.recommend_next_best_action(case_id=case.id, db=db)
                customer = case.customer
                diag = case.diagnoses[0] if case.diagnoses else None
                recommendations.append(
                    {
                        "case_id": str(case.id),
                        "customer_name": customer.name if customer else "Customer",
                        "customer_email": customer.email if customer else None,
                        "customer_phone": customer.phone if customer else None,
                        "amount_at_risk": float(case.amount_at_risk or 0.0),
                        "currency": case.currency or "INR",
                        "case_status": case.status,
                        "failure_category": diag.category if diag else "UNKNOWN",
                        "recommended_action": nba["recommended_action"],
                        "predicted_probability": nba["predicted_probability"],
                        "expected_recovered_value": nba["expected_recovered_value"],
                        "policy_status": "ALLOWED" if nba["recommended_action"] != "NO_ACTION" else "PAUSED",
                        "decision_mode": nba["decision_mode"],
                        "model_version": nba["model_version"],
                        "reason": nba["reason"],
                        "ranking": nba.get("ranking", []),
                    }
                )
            except Exception as e:
                logger.warning(f"[NBA_DASHBOARD_ERROR] Could not recommend for case {case.id}: {e}")

        return recommendations

    @classmethod
    def get_recovery_trend(cls, db: Session, days: int = 30) -> Dict[str, Any]:
        """Aggregate daily and cumulative authoritative recovery money over time."""
        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=days)

        outcomes = (
            db.query(RecoveryOutcome)
            .filter(RecoveryOutcome.occurred_at >= start_date)
            .order_by(RecoveryOutcome.occurred_at.asc())
            .all()
        )

        daily_map: Dict[str, float] = {}
        # Pre-populate all days with 0.0
        for i in range(days + 1):
            d_str = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
            daily_map[d_str] = 0.0

        for out in outcomes:
            d_str = out.occurred_at.strftime("%Y-%m-%d")
            daily_map[d_str] = daily_map.get(d_str, 0.0) + float(out.amount_recovered or 0.0)

        daily_series = []
        cumulative_series = []
        running_total = 0.0

        for d_str in sorted(daily_map.keys()):
            val = round(daily_map[d_str], 2)
            running_total += val
            daily_series.append({"date": d_str, "amount": val})
            cumulative_series.append({"date": d_str, "cumulative_amount": round(running_total, 2)})

        return {
            "daily_trend": daily_series,
            "cumulative_trend": cumulative_series,
            "total_period_recovered": round(running_total, 2),
        }

    @classmethod
    def get_model_status_info(cls) -> Dict[str, Any]:
        """Retrieve model metadata, evaluation metrics, and active/cold-start state."""
        meta_path = MODELS_DIR / f"{DEFAULT_MODEL_VERSION}_metadata.json"
        pipe = RecoveryProbabilityModelService.load_model(DEFAULT_MODEL_VERSION)

        if not pipe or not meta_path.exists():
            return {
                "model_name": "RecoveryProbabilityLogisticRegression",
                "model_version": DEFAULT_MODEL_VERSION,
                "status": "COLD_START",
                "is_active": False,
                "training_samples": 0,
                "last_trained": None,
                "message": "Cold-start mode — collecting real recovery outcomes.",
                "metrics": {
                    "roc_auc": None,
                    "pr_auc": None,
                    "brier_score": None,
                    "calibration_error": None,
                },
            }

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            return {
                "model_name": meta.get("model_name", "RecoveryProbabilityLogisticRegression"),
                "model_version": meta.get("model_version", DEFAULT_MODEL_VERSION),
                "status": "ACTIVE",
                "is_active": True,
                "training_samples": meta.get("train_samples", 0),
                "test_samples": meta.get("test_samples", 0),
                "last_trained": meta.get("training_timestamp"),
                "feature_schema_version": meta.get("feature_schema_version", "v2_nba"),
                "metrics": meta.get("metrics", {}),
                "feature_coefficients": meta.get("feature_coefficients", [])[:10],
            }
        except Exception as e:
            return {
                "model_name": "RecoveryProbabilityLogisticRegression",
                "model_version": DEFAULT_MODEL_VERSION,
                "status": "ERROR",
                "is_active": False,
                "error": str(e),
            }

    @classmethod
    def get_case_audit_timeline(cls, case_id: str, db: Session) -> List[Dict[str, Any]]:
        """Fetch chronological audit trail for a specific recovery case from immutable AuditLog."""
        cid = None
        try:
            import uuid
            cid = uuid.UUID(str(case_id))
        except Exception:
            return []

        logs = (
            db.query(AuditLog)
            .filter(AuditLog.recovery_case_id == cid)
            .order_by(AuditLog.timestamp.asc())
            .all()
        )

        timeline = []
        for log in logs:
            timeline.append(
                {
                    "timestamp": log.timestamp.isoformat(),
                    "event": log.action,
                    "actor_type": log.actor_type,
                    "actor_id": log.actor_id,
                    "metadata": log.audit_metadata or {},
                }
            )

        return timeline

    @classmethod
    def get_promise_to_pay_list(
        cls, db: Session, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Fetch all Promise-to-Pay commitments with overdue flag."""
        now = datetime.now(timezone.utc)
        ptps = (
            db.query(PromiseToPay)
            .order_by(desc(PromiseToPay.created_at))
            .limit(limit)
            .all()
        )

        results = []
        for p in ptps:
            customer = p.customer
            p_date = p.promised_date
            if p_date and p_date.tzinfo is None:
                p_date = p_date.replace(tzinfo=timezone.utc)

            is_overdue = bool(p.status in ["ACTIVE", "PENDING"] and p_date and p_date < now)

            results.append(
                {
                    "id": str(p.id),
                    "case_id": str(p.recovery_case_id),
                    "customer_name": customer.name if customer else "Customer",
                    "customer_email": customer.email if customer else None,
                    "customer_phone": customer.phone if customer else None,
                    "amount": float(p.promised_amount or p.amount_due or 0.0),
                    "promised_date": p.promised_date.isoformat() if p.promised_date else None,
                    "status": p.status,
                    "source": "TWILIO_VOICE",
                    "is_overdue": is_overdue,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
            )

        return results
