"""Human handoff: freeze automation when a customer disputes, asks for a person, or is the wrong person."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.recovery_case import RecoveryCase

logger = logging.getLogger(__name__)

HANDOFF_FREEZE_RULE = "HUMAN_HANDOFF_FREEZE"


class HandoffService:
    @classmethod
    def freeze(cls, db: Session, *, case: RecoveryCase, reason: str, source: str, metadata: Optional[Dict[str, Any]] = None) -> RecoveryCase:
        """Mark the case for a human, pause the plan, and stop all automation until an operator releases it."""
        from app.services.recovery_scheduler import RecoveryScheduler

        meta = dict(case.case_metadata or {})
        if meta.get("human_handoff"):
            return case
        meta.update({
            "human_handoff": True,
            "handoff_reason": reason,
            "handoff_source": source,
            "handoff_at": datetime.now(timezone.utc).isoformat(),
            **(metadata or {}),
        })
        case.case_metadata = meta
        RecoveryScheduler.pause_plan(db, case_id=case.id, reason=f"HUMAN_HANDOFF: {reason}")
        db.add(AuditLog(
            recovery_case_id=case.id, actor_type="SYSTEM", actor_id="handoff_service_v1", action="HUMAN_HANDOFF",
            entity_type="RecoveryCase", entity_id=str(case.id), audit_metadata={"reason": reason, "source": source, **(metadata or {})},
        ))
        db.flush()
        logger.info(f"[HUMAN_HANDOFF] case={case.id} reason={reason} source={source}")
        return case

    @classmethod
    def release(cls, db: Session, *, case: RecoveryCase, operator: str, note: Optional[str] = None) -> RecoveryCase:
        from app.services.recovery_scheduler import RecoveryScheduler

        meta = dict(case.case_metadata or {})
        meta["human_handoff"] = False
        meta["handoff_released_at"] = datetime.now(timezone.utc).isoformat()
        meta["handoff_released_by"] = operator
        case.case_metadata = meta
        RecoveryScheduler.resume_plan(db, case_id=case.id)
        db.add(AuditLog(
            recovery_case_id=case.id, actor_type="OPERATOR", actor_id=operator, action="HUMAN_HANDOFF_RELEASED",
            entity_type="RecoveryCase", entity_id=str(case.id), audit_metadata={"note": note},
        ))
        db.flush()
        return case

    @staticmethod
    def is_frozen(case: Optional[RecoveryCase]) -> bool:
        return bool(case is not None and (case.case_metadata or {}).get("human_handoff"))
