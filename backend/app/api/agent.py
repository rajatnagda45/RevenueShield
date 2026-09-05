"""Recovery agent endpoints: run the planner on a case, inspect runs and traces, list tools."""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.runner import RecoveryAgentRunner
from app.agent.tools import AgentToolbox
from app.core.config import settings
from app.db.session import get_db
from app.models.agent_run import AgentRun

router = APIRouter(prefix="/agent", tags=["Recovery Agent"])


class RunRequest(BaseModel):
    dry_run: bool = True
    provider: Optional[str] = None  # anthropic | openai | null | auto
    reference_time: Optional[datetime] = None


def _require_internal_secret(x_internal_secret: Optional[str]) -> None:
    if settings.INTERNAL_API_SECRET and x_internal_secret != settings.INTERNAL_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid internal secret.")


@router.get("/tools", summary="The bounded tool surface exposed to the planner")
def list_tools() -> List[Dict[str, Any]]:
    return [t.model_dump() for t in AgentToolbox.specs()]


@router.post("/cases/{case_id}/run", summary="Run the recovery agent on a case (LLM planner with deterministic fallback)")
def run_agent(case_id: uuid.UUID, body: RunRequest = RunRequest(), db: Session = Depends(get_db), x_internal_secret: Optional[str] = Header(None, alias="X-Internal-Secret")) -> Dict[str, Any]:
    _require_internal_secret(x_internal_secret)
    try:
        run = RecoveryAgentRunner.run(db, case_id, dry_run=body.dry_run, provider_name=body.provider, now=body.reference_time)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    db.commit()
    return RecoveryAgentRunner.serialize(run)


@router.get("/runs", summary="List agent runs")
def list_runs(case_id: Optional[uuid.UUID] = Query(None), status_filter: Optional[str] = Query(None, alias="status"), limit: int = Query(50, ge=1, le=500), db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    stmt = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
    if case_id:
        stmt = stmt.where(AgentRun.recovery_case_id == case_id)
    if status_filter:
        stmt = stmt.where(AgentRun.status == status_filter.upper())
    return [RecoveryAgentRunner.serialize(r) for r in db.scalars(stmt).all()]


@router.get("/runs/{run_id}", summary="One agent run with its dossier and full trace")
def get_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> Dict[str, Any]:
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id))
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent run not found.")
    return RecoveryAgentRunner.serialize(run, include_dossier=True)
