"""Hash-chained audit log.

Every AuditLog row gets a monotonically increasing `sequence`, the `prev_hash` of the previous row
and `row_hash = sha256(prev_hash || canonical_json(row))`. Editing or deleting any historical row
breaks every later hash, and `AuditChain.verify()` reports the first broken link. The chain is
written by a SQLAlchemy `before_flush` hook so no service has to remember to do it.

Concurrency note: the sequence has a unique index. Two writers racing for the same sequence will
make one of them fail on commit (the caller's retry/backoff handles it); a forked chain is
therefore impossible to persist silently. Rows created before this feature have NULL sequence and
are reported as `legacy_rows`, not as breaks.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog

_creation_counter = itertools.count()
_INSTALLED = False


def canonical_payload(row: AuditLog) -> str:
    """Deterministic JSON of the fields that are known before insert."""
    body = {
        "id": str(row.id),
        "recovery_case_id": str(row.recovery_case_id) if row.recovery_case_id else None,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "metadata": row.audit_metadata or {},
        "sequence": row.sequence,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


def compute_row_hash(prev_hash: Optional[str], payload: str) -> str:
    return hashlib.sha256(f"{prev_hash or 'GENESIS'}|{payload}".encode("utf-8")).hexdigest()


class AuditChain:
    """Chain writer (used by the flush hook) and verifier."""

    @staticmethod
    def _last_link(session: Session) -> tuple[int, Optional[str]]:
        with session.no_autoflush:
            row = session.execute(
                select(AuditLog.sequence, AuditLog.row_hash)
                .where(AuditLog.sequence.isnot(None))
                .order_by(AuditLog.sequence.desc())
                .limit(1)
            ).first()
        if not row:
            return 0, None
        return int(row[0]), row[1]

    @classmethod
    def chain_pending(cls, session: Session) -> int:
        """Assign sequence/prev_hash/row_hash to every new AuditLog in the session. Returns count."""
        pending = [o for o in session.new if isinstance(o, AuditLog) and o.sequence is None]
        if not pending:
            return 0
        pending.sort(key=lambda o: getattr(o, "_creation_order", 0))
        seq, prev = cls._last_link(session)
        for row in pending:
            if row.id is None:
                row.id = uuid.uuid4()
            seq += 1
            row.sequence = seq
            row.prev_hash = prev
            row.row_hash = compute_row_hash(prev, canonical_payload(row))
            prev = row.row_hash
        return len(pending)

    @classmethod
    def verify(cls, session: Session, case_id: Optional[uuid.UUID] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """Recompute the chain in sequence order. `case_id` filters the *report*, the chain is global."""
        stmt = select(AuditLog).where(AuditLog.sequence.isnot(None)).order_by(AuditLog.sequence.asc())
        if limit:
            stmt = stmt.limit(limit)
        rows: List[AuditLog] = list(session.scalars(stmt).all())
        legacy = int(session.scalar(select(func.count(AuditLog.id)).where(AuditLog.sequence.is_(None))) or 0)

        prev: Optional[str] = None
        expected_seq = rows[0].sequence if rows else 1
        first_break: Optional[Dict[str, Any]] = None
        checked = 0
        for row in rows:
            problem = None
            if row.sequence != expected_seq:
                problem = f"sequence gap: expected {expected_seq}, found {row.sequence}"
            elif row.prev_hash != prev:
                problem = "prev_hash does not match previous row"
            else:
                recomputed = compute_row_hash(prev, canonical_payload(row))
                if recomputed != row.row_hash:
                    problem = "row_hash mismatch (row content changed after write)"
            if problem:
                first_break = {"sequence": row.sequence, "audit_id": str(row.id), "action": row.action, "recovery_case_id": str(row.recovery_case_id) if row.recovery_case_id else None, "problem": problem}
                break
            prev = row.row_hash
            expected_seq = row.sequence + 1
            checked += 1

        report: Dict[str, Any] = {
            "ok": first_break is None,
            "chained_rows": len(rows),
            "verified_rows": checked,
            "legacy_rows": legacy,
            "head_sequence": rows[-1].sequence if rows else 0,
            "head_hash": rows[-1].row_hash if rows else None,
            "first_break": first_break,
            "verified_at": datetime.utcnow().isoformat() + "Z",
        }
        if case_id:
            case_rows = [r for r in rows if r.recovery_case_id == case_id]
            report["case"] = {
                "recovery_case_id": str(case_id),
                "rows": len(case_rows),
                "sequences": [r.sequence for r in case_rows],
                "all_verified": all(r.sequence < (first_break["sequence"] if first_break else float("inf")) for r in case_rows),
            }
        return report


def _remember_creation_order(target: AuditLog, args, kwargs) -> None:
    target._creation_order = next(_creation_counter)


def _before_flush(session: Session, flush_context, instances) -> None:
    AuditChain.chain_pending(session)


def install_audit_chain() -> None:
    """Idempotently register the ORM hooks. Called at import of app.models."""
    global _INSTALLED
    if _INSTALLED:
        return
    event.listen(AuditLog, "init", _remember_creation_order)
    event.listen(Session, "before_flush", _before_flush)
    _INSTALLED = True
